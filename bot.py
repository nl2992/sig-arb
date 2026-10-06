"""
bot.py — the systematic layer on top of signals.py.

Three modes, meant to be adopted in order:

  --mode signal   (default) scan loop, print tickets, never touches order endpoints
  --mode confirm  scan loop; for each new signal, show tickets and ask y/N.
                  On "y": quote -> place legs via API (dry-run unless --live)
  --mode auto     scan loop; executes signals that pass risk checks with no prompt
                  (live needs --live, sig_client.PLACE_PAYLOAD_CONFIRMED = True and
                  "manual_approval": false in config/risk_limits.json)

Limits in config/risk_limits.json always apply; CLI caps can only tighten them.
logs/KILL_SWITCH stops all new orders. In live mode an UNKNOWN, LEGGED or
IMBALANCED result engages it, because nothing unwinds residuals automatically.
The loop writes logs/bot_status.json every tick for the dashboard.

    python bot.py --mode signal --interval 5
    python bot.py --mode confirm --interval 5 --live
    python bot.py --mode auto --interval 3 --live --min-edge 0.005 --max-gross 50000

All executions are appended to logs/executions.jsonl.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import pathlib
import signal
import time
import uuid
from typing import Optional

from arb_engine import ArbResult, Book, breakeven_limit, group_markets, max_executable_arb
import arb_exits
import conviction
import fair_value
import fast_scan
import gates
import holdings_check
import news_watch
import positions
import resting_exits
import scalper
import sig_client
from sig_client import Client
from signals import HERE, Snapshot, generate, load_exhaustive, log_csv, render, sig_key, tickets

log = logging.getLogger("sigarb")
EXEC_LOG = HERE / "logs" / "executions.jsonl"
KILL_SWITCH = HERE / "logs" / "KILL_SWITCH"
INTENT_LOG = HERE / "logs" / "order_intents.jsonl"
BOT_STATUS = HERE / "logs" / "bot_status.json"
# Latest books the bot has read, shared with the dashboard so it never polls SIG itself.
BOT_BOOKS = HERE / "logs" / "bot_books.json"
RISK_LIMITS = HERE / "config" / "risk_limits.json"
# Results that leave naked or unknown exposure; live mode stops on these.
HALT_STATUSES = {"UNKNOWN", "LEGGED", "IMBALANCED"}
# Live orders stop this long before the SIG access token expires.
TOKEN_MARGIN_S = 300
# The bot renews its own session this long before expiry (needs its own login; README).
REFRESH_AHEAD_S = 600


def kill_switch_engaged(path: pathlib.Path = None) -> bool:
    return (path or KILL_SWITCH).exists()


def replace_file(tmp: pathlib.Path, path: pathlib.Path, tries: int = 20) -> None:
    """tmp.replace(path), retried: on Windows it fails while a reader (dashboard, status) has path open."""
    for i in range(tries):
        try:
            return tmp.replace(path)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def engage_kill_switch(reason: str, actor: str = "bot", path: pathlib.Path = None) -> None:
    """Create the kill switch atomically. Idempotent; never released from here."""
    path = path or KILL_SWITCH
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{reason}\nactor={actor} at={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}\n")
    replace_file(tmp, path)


# ----------------------------------------------------------- execution
def _fill_of(resp: dict, default_qty: float, default_px: float):
    """Pull filled qty / avg price / order ids out of a place() response. The fill keys
    are confirmed only once PLACE_PAYLOAD_CONFIRMED is; dry-run assumes a full fill."""
    if resp.get("dryRun"):
        return default_qty, default_px, []
    q = next((resp[k] for k in ("filledQuantity", "filled", "filledQty") if k in resp), None)
    p = next((resp[k] for k in ("avgPrice", "averagePrice", "fillPrice") if k in resp), None)
    oids = [o.get("orderId") or o.get("id") for o in resp.get("orders") or [resp]]
    return (float(q if q is not None else 0), float(p if p is not None else default_px),
            [o for o in oids if o])


def leg_plan(r: ArbResult) -> list:
    """Legs in send order (thinnest first) with YES-terms side, limit and quantity."""
    yes_side = "SELL" if r.direction == "SELL_ALL" else "BUY"
    order = sorted(r.legs, key=lambda l: min(q for _, q in l.fills))
    return [{"market_id": l.market_id, "exchange_id": l.exchange_id, "yes_side": yes_side,
             "limit": l.limit, "qty": r.qty} for l in order]


def preview(cli: Client, r: ArbResult) -> list:
    """The exact order bodies execute() would send for the first pass (never sends).
    The last leg may later chase toward break-even, and later legs shrink to fills."""
    out = []
    for k, leg in enumerate(leg_plan(r)):
        resp = cli.place(leg["market_id"], leg["exchange_id"], leg["yes_side"], leg["limit"],
                         leg["qty"], dry_run=True, client_order_id=f"preview:{r.race}:{k}")
        out.append({**leg, "bodies": [o["body"] for o in resp["orders"]]})
    return out


# Ctrl+C / SIGTERM must never land between an order leaving and its fill being recorded
# (that lost fills on #376 and #383 on 1 Oct). Inside critical() a stop request is
# deferred until the order is journaled and booked, then raised.
_critical_depth = 0
_stop_pending = False


def _on_stop_signal(signum, frame):
    global _stop_pending
    if _critical_depth:
        _stop_pending = True
        log.warning("stop requested: finishing the order in flight first")
        return
    raise KeyboardInterrupt


@contextlib.contextmanager
def critical():
    global _critical_depth, _stop_pending
    _critical_depth += 1
    try:
        yield
    finally:
        _critical_depth -= 1
        if not _critical_depth and _stop_pending:
            _stop_pending = False
            raise KeyboardInterrupt


def place_tracked(cli: Client, strategy: str, market_id: int, exchange_id: int, yes_side: str,
                  limit: float, qty: float, live: bool, client_order_id: str, holdings: float = None,
                  resting: bool = False) -> dict:
    """cli.place with a write-ahead intent: SENT is on disk before the order leaves, the
    result after it returns. A process killed in between leaves an in-doubt intent that
    holdings_check recovers from SIG's holdings on the next start."""
    if live:
        holdings_check.write_intent(client_order_id, strategy, market_id, yes_side, limit, qty, INTENT_LOG,
                                    resting=resting)
    kw = {} if holdings is None else {"holdings": holdings}
    try:
        resp = cli.place(market_id, exchange_id, yes_side, limit, qty, dry_run=not live,
                         client_order_id=client_order_id, **kw)
    except Exception as e:
        if live:
            holdings_check.resolve_intent(client_order_id, "ERROR", INTENT_LOG, error=str(e)[:200])
        raise
    if live:
        holdings_check.resolve_intent(client_order_id, "UNKNOWN" if resp.get("_unknown") else "DONE", INTENT_LOG,
                                      filled=resp.get("filledQuantity"))
    return resp


def execute(cli: Client, r: ArbResult, live: bool, chase_ticks: int = 2, tick: float = 0.005,
            fee: float = 0.0, kill_switch: pathlib.Path = None, run_id: str = None,
            pre_quote: bool = False) -> dict:
    """Leg the arb: thinnest leg first; later legs sized to actual fill; last leg
    may chase up to `chase_ticks` but never past break-even. Stops before any leg
    if the kill switch is engaged, and on any order whose outcome is unknown."""
    run_id = run_id or uuid.uuid4().hex[:12]
    plan = leg_plan(r)
    target, vwaps, rep = r.qty, [], []
    for k, leg in enumerate(plan):
        if kill_switch_engaged(kill_switch):
            return {"status": "KILLED" if k == 0 else "LEGGED", "legs": rep, "run_id": run_id,
                    "action": "manually flatten earlier legs" if k else None}
        yes_side, limit = leg["yes_side"], leg["limit"]
        if k == len(plan) - 1 and vwaps:
            be = breakeven_limit(r.direction, vwaps, fee, len(plan))
            c = chase_ticks * tick
            limit = max(be, limit - c) if yes_side == "SELL" else min(be, limit + c)
        # The quote endpoint only previews collateral; it costs a round trip per leg,
        # so it is opt-in (--pre-quote). The limit price already bounds the fill.
        if live and pre_quote:
            try:
                for o in sig_client.orders_for(yes_side, limit, target):
                    cli.quote(leg["exchange_id"], o["orderType"], o["priceLimit"], o["quantity"])
            except Exception as e:
                rep.append({"market": leg["market_id"], "error": f"quote: {e}"})
                return {"status": "ABORT" if k == 0 else "LEGGED", "legs": rep, "run_id": run_id}
        coid = f"{run_id}:{k}"
        resp = place_tracked(cli, "arb", leg["market_id"], leg["exchange_id"], yes_side, limit, target,
                             live, coid)
        fq, fp, oids = _fill_of(resp, target, limit)
        rep.append({"market": leg["market_id"], "side": yes_side, "limit": round(limit, 4),
                    "req": target, "filled": fq, "avg": fp, "client_order_id": coid,
                    "dryRun": bool(resp.get("dryRun"))})
        if resp.get("_unknown"):
            return {"status": "UNKNOWN", "legs": rep, "run_id": run_id,
                    "action": "reconcile holdings and open orders before any retry"}
        if fq < target:
            for oid in oids:
                cli.cancel(oid, dry_run=not live)
        if fq <= 0:
            return {"status": "LEGGED" if k else "MISS", "legs": rep, "run_id": run_id,
                    "action": "manually flatten earlier legs" if k else None}
        target, _ = fq, vwaps.append(fp)
    fills = [x["filled"] for x in rep]
    hedged = min(fills)
    residual = {x["market"]: x["filled"] - hedged for x in rep if x["filled"] > hedged}
    if residual:   # earlier legs over-filled vs. later ones -> naked exposure
        return {"status": "IMBALANCED", "qty": hedged, "legs": rep, "residual": residual,
                "run_id": run_id, "action": "flatten residual or work the short leg manually"}
    return {"status": "DONE", "qty": hedged, "legs": rep, "run_id": run_id}


