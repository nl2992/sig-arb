"""
scalper.py — short-horizon SIG strategies driven by a fast Polymarket feed.

PolyFeed      polls Polymarket's public CLOB (POST /books, every watched book in one
              request) every few seconds and keeps a mid-price history per SIG market.

LeadLag (ll)  Polymarket moves first. When its mid moves >= `move` within `lookback_s`
              and SIG still quotes >= `edge` behind the new mid, take SIG's stale quote.
              Exit on take-profit (SIG caught up, or +`tp`), stop (-`stop`, or the
              reference reverses through the entry) or time (`max_hold_s`).

MarketMaker   Rests a bid and an ask inside wide SIG spreads around the live fair price,
  (mm)        at least `edge` from fair. With inventory it quotes only the exit side at a
              small profit (falling back to fair after `max_hold_s`). Quotes are pulled on
              any reference move, when the kill switch is on, and on shutdown. Inventory is
              inferred from account holdings (MM only trades markets no other strategy
              holds), priced at the resting quote that filled.

Each strategy has its own ledger (logs/ll_positions.json, logs/mm_positions.json).
"""
from __future__ import annotations

import collections
import json
import logging
import math
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from arb_engine import Book
from crossvenue_adapters import PolymarketAdapter
from market_matches import load_registry

ROOT = pathlib.Path(__file__).parent
TOKENS = ROOT / "logs" / "poly_tokens.json"
REGISTRY = ROOT / "config" / "market_matches.json"
CLOB = "https://clob.polymarket.com"
TICK = 0.005
log = logging.getLogger("sigarb")


def floor_tick(p: float) -> float:
    return round(math.floor(p / TICK + 1e-9) * TICK, 4)


def ceil_tick(p: float) -> float:
    return round(math.ceil(p / TICK - 1e-9) * TICK, 4)


# ------------------------------------------------------------------ feed
class PolyFeed:
    def __init__(self, registry: pathlib.Path = REGISTRY, poll_s: float = 3.0, history_s: float = 3600,
                 max_age_s: float = 30, max_spread: float = 0.06, session=None):
        matches = [m for m in load_registry(registry)
                   if m.status == "APPROVED" and m.reference_venue == "polymarket"]
        self.poly_by_sig: Dict[int, List[str]] = collections.defaultdict(list)
        for m in matches:
            self.poly_by_sig[m.sig_market_id].append(m.reference_market_id)
        self.poll_s, self.history_s, self.max_age_s, self.max_spread = poll_s, history_s, max_age_s, max_spread
        self.s = session or requests.Session()
        self.tokens: Dict[str, str] = {}                  # polymarket market id -> YES token id
        self.hist: Dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()
        self.last_poll: Optional[float] = None
        self.errors = 0

    def load_tokens(self, cache: pathlib.Path = TOKENS, concurrency: int = 8) -> int:
        try:
            self.tokens = json.loads(cache.read_text())
        except (OSError, ValueError):
            self.tokens = {}
        missing = sorted({p for ps in self.poly_by_sig.values() for p in ps} - set(self.tokens))
        if missing:
            pa = PolymarketAdapter()

            def one(pid):
                try:
                    m = pa._get(f"/markets/{pid}")
                    ids = json.loads(m.get("clobTokenIds") or "[]")
                    outs = [o.upper() for o in json.loads(m.get("outcomes") or "[]")]
                    return pid, dict(zip(outs, ids)).get("YES")
                except Exception:
                    return pid, None
            with ThreadPoolExecutor(concurrency) as ex:
                for pid, tok in ex.map(one, missing):
                    if tok:
                        self.tokens[pid] = tok
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(self.tokens))
        return len(self.tokens)

    def poll(self) -> int:
        tok_to_sig = collections.defaultdict(list)
        for sig, pids in self.poly_by_sig.items():
            for p in pids:
                if p in self.tokens:
                    tok_to_sig[self.tokens[p]].append(sig)
        toks = sorted(tok_to_sig)
        now, mids = time.time(), collections.defaultdict(list)
        for i in range(0, len(toks), 100):
            r = self.s.post(f"{CLOB}/books", json=[{"token_id": t} for t in toks[i:i + 100]], timeout=10)
            r.raise_for_status()
            for b in r.json():
                bids = [float(x["price"]) for x in b.get("bids") or []]
                asks = [float(x["price"]) for x in b.get("asks") or []]
                if not bids or not asks:
                    continue
                bid, ask = max(bids), min(asks)
                if ask < bid or ask - bid > self.max_spread:
                    continue
                for sig in tok_to_sig.get(b.get("asset_id"), []):
                    mids[sig].append((bid + ask) / 2)
        with self.lock:
            for sig, ms in mids.items():
                h = self.hist[sig]
                h.append((now, sum(ms) / len(ms)))
                while h and now - h[0][0] > self.history_s:
                    h.popleft()
            self.last_poll = now
        return len(mids)

    def start(self) -> threading.Thread:
        def loop():
            while True:
                try:
                    self.poll()
                except Exception:
                    self.errors += 1
                time.sleep(self.poll_s)
        t = threading.Thread(target=loop, name="poly-feed", daemon=True)
        t.start()
        return t

    def mid(self, sig: int) -> Optional[float]:
        with self.lock:
            h = self.hist.get(sig)
            if not h or time.time() - h[-1][0] > self.max_age_s:
                return None
            return h[-1][1]

    def move(self, sig: int, lookback_s: float) -> Optional[float]:
        """Mid now minus the oldest mid within the lookback (None without enough history)."""
        with self.lock:
            h = self.hist.get(sig)
            if not h or time.time() - h[-1][0] > self.max_age_s:
                return None
            cutoff = h[-1][0] - lookback_s
            past = next((m for t, m in h if t >= cutoff), None)
            if past is None or h[-1][0] - h[0][0] < min(lookback_s, 60):
                return None
            return h[-1][1] - past

    def movers(self, threshold: float, lookback_s: float) -> List[int]:
        out = []
        for sig in list(self.hist):
            mv = self.move(sig, lookback_s)
            if mv is not None and abs(mv) >= threshold:
                out.append(sig)
        return out


