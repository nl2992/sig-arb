"""
fair_value.py — trade SIG toward the Kalshi / Polymarket consensus price.

Fair value for a SIG market is the mean mid of its APPROVED reference markets
(config/market_matches.json), using only quotes that are fresh and reasonably tight,
and only when the venues agree. Reference prices move first on news, so this is also
the bot's news signal.

  entry: SIG bid >= fair + threshold  -> SELL YES (buy NO) into those bids
         SIG ask <= fair - threshold  -> BUY YES from those asks
  exit:  a fair-value position is closed once SIG trades back within `exit_band` of
         fair (short-term round trip); otherwise it is held to settlement.

Fair-value positions live in their own ledger (logs/fv_positions.json) so hedged arb
legs are never mistaken for them.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from arb_engine import Book
from crossvenue_adapters import KalshiAdapter, PolymarketAdapter
from crossvenue_models import parse_float
from market_matches import load_registry

ROOT = pathlib.Path(__file__).parent
REGISTRY = ROOT / "config" / "market_matches.json"
LEDGER = ROOT / "logs" / "fv_positions.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ReferencePrices:
    """Fresh YES bid/ask for every approved reference market, refreshed in parallel."""

    def __init__(self, registry: pathlib.Path = REGISTRY, max_age_s: float = 300, max_spread: float = 0.06,
                 max_disagree: float = 0.04, concurrency: int = 8, adapters: Optional[dict] = None):
        self.matches = [m for m in load_registry(registry) if m.status == "APPROVED"]
        self.by_sig: Dict[int, list] = {}
        for m in self.matches:
            self.by_sig.setdefault(m.sig_market_id, []).append((m.reference_venue, m.reference_market_id))
        self.max_age_s, self.max_spread, self.max_disagree = max_age_s, max_spread, max_disagree
        self.concurrency = concurrency
        self.adapters = adapters or {"kalshi": KalshiAdapter(), "polymarket": PolymarketAdapter()}
        self.quotes: Dict[tuple, tuple] = {}          # (venue, id) -> (bid, ask, fetched_monotonic)
        self.lock = threading.Lock()
        self.last_refresh: Optional[str] = None
        self.errors = 0

    def _quote(self, venue: str, ref_id: str) -> Optional[tuple]:
        if venue == "kalshi":
            item = self.adapters["kalshi"]._get(f"/markets/{ref_id}")
            item = item.get("market", item) if isinstance(item, dict) else {}
            return parse_float(item.get("yes_bid_dollars")), parse_float(item.get("yes_ask_dollars"))
        item = self.adapters["polymarket"]._get(f"/markets/{ref_id}")
        return parse_float(item.get("bestBid")), parse_float(item.get("bestAsk"))

    def refresh(self) -> int:
        keys = sorted({(m.reference_venue, m.reference_market_id) for m in self.matches})

        def one(key):
            try:
                return key, self._quote(*key)
            except (requests.RequestException, ValueError, AttributeError):
                return key, None
        got, errors = {}, 0
        with ThreadPoolExecutor(self.concurrency) as ex:
            for key, q in ex.map(one, keys):
                if q and q[0] is not None and q[1] is not None:
                    got[key] = (q[0], q[1], time.monotonic())
                else:
                    errors += 1
        with self.lock:
            self.quotes.update(got)
            self.errors, self.last_refresh = errors, _now()
        return len(got)

    def start(self, interval_s: float = 120) -> threading.Thread:
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception:
                    pass
                time.sleep(interval_s)
        t = threading.Thread(target=loop, name="reference-prices", daemon=True)
        t.start()
        return t

    def fair(self, sig_market_id: int) -> Optional[dict]:
        """Consensus fair value, or None if no usable / agreeing reference."""
        now = time.monotonic()
        mids, sources = [], []
        with self.lock:
            for venue, ref_id in self.by_sig.get(sig_market_id, []):
                q = self.quotes.get((venue, ref_id))
                if not q or now - q[2] > self.max_age_s:
                    continue
                bid, ask = q[0], q[1]
                if ask < bid or ask - bid > self.max_spread:
                    continue
                mids.append((bid + ask) / 2)
                sources.append({"venue": venue, "id": ref_id, "bid": bid, "ask": ask})
        if not mids or max(mids) - min(mids) > self.max_disagree:
            return None
        return {"fair": sum(mids) / len(mids), "sources": sources}


def entry_signal(book: Book, fair: float, threshold: float, capital_left: float,
                 min_qty: float = 10) -> Optional[dict]:
    """Walk SIG levels at least `threshold` past fair. Capital per share: YES costs its
    price; selling YES (buying NO) costs 1 - price."""
    if capital_left <= 0:
        return None
    for side, levels, ok, cost in (
            ("SELL", book.bids, lambda p: p >= fair + threshold, lambda p: 1 - p),
            ("BUY", book.asks, lambda p: p <= fair - threshold, lambda p: p)):
        qty = spent = notional = 0.0
        limit = None
        for price, size in levels:
            if not ok(price):
                break
            take = min(size, (capital_left - spent) / cost(price))
            if take <= 0:
                break
            qty += take; spent += take * cost(price); notional += take * price; limit = price
        qty = float(int(qty))
        if qty >= min_qty and limit is not None:
            vwap = notional / qty if qty else limit
            edge = (vwap - fair) if side == "SELL" else (fair - vwap)
            return {"yes_side": side, "limit": limit, "qty": qty, "fair": round(fair, 4),
                    "vwap": round(vwap, 4), "edge": round(edge, 4), "capital": round(spent, 2),
                    "expected_pnl": round(edge * qty, 2)}
    return None


def slip_ok(price: float, fair: float, *, selling: bool, max_slip: float) -> bool:
    """One-sided slippage guard for exits: refuse only prices more than `max_slip` worse
    than fair. A SIG price that lags on our side of fair is the best exit there is."""
    return price >= fair - max_slip if selling else price <= fair + max_slip


def exit_signal(book: Book, fair: float, position: float, exit_band: float, entry: Optional[float] = None,
                tp: Optional[float] = None, stop: Optional[float] = None, max_slip: float = 0.03,
                held_s: float = 0.0, max_hold_s: Optional[float] = None) -> Optional[dict]:
    """Close a fair-value position (signed YES qty). Reasons, first match wins:
    target    SIG pays >= `tp` better than the entry
    stop      the reference fair moved >= `stop` against the entry (thesis broken); only
              executed at a SIG price no more than `max_slip` worse than fair, never into
              an empty book (a price at or better than fair is always taken)
    converged SIG is back within `exit_band` of fair (the mispricing is gone)
    time      held longer than `max_hold_s` (off when None), same slippage guard."""
    def guarded(price):
        return slip_ok(price, fair, selling=position > 0, max_slip=max_slip)
    if position > 0 and book.bids:
        price, size = book.bids[0]
        why = ("target" if entry is not None and tp and price >= entry + tp
               else "stop" if entry is not None and stop and fair <= entry - stop and guarded(price)
               else "converged" if price >= fair - exit_band
               else "time" if max_hold_s and held_s >= max_hold_s and guarded(price) else None)
        if why:
            return {"yes_side": "SELL", "limit": price, "qty": float(min(position, size)),
                    "fair": round(fair, 4), "exit": why, "entry": None if entry is None else round(entry, 4)}
    if position < 0 and book.asks:
        price, size = book.asks[0]
        why = ("target" if entry is not None and tp and price <= entry - tp
               else "stop" if entry is not None and stop and fair >= entry + stop and guarded(price)
               else "converged" if price <= fair + exit_band
               else "time" if max_hold_s and held_s >= max_hold_s and guarded(price) else None)
        if why:
            return {"yes_side": "BUY", "limit": price, "qty": float(min(-position, size)),
                    "fair": round(fair, 4), "exit": why, "entry": None if entry is None else round(entry, 4)}
    return None


class Ledger:
    """Signed YES quantity and capital for fair-value positions only."""

    def __init__(self, path: pathlib.Path = LEDGER):
        self.path = path
        try:
            self.rows = {int(k): v for k, v in json.loads(path.read_text()).items()}
        except (OSError, ValueError):
            self.rows = {}

    def position(self, market_id: int) -> float:
        return float(self.rows.get(market_id, {}).get("qty", 0.0))

    def capital(self, market_id: int) -> float:
        return float(self.rows.get(market_id, {}).get("capital", 0.0))

    def avg_yes(self, market_id: int) -> Optional[float]:
        """Average entry in YES terms (long YES: price paid; short YES: 1 - NO price paid)."""
        q, cap = self.position(market_id), self.capital(market_id)
        if not q:
            return None
        return cap / q if q > 0 else 1 - cap / -q

    def held_for(self, market_id: int) -> float:
        opened = self.rows.get(market_id, {}).get("opened_at")
        return time.time() - opened if opened and self.position(market_id) else 0.0

    def gross(self) -> float:
        return sum(float(r.get("capital", 0.0)) for r in self.rows.values())

    def record(self, market_id: int, yes_side: str, qty: float, price: float) -> None:
        """Apply a fill. Opening adds capital; reducing releases it pro rata."""
        if qty <= 0:
            return
        row = self.rows.setdefault(market_id, {"qty": 0.0, "capital": 0.0, "realized": 0.0})
        signed = qty if yes_side == "BUY" else -qty
        pos = row["qty"]
        if pos == 0:
            row["opened_at"] = time.time()
        if pos == 0 or (pos > 0) == (signed > 0):          # opening / adding
            row["capital"] += qty * (price if signed > 0 else 1 - price)
            row["qty"] = pos + signed
        else:                                               # reducing
            closed = min(abs(signed), abs(pos))
            avg_cost = row["capital"] / abs(pos)
            proceeds = closed * (price if pos > 0 else 1 - price)
            row["realized"] = round(row.get("realized", 0.0) + proceeds - closed * avg_cost, 4)
            row["capital"] -= closed * avg_cost
            row["qty"] = pos + signed
            if abs(signed) > closed:                        # flipped through zero
                rest = abs(signed) - closed
                row["capital"] = rest * (price if signed > 0 else 1 - price)
        if abs(row["qty"]) < 1e-9:
            row["qty"], row["capital"] = 0.0, 0.0
        row["updated_at"] = _now()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in self.rows.items()}, indent=1))
        tmp.replace(self.path)


def net_holding(cli, market_id: int) -> float:
    """Account-wide signed YES holding (arb + fair value) for order construction."""
    return sum(float(h.get("quantity") or 0) for h in cli.holdings(market_id)
               if str(h.get("settlementOption", "YES")).upper() == "YES")


def touch_edge(book: Book, fair: float) -> float:
    """How far SIG's best price is past fair (positive = tradeable toward fair)."""
    edges = []
    if book.bids:
        edges.append(book.bids[0][0] - fair)
    if book.asks:
        edges.append(fair - book.asks[0][0])
    return max(edges) if edges else 0.0