def capital_spent(res: dict) -> float:
    """Cash an arb execution used: YES bought at avg, or NO bought (a YES SELL) at 1 - avg."""
    return sum(x["filled"] * (x["avg"] if x["side"] == "BUY" else 1.0 - x["avg"])
               for x in res.get("legs", []) if x.get("filled") and not x.get("dryRun"))


def execute_parallel(cli: Client, r: ArbResult, live: bool, chase_ticks: int = 2, tick: float = 0.005,
                     fee: float = 0.0, kill_switch: pathlib.Path = None, run_id: str = None,
                     floor=None) -> dict:
    """Send every leg at once at its walk limit, so no leg waits on another's round trip.
    If exactly one leg comes back short, top it up once (chasing at most `chase_ticks`,
    never past the break-even implied by the other legs' fills, or past `floor(others)`
    when given). Anything still uneven is IMBALANCED / LEGGED and halts the bot as in execute()."""
    from concurrent.futures import ThreadPoolExecutor
    run_id = run_id or uuid.uuid4().hex[:12]
    if kill_switch_engaged(kill_switch):
        return {"status": "KILLED", "legs": [], "run_id": run_id}
    plan = leg_plan(r)

    def send(k, leg, limit, qty, suffix=""):
        coid = f"{run_id}:{k}{suffix}"
        try:
            resp = place_tracked(cli, "arb", leg["market_id"], leg["exchange_id"], leg["yes_side"], limit, qty,
                                 live, coid)
        except Exception as e:
            # A rejected leg (e.g. 400 insufficient funds) placed nothing. Raising here lost the
            # other legs' fills on 6 Oct (California Governor left one-sided, unjournaled); as a
            # zero fill it goes through the top-up / IMBALANCED / LEGGED handling below.
            log.error("leg %s on #%s rejected: %s", coid, leg["market_id"], e)
            return {"market": leg["market_id"], "side": leg["yes_side"], "limit": round(limit, 4), "req": qty,
                    "filled": 0.0, "avg": limit, "client_order_id": coid, "dryRun": False,
                    "error": str(e)[:200], "_unknown": False}
        fq, fp, oids = _fill_of(resp, qty, limit)
        if fq < qty and not resp.get("_unknown"):
            for oid in oids:
                cli.cancel(oid, dry_run=not live)
        return {"market": leg["market_id"], "side": leg["yes_side"], "limit": round(limit, 4), "req": qty,
                "filled": fq, "avg": fp, "client_order_id": coid, "dryRun": bool(resp.get("dryRun")),
                "_unknown": bool(resp.get("_unknown"))}

    with ThreadPoolExecutor(len(plan)) as ex:
        rep = list(ex.map(lambda kl: send(kl[0], kl[1], kl[1]["limit"], r.qty), enumerate(plan)))
    if any(x.pop("_unknown") for x in rep):
        return {"status": "UNKNOWN", "legs": rep, "run_id": run_id, "parallel": True,
                "action": "reconcile holdings and open orders before any retry"}

    fills = [x["filled"] for x in rep]
    top = max(fills)
    short = [k for k, f in enumerate(fills) if f < top]
    if top > 0 and len(short) == 1 and not kill_switch_engaged(kill_switch):
        k = short[0]
        leg, need = plan[k], top - fills[k]
        others = [x["avg"] for j, x in enumerate(rep) if j != k]
        be = floor(others) if floor else breakeven_limit(r.direction, others, fee, len(plan))
        c = chase_ticks * tick
        limit = (max(be, leg["limit"] - c) if leg["yes_side"] == "SELL" else min(be, leg["limit"] + c))
        extra = send(k, leg, limit, need, suffix="r")
        if extra.pop("_unknown"):
            rep.append(extra)
            return {"status": "UNKNOWN", "legs": rep, "run_id": run_id, "parallel": True,
                    "action": "reconcile holdings and open orders before any retry"}
        extra["repair"] = True
        got = rep[k]["filled"] + extra["filled"]
        if got:
            rep[k]["avg"] = (rep[k]["avg"] * rep[k]["filled"] + extra["avg"] * extra["filled"]) / got
        rep[k]["filled"] = got
        rep.append(extra)

    legs = rep[:len(plan)]
    fills = [x["filled"] for x in legs]
    if max(fills) <= 0:
        return {"status": "MISS", "legs": rep, "run_id": run_id, "parallel": True}
    hedged = min(fills)
    if hedged <= 0:
        return {"status": "LEGGED", "legs": rep, "run_id": run_id, "parallel": True,
                "action": "manually flatten the filled legs"}
    residual = {x["market"]: x["filled"] - hedged for x in legs if x["filled"] > hedged}
    if residual:
        return {"status": "IMBALANCED", "qty": hedged, "legs": rep, "residual": residual, "run_id": run_id,
                "parallel": True, "action": "flatten residual or work the short leg manually"}
    return {"status": "DONE", "qty": hedged, "legs": rep, "run_id": run_id, "parallel": True}


def leg_depth(book: Book, direction: str, limit: float) -> float:
    """Shares resting at or better than our limit on the side this leg takes."""
    if direction == "SELL_ALL":                      # we sell YES into bids >= limit
        return sum(q for p, q in book.bids if p >= limit - 1e-9)
    return sum(q for p, q in book.asks if p <= limit + 1e-9)


def depth_limited(r: ArbResult, snap: Snapshot, a, budget: Optional[float], ratio: float) -> Optional[ArbResult]:
    """Shrink an arb until every leg has >= `ratio` x our size resting at or better than
    its limit, so one competitor taking part of a level cannot leave us one-sided
    (Nebraska and VA-01 legged that way on 1-2 Oct). None if nothing worthwhile is left."""
    if not ratio:
        return r
    books = {m["id"]: Book.from_levels(m["id"], snap.levels[m["id"]]) for m in snap.markets}
    legs = list(group_markets(snap.markets).get(r.race, {}).values())
    for _ in range(6):
        cap = min(leg_depth(books[l.market_id], r.direction, l.limit) for l in r.legs) / ratio
        if r.qty <= cap + 1e-9:
            return r if r.pnl >= a.min_pnl else None
        cap = float(int(cap))
        if cap < 10:
            return None
        kw = dict(min_edge=a.min_edge, fee_per_share=a.fee, max_qty=cap)
        if budget:
            kw["cash"] = min(budget, a.max_per_race) if getattr(a, "max_per_race", None) else budget
        r = max_executable_arb(r.race, [books[m] for m in legs], r.direction, **kw)
        if r is None:
            return None
    return None


# ---------------------------------------------------------- risk gate
class Risk:
    def __init__(self, a):
        self.a, self.gross, self.last_fire = a, 0.0, {}

    def ok(self, r: ArbResult) -> tuple[bool, str]:
        if r.marginal_edge < self.a.min_edge:
            return False, "edge"
        if len(r.legs) >= 3 and r.marginal_edge < self.a.min_edge_3leg:
            return False, "3-leg edge"
        if getattr(self.a, "max_per_race", None) and r.capital > self.a.max_per_race + 1e-9:
            return False, "per-race cap"
        if self.gross + r.capital > self.a.max_gross:
            high_ev = r.capital > 0 and r.pnl / r.capital >= getattr(self.a, "reserve_min_roi", float("inf"))
            if not high_ev or self.gross + r.capital > self.a.max_gross + getattr(self.a, "reserve", 0.0):
                return False, "gross cap"
        if time.time() - self.last_fire.get(r.race, 0) < self.a.cooldown:
            return False, "cooldown"
        return True, ""

    def book(self, r: ArbResult, res: dict):
        self.last_fire[r.race] = time.time()
        if res.get("status") in ("DONE", "LEGGED", "IMBALANCED", "UNKNOWN"):
            self.gross += r.capital


def roi(plan: dict) -> float:
    """Expected return on capital of an entry plan (0 when unknown)."""
    cap = plan.get("capital") or 0.0
    return plan.get("expected_pnl", 0.0) / cap if cap > 0 else 0.0


def account_room(a, risk, high_ev: bool = False) -> float:
    """Capital left under the account cap (venue_exposure.sig). Above it sits the high-EV
    reserve (risk_limits.json high_ev_reserve), open only to entries expected to return at
    least reserve_min_roi on their capital."""
    if risk is None:
        return float("inf")
    room = a.max_gross + (getattr(a, "reserve", 0.0) if high_ev else 0.0) - risk.gross
    claw = getattr(a, "claw", None)
    if claw is not None:                     # capital freed by exits belongs to market making now
        room = min(room, claw.room(risk.gross))
    return room


