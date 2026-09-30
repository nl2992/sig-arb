"""Read-only local dashboard. Run: python3 dashboard.py --port 8765."""
import argparse
import datetime as dt
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from arb_engine import group_markets, scan
from sig_client import Client
from kelly import size_position
from news import fetch_news
from signals import Snapshot, load_exhaustive, scan_diagnostics
from crossvenue_adapters import fetch_public, load_market_links, load_targeted_market_ids
from crossvenue_models import PriceObservation
from market_matches import load_registry
from movement_scanner import scan_movements
from relative_value import scan_pairs
from portfolio import fetch_sig_portfolio
from news_guard import active_breakers

ROOT = Path(__file__).parent
NEWS_BREAKERS = ROOT / 'config' / 'news_circuit_breakers.json'


def _snapshot_age_seconds(snapshot):
    observed = dt.datetime.fromisoformat(snapshot.ts.replace('Z', '+00:00'))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds())


def _read_history(path=ROOT / 'logs' / 'crossvenue-history.jsonl'):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _crossvenue_report(snapshot, payload, matches_path=ROOT / 'fixtures/crossvenue/matches.json'):
    payload = payload or {}
    observations = [PriceObservation(**row)
                    for venue in payload.values()
                    for row in venue.get('observations', [])]
    try:
        matches = load_registry(matches_path)
    except (OSError, ValueError):
        matches = []
    history = _read_history()
    historical = [PriceObservation(
        venue=row['reference_venue'], market_id=row['reference_market_id'],
        outcome_id=row['reference_outcome_id'], observed_at=row['observed_at'],
        last=row['reference_price'], source='crossvenue-history', price_basis='last')
        for row in history if row.get('reference_price') is not None]
    all_observations = historical + observations
    movement = scan_movements(snapshot, all_observations, matches).to_dict()
    relative = scan_pairs(history, matches)
    market_index = {(venue, str(row.get('market_id'))): row
                    for venue, data in payload.items()
                    for row in data.get('markets', [])}
    observation_index = {(row.venue, row.market_id, row.outcome_id.upper()): row
                         for row in observations}
    books = snapshot.books()
    breakers = active_breakers(NEWS_BREAKERS)
    enriched = []
    now = dt.datetime.now(dt.timezone.utc)
    for candidate in movement.get('candidates', []):
        match = next((m for m in matches if m.sig_market_id == candidate['sig_market_id']
                      and m.reference_venue == candidate['reference_venue']
                      and m.reference_market_id == candidate['reference_market_id']), None)
        ref = observation_index.get((candidate['reference_venue'], candidate['reference_market_id'],
                                     match.reference_outcome_id.upper() if match else 'YES'))
        metadata = market_index.get((candidate['reference_venue'], candidate['reference_market_id']), {})
        book = books.get(candidate['sig_market_id'])
        required = book.asks if candidate['direction'] == 'BUY_YES' and book else book.bids if book else []
        top_qty = required[0][1] if required else None
        try:
            age = max(0.0, (now - dt.datetime.fromisoformat(ref.observed_at.replace('Z', '+00:00'))).total_seconds()) if ref else None
        except (TypeError, ValueError):
            age = None
        candidate.update({
            'liquidity': {'sig_top_qty': top_qty, 'reference_bid_size': ref.bid_size if ref else None,
                          'reference_ask_size': ref.ask_size if ref else None},
            'freshness_seconds': age,
            'mapping_status': match.status if match else 'UNRESOLVED',
            'settlement_status': 'RULES_PRESENT_REVIEWED' if metadata.get('rules_text') else 'RULES_UNAVAILABLE',
            'fee_assumption': 0.0,
            'fee_status': 'UNVERIFIED_PUBLIC_SCHEDULE',
            'roi_estimate': round(candidate['gap_pp'] / abs(candidate['sig_price']) / 100, 6) if candidate.get('gap_pp') and candidate.get('sig_price') else None,
            'execution_risk': ['RESEARCH_ONLY', 'MULTI_VENUE_FILL_RISK', 'FEES_UNVERIFIED'],
            'news_status': 'ACTIVE_CIRCUIT_BREAKER' if breakers.get(candidate['sig_market_id']) else 'CLEAR',
            'execution_ready': False,
        })
        if breakers.get(candidate['sig_market_id']):
            candidate['execution_risk'].append('NEWS_CIRCUIT_BREAKER')
        enriched.append(candidate)
    market_counts = {venue: len(data.get('markets', [])) for venue, data in payload.items()}
    observation_counts = {venue: len(data.get('observations', [])) for venue, data in payload.items()}
    approved = [m for m in matches if m.status == 'APPROVED']
    link_rows = load_market_links(ROOT / 'docs' / 'market-links.csv')
    return {
        'market_counts': market_counts,
        'observation_counts': observation_counts,
        'book_counts': {venue: len(data.get('books', [])) for venue, data in payload.items()},
        'mapping_counts': {'discovered': len(link_rows), 'registry_rows': len(matches), 'approved': len(approved)},
        'mapping_candidates': [{k: row.get(k) for k in (
            'sig_market_id', 'event', 'sig_url', 'kalshi_market_id', 'kalshi_url',
            'polymarket_market_id', 'polymarket_url', 'status')}
            for row in link_rows],
        'movement': movement,
        'opportunities': enriched,
        'relative_value': relative,
        'history_points': len(history),
        'depth_coverage': 'SIG_FULL_RELAY_BOOKS_PLUS_ALL_RETURNED_KALSHI_AND_POLYMARKET_BOOKS',
        'book_coverage': {venue: data.get('book_coverage', 'UNKNOWN') for venue, data in payload.items()},
        'fees': {'sig': 'dashboard input', 'kalshi': 'unverified', 'polymarket': 'unverified'},
        'research_only': True,
    }