def plan_market(book: Book, fair: Optional[dict], ledger: Ledger, *, threshold: float, exit_band: float,
                max_per_market: float, gross_left: float, unit: Optional[float] = None,
                tp: Optional[float] = None, stop: Optional[float] = None, max_slip: float = 0.03,
                max_hold_s: Optional[float] = None, min_sources: int = 1,
                kelly_bankroll: Optional[float] = None, kelly_fraction: float = 0.25) -> Optional[dict]:
    """Exit first, else entry; None when nothing to do. With `unit`, entry capital scales
    with the gap: unit x (edge at the touch / threshold), so a large move on Kalshi or
    Polymarket that SIG has not followed earns a proportionally large position."""
    if not fair:
        return None
    f = fair["fair"]
    pos = ledger.position(book.market_id)
    if pos:
        ex = exit_signal(book, f, pos, exit_band, entry=ledger.avg_yes(book.market_id), tp=tp, stop=stop,
                         max_slip=max_slip, held_s=ledger.held_for(book.market_id), max_hold_s=max_hold_s)
        if ex:
            return ex
    room = min(max_per_market - ledger.capital(book.market_id), gross_left)
    if unit:
        room = min(room, unit * max(0.0, touch_edge(book, f)) / threshold)
    if min_sources > 1 and len(fair.get("sources") or []) < min_sources:
        return None                      # entries need every required venue quoting and agreeing
    sig = entry_signal(book, f, threshold, room)
    # never add against an existing fair-value position; exits handle that side
    if sig and pos and (pos > 0) != (sig["yes_side"] == "BUY"):
        return None
    if sig and kelly_bankroll:
        sig = kelly_cap(book, sig, f, kelly_bankroll, kelly_fraction)
    return sig


def kelly_cap(book: Book, sig: dict, fair: float, bankroll: float, fraction: float,
              min_qty: float = 10) -> Optional[dict]:
    """Cap an entry at fractional Kelly for a binary paying 1, with `fair` as the win
    probability: a 5c edge on a 50c contract is a much smaller bet than on a 90c one."""
    import kelly
    if sig["yes_side"] == "BUY":
        ladder = [(p, q) for p, q in book.asks if p <= sig["limit"] + 1e-9]
        prob = fair
    else:                                # buying No at 1 - bid
        ladder = [(round(1 - p, 6), q) for p, q in book.bids if p >= sig["limit"] - 1e-9]
        prob = 1 - fair
    if not ladder or not 0 < prob < 1:
        return None
    k = kelly.size_position(ladder, prob, bankroll, fraction=fraction)
    qty = float(int(min(sig["qty"], k["qty"])))
    if qty < min_qty:
        return None
    if qty < sig["qty"]:
        sig = {**sig, "qty": qty, "capital": round(sig["capital"] * qty / sig["qty"], 2),
               "expected_pnl": round(sig["edge"] * qty, 2), "kelly_capped": True}
    return sig