class Claw:
    """Capital freed when fv/cv/arb positions hit an exit rule moves to market making. Only
    fv/cv entries are held to it; arbs (riskless) may use freed capital up to the gross cap.
    The non-MM strategies' deployed capital is a ratchet: it can only fall (exits), never
    re-grow; what it gives up is added to the MM budget, up to `mm_max`. Persisted, so a
    restart keeps what was clawed back."""

    def __init__(self, path: pathlib.Path, mm_base: float, mm_max: float, slack: float = 200.0):
        self.path, self.mm_base, self.mm_max, self.slack = path, mm_base, mm_max, slack
        self.start = self.cap = None
        self.mm_gross = 0.0
        try:
            d = json.loads(path.read_text())
            self.start, self.cap = float(d["start"]), float(d["cap"])
        except Exception:
            pass

    def update(self, gross: float, mm_gross: float) -> None:
        """From the minute snapshot: ratchet the non-MM cap down to what is deployed now."""
        self.mm_gross = mm_gross
        nonmm = gross - mm_gross
        if self.start is None:
            self.start = self.cap = nonmm
        self.cap = min(self.cap, nonmm + self.slack)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"start": round(self.start, 2), "cap": round(self.cap, 2),
                                   "reclaimed": round(self.reclaimed(), 2), "mm_budget": round(self.mm_budget(), 2)}))
        replace_file(tmp, self.path)

    def reclaimed(self) -> float:
        return max(0.0, (self.start or 0.0) - (self.cap or 0.0))

    def mm_budget(self) -> float:
        return min(self.mm_max, self.mm_base + self.reclaimed())

    def room(self, gross: float) -> float:
        if self.cap is None:
            return float("inf")
        return self.cap - (gross - self.mm_gross)


def reserve_plan(plan: Optional[dict], replan, a, base_left: float, high_left: float) -> Optional[dict]:
    """Where the normal caps leave less room than the high-EV reserve, re-plan the entry with
    the reserve's room (strategy caps do not apply to it; per-market and per-race caps do) and
    keep it only if it is larger and expected to return >= reserve_min_roi."""
    if (plan and plan.get("exit")) or not getattr(a, "reserve", 0.0) or high_left <= max(base_left, 0.0):
        return plan
    hi = replan(high_left)
    if hi and not hi.get("exit") and roi(hi) >= a.reserve_min_roi and hi["qty"] > (plan["qty"] if plan else 0):
        return {**hi, "reserve": True}
    return plan


def journal(r: ArbResult, res: dict, mode: str, live: bool = False):
    EXEC_LOG.parent.mkdir(exist_ok=True)
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode, "live": live,
                            "race": r.race, "dir": r.direction, "qty": r.qty, "pnl": r.pnl,
                            "capital": r.capital, "result": res}) + "\n")


def run_fair_value(cli: Client, snap: Snapshot, refs, ledger, a, live: bool, risk=None,
                   holdings: dict = None, skip: set = frozenset(), touched: set = None,
                   skip_entries: set = frozenset(), exits_claim=None, news_ok=None) -> dict:
    """Fair-value orders for the markets in one freshly read race (single-leg, IOC-like).
    Room is the tightest of: account-wide exposure cap, fair-value cap, per-race cap
    (sibling markets are one bet), per-market cap; size scales with the gap."""
    out = {"fv_signals": 0, "fv_orders": 0, "fv_skipped": 0}
    for m in snap.markets:
        if m["id"] in skip:
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        race_used = sum(ledger.capital(x["id"]) for x in snap.markets)
        race_left = getattr(a, "fv_max_race", float("inf")) - race_used
        gross_left = min(a.fv_max_gross - ledger.gross(), race_left, account_room(a, risk))
        fair = refs.fair(m["id"])

        def replan(room):
            return fair_value.plan_market(book, fair, ledger, threshold=a.fv_threshold,
                                          exit_band=a.fv_exit, max_per_market=a.fv_max_market,
                                          gross_left=room, unit=getattr(a, "fv_unit", None),
                                          tp=getattr(a, "fv_tp", None), stop=getattr(a, "fv_stop", None),
                                          max_slip=getattr(a, "fv_max_slip", 0.03),
                                          max_hold_s=getattr(a, "fv_max_hold", 0) or None)
        plan = reserve_plan(replan(gross_left), replan, a, gross_left,
                            min(race_left, account_room(a, risk, high_ev=True)))
        if not plan:
            continue
        if not plan.get("exit") and m["id"] in skip_entries:
            continue                           # another strategy works this market; exits still run
        if not plan.get("exit") and not (getattr(a, "fv_min_fair", 0.0) <= plan["fair"] <= getattr(a, "fv_max_fair", 1.0)):
            continue                           # no new long shots: SIG's crowd keeps them rich until settlement
        if not plan.get("exit") and news_ok is not None and not news_ok(m["id"], plan["yes_side"]):
            continue                           # fresh news: only with the reference move (news_watch)
        if plan.get("exit") and exits_claim is not None and not exits_claim(m["id"]):
            continue                           # a resting exit works it (stops wait for the next snapshot)
        out["fv_signals"] += 1
        kind = f"EXIT_{str(plan['exit']).upper()}" if plan.get("exit") else "ENTRY"
        desc = (f"FV {kind} #{m['id']} {m['title'][9:60]}: {plan['yes_side']} YES x{plan['qty']:g} "
                f"@ {plan['limit']} (fair {plan['fair']}" + (f", edge {plan['edge']}" if 'edge' in plan else "") + ")"
                + (f" [high-EV reserve, {roi(plan):.1%} expected]" if plan.get("reserve") else ""))
        if kill_switch_engaged():
            log.info("kill switch engaged; not sending %s", desc)
            out["fv_skipped"] += 1
            continue
        if a.mode == "signal":
            print(desc)
            continue
        if a.mode == "confirm" and input(f"{desc} {'LIVE' if live else 'DRY RUN'}? [y/N] ").strip().lower() != "y":
            out["fv_skipped"] += 1
            continue
        if touched is not None:
            touched.add(m["id"])
        with critical():
            coid = f"fv:{uuid.uuid4().hex[:12]}"
            try:
                # Account-wide holding from the minute snapshot (kept current after each fill),
                # so the engine closes before opening without an extra request per order.
                if not live:
                    holding = 0.0
                elif holdings is not None:
                    holding = holdings.get(m["id"], 0.0)
                else:
                    holding = fair_value.net_holding(cli, m["id"])
                resp = place_tracked(cli, "fv", m["id"], book.exchange_id, plan["yes_side"], plan["limit"],
                                     plan["qty"], live, coid, holdings=holding)
            except Exception as e:
                log.error("fv order failed on #%s: %s", m["id"], e)
                out["fv_skipped"] += 1
                continue
            fq, fp, oids = _fill_of(resp, plan["qty"], plan["limit"])
            if fq < plan["qty"]:
                for oid in oids:
                    cli.cancel(oid, dry_run=not live)
            if live and fq > 0:
                ledger.record(m["id"], plan["yes_side"], fq, plan["limit"])
                if holdings is not None:
                    holdings[m["id"]] = holdings.get(m["id"], 0.0) + (fq if plan["yes_side"] == "BUY" else -fq)
                if risk is not None:
                    cost = fq * (plan["limit"] if plan["yes_side"] == "BUY" else 1 - plan["limit"])
                    risk.gross += -cost if plan.get("exit") else cost
            status = "UNKNOWN" if resp.get("_unknown") else ("DONE" if fq >= plan["qty"] else "PARTIAL" if fq else "MISS")
            res = {"status": status, "strategy": "fv", "kind": kind, "market": m["id"], "plan": plan,
                   "filled": fq, "client_order_id": coid, "dryRun": bool(resp.get("dryRun"))}
            EXEC_LOG.parent.mkdir(exist_ok=True)
            with EXEC_LOG.open("a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": a.mode, "live": live,
                                    "race": m["title"], "dir": f"FV_{kind}", "qty": plan["qty"],
                                    "pnl": plan.get("expected_pnl", 0.0), "capital": plan.get("capital", 0.0),
                                    "result": res}) + "\n")
            out["fv_orders"] += 1
            log.info("%s -> %s filled %g", desc, status, fq)
            if live and status == "UNKNOWN":
                # In-doubt single-market order: the intent stays UNKNOWN and holdings_check books
                # or clears it from SIG's holdings at the next check (kill switch if unexplained).
                log.warning("fv order on #%s has an unknown outcome; holdings check will reconcile", m["id"])
    return out