# ------------------------------------------------------------------ lead-lag
def leadlag_plan(book: Book, poly_mid: Optional[float], move: Optional[float], ledger, *, edge: float,
                 move_min: float, unit: float, max_market: float, gross_left: float, tp: float, stop: float,
                 max_hold_s: float, exit_band: float = 0.005) -> Optional[dict]:
    """Exit an open lead-lag position first; else enter on a reference move SIG has not followed."""
    pos = ledger.position(book.market_id)
    if pos:
        entry = ledger.avg_yes(book.market_id)
        held = ledger.held_for(book.market_id)
        if pos > 0 and book.bids:
            bid, size = book.bids[0]
            why = ("target" if bid >= entry + tp
                   else "stop" if bid <= entry - stop or (poly_mid is not None and poly_mid <= entry)
                   else "converged" if poly_mid is not None and bid >= poly_mid - exit_band
                   else "time" if held >= max_hold_s else None)
            if why:
                return {"yes_side": "SELL", "limit": bid, "qty": float(min(pos, size)), "exit": why,
                        "entry": round(entry, 4)}
        if pos < 0 and book.asks:
            ask, size = book.asks[0]
            why = ("target" if ask <= entry - tp
                   else "stop" if ask >= entry + stop or (poly_mid is not None and poly_mid >= entry)
                   else "converged" if poly_mid is not None and ask <= poly_mid + exit_band
                   else "time" if held >= max_hold_s else None)
            if why:
                return {"yes_side": "BUY", "limit": ask, "qty": float(min(-pos, size)), "exit": why,
                        "entry": round(entry, 4)}
        return None
    if poly_mid is None or move is None or abs(move) < move_min:
        return None
    budget = min(unit * abs(move) / move_min, max_market, gross_left)
    if budget <= 0:
        return None
    if move > 0 and book.asks and book.asks[0][0] <= poly_mid - edge:     # reference up, SIG still cheap
        qty = spent = 0.0
        for price, size in book.asks:
            if price > poly_mid - edge:
                break
            take = min(size, (budget - spent) / price)
            qty += take; spent += take * price; limit = price
        qty = float(int(qty))
        if qty >= 10:
            return {"yes_side": "BUY", "limit": limit, "qty": qty, "fair": round(poly_mid, 4),
                    "move": round(move, 4), "capital": round(spent, 2)}
    if move < 0 and book.bids and book.bids[0][0] >= poly_mid + edge:     # reference down, SIG still rich
        qty = spent = 0.0
        for price, size in book.bids:
            if price < poly_mid + edge:
                break
            take = min(size, (budget - spent) / (1 - price))
            qty += take; spent += take * (1 - price); limit = price
        qty = float(int(qty))
        if qty >= 10:
            return {"yes_side": "SELL", "limit": limit, "qty": qty, "fair": round(poly_mid, 4),
                    "move": round(move, 4), "capital": round(spent, 2)}
    return None