def report(snapshot, params, crossvenue=None):
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
    min_roi = number("roi", 5) / 100
    snapshot_age = _snapshot_age_seconds(snapshot)
    groups = group_markets(snapshot.markets)
    exhaustive = load_exhaustive()
    breakers = active_breakers(NEWS_BREAKERS)
    scan_report = scan_diagnostics(snapshot, exhaustive, fee_per_share=fee,
                                   min_edge=edge, cash=budget or None)
    results = scan_report.opportunities
    titles = {m["id"]: m["title"] for m in snapshot.markets}
    rows = []
    punts = []
    for r in results:
        no = r.direction == "SELL_ALL"
        row = dict(
            race=r.race, direction=r.direction, qty=r.qty, profit=r.pnl,
            capital=r.capital, roi=r.roi, edge=r.avg_edge,
            top_edge=r.top_edge, marginal_edge=r.marginal_edge,
            freshness_seconds=round(snapshot_age, 3),
            mapping_status='LOCAL_SIG', settlement_status='SIG_RULES_UNVERIFIED',
            fee_assumption=fee,
            execution_risk=['PARTIAL_FILL', 'QUOTE_AGE_CHECK_REQUIRED'],
            news_status='ACTIVE_CIRCUIT_BREAKER' if any(breakers.get(l.market_id) for l in r.legs) else 'CLEAR',
            execution_ready=False,
            vwap=sum(1-l.vwap if no else l.vwap for l in r.legs),
            steps=r.steps,
            legs=[dict(id=l.market_id, title=titles[l.market_id],
                       side="BUY NO" if no else "BUY YES", qty=l.qty,
                       vwap=1-l.vwap if no else l.vwap,
                       limit=1-l.limit if no else l.limit) for l in r.legs])
        if row['news_status'] == 'ACTIVE_CIRCUIT_BREAKER':
            row['execution_risk'].append('NEWS_CIRCUIT_BREAKER')
        if snapshot_age > 30:
            row['execution_risk'].append('STALE_SIG_SNAPSHOT')
        if r.pnl >= minimum and r.roi + 1e-12 >= min_roi:
            rows.append(row)
        elif r.pnl >= minimum:
            row["required_roi"] = min_roi
            punts.append(row)
    near = [dict(race=d["race"], dir=d["direction"], edge=d["top_edge"])
            for d in scan_report.diagnostics
            if d["top_edge"] is not None and d["top_edge"] <= 0]
    near.sort(key=lambda r: -r["edge"])
    return dict(ts=snapshot.ts, markets=len(snapshot.markets), races=len(groups),
                exhaustive=len(exhaustive), fee=fee, budget=budget,
                overall_capital=overall, cap_pct=cap_pct,
                signals=rows, near=near[:12],
                punts=punts[:25],
                diagnostics=scan_report.diagnostics,
                liquidity=scan_report.liquidity,
                crossvenue=_crossvenue_report(snapshot, crossvenue),
                markets_list=[dict(id=m['id'], title=m['title']) for m in snapshot.markets])