def run_arb_exit(cli: Client, snap: Snapshot, race: str, arb_pos: dict, acct: dict, a, live: bool,
                 risk=None) -> Optional[dict]:
    """Unwind a race's complete arb sets before settlement when that is profitable and banks
    at least --arb-exit-share of the settlement profit (arb_exits.py). Auto mode only."""
    ids = list(group_markets(snap.markets).get(race, {}).values())
    if not ids or any(m not in snap.levels for m in ids):
        return None
    if risk is not None and time.time() - risk.last_fire.get(("exit", race), 0) < a.cooldown:
        return None
    books = {m: Book.from_levels(m, snap.levels[m]) for m in ids}
    plan = arb_exits.unwind_plan(race, ids, books, arb_pos, acct, min_share=a.arb_exit_share,
                                 depth_ratio=a.arb_depth_ratio)
    if not plan:
        return None
    if risk is not None:
        risk.last_fire[("exit", race)] = time.time()
    desc = (f"ARB EXIT {race}: sell {plan['sets']} sets x{plan['qty']:g}, profit {plan['profit']:.2f} "
            f"(settlement {plan['settle_profit']:.2f}), frees {plan['cost']:.0f}")
    if kill_switch_engaged() or a.mode != "auto":
        log.info("not sending %s (%s)", desc, "kill switch" if kill_switch_engaged() else a.mode)
        return None
    r = arb_exits.as_result(plan)
    with critical():
        res = execute_parallel(cli, r, live=live, chase_ticks=a.chase_ticks, fee=a.fee,
                               floor=arb_exits.repair_floor(plan))
        res.update(unwind=True, settle_pnl=plan["settle_profit"], released=plan["cost"])
        for leg in res.get("legs", []):
            if live and leg.get("filled") and not leg.get("dryRun") and not leg.get("repair"):
                acct[leg["market"]] = acct.get(leg["market"], 0.0) + (
                    leg["filled"] if leg["side"] == "BUY" else -leg["filled"])
        journal(r, res, a.mode, live)
    if live and risk is not None and res.get("qty"):
        risk.gross -= plan["cost"] * res["qty"] / plan["qty"]
    log.info("%s -> %s", desc, json.dumps(res))
    return res


def journal_single(strategy: str, market_id: int, yes_side: str, qty: float, price: float, kind: str,
                   live: bool, **extra) -> None:
    EXEC_LOG.parent.mkdir(exist_ok=True)
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "auto", "live": live,
                            "race": f"#{market_id}", "dir": f"{strategy.upper()}_{kind}", "qty": qty,
                            "pnl": 0.0, "capital": 0.0,
                            "result": {"status": kind, "strategy": strategy, "market": market_id,
                                       "yes_side": yes_side, "price": price, **extra}}) + "\n")


def run_conviction(cli: Client, snap: Snapshot, targets: dict, fair_of, ledger, a, live: bool, risk=None,
                   holdings: dict = None, touched: set = None, exits_claim=None, news_ok=None) -> dict:
    """Enter / stop out conviction bets for the targets in one freshly read race."""
    out = {"cv_signals": 0, "cv_orders": 0}
    for m in snap.markets:
        if m["id"] not in targets:
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        gross_left = min(a.cv_max_gross - ledger.gross(), account_room(a, risk))
        fair = fair_of(m["id"])

        def replan(room):
            return conviction.plan(book, fair, ledger, min_edge=a.cv_min_edge, max_bet=a.cv_max_bet,
                                   gross_left=room, stop=a.cv_stop, max_slip=getattr(a, "fv_max_slip", 0.03),
                                   exit_band=getattr(a, "cv_exit", None), take=getattr(a, "cv_take", None))
        plan = reserve_plan(replan(gross_left), replan, a, gross_left, account_room(a, risk, high_ev=True))
        if not plan:
            continue
        if plan.get("exit") and exits_claim is not None and not exits_claim(m["id"]):
            continue                           # a resting exit works it (stops wait for the next snapshot)
        if not plan.get("exit") and not getattr(a, "cv_entries", True):
            continue                           # exits only: held bets close at break-even or better
        if not plan.get("exit") and news_ok is not None and not news_ok(m["id"], plan["yes_side"]):
            continue                           # fresh news: only with the reference move (news_watch)
        out["cv_signals"] += 1
        kind = f"EXIT_{plan['exit'].upper()}" if plan.get("exit") else "ENTRY"
        desc = (f"CV {kind} #{m['id']} {m['title'][9:60]}: {plan['yes_side']} YES x{plan['qty']:g} @ {plan['limit']}"
                f" (fair {plan['fair']}" + (f", edge {plan['edge']}, capital {plan['capital']})" if "edge" in plan else ")")
                + (f" [high-EV reserve, {roi(plan):.1%} expected]" if plan.get("reserve") else ""))
        if kill_switch_engaged() or a.mode != "auto":
            log.info("not sending %s (%s)", desc, "kill switch" if kill_switch_engaged() else a.mode)
            continue
        if touched is not None:
            touched.add(m["id"])
        with critical():
            coid = f"cv:{uuid.uuid4().hex[:12]}"
            holding = holdings.get(m["id"], 0.0) if (live and holdings is not None) else 0.0
            try:
                resp = place_tracked(cli, "cv", m["id"], book.exchange_id, plan["yes_side"], plan["limit"],
                                     plan["qty"], live, coid, holdings=holding)
            except Exception as e:
                log.error("cv order failed on #%s: %s", m["id"], e)
                continue
            fq, fp, oids = _fill_of(resp, plan["qty"], plan["limit"])
            if fq < plan["qty"]:
                for oid in oids:
                    cli.cancel(oid, dry_run=not live)
            if live and fq > 0:
                ledger.record(m["id"], plan["yes_side"], fq, plan["limit"])
                if holdings is not None:
                    holdings[m["id"]] = holdings.get(m["id"], 0.0) + (fq if plan["yes_side"] == "BUY" else -fq)
                if risk is not None:
                    cost = fq * (plan["limit"] if plan["yes_side"] == "BUY" else 1 - plan["limit"])
                    risk.gross += -cost if plan.get("exit") else cost
            status = "UNKNOWN" if resp.get("_unknown") else ("DONE" if fq >= plan["qty"] else "PARTIAL" if fq else "MISS")
            journal_single("cv", m["id"], plan["yes_side"], fq, plan["limit"], kind, live, plan=plan, status_=status,
                           client_order_id=coid)
            out["cv_orders"] += 1
            log.info("%s -> %s filled %g", desc, status, fq)
            if live and status == "UNKNOWN":
                log.warning("cv order on #%s has an unknown outcome; holdings check will reconcile", m["id"])
    return out


def run_leadlag(cli: Client, snap: Snapshot, feed, ledger, a, live: bool, risk=None, holdings: dict = None,
                skip: set = frozenset(), touched: set = None) -> dict:
    """Lead-lag scalps for one freshly read race: exits first, then entries on reference moves."""
    out = {"ll_signals": 0, "ll_orders": 0}
    for m in snap.markets:
        if m["id"] in skip:
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        gross_left = min(a.ll_max_gross - ledger.gross(),
                         (a.max_gross - risk.gross) if risk is not None else float("inf"))
        plan = scalper.leadlag_plan(book, feed.mid(m["id"]), feed.move(m["id"], a.ll_lookback), ledger,
                                    edge=a.ll_edge, move_min=a.ll_move, unit=a.ll_unit, max_market=a.ll_max_market,
                                    gross_left=gross_left, tp=a.ll_tp, stop=a.ll_stop, max_hold_s=a.ll_max_hold)
        if not plan:
            continue
        out["ll_signals"] += 1
        kind = "EXIT_" + plan["exit"].upper() if plan.get("exit") else "ENTRY"
        desc = (f"LL {kind} #{m['id']} {m['title'][9:60]}: {plan['yes_side']} YES x{plan['qty']:g} @ {plan['limit']}"
                + (f" (poly {plan['fair']}, move {plan['move']:+})" if "fair" in plan else f" (entry {plan['entry']})"))
        if kill_switch_engaged() or a.mode != "auto":
            log.info("not sending %s (%s)", desc, "kill switch" if kill_switch_engaged() else a.mode)
            continue
        if touched is not None:
            touched.add(m["id"])
        with critical():
            coid = f"ll:{uuid.uuid4().hex[:12]}"
            holding = holdings.get(m["id"], 0.0) if (live and holdings is not None) else 0.0
            try:
                resp = place_tracked(cli, "ll", m["id"], book.exchange_id, plan["yes_side"], plan["limit"],
                                     plan["qty"], live, coid, holdings=holding)
            except Exception as e:
                log.error("ll order failed on #%s: %s", m["id"], e)
                continue
            fq, fp, oids = _fill_of(resp, plan["qty"], plan["limit"])
            if fq < plan["qty"]:
                for oid in oids:
                    cli.cancel(oid, dry_run=not live)
            if live and fq > 0:
                before = ledger.position(m["id"])
                ledger.record(m["id"], plan["yes_side"], fq, plan["limit"])
                if holdings is not None:
                    holdings[m["id"]] = holdings.get(m["id"], 0.0) + (fq if plan["yes_side"] == "BUY" else -fq)
                if risk is not None:
                    cost = fq * (plan["limit"] if plan["yes_side"] == "BUY" else 1 - plan["limit"])
                    risk.gross += -cost if plan.get("exit") else cost
            status = "UNKNOWN" if resp.get("_unknown") else ("DONE" if fq >= plan["qty"] else "PARTIAL" if fq else "MISS")
            journal_single("ll", m["id"], plan["yes_side"], fq, plan["limit"], kind, live, plan=plan, status_=status,
                           client_order_id=coid)
            out["ll_orders"] += 1
            log.info("%s -> %s filled %g", desc, status, fq)
            if live and status == "UNKNOWN":
                log.warning("ll order on #%s has an unknown outcome; holdings check will reconcile", m["id"])
    return out