# ------------------------------------------------------------------ market making
def classify_open_order(o: dict) -> Optional[tuple]:
    """('bid'|'ask', YES price, remaining qty) for a portfolio open order."""
    side, action = str(o.get("side", "")).lower(), str(o.get("action", "")).lower()
    try:
        p, q = float(o["priceLimit"]), abs(float(o["quantity"]))
    except (KeyError, TypeError, ValueError):
        return None
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), p, q
    if side == "no":
        return ("ask" if action == "buy" else "bid"), round(1 - p, 4), q
    return None


def mm_quotes(book: Book, fair: float, inventory: float, avg_entry: Optional[float], held_s: float, *,
              edge: float, size: float, max_inventory: float, take: float, max_hold_s: float,
              min_spread: float) -> Dict[str, tuple]:
    """Desired resting quotes {'bid'|'ask': (YES price, qty)}."""
    if not book.bids or not book.asks:
        return {}
    bb, ba = book.bids[0][0], book.asks[0][0]
    out: Dict[str, tuple] = {}
    if inventory > 0:                                  # long YES: only the exit ask
        target = fair if held_s >= max_hold_s else max(fair, avg_entry + take)
        px = max(ceil_tick(target), round(bb + TICK, 4))
        out["ask"] = (px, inventory)
        room = max_inventory - inventory
        if room >= 10 and ba - bb >= min_spread:
            bid = min(round(bb + TICK, 4), floor_tick(fair - edge))
            if bid < ba and bid > 0:
                out["bid"] = (bid, min(size, room))
        return out
    if inventory < 0:                                  # short YES: only the exit bid
        target = fair if held_s >= max_hold_s else min(fair, avg_entry - take)
        px = min(floor_tick(target), round(ba - TICK, 4))
        out["bid"] = (px, -inventory)
        room = max_inventory + inventory
        if room >= 10 and ba - bb >= min_spread:
            ask = max(round(ba - TICK, 4), ceil_tick(fair + edge))
            if ask > bb and ask < 1:
                out["ask"] = (ask, min(size, room))
        return out
    if ba - bb < min_spread:
        return {}
    bid = min(round(bb + TICK, 4), floor_tick(fair - edge))
    ask = max(round(ba - TICK, 4), ceil_tick(fair + edge))
    if 0 < bid < ba:
        out["bid"] = (bid, size)
    if bb < ask < 1:
        out["ask"] = (ask, size)
    return out


