"""
resting_exits.py — standing take-profit orders for fair-value and conviction positions.

Without this, a position only exits when SIG's best price already reaches its exit rule,
and then takes that price. A resting order at the rule's price also catches every SIG
trader willing to trade there (SIG spreads are often 1-3c, so it is usually the best price
on its side of the book).

Price (YES terms; rounded a tick in our favour):
  fv  the prices where fair_value.exit_signal's target / converged rules fire:
      long: sell at min(entry + tp, fair - band); short: buy at max(entry - tp, fair + band)
  cv  take profit once SIG reaches fair, never below the entry:
      long: sell at max(fair - band, entry);       short: buy at min(fair + band, entry)
No order while the strategy's stop condition holds: the taker stop handles that.

Size: the strategy's position, capped by the account's net holding on that side (SIG nets
YES and NO in one market), so an exit can never take the account past flat.

Markets: only where no other bot strategy works the market (market making, lead-lag, or
both fv and cv). The market maker in turn stays out of markets with resting exits, and the
fv/cv taker exits skip them, so a holdings change there is ours to explain.

Fills: inferred from the minute account snapshot, before reconciliation: the part of a
market's holding change that no ledger or arb journal explains, in the exit's direction and
up to the order's outstanding size, is booked to the strategy's ledger at the order price.
Orders cancelled or gone stay attributable for `grace_s`. The holdings check's in-doubt
recovery (intents flagged resting) is the backstop, e.g. for a fill just before shutdown.
After any cancel the fv/cv taker exits wait for the next snapshot, so a stop never sells
shares a resting order already sold.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import holdings_check
from arb_engine import Book
from scalper import ceil_tick, classify_open_order, floor_tick

log = logging.getLogger("sigarb")
TOL = 0.5


def exit_price(rule: dict, pos: float, entry: float, fair: float) -> Optional[float]:
    """YES-terms price of the resting exit for a signed position, or None."""
    band, tp = rule.get("band", 0.0), rule.get("tp")
    if pos > 0:
        px = fair - band
        if rule.get("floor_at_entry"):
            px = max(px, entry)
        elif tp:
            px = min(px, entry + tp)
        px = ceil_tick(px)
    else:
        px = fair + band
        if rule.get("floor_at_entry"):
            px = min(px, entry)
        elif tp:
            px = max(px, entry - tp)
        px = floor_tick(px)
    return px if 0 < px < 1 else None


def stop_hit(rule: dict, pos: float, entry: float, fair: float) -> bool:
    stop = rule.get("stop")
    if not stop:
        return False
    return fair <= entry - stop if pos > 0 else fair >= entry + stop


class RestingExits:
    def __init__(self, cli, ledgers: Dict[str, object], place, *, rules: Dict[str, dict], live: bool = False,
                 requote_s: float = 60.0, grace_s: float = 180.0, pace_s: float = 1.0, max_per_cycle: int = 6,
                 min_qty: float = 10.0, intent_log=None):
        self.cli, self.ledgers, self.place, self.rules, self.live = cli, ledgers, place, rules, live
        self.intent_log = intent_log
        self.requote_s, self.grace_s, self.pace_s, self.max_per_cycle = requote_s, grace_s, pace_s, max_per_cycle
        self.min_qty = min_qty
        self.orders: Dict[int, dict] = {}            # market -> our resting exit
        self.gone: Dict[int, List[dict]] = {}        # market -> cancelled/vanished exits still attributable
        self.blocked: Dict[int, float] = {}          # market -> time of our last cancel (takers wait for a sync)
        self.taker_at: Dict[int, float] = {}         # market -> time a taker exit claimed it (we wait for a sync)
        self.lock = threading.RLock()
        self.markets_snapshot: frozenset = frozenset()
        self.takers_held: frozenset = frozenset()
        self.claimed: frozenset = frozenset()          # markets whose unexplained fills are ours to book
        self.stats = {"resting": 0, "placed": 0, "cancelled": 0, "fills": 0, "errors": 0}

    # -- what we want resting in one market --------------------------------------------
    def want(self, m: int, fairs: Dict[str, Optional[float]], holding: float, busy: bool) -> Optional[dict]:
        if busy:
            return None
        held = [(name, led) for name, led in self.ledgers.items() if led.position(m)]
        if len(held) != 1:
            return None                              # nothing to exit, or two strategies share it
        name, led = held[0]
        pos, rule, fair = led.position(m), self.rules.get(name), fairs.get(name)
        if rule is None or fair is None or (pos > 0) != (holding > 0):
            return None                              # no rule / no reference / account netted the other way
        entry = led.avg_yes(m)
        if entry is None or stop_hit(rule, pos, entry, fair):
            return None
        px = exit_price(rule, pos, entry, fair)
        qty = float(int(min(abs(pos), abs(holding))))
        if px is None or qty < self.min_qty:
            return None
        return {"strategy": name, "yes_side": "SELL" if pos > 0 else "BUY", "price": px, "qty": qty}

    # -- coordination with the taker exits (main thread) --------------------------------
    def claim_taker(self, m: int) -> bool:
        """A fv/cv taker exit may trade market m only if no resting exit works it (or was
        just cancelled there); the claim then keeps resting exits out of m until the next
        snapshot has booked the taker's fill."""
        with self.lock:
            if m in self.orders or m in self.blocked:
                return False
            self.taker_at[m] = time.time()
            self._after_change()
            return True

    # -- order traffic (worker thread; the lock is never held across a SIG request) -----
    def on_book(self, book: Book, fairs: Dict[str, Optional[float]], holding: float, busy: bool) -> dict:
        m = book.market_id
        with self.lock:
            cur, w = self.orders.get(m), self.want(m, fairs, holding, busy)
            if m in self.taker_at:
                w = None                             # a taker exit traded here: wait for the snapshot
            # an order SIG no longer lists (or fully booked) is cancelled by id before any
            # replacement, so two exits can never rest in one market
            settled = cur and (cur.get("vanished") or cur["booked"] >= cur["qty"] - TOL)
            if cur and cur.get("placing"):
                return {}
            if cur and w and not settled and (cur["strategy"], cur["yes_side"]) == (w["strategy"], w["yes_side"]):
                outstanding = cur["qty"] - cur["booked"]
                same_px = abs(cur["price"] - w["price"]) < 1e-6
                must_shrink = w["qty"] < outstanding - TOL      # never rest more than the account holds
                if not must_shrink and (same_px and abs(w["qty"] - outstanding) <= max(10.0, 0.05 * outstanding)
                                        or time.time() - cur["at"] < self.requote_s):
                    return {}
            if not cur and not w:
                return {}
            old = self._detach(m) if cur else None
            new = None
            if w:
                coid = f"{w['strategy']}x:{m}:{int(time.time() * 1000)}"
                new = self.orders[m] = {**w, "booked": 0.0, "coid": coid, "id": None, "at": time.time(),
                                        "placing": True}
            self._after_change()
        actions = {}
        if old:
            self._send_cancel(m, old)
            actions["cancelled"] = 1
        if new:
            try:
                resp = self.place(self.cli, new["strategy"], m, book.exchange_id, new["yes_side"], new["price"],
                                  new["qty"], self.live, new["coid"], holdings=holding, resting=True)
            except Exception:
                with self.lock:                      # may or may not rest: find and cancel it by price later
                    new.pop("placing", None)
                    new["vanished"] = True
                raise
            oid = next((o.get("orderId") or o.get("id") for o in (resp.get("orders") or [resp])
                        if o.get("orderId") or o.get("id")), None)
            with self.lock:
                new["id"] = oid
                new["at"] = time.time()
                new.pop("placing", None)
            self.stats["placed"] += 1
            actions["placed"] = (new["strategy"], new["yes_side"], new["price"], new["qty"])
        return actions

    def _detach(self, m: int) -> Optional[dict]:
        """(lock held) Stop tracking m's order as resting; it stays attributable for grace_s
        and the takers wait for the next snapshot."""
        o = self.orders.pop(m, None)
        if o:
            o["until"] = time.time() + self.grace_s
            self.gone.setdefault(m, []).append(o)
            self.blocked[m] = time.time()
        return o

    def _send_cancel(self, m: int, o: dict) -> None:
        try:
            oid = o.get("id") or self._find_open_id(m, o)
            if oid is not None:
                self.cli.cancel(oid, dry_run=False)
            self.stats["cancelled"] += 1
        except Exception as e:                     # already filled or gone: sync attributes any fill
            log.warning("resting exit cancel failed #%s: %s", m, e)

    def _find_open_id(self, m: int, o: dict):
        for x in self.cli.portfolio().get("openOrders", []):
            if self._matches(x, m, o):
                return x.get("id")
        return None

    @staticmethod
    def _matches(x: dict, m: int, o: dict) -> bool:
        try:
            if int(x.get("marketId", -1)) != m:
                return False
        except (TypeError, ValueError):
            return False
        c = classify_open_order(x)
        side = "ask" if o["yes_side"] == "SELL" else "bid"
        return bool(c) and c[0] == side and abs(c[1] - o["price"]) < 1e-6

    def cancel_all(self) -> int:
        with self.lock:
            detached = [(m, self._detach(m)) for m in list(self.orders)]
            self._after_change()
        for m, o in detached:
            self._send_cancel(m, o)
        return len(detached)

    # -- fills, from the minute snapshot (main thread, before reconciliation) ----------
    def sync(self, portfolio: dict, expected_all: Callable[[], Dict[int, float]], fetched_at: float) -> List[dict]:
        with self.lock:
            actual = holdings_check.actual_holdings(portfolio)
            exp = expected_all()
            fills, now = [], time.time()
            for m in set(self.orders) | set(self.gone):
                unexplained = actual.get(m, 0.0) - exp.get(m, 0.0)
                for o in ([self.orders[m]] if m in self.orders else []) + self.gone.get(m, []):
                    sign = -1.0 if o["yes_side"] == "SELL" else 1.0
                    if unexplained * sign <= TOL:
                        break
                    f = min(abs(unexplained), o["qty"] - o["booked"])
                    if f < TOL:
                        continue
                    self.ledgers[o["strategy"]].record(m, o["yes_side"], f, o["price"])
                    o["booked"] += f
                    unexplained -= sign * f
                    holdings_check.resolve_intent(o["coid"], "DONE", self.intent_log, filled=o["booked"])
                    self.stats["fills"] += 1
                    fills.append({"market_id": m, "strategy": o["strategy"], "yes_side": o["yes_side"],
                                  "qty": f, "price": o["price"]})
            # An order SIG no longer lists (filled, or gone) is flagged; the worker cancels it by
            # id (harmless if already gone) and keeps it attributable for grace_s.
            opened = portfolio.get("openOrders", [])
            for m, o in self.orders.items():
                mine = next((x for x in opened if self._matches(x, m, o)), None)
                if mine is not None:
                    o["id"] = o.get("id") or mine.get("id")
                elif not o.get("placing") and fetched_at - o["at"] > 20:
                    o["vanished"] = True
            for m in list(self.gone):
                keep = []
                for o in self.gone[m]:
                    if o["until"] > now and o["booked"] < o["qty"] - TOL:
                        keep.append(o)
                    else:
                        holdings_check.resolve_intent(o["coid"], "CLOSED", self.intent_log, filled=o["booked"])
                self.gone[m] = keep
                if not keep:
                    self.gone.pop(m)
            for claims in (self.blocked, self.taker_at):   # this snapshot was read after the order
                for m, t in list(claims.items()):
                    if fetched_at > t + 2:
                        claims.pop(m)
            self._after_change()
            return fills

    def _after_change(self) -> None:
        self.markets_snapshot = frozenset(self.orders)
        self.takers_held = frozenset(self.orders) | frozenset(self.blocked)
        self.claimed = self.takers_held | frozenset(self.gone)
        self.stats["resting"] = len(self.orders)

    # -- worker thread ----------------------------------------------------------------
    def start_worker(self, kill_switch: Callable[[], bool]) -> threading.Thread:
        self._pending: Dict[int, tuple] = {}
        self._qlock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._kill = kill_switch
        self._thread = threading.Thread(target=self._run, name="resting-exits", daemon=True)
        self._thread.start()
        return self._thread

    def submit(self, book: Book, fairs: Dict[str, Optional[float]], holding: float, busy: bool) -> None:
        """Queue one market (newest per market wins). Cheap; called from the scan loop."""
        with self._qlock:
            self._pending[book.market_id] = (book, fairs, holding, busy, time.time())
        self._wake.set()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                if self._kill() or not self.live:
                    if self.orders:
                        n = self.cancel_all()
                        log.warning("resting exits: pulled %d order(s) (kill switch / not live)", n)
                    with self._qlock:
                        self._pending.clear()
                    continue
                with self._qlock:
                    pending, self._pending = self._pending, {}
                sent = 0
                for m, (book, fairs, holding, busy, at) in pending.items():
                    if self._stop.is_set() or self._kill():
                        break
                    if sent >= self.max_per_cycle:          # pace order traffic; requeue the rest
                        with self._qlock:
                            self._pending.setdefault(m, (book, fairs, holding, busy, at))
                        continue
                    if time.time() - at > 30:               # book too old to price from
                        continue
                    act = self.on_book(book, fairs, holding, busy)
                    if act:
                        sent += 1
                        log.info("EXITS #%s: %s", m, act)
                        self._stop.wait(self.pace_s)
                backoff = 2.0
            except Exception as e:
                self.stats["errors"] += 1
                backoff = min(120.0, backoff * 2)
                log.warning("resting exits: %s (backing off %.0fs)", e, backoff)
                self._stop.wait(backoff)

    def stop_worker(self, timeout: float = 60) -> None:
        if getattr(self, "_thread", None):
            self._stop.set()
            self._wake.set()
            self._thread.join(timeout)