def run_market_maker(mm, snap: Snapshot, feed, refs, ledgers: dict, holdings: dict, a,
                     skip: set = frozenset(), exits_markets: frozenset = frozenset(),
                     news_held: frozenset = frozenset()) -> None:
    """Queue requotes for one freshly read race; the MM worker thread sends the orders."""
    for m in snap.markets:
        if m["id"] in skip or (m["id"] in exits_markets and m["id"] not in mm.active_snapshot):
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        # MM keeps its own inventory books, so it may quote markets fair value holds; it stays
        # out of lead-lag and conviction markets, whose exits must not compete with quotes.
        other = ledgers["ll"].position(m["id"]) + ledgers["cv"].position(m["id"])
        fair = feed.mid(m["id"]) if feed is not None else None
        if fair is None and refs is not None:
            f_ = refs.fair(m["id"])
            fair = f_["fair"] if f_ else None
        # the self-test needs a market nobody holds with a real bid well above 0.01
        test_ok = not other and not holdings.get(m["id"]) and bool(book.bids) and book.bids[0][0] >= 0.05
        if mm.tested and not mm.eligible(m["id"], other, fair):
            continue
        if not mm.tested and not test_ok:
            continue
        mv = feed.move(m["id"], 120) if feed is not None else None
        moving = (mv is not None and abs(mv) >= 0.01) or m["id"] in news_held   # fresh news: pull quotes
        mm.submit(book, fair, moving, holdings.get(m["id"], 0.0), test_ok)


_mm_selftest_done = False


def cancel_all_open_orders(cli: Client) -> int:
    """Cancel every resting order on the account. The bot never means to keep orders
    across a restart, so this runs at startup (crash leftovers) and at shutdown."""
    n = 0
    for o in cli.portfolio().get("openOrders", []):
        try:
            cli.cancel(o["id"], dry_run=False)
            n += 1
        except Exception as e:
            log.warning("cancel failed for order %s on #%s: %s", o.get("id"), o.get("marketId"), e)
    return n


# ---------------------------------------------------------- limits / status
def apply_limits(a, limits: dict):
    """Tighten CLI caps to config/risk_limits.json; the file always wins when stricter."""
    a.max_gross = min(a.max_gross, limits["venue_exposure"].get("sig", a.max_gross))
    a.max_per_race = min(a.max_per_race, limits["per_trade_capital"], limits["event_exposure"])
    a.min_edge = max(a.min_edge, limits["min_net_edge"])
    res = limits.get("high_ev_reserve") or {}
    a.reserve = float(res.get("capital", 0.0))
    a.reserve_min_roi = float(res.get("min_roi", float("inf")))
    return a


def live_blockers(a, limits: dict, kill_switch: pathlib.Path = None, cli: Client = None) -> list:
    """Why orders would be dry-run right now (empty list = live orders allowed)."""
    out = []
    if a.mode == "signal":
        out.append("signal mode never sends orders")
    if not a.live:
        out.append("--live not set")
    if not sig_client.PLACE_PAYLOAD_CONFIRMED:
        out.append("PLACE_PAYLOAD_CONFIRMED is False")
    if a.mode == "auto" and limits.get("manual_approval", True):
        out.append("risk_limits.json manual_approval is true (auto needs false)")
    if kill_switch_engaged(kill_switch):
        out.append("kill switch engaged")
    if cli is not None:
        if not (cli.access_token and cli.profile_id):
            out.append("no SIG session token in SIG_COOKIE (sb-*-auth-token)")
        else:
            left = cli.token_seconds_left()
            if left is not None and left < TOKEN_MARGIN_S:
                out.append("SIG access token expires in under 5 min; copy a fresh cookie and restart")
    return out