class MarketMaker:
    def __init__(self, cli, ledger, *, edge=0.01, size=300, max_inventory=1000, take=0.01, max_hold_s=1800,
                 min_spread=0.02, max_markets=8, requote_s=15, live=False, place=None):
        self.cli, self.ledger, self.live = cli, ledger, live
        self.edge, self.size, self.max_inventory, self.take = edge, size, max_inventory, take
        self.max_hold_s, self.min_spread, self.max_markets, self.requote_s = max_hold_s, min_spread, max_markets, requote_s
        self.place = place                 # place_tracked-compatible callable (writes intents)
        self.open: Dict[int, List[dict]] = {}          # market -> open orders (from portfolio)
        self.quoted_at: Dict[int, float] = {}
        self.active: set = set()                        # markets with MM quotes or inventory
        self.enabled = True
        self.last_quote_px: Dict[tuple, float] = {}    # (market, side) -> price of our last quote

    # -- account sync: open orders + inventory inferred from holdings
    def sync(self, portfolio: dict, other_expected: Dict[int, float]) -> List[dict]:
        self.open = collections.defaultdict(list)
        for o in portfolio.get("openOrders", []):
            try:
                self.open[int(o["marketId"])].append(o)
            except (KeyError, TypeError, ValueError):
                continue
        held = collections.defaultdict(float)
        for h in portfolio.get("holdings", []):
            if str(h.get("settlementOption", "YES")).upper() == "YES":
                held[int(h["marketId"])] += float(h.get("quantity") or 0)
        fills = []
        for m in set(self.active) | {int(k) for k in self.ledger.rows}:
            inv = held.get(m, 0.0) - other_expected.get(m, 0.0)
            delta = inv - self.ledger.position(m)
            if abs(delta) < 0.5:
                continue
            side = "BUY" if delta > 0 else "SELL"
            price = self.last_quote_px.get((m, "bid" if delta > 0 else "ask"))
            if price is None:
                continue                                 # not ours to explain
            self.ledger.record(m, side, abs(delta), price)
            fills.append({"market_id": m, "yes_side": side, "qty": abs(delta), "price": price})
        for m in list(self.active):
            if not self.ledger.position(m) and not self.open.get(m):
                self.active.discard(m)
        return fills

    def cancel_market(self, m: int) -> int:
        n = 0
        for o in self.open.get(m, []):
            try:
                self.cli.cancel(o["id"], dry_run=False)
                n += 1
            except Exception as e:
                log.warning("mm cancel failed #%s order %s: %s", m, o.get("id"), e)
        self.open[m] = []
        return n

    def cancel_all(self) -> int:
        return sum(self.cancel_market(m) for m in list(self.open))

    def eligible(self, m: int, other_positions: float) -> bool:
        if not self.enabled or other_positions:
            return False
        return m in self.active or len(self.active) < self.max_markets

    def on_book(self, book: Book, fair: Optional[float], moving: bool, holding: float) -> dict:
        """Requote one market. Returns a summary of actions."""
        m = book.market_id
        inv = self.ledger.position(m)
        if fair is None or (moving and not inv):
            if self.open.get(m):
                return {"pulled": self.cancel_market(m)}
            return {}
        if time.time() - self.quoted_at.get(m, 0) < self.requote_s:
            return {}
        want = mm_quotes(book, fair, inv, self.ledger.avg_yes(m), self.ledger.held_for(m), edge=self.edge,
                         size=self.size, max_inventory=self.max_inventory, take=self.take,
                         max_hold_s=self.max_hold_s, min_spread=self.min_spread)
        have = {}
        for o in self.open.get(m, []):
            c = classify_open_order(o)
            if c:
                have.setdefault(c[0], []).append((o, c[1], c[2]))
        actions = {"cancelled": 0, "placed": []}
        for side in ("bid", "ask"):
            cur = have.get(side, [])
            w = want.get(side)
            keep = [x for x in cur if w and abs(x[1] - w[0]) < 1e-6]
            for o, _, _ in cur:
                if not keep or o is not keep[0][0]:
                    try:
                        self.cli.cancel(o["id"], dry_run=False)
                        actions["cancelled"] += 1
                    except Exception as e:
                        log.warning("mm cancel failed #%s: %s", m, e)
            if w and not keep and w[1] >= 10:
                yes_side = "BUY" if side == "bid" else "SELL"
                coid = f"mm:{m}:{side}:{int(time.time())}"
                try:
                    self.place(self.cli, "mm", m, book.exchange_id, yes_side, w[0], float(int(w[1])),
                               self.live, coid, holdings=holding)
                    self.last_quote_px[(m, side)] = w[0]
                    actions["placed"].append((side, w[0], int(w[1])))
                except Exception as e:
                    log.warning("mm place failed #%s %s: %s", m, side, e)
        self.quoted_at[m] = time.time()
        if want or inv:
            self.active.add(m)
        return actions

    def self_test(self, book: Book, holding: float) -> bool:
        """Place a resting order that cannot fill, find it, cancel it, confirm it is gone."""
        if not self.live:
            return True
        coid = f"mm:selftest:{int(time.time())}"
        try:
            self.place(self.cli, "mm", book.market_id, book.exchange_id, "BUY", 0.01, 50.0, True, coid,
                       holdings=holding)
            time.sleep(1.5)
            mine = [o for o in self.cli.portfolio().get("openOrders", []) if int(o.get("marketId", -1)) == book.market_id
                    and classify_open_order(o) and classify_open_order(o)[0] == "bid"
                    and abs(classify_open_order(o)[1] - 0.01) < 1e-6]
            if not mine:
                log.error("mm self-test: resting order not found in open orders; market making disabled")
                return False
            for o in mine:
                self.cli.cancel(o["id"], dry_run=False)
            time.sleep(1.5)
            left = [o for o in self.cli.portfolio().get("openOrders", []) if o.get("id") in {x["id"] for x in mine}]
            if left:
                log.error("mm self-test: cancel did not remove the order; market making disabled")
                return False
            log.info("mm self-test passed (resting order placed and cancelled on #%s)", book.market_id)
            return True
        except Exception as e:
            log.error("mm self-test failed: %s; market making disabled", e)
            return False
