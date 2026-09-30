"""Read-only local dashboard. Run: python3 dashboard.py --port 8765."""
import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from arb_engine import group_markets, scan
from sig_client import Client
from kelly import size_position
from news import fetch_news
from signals import Snapshot, load_exhaustive

ROOT = Path(__file__).parent


def report(snapshot, params):
    def number(name, default):
        value = float(params.get(name, [default])[0])
        if not math.isfinite(value) or value < 0:
            raise ValueError(name + " must be a finite nonnegative number")
        return value

    fee = number("fee", 0)
    budget = number("budget", 0)
    overall = number("capital", 0)
    cap_pct = number("cap_pct", 5) / 100
    if overall and cap_pct:
        per_punt = overall * cap_pct
        budget = min(budget, per_punt) if budget else per_punt
    edge = number("edge", 0)
    minimum = number("profit", 1)
    min_roi = number("roi", 10) / 100
    books = snapshot.books()
    groups = group_markets(snapshot.markets)
    exhaustive = load_exhaustive()
    results = scan(groups, books, exhaustive, fee_per_share=fee,
                   min_edge=edge, cash=budget or None)
    titles = {m["id"]: m["title"] for m in snapshot.markets}
    rows = []
    punts = []
    for r in results:
        no = r.direction == "SELL_ALL"
        row = dict(
            race=r.race, direction=r.direction, qty=r.qty, profit=r.pnl,
            capital=r.capital, roi=r.roi, edge=r.avg_edge,
            top_edge=r.top_edge, marginal_edge=r.marginal_edge,
            vwap=sum(1-l.vwap if no else l.vwap for l in r.legs),
            steps=r.steps,
            legs=[dict(id=l.market_id, title=titles[l.market_id],
                       side="BUY NO" if no else "BUY YES", qty=l.qty,
                       vwap=1-l.vwap if no else l.vwap,
                       limit=1-l.limit if no else l.limit) for l in r.legs])
        if r.pnl >= minimum and r.roi + 1e-12 >= min_roi:
            rows.append(row)
        elif r.pnl >= minimum:
            row["required_roi"] = min_roi
            punts.append(row)
    near = []
    for race, legs in groups.items():
        if len(legs) < 2 or any(mid not in books for mid in legs.values()):
            continue
        for direction in (["SELL_ALL", "BUY_ALL"] if race in exhaustive else ["SELL_ALL"]):
            prices = [(books[mid].best_bid() if direction == "SELL_ALL"
                       else books[mid].best_ask()) for mid in legs.values()]
            if any(price is None for price in prices):
                continue
            edge = (sum(prices)-1 if direction == "SELL_ALL" else 1-sum(prices))-len(prices)*fee
            if edge <= 0:
                near.append(dict(race=race, dir=direction, edge=edge))
    near.sort(key=lambda r: -r["edge"])
    return dict(ts=snapshot.ts, markets=len(snapshot.markets), races=len(groups),
                exhaustive=len(exhaustive), fee=fee, budget=budget,
                overall_capital=overall, cap_pct=cap_pct,
                signals=rows, near=near[:12],
                punts=punts[:25],
                markets_list=[dict(id=m['id'], title=m['title']) for m in snapshot.markets])


class Source:
    def __init__(self, replay=None):
        self.replay = replay
        self.lock = threading.Lock()
        self.snapshot = None
        self.fetched = 0
        self.news_cache = {}

    def accept_browser_snapshot(self, payload):
        markets = payload.get('markets')
        levels = payload.get('levels')
        if not isinstance(markets, list) or not isinstance(levels, dict):
            raise ValueError('Snapshot must include markets and levels')
        if len(markets) > 1000 or len(levels) > 1000:
            raise ValueError('Snapshot is too large')
        if any(not isinstance(m, dict) or 'id' not in m or 'title' not in m for m in markets):
            raise ValueError('Snapshot contains an invalid market')
        if any(not isinstance(v, list) for v in levels.values()):
            raise ValueError('Snapshot contains invalid order levels')
        snapshot = Snapshot(
            payload.get('ts', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())),
            markets, {int(k): v for k, v in levels.items()})
        with self.lock:
            self.snapshot = snapshot
            self.fetched = time.monotonic()
        return {'ok': True, 'markets': len(markets), 'levels': len(levels), 'ts': snapshot.ts}

    def news(self, market_id):
        with self.lock:
            if self.replay:
                return dict(headlines=[], tradingStatus='Replay', contextSummary='News unavailable in replay mode.')
            cached = self.news_cache.get(market_id)
            if cached and time.monotonic()-cached[0] < 300:
                return cached[1]
            data = fetch_news(Client(), market_id)
            self.news_cache[market_id] = (time.monotonic(), data)
            return data

    def get(self):
        with self.lock:
            if self.snapshot is None or time.monotonic()-self.fetched >= 15:
                if self.replay:
                    snapshot = Snapshot.load(self.replay)
                else:
                    client = Client()
                    snapshot = Snapshot.fetch(client, client.markets())
                self.snapshot = snapshot
                self.fetched = time.monotonic()
            return self.snapshot


def handler(source):
    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_body(204, b'', 'text/plain')

        def do_POST(self):
            if urlparse(self.path).path != '/api/browser_snapshot':
                self.send_body(404, b'Not found', 'text/plain')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length > 20_000_000:
                    raise ValueError('Snapshot too large')
                result = source.accept_browser_snapshot(json.loads(self.rfile.read(length)))
                self.send_body(200, json.dumps(result).encode(), 'application/json')
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/api/signals", "/api/kelly", "/api/news"):
                try:
                    params = parse_qs(url.query)
                    # Validate inputs before making external requests.
                    for values in params.values():
                        value = float(values[0])
                        if not math.isfinite(value) or value < 0:
                            raise ValueError("Invalid filter")
                    if url.path == '/api/news':
                        mid = int(params['market'][0])
                        if mid <= 0:
                            raise ValueError('Invalid market')
                        self.send_body(200, json.dumps(source.news(mid)).encode(), 'application/json')
                        return
                    snapshot = source.get()
                    if url.path == '/api/kelly':
                        mid = int(params['market'][0])
                        book = snapshot.books()[mid]
                        no = params.get('no', ['0'])[0] == '1'
                        ladder = [(1-p, q) for p, q in book.bids] if no else book.asks
                        data = size_position(ladder, float(params['probability'][0]),
                                             float(params['bankroll'][0]),
                                             float(params.get('fraction', ['0.25'])[0]),
                                             float(params.get('fee', ['0'])[0]))
                        data['ts'] = snapshot.ts
                    else:
                        data = report(snapshot, params)
                    data["mode"] = "replay" if source.replay else "live"
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), "application/json")
                except ValueError as exc:
                    self.send_body(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except Exception:
                    self.send_body(502, b'{"error":"Market data unavailable. Retry the scan."}', "application/json")
            elif url.path in ("/", "/dashboard.css", "/dashboard.js"):
                file = "dashboard.html" if url.path == "/" else url.path[1:]
                mime = {"html": "text/html", "css": "text/css", "js": "application/javascript"}
                self.send_body(200, (ROOT / "web" / file).read_bytes(), mime[file.split(".")[-1]])
            else:
                self.send_body(404, b"Not found", "text/plain")

        def send_body(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--replay")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(Source(args.replay)))
    print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