class Source:
    def __init__(self, replay=None, browser_snapshot_path=None):
        self.replay = replay
        self.browser_snapshot_path = Path(browser_snapshot_path or ROOT / 'logs/browser_snapshot.json')
        self.lock = threading.Lock()
        self.snapshot = None
        self.fetched = 0
        self.news_cache = {}
        self.crossvenue_cache = None
        self.crossvenue_fetched = 0
        self.portfolio_cache = None
        self.portfolio_fetched = 0

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
        self.browser_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.browser_snapshot_path.with_suffix(self.browser_snapshot_path.suffix + '.tmp')
        tmp.write_text(json.dumps({'ts': snapshot.ts, 'markets': snapshot.markets,
                                   'levels': {str(k): v for k, v in snapshot.levels.items()}},
                                  allow_nan=False))
        tmp.replace(self.browser_snapshot_path)
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

    def get_crossvenue(self):
        with self.lock:
            if self.replay:
                return {}
            if self.crossvenue_cache is None or time.monotonic() - self.crossvenue_fetched >= 60:
                inventory_limit = int(os.environ.get('CROSSVENUE_MARKET_LIMIT', '0'))
                if inventory_limit < 0:
                    raise ValueError('CROSSVENUE_MARKET_LIMIT must be nonnegative')
                targeted = load_targeted_market_ids(ROOT / 'docs' / 'market-links.csv')
                self.crossvenue_cache = fetch_public(['kalshi', 'polymarket'], inventory_limit,
                                                     market_ids=targeted)
                self.crossvenue_fetched = time.monotonic()
            return self.crossvenue_cache

    def get_portfolio(self):
        with self.lock:
            if self.replay:
                return {'venue': 'sig', 'status': 'REPLAY', 'read_only': True,
                        'kill_switch_required': False,
                        'note': 'Account reconciliation is unavailable in replay mode.'}
            if self.portfolio_cache is not None and time.monotonic() - self.portfolio_fetched < 60:
                return self.portfolio_cache
            try:
                client = Client()
                markets = client.markets()
                data = fetch_sig_portfolio(client, markets)
            except PermissionError:
                data = {'venue': 'sig', 'status': 'AUTH_REQUIRED', 'read_only': True,
                        'kill_switch_required': True,
                        'note': 'Signed-in SIG account data is unavailable.'}
            except Exception:
                data = {'venue': 'sig', 'status': 'UNAVAILABLE', 'read_only': True,
                        'kill_switch_required': True,
                        'note': 'SIG account data could not be read.'}
            self.portfolio_cache = data
            self.portfolio_fetched = time.monotonic()
            return data


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
                        data = report(snapshot, params, source.get_crossvenue())
                    data["mode"] = "replay" if source.replay else "live"
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), "application/json")
                except ValueError as exc:
                    self.send_body(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except Exception:
                    self.send_body(502, b'{"error":"Market data unavailable. Retry the scan."}', "application/json")
            elif url.path == '/api/crossvenue':
                try:
                    self.send_body(200, json.dumps(source.get_crossvenue(), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Cross-venue data unavailable. Retry the scan."}', 'application/json')
            elif url.path == '/api/portfolio':
                try:
                    self.send_body(200, json.dumps(source.get_portfolio(), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Portfolio state unavailable. Retry the reconciliation check."}', 'application/json')
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