def write_books(path: pathlib.Path, cache: dict) -> None:
    """cache: market_id -> (market, levels, observed_at). The snapshot ts is the OLDEST
    observation, so consumers never treat a rotated-in stale book as fresh."""
    if not cache:
        return
    rows = sorted(cache.items())
    payload = {"ts": min(obs for _, (_, _, obs) in rows),
               "markets": [m for _, (m, _, _) in rows],
               "levels": {str(mid): lv for mid, (_, lv, _) in rows},
               "observed_at": {str(mid): obs for mid, (_, _, obs) in rows}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    replace_file(tmp, path)


def write_status(path: pathlib.Path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "pid": os.getpid(), **fields}, default=str))
    replace_file(tmp, path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=["signal", "confirm", "auto"], default="signal")
    ap.add_argument("--live", action="store_true", help="actually send orders (confirm/auto)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--min-edge", type=float, default=0.005)
    ap.add_argument("--min-edge-3leg", type=float, default=0.01)
    ap.add_argument("--min-pnl", type=float, default=5.0)
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--budget", type=float, default=None, help="capital per signal (default live balance)")
    ap.add_argument("--max-per-race", type=float, default=10_000)
    ap.add_argument("--max-gross", type=float, default=float("inf"),
                    help="overall cap; config/risk_limits.json venue_exposure.sig applies (CLI can only tighten)")
    ap.add_argument("--max-qty", type=float, default=None)
    ap.add_argument("--cooldown", type=float, default=30, help="sec before re-firing the same race")
    ap.add_argument("--chase-ticks", type=int, default=2)
    ap.add_argument("--arb-depth-ratio", type=float, default=2.0,
                    help="arb legs need this multiple of our size resting at or better than the limit (0 = off)")
    ap.add_argument("--arb-exit-share", type=float, default=0.5,
                    help="unwind held arbs early once that banks this share of the settlement profit")
    ap.add_argument("--no-arb-exits", dest="arb_exits", action="store_false",
                    help="hold arbs to settlement")
    ap.add_argument("--near", type=int, default=0)
    ap.add_argument("--limits", type=pathlib.Path, default=RISK_LIMITS)
    ap.add_argument("--hot-band", type=float, default=0.005,
                    help="re-read every tick races whose top-of-book edge is within this of an arb")
    ap.add_argument("--sweep-races", type=int, default=8, help="cold races re-read per tick (rotating)")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel book requests (SIG returns 429 above its limit)")
    ap.add_argument("--refresh-universe", type=float, default=1800, help="sec between market-list refreshes")
    ap.add_argument("--pre-quote", action="store_true", help="call the quote endpoint before each leg (slower)")
    ap.add_argument("--sequential", action="store_true",
                    help="send legs one after another (thinnest first) instead of all at once")
    ap.add_argument("--balance-every", type=float, default=60, help="sec between balance refreshes")
    ap.add_argument("--strategy", default="arb",
                    help="comma list: arb (complete-set arbs), fv (trade toward Kalshi/Polymarket fair value)")
    ap.add_argument("--fv-threshold", type=float, default=0.03, help="fv: enter when SIG is this far past fair")
    ap.add_argument("--fv-exit", type=float, default=0.01, help="fv: close once SIG is within this of fair")
    ap.add_argument("--fv-unit", type=float, default=500,
                    help="fv: capital at an edge equal to --fv-threshold; scales linearly with the gap")
    ap.add_argument("--fv-max-market", type=float, default=2000, help="fv: capital cap per market")
    ap.add_argument("--fv-max-gross", type=float, default=45000,
                    help="fv: cap on fair-value capital (leaves room for ll/mm under the venue cap)")
    ap.add_argument("--fv-tp", type=float, default=0.01, help="fv: take profit vs entry")
    ap.add_argument("--fv-min-fair", type=float, default=0.10,
                    help="fv: new entries only where fair is at or above (long shots sit red on SIG's marks)")
    ap.add_argument("--fv-max-fair", type=float, default=0.90, help="fv: new entries only where fair is at or below")
    ap.add_argument("--fv-stop", type=float, default=0.05,
                    help="fv: exit if the reference fair moves this far against the entry")
    ap.add_argument("--fv-max-slip", type=float, default=0.03, help="fv: stop/time exits only within this of fair")
    ap.add_argument("--fv-max-hold", type=float, default=0, help="fv: time stop in sec (0 = hold to settlement)")
    ap.add_argument("--ref-interval", type=float, default=120, help="fv: sec between reference refreshes")
    ap.add_argument("--poly-poll", type=float, default=3.0, help="ll/mm: sec between Polymarket book polls")
    ap.add_argument("--ll-move", type=float, default=0.02, help="ll: Polymarket mid move that triggers")
    ap.add_argument("--ll-lookback", type=float, default=300, help="ll: window for the move, sec")
    ap.add_argument("--ll-edge", type=float, default=0.015, help="ll: SIG must be this far behind the new mid")
    ap.add_argument("--ll-unit", type=float, default=500, help="ll: capital at a move of --ll-move (scales)")
    ap.add_argument("--ll-max-market", type=float, default=1500)
    ap.add_argument("--ll-max-gross", type=float, default=10000)
    ap.add_argument("--ll-tp", type=float, default=0.02, help="ll: take profit vs entry")
    ap.add_argument("--ll-stop", type=float, default=0.03, help="ll: stop loss vs entry")
    ap.add_argument("--ll-max-hold", type=float, default=2700, help="ll: time stop, sec")
    ap.add_argument("--mm-edge", type=float, default=0.015, help="mm: quote at least this far from fair")
    ap.add_argument("--mm-size", type=float, default=600, help="mm: shares per quote")
    ap.add_argument("--mm-max-inventory", type=float, default=600, help="mm: shares per market")
    ap.add_argument("--mm-max-capital", type=float, default=15000,
                    help="mm: total inventory capital (its own budget); at the cap only exits are quoted")
    ap.add_argument("--mm-take", type=float, default=0.01, help="mm: exit profit vs entry")
    ap.add_argument("--mm-max-hold", type=float, default=600, help="mm: after this, exit at break-even")
    ap.add_argument("--mm-flatten", type=float, default=1800,
                    help="mm: after this, flatten through the book at up to --mm-max-loss")
    ap.add_argument("--mm-max-loss", type=float, default=0.01, help="mm: most given up per share to flatten")
    ap.add_argument("--mm-min-spread", type=float, default=0.0,
                    help="mm: only quote SIG spreads this wide (0: every spread; quotes still sit --mm-edge from fair)")
    ap.add_argument("--mm-max-markets", type=int, default=100)
    ap.add_argument("--mm-requote", type=float, default=60, help="mm: min sec between requotes of one market")
    ap.add_argument("--no-mm-claw", dest="mm_claw", action="store_false",
                    help="do not move capital freed by fv/cv/arb exits to market making")
    ap.add_argument("--mm-claw-max", type=float, default=50000, help="mm: budget ceiling with clawed-back capital")
    ap.add_argument("--cv-max-bets", type=int, default=6, help="cv: concurrent conviction bets")
    ap.add_argument("--cv-max-bet", type=float, default=4000, help="cv: capital per bet")
    ap.add_argument("--cv-max-gross", type=float, default=24000, help="cv: total capital")
    ap.add_argument("--cv-min-edge", type=float, default=0.02, help="cv: SIG vs fair gap to enter")
    ap.add_argument("--cv-min-fair", type=float, default=0.15, help="cv: only races with fair at or above")
    ap.add_argument("--cv-max-fair", type=float, default=0.85, help="cv: only races with fair at or below")
    ap.add_argument("--cv-stop", type=float, default=0.10, help="cv: exit if the consensus moves this far against")
    ap.add_argument("--cv-refresh", type=float, default=120, help="cv: sec between target re-ranking")
    ap.add_argument("--cv-entries", dest="cv_entries", action="store_true",
                    help="cv: allow new conviction bets and top-ups (off: held bets only exit)")
    ap.add_argument("--cv-take", type=float, default=0.005,
                    help="cv: exit once SIG pays this much over the entry (break-even or better)")
    ap.add_argument("--cv-exit", type=float, default=0.005,
                    help="cv: take profit once SIG is within this of fair (never below the entry)")
    ap.add_argument("--no-resting-exits", dest="resting_exits", action="store_false",
                    help="do not rest take-profit orders for fv/cv positions")
    ap.add_argument("--exit-requote", type=float, default=60, help="resting exits: min sec between requotes")
    ap.add_argument("--no-news", dest="news", action="store_false", help="do not watch SIG's news feed")
    ap.add_argument("--news-poll", type=float, default=1200, help="news: sec between polls of one race's feed")
    ap.add_argument("--news-hold", type=float, default=900,
                    help="news: after a new headline, entries only follow the reference move for this long")
    ap.add_argument("--mm-min-price", type=float, default=0.10, help="mm: only quote fair values at or above")
    ap.add_argument("--mm-max-price", type=float, default=0.90, help="mm: only quote fair values at or below")
    ap.add_argument("--mm-poll", type=float, default=20, help="mm: sec between open-order/fill syncs")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    signal.signal(signal.SIGINT, _on_stop_signal)
    signal.signal(signal.SIGTERM, _on_stop_signal)
    if hasattr(signal, "SIGBREAK"):          # Windows: the supervisor stops children with CTRL_BREAK
        signal.signal(signal.SIGBREAK, _on_stop_signal)

    limits = gates.load_limits(a.limits)     # malformed limits -> refuse to start
    apply_limits(a, limits)
    cli = Client(concurrency=a.concurrency)
    markets = fast_scan.load_markets(cli)
    exhaustive = load_exhaustive()
    scanner = fast_scan.TieredScanner(cli, markets, exhaustive, hot_band=a.hot_band,
                                      sweep_races=a.sweep_races, concurrency=a.concurrency)
    # one pass reads about 1.5 ticks' worth of SIG's request budget, most deserving races first
    scanner.books_per_tick = lambda: max(6, int(cli.pacer.rate * a.interval * 1.5))
    last_universe, full_sweep, scan = time.time(), True, {}
    risk = Risk(a)
    seen = set()
    counts = {"signals": 0, "executed": 0, "skipped": 0}
    last_exec = None
    log.info("mode=%s live=%s markets=%d max_gross=%g (+%g high-EV reserve at >= %g return) max_per_race=%g min_edge=%g",
             a.mode, a.live, len(markets), a.max_gross, a.reserve, a.reserve_min_roi, a.max_per_race, a.min_edge)

    strategies = {x.strip() for x in a.strategy.split(",") if x.strip()}
    if not strategies <= {"arb", "cv", "fv", "ll", "mm"}:
        raise SystemExit(f"unknown --strategy {a.strategy}")
    refs = None
    ledger = fair_value.Ledger()            # also needed to reconcile holdings when fv is off
    ll_ledger = fair_value.Ledger(HERE / "logs" / "ll_positions.json")
    mm_ledger = fair_value.Ledger(HERE / "logs" / "mm_positions.json")
    cv_ledger = fair_value.Ledger(HERE / "logs" / "cv_positions.json")
    ledgers = {"fv": ledger, "ll": ll_ledger, "mm": mm_ledger, "cv": cv_ledger}
    checker = holdings_check.HoldingsCheck(ledgers, lambda why: engage_kill_switch(why, actor="holdings_check"),
                                           intent_log=INTENT_LOG, exec_log=EXEC_LOG)
    acct = {}                                # market_id -> signed YES holding (minute snapshot)
    for attempt in range(1, 6):              # never trade before the sweep and the check succeed
        try:
            if a.live and sig_client.PLACE_PAYLOAD_CONFIRMED:
                n = cancel_all_open_orders(cli)
                if n:
                    log.warning("startup: cancelled %d resting order(s) left by a previous run", n)
                    time.sleep(2)
            port = cli.portfolio()
            acct = holdings_check.actual_holdings(port)
            rep = checker.check(port, cli.transactions, immediate=True)
            log.info("holdings check at start: %s", json.dumps(rep))
            break
        except Exception as e:
            log.error("startup sweep/check attempt %d failed: %s", attempt, e)
            time.sleep(10 * attempt)
    else:
        engage_kill_switch("startup could not sweep resting orders and reconcile holdings (SIG unreachable)")
    feed = mm = None
    if strategies & {"ll", "mm"}:
        feed = scalper.PolyFeed(poll_s=a.poly_poll)
        log.info("poly feed: %d YES tokens for %d SIG markets", feed.load_tokens(), len(feed.poly_by_sig))
        feed.poll()
        feed.start()
    if "mm" in strategies:
        mm = scalper.MarketMaker(cli, mm_ledger, edge=a.mm_edge, size=a.mm_size, max_inventory=a.mm_max_inventory,
                                 take=a.mm_take, max_hold_s=a.mm_max_hold, min_spread=a.mm_min_spread,
                                 max_markets=a.mm_max_markets, place=place_tracked,
                                 min_price=a.mm_min_price, max_price=a.mm_max_price, max_capital=a.mm_max_capital,
                                 flatten_s=a.mm_flatten, max_loss=a.mm_max_loss, requote_s=a.mm_requote)
        mm.enabled = False                   # until the live self-test passes (in the worker)
    a.claw = (Claw(HERE / "logs" / "claw.json", a.mm_max_capital, a.mm_claw_max)
              if getattr(a, "mm_claw", False) and mm is not None else None)
    worker_live = {"live": False}
    if strategies & {"fv", "mm", "cv"}:
        refs = fair_value.ReferencePrices()
        log.info("reference prices: %d approved mappings; first refresh...", len(refs.matches))
        refs.refresh()
        refs.start(a.ref_interval)
        a.fv_max_gross = min(a.fv_max_gross, limits["venue_exposure"].get("sig", a.fv_max_gross))
        a.fv_max_market = min(a.fv_max_market, limits["per_trade_capital"])
        a.fv_max_race = limits["event_exposure"]
    a.fv_max_race = getattr(a, "fv_max_race", limits["event_exposure"])
    a.fv_max_market = min(a.fv_max_market, limits["per_trade_capital"])
    a.ll_max_market = min(a.ll_max_market, limits["per_trade_capital"])
    def others_expected(exclude: str) -> dict:
        rows = {}
        for name, led in ledgers.items():
            if name == exclude:
                continue
            for k, row in led.rows.items():
                rows.setdefault(int(k), {"qty": 0.0})["qty"] += float(row.get("qty", 0))
        return holdings_check.expected_holdings(rows, EXEC_LOG)

    def mm_fill(f_):
        log.info("MM FILL #%s %s %g @ %s", f_["market_id"], f_["yes_side"], f_["qty"], f_["price"])
        journal_single("mm", f_["market_id"], f_["yes_side"], f_["qty"], f_["price"], "FILL", worker_live["live"])

    if mm is not None:
        mm.start_worker(lambda: others_expected("mm"), kill_switch_engaged, on_fill=mm_fill, poll_s=a.mm_poll)

    # Standing take-profit orders for fair-value and conviction positions (own worker thread).
    exits = None
    if getattr(a, "resting_exits", False) and strategies & {"fv", "cv"} and refs is not None:
        rules = {"fv": {"tp": a.fv_tp, "band": a.fv_exit, "stop": a.fv_stop},
                 "cv": {"band": a.cv_exit, "stop": a.cv_stop, "floor_at_entry": True, "take": a.cv_take}}
        exits = resting_exits.RestingExits(cli, {k: ledgers[k] for k in ("fv", "cv") if k in strategies},
                                           place_tracked, rules=rules, requote_s=a.exit_requote, intent_log=INTENT_LOG)
        exits.start_worker(kill_switch_engaged)
        if mm is not None:
            mm.skip_sync = lambda: exits.claimed

    # SIG's news feed as a trigger (rescan the race) and a guard (entries follow the reference move).
    nw = None
    if getattr(a, "news", False) and refs is not None:
        nw = news_watch.NewsWatch(cli, {r: sorted(legs.values()) for r, legs in scanner.groups.items()},
                                  poll_s=a.news_poll, hold_s=a.news_hold)

    def fair_of(m_):
        f_ = feed.mid(m_) if feed is not None else None
        if f_ is None and refs is not None:
            f_ = (refs.fair(m_) or {}).get("fair")
        return f_

    cv_targets, cv_ranked_at = {}, 0.0
    arb_pos = None                           # arb legs from the journal; reloaded after arb trades
    balance, balance_at = None, 0.0
    book_cache = {}
    live = False
    try:
        while True:
            t0 = time.time()
            if cli.reload_cookie():
                log.info("SIG cookie reloaded from .env; token valid %.0f min", (cli.token_seconds_left() or 0) / 60)
            left = cli.token_seconds_left()
            if cli.can_refresh and left is not None and left < REFRESH_AHEAD_S:
                try:
                    left = cli.refresh_session()
                    log.info("SIG session renewed; token valid %.0f min", (left or 0) / 60)
                except Exception as e:
                    log.error("SIG session renewal failed: %s", e)
                    if "already_used" in str(e) or "invalid_grant" in str(e) or "(400)" in str(e):
                        # The refresh token is dead (e.g. a browser on the same login renewed
                        # first). Retrying can never succeed: stop, and wait for a fresh cookie.
                        cli.can_refresh = False
                        log.error("SIG session revoked: run python go_live.py set-cookie "
                                  "(private window), then restart")
            blockers = live_blockers(a, limits, cli=cli)
            live = a.mode != "signal" and not blockers
            if a.mode != "signal" and a.live and blockers:
                log.warning("orders are DRY RUN: %s", "; ".join(blockers))
            error = None
            try:
                if time.time() - last_universe > a.refresh_universe:
                    scanner.set_markets(fast_scan.load_markets(cli, refresh=True))
                    last_universe = time.time()
                if time.time() - balance_at > a.balance_every and not scanner.paused_for():
                    if not a.budget:
                        fresh_balance = cli.balance()     # 0.0 is a real balance, not "unknown"
                        balance = balance if fresh_balance is None else fresh_balance
                    # One account snapshot a minute: the exposure cap counts every open position
                    # (arb and fair value, including ones from earlier runs), fair-value orders
                    # read holdings from it, and holdings are reconciled against bot records.
                    try:
                        fetched_at = time.time()
                        port = cli.portfolio()
                        risk.gross = holdings_check.deployed(port)
                        if getattr(a, "claw", None) is not None:
                            a.claw.update(risk.gross, mm_ledger.gross())
                        if mm is not None:
                            # MM inventory also stays under the account cap, so its fills never eat
                            # the cash buffer arb legs need; at the cap it only quotes exits
                            budget = a.claw.mm_budget() if getattr(a, "claw", None) is not None else a.mm_max_capital
                            room = a.max_gross - risk.gross
                            mm.max_capital = min(budget, mm_ledger.gross() + room) if room > 0 else 0.0
                        acct = holdings_check.actual_holdings(port)
                        arb_pos = None
                        if exits is not None:          # book resting-exit fills before MM and reconciliation
                            for f_ in exits.sync(port, lambda: holdings_check.expected_holdings(checker.rows(), EXEC_LOG),
                                                 fetched_at):
                                log.info("EXIT FILL #%s %s %s %g @ %s (resting)", f_["market_id"], f_["strategy"],
                                         f_["yes_side"], f_["qty"], f_["price"])
                                journal_single(f_["strategy"], f_["market_id"], f_["yes_side"], f_["qty"], f_["price"],
                                               "EXIT_RESTING", live)
                        if mm is not None:
                            with mm.lock:              # fills and reconciliation see one snapshot
                                mm.sync_with(port)
                                rep = checker.check(port, cli.transactions)
                        else:
                            rep = checker.check(port, cli.transactions)
                        if not rep["ok"]:
                            log.warning("holdings check: %s", json.dumps(rep))
                        try:                           # dashboard positions & P&L, no extra SIG calls
                            positions.write(positions.build(port, ledgers, fair_of,
                                                            titles={mk['id']: mk['title'] for mk in markets}))
                        except Exception as e:
                            log.warning("positions report failed: %s", e)
                    except Exception as e:
                        log.warning("could not read account snapshot: %s", e)
                    balance_at = time.time()
                budget = a.budget or balance or 100_000
                scan = {}
                if mm is not None:
                    mm.live = worker_live["live"] = live     # the worker pulls quotes when not live
                if exits is not None:
                    exits.live = live                        # the worker pulls its orders when not live
                exits_claim = exits.claim_taker if exits is not None else None
                if feed is not None:
                    hot_mkts = set(feed.movers(min(a.ll_move, 0.01), a.ll_lookback))
                    hot_mkts |= {int(k) for k, r in ll_ledger.rows.items() if r.get("qty")}
                    if mm is not None:
                        hot_mkts |= mm.active_snapshot
                    scanner.boost = {r for r in map(scanner.race_of, hot_mkts) if r}
                if "cv" in strategies and time.time() - cv_ranked_at > a.cv_refresh and book_cache:
                    cv_targets = conviction.pick_targets(
                        {mid_: Book.from_levels(mid_, lv) for mid_, (_, lv, _) in book_cache.items()},
                        {mid_: mk["title"] for mid_, (mk, _, _) in book_cache.items()}, fair_of, cv_ledger,
                        min_edge=a.cv_min_edge, min_fair=a.cv_min_fair, max_fair=a.cv_max_fair,
                        max_bets=a.cv_max_bets)
                    cv_ranked_at = time.time()
                    if feed is not None or True:
                        scanner.boost |= {r for r in map(scanner.race_of, cv_targets) if r}
                if "arb" in strategies:              # held arb sets are read often, for early exits
                    if arb_pos is None:
                        arb_pos = positions.arb_costs(EXEC_LOG)
                    scanner.priority = {r for r, legs in scanner.groups.items()
                                        if arb_exits.complete_sets(list(legs.values()), arb_pos, acct)}
                if nw is not None:
                    if not scanner.paused_for():
                        nw.step()                            # one news request per pass, between book scans
                    watch_mkts = set(acct) | set(cv_targets) | (set(mm.active_snapshot) if mm is not None else set())
                    nw.watch({r for r in map(scanner.race_of, watch_mkts) if r})
                    for ev in nw.drain(fair_of):
                        log.info("NEWS %s: %s", ev["race"], " | ".join(str(h["title"])[:90] for h in ev["headlines"][:3]))
                    scanner.boost = set(getattr(scanner, "boost", set())) | nw.boosted_races()
                news_ok = (lambda m_, s_: nw.entry_ok(m_, s_, fair_of)) if nw is not None else None
                news_held = nw.held_markets() if nw is not None else frozenset()
                # Each race is evaluated, and traded, as soon as all its books arrive.
                for race, snap in scanner.stream(full=full_sweep, stats=scan):
                    for m in snap.markets:
                        book_cache[m["id"]] = (m, snap.levels[m["id"]], snap.ts)
                    # Arbs first: their profit is locked, and any other strategy trading first
                    # would consume the liquidity the arb's depth walk counted on (Nebraska, 1 Oct).
                    race_traded = False
                    if "arb" in strategies and getattr(a, "arb_exits", False):
                        if arb_pos is None:
                            arb_pos = positions.arb_costs(EXEC_LOG)
                        res = run_arb_exit(cli, snap, race, arb_pos, acct, a, live, risk)
                        if res is not None:
                            arb_pos = None
                            counts["arb_exits"] = counts.get("arb_exits", 0) + 1
                            last_exec = {"race": race, "status": res["status"], "live": live, "unwind": True}
                            if live and res["status"] in HALT_STATUSES:
                                engage_kill_switch(f"{res['status']} unwinding {race} (run {res.get('run_id')}): "
                                                   f"{res.get('action') or 'operator review required'}")
                                log.error("kill switch engaged after %s unwinding %s", res["status"], race)
                            continue                 # books are stale now; entries wait for the next read
                    sigs, near, titles = (generate(snap, a, exhaustive, budget) if "arb" in strategies
                                          else ([], [], {}))
                    fresh = [s for s in sigs if sig_key(s) not in seen]
                    seen = {k for k in seen if k[0] != race} | {sig_key(s) for s in sigs}
                    counts["signals"] += len(fresh)
                    if fresh:
                        print(render(snap, fresh, near, titles, budget))
                        log_csv(snap, fresh)
                    for r in fresh:
                        if a.mode == "signal":
                            continue
                        sized = depth_limited(r, snap, a, budget, a.arb_depth_ratio)
                        if sized is None:
                            log.info("skip %s (legs too thin for %gx depth)", r.race, a.arb_depth_ratio)
                            counts["skipped"] += 1
                            continue
                        if sized.qty < r.qty:
                            log.info("%s: size %g -> %g for %gx leg depth", r.race, r.qty, sized.qty, a.arb_depth_ratio)
                        r = sized
                        if live and not a.budget and balance is not None and r.capital > balance:
                            log.info("skip %s (needs %.0f, cash left %.0f)", r.race, r.capital, balance)
                            counts["skipped"] += 1
                            continue
                        if kill_switch_engaged():
                            log.warning("kill switch engaged; not executing %s", r.race)
                            counts["skipped"] += 1
                            continue
                        ok, why = risk.ok(r)
                        if not ok:
                            log.info("skip %s (%s)", r.race, why)
                            counts["skipped"] += 1
                            continue
                        if a.mode == "confirm":
                            print("\n".join(tickets(r, titles)))
                            if input(f"Execute {r.race} {r.direction} x{r.qty:g} "
                                     f"(pnl {r.pnl:.2f}) {'LIVE' if live else 'DRY RUN'}? [y/N] ").strip().lower() != "y":
                                continue
                        race_traded = True
                        with critical():
                            if a.sequential:
                                res = execute(cli, r, live=live, chase_ticks=a.chase_ticks, fee=a.fee,
                                              pre_quote=a.pre_quote)
                            else:
                                res = execute_parallel(cli, r, live=live, chase_ticks=a.chase_ticks, fee=a.fee)
                            risk.book(r, res)
                            arb_pos = None
                            for leg in res.get("legs", []):
                                if live and leg.get("filled") and not leg.get("dryRun") and not leg.get("repair"):
                                    acct[leg["market"]] = acct.get(leg["market"], 0.0) + (
                                        leg["filled"] if leg["side"] == "BUY" else -leg["filled"])
                            journal(r, res, a.mode, live)
                        if live and not a.budget and balance:
                            # balance is read once a minute: spend it down here, or the next race this
                            # pass is sized on cash already gone and SIG rejects one of its legs
                            balance = max(0.0, balance - capital_spent(res))
                            budget = balance or budget
                        counts["executed"] += 1
                        last_exec = {"race": r.race, "status": res["status"], "live": live,
                                     "book_age_s": round(time.time() - t0, 1)}
                        log.info("EXEC %s -> %s", r.race, json.dumps(res))
                        if live and res["status"] in HALT_STATUSES:
                            engage_kill_switch(f"{res['status']} on {r.race} (run {res.get('run_id')}): "
                                               f"{res.get('action') or 'operator review required'}")
                            log.error("kill switch engaged after %s on %s", res["status"], r.race)
                    # Single-market strategies only on books no order has touched this pass.
                    if race_traded:
                        continue
                    touched = set()
                    mm_busy = set(mm.active_snapshot) if mm is not None else set()
                    exits_mkts = exits.markets_snapshot if exits is not None else frozenset()
                    if "cv" in strategies:
                        for k, v in run_conviction(cli, snap, cv_targets, fair_of, cv_ledger, a, live, risk, acct,
                                                   touched, exits_claim=exits_claim, news_ok=news_ok).items():
                            counts[k] = counts.get(k, 0) + v
                    if "ll" in strategies:
                        for k, v in run_leadlag(cli, snap, feed, ll_ledger, a, live, risk, acct,
                                                skip=mm_busy | exits_mkts | {int(k) for k, r in ledger.rows.items() if r.get("qty")},
                                                touched=touched).items():
                            counts[k] = counts.get(k, 0) + v
                    if "fv" in strategies:
                        fv_counts = run_fair_value(cli, snap, refs, ledger, a, live, risk, acct,
                                                   skip=touched, touched=touched,
                                                   skip_entries=mm_busy | set(cv_targets), exits_claim=exits_claim,
                                                   news_ok=news_ok)
                        for k, v in fv_counts.items():
                            counts[k] = counts.get(k, 0) + v
                    if mm is not None and live and not kill_switch_engaged():
                        # MM stays out of markets resting exits work or may soon work (fv/cv positions)
                        mm_avoid = exits_mkts | ({mk["id"] for mk in snap.markets
                                                  if ledger.position(mk["id"]) or cv_ledger.position(mk["id"])}
                                                 if exits is not None else set())
                        run_market_maker(mm, snap, feed, refs, ledgers, acct, a, skip=touched,
                                         exits_markets=frozenset(mm_avoid), news_held=news_held)
                    if exits is not None and live:
                        for mk in snap.markets:
                            m_ = mk["id"]
                            fvp, cvp = ledger.position(m_), cv_ledger.position(m_)
                            if not (fvp or cvp or m_ in exits_mkts):
                                continue
                            busy = (m_ in mm_busy or bool(mm_ledger.position(m_)) or bool(ll_ledger.position(m_))
                                    or bool(fvp and cvp))
                            exits.submit(Book.from_levels(m_, snap.levels[m_]),
                                         {"fv": (refs.fair(m_) or {}).get("fair"), "cv": fair_of(m_)},
                                         acct.get(m_, 0.0), busy)
                scan.pop("complete_races", None)
                write_books(BOT_BOOKS, book_cache)
                if scan.get("rate_limited"):
                    log.warning("SIG rate limit (429): pausing scans %.0fs", scan["paused_s"])
                elif scan.get("complete"):
                    full_sweep = False
            except KeyboardInterrupt:
                raise
            except Exception as e:
                error = str(e)[:300]
                log.exception("tick failed: %s", e)
            write_status(BOT_STATUS, mode=a.mode, live_requested=a.live, live=live,
                         live_blockers=blockers, kill_switch=kill_switch_engaged(),
                         payload_confirmed=sig_client.PLACE_PAYLOAD_CONFIRMED,
                         interval=a.interval, gross=round(risk.gross, 2), max_gross=a.max_gross,
                         reserve=({"capital": a.reserve, "min_roi": a.reserve_min_roi,
                                   "used": round(max(0.0, risk.gross - a.max_gross), 2)} if a.reserve else None),
                         max_per_race=a.max_per_race, min_edge=a.min_edge, counts=counts,
                         last_exec=last_exec, error=error, token_seconds_left=cli.token_seconds_left(),
                         scan=scan, pacer=cli.pacer.snapshot(), strategies=sorted(strategies), holdings_check=checker.last,
                     cv={"gross": round(cv_ledger.gross(), 2), "bets": sum(1 for r in cv_ledger.rows.values() if r.get("qty")),
                         "targets": {str(k): v for k, v in cv_targets.items()}},
                     ll={"gross": round(ll_ledger.gross(), 2), "positions": sum(1 for r in ll_ledger.rows.values() if r.get("qty")),
                         "realized": round(sum(r.get("realized", 0) for r in ll_ledger.rows.values()), 2),
                         "poly_last_poll": feed.last_poll if feed else None},
                     mm=None if mm is None else {"enabled": mm.enabled, "tested": mm.tested, "markets": sorted(mm.active_snapshot), **mm.stats,
                         "inventory_capital": round(mm_ledger.gross(), 2),
                         "realized": round(sum(r.get("realized", 0) for r in mm_ledger.rows.values()), 2)},
                         exits=None if exits is None else {**exits.stats, "markets": sorted(exits.markets_snapshot)},
                         claw=None if a.claw is None or a.claw.cap is None else {
                             "start": round(a.claw.start, 2), "cap": round(a.claw.cap, 2),
                             "reclaimed": round(a.claw.reclaimed(), 2), "mm_budget": round(a.claw.mm_budget(), 2)},
                         news=None if nw is None else {**nw.stats, "watched": len(nw.watched), "holds": sorted(nw.holds)},
                         fv=None if refs is None else {"gross": round(ledger.gross(), 2), "max_gross": a.fv_max_gross,
                                                         "positions": sum(1 for r in ledger.rows.values() if r.get("qty")),
                                                         "references": refs.last_refresh})
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    finally:
        # Never leave resting orders behind. Further stop signals are ignored so a second
        # SIGINT/SIGTERM cannot cut this cleanup short (that left two quotes live on 1 Oct).
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        if mm is not None:
            mm.stop_worker(60)                 # finish the order in flight, then stop
        if exits is not None:
            exits.stop_worker(60)
        if nw is not None:
            nw.stop()
        try:
            # Same guard as the startup sweep: a dry run must never cancel real orders on the account.
            if a.live and sig_client.PLACE_PAYLOAD_CONFIRMED:
                log.info("shutdown: cancelled %d resting order(s)", cancel_all_open_orders(cli))
        except Exception as e:
            log.error("shutdown: could not list resting orders (%s); cancelling known quotes", e)
            if mm is not None:                 # SIG would not list them: use the worker's last view
                try:
                    log.info("shutdown: cancelled %d known quote(s)", mm.cancel_all())
                except Exception as e2:
                    log.error("shutdown: could not cancel known quotes: %s", e2)
            if exits is not None:
                try:
                    log.info("shutdown: cancelled %d resting exit(s)", exits.cancel_all())
                except Exception as e2:
                    log.error("shutdown: could not cancel resting exits: %s", e2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
