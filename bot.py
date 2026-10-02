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

from arb_engine import ArbResult, Book, breakeven_limit
import fair_value
import fast_scan
import gates
import holdings_check
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


def engage_kill_switch(reason: str, actor: str = "bot", path: pathlib.Path = None) -> None:
    """Create the kill switch atomically. Idempotent; never released from here."""
    path = path or KILL_SWITCH
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{reason}\nactor={actor} at={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}\n")
    tmp.replace(path)


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
                  limit: float, qty: float, live: bool, client_order_id: str, holdings: float = None) -> dict:
    """cli.place with a write-ahead intent: SENT is on disk before the order leaves, the
    result after it returns. A process killed in between leaves an in-doubt intent that
    holdings_check recovers from SIG's holdings on the next start."""
    if live:
        holdings_check.write_intent(client_order_id, strategy, market_id, yes_side, limit, qty, INTENT_LOG)
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


def execute_parallel(cli: Client, r: ArbResult, live: bool, chase_ticks: int = 2, tick: float = 0.005,
                     fee: float = 0.0, kill_switch: pathlib.Path = None, run_id: str = None) -> dict:
    """Send every leg at once at its walk limit, so no leg waits on another's round trip.
    If exactly one leg comes back short, top it up once (chasing at most `chase_ticks`,
    never past the break-even implied by the other legs' fills). Anything still uneven
    is IMBALANCED / LEGGED and halts the bot as in execute()."""
    from concurrent.futures import ThreadPoolExecutor
    run_id = run_id or uuid.uuid4().hex[:12]
    if kill_switch_engaged(kill_switch):
        return {"status": "KILLED", "legs": [], "run_id": run_id}
    plan = leg_plan(r)

    def send(k, leg, limit, qty, suffix=""):
        coid = f"{run_id}:{k}{suffix}"
        resp = place_tracked(cli, "arb", leg["market_id"], leg["exchange_id"], leg["yes_side"], limit, qty,
                             live, coid)
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
        be = breakeven_limit(r.direction, [x["avg"] for j, x in enumerate(rep) if j != k], fee, len(plan))
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
            return False, "gross cap"
        if time.time() - self.last_fire.get(r.race, 0) < self.a.cooldown:
            return False, "cooldown"
        return True, ""

    def book(self, r: ArbResult, res: dict):
        self.last_fire[r.race] = time.time()
        if res.get("status") in ("DONE", "LEGGED", "IMBALANCED", "UNKNOWN"):
            self.gross += r.capital


def journal(r: ArbResult, res: dict, mode: str, live: bool = False):
    EXEC_LOG.parent.mkdir(exist_ok=True)
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode, "live": live,
                            "race": r.race, "dir": r.direction, "qty": r.qty, "pnl": r.pnl,
                            "capital": r.capital, "result": res}) + "\n")


def run_fair_value(cli: Client, snap: Snapshot, refs, ledger, a, live: bool, risk=None,
                   holdings: dict = None, skip: set = frozenset(), touched: set = None) -> dict:
    """Fair-value orders for the markets in one freshly read race (single-leg, IOC-like).
    Room is the tightest of: account-wide exposure cap, fair-value cap, per-race cap
    (sibling markets are one bet), per-market cap; size scales with the gap."""
    out = {"fv_signals": 0, "fv_orders": 0, "fv_skipped": 0}
    for m in snap.markets:
        if m["id"] in skip:
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        race_used = sum(ledger.capital(x["id"]) for x in snap.markets)
        gross_left = min(a.fv_max_gross - ledger.gross(),
                         getattr(a, "fv_max_race", float("inf")) - race_used,
                         (a.max_gross - risk.gross) if risk is not None else float("inf"))
        plan = fair_value.plan_market(book, refs.fair(m["id"]), ledger, threshold=a.fv_threshold,
                                      exit_band=a.fv_exit, max_per_market=a.fv_max_market,
                                      gross_left=gross_left, unit=getattr(a, "fv_unit", None),
                                      tp=getattr(a, "fv_tp", None), stop=getattr(a, "fv_stop", None),
                                      max_slip=getattr(a, "fv_max_slip", 0.03),
                                      max_hold_s=getattr(a, "fv_max_hold", 0) or None)
        if not plan:
            continue
        out["fv_signals"] += 1
        kind = f"EXIT_{str(plan['exit']).upper()}" if plan.get("exit") else "ENTRY"
        desc = (f"FV {kind} #{m['id']} {m['title'][9:60]}: {plan['yes_side']} YES x{plan['qty']:g} "
                f"@ {plan['limit']} (fair {plan['fair']}" + (f", edge {plan['edge']}" if 'edge' in plan else "") + ")")
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
                engage_kill_switch(f"UNKNOWN fair-value order on #{m['id']} ({coid}): reconcile before retrying")
    return out


def journal_single(strategy: str, market_id: int, yes_side: str, qty: float, price: float, kind: str,
                   live: bool, **extra) -> None:
    EXEC_LOG.parent.mkdir(exist_ok=True)
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "auto", "live": live,
                            "race": f"#{market_id}", "dir": f"{strategy.upper()}_{kind}", "qty": qty,
                            "pnl": 0.0, "capital": 0.0,
                            "result": {"status": kind, "strategy": strategy, "market": market_id,
                                       "yes_side": yes_side, "price": price, **extra}}) + "\n")


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
                engage_kill_switch(f"UNKNOWN lead-lag order on #{m['id']} ({coid}): reconcile before retrying")
    return out


def run_market_maker(mm, snap: Snapshot, feed, refs, ledgers: dict, holdings: dict, a,
                     skip: set = frozenset()) -> None:
    """Requote every eligible market in one freshly read race."""
    global _mm_selftest_done
    for m in snap.markets:
        if m["id"] in skip:
            continue
        book = Book.from_levels(m["id"], snap.levels[m["id"]])
        other = ledgers["fv"].position(m["id"]) + ledgers["ll"].position(m["id"])
        if not _mm_selftest_done:
            if other or holdings.get(m["id"]) or not book.bids or book.bids[0][0] < 0.05:
                continue
            with critical():
                mm.enabled = mm.self_test(book, holdings.get(m["id"], 0.0))
            _mm_selftest_done = True
            continue
        fair = feed.mid(m["id"]) if feed is not None else None
        if fair is None and refs is not None:
            f_ = refs.fair(m["id"])
            fair = f_["fair"] if f_ else None
        if not mm.eligible(m["id"], other, fair):
            continue
        mv = feed.move(m["id"], 120) if feed is not None else None
        moving = mv is not None and abs(mv) >= 0.01
        with critical():
            act = mm.on_book(book, fair, moving, holdings.get(m["id"], 0.0))
        if act.get("placed") or act.get("cancelled") or act.get("pulled"):
            log.info("MM #%s fair %s: %s", m["id"], None if fair is None else round(fair, 4), act)


_mm_selftest_done = False


# ---------------------------------------------------------- limits / status
def apply_limits(a, limits: dict):
    """Tighten CLI caps to config/risk_limits.json; the file always wins when stricter."""
    a.max_gross = min(a.max_gross, limits["venue_exposure"].get("sig", a.max_gross))
    a.max_per_race = min(a.max_per_race, limits["per_trade_capital"], limits["event_exposure"])
    a.min_edge = max(a.min_edge, limits["min_net_edge"])
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
    tmp.replace(path)


def write_status(path: pathlib.Path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "pid": os.getpid(), **fields}, default=str))
    tmp.replace(path)


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
    ap.add_argument("--max-gross", type=float, default=60_000)
    ap.add_argument("--max-qty", type=float, default=None)
    ap.add_argument("--cooldown", type=float, default=30, help="sec before re-firing the same race")
    ap.add_argument("--chase-ticks", type=int, default=2)
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
    ap.add_argument("--fv-max-gross", type=float, default=30000,
                    help="fv: cap on fair-value capital (leaves room for ll/mm under the venue cap)")
    ap.add_argument("--fv-tp", type=float, default=0.03, help="fv: take profit vs entry")
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
    ap.add_argument("--mm-edge", type=float, default=0.01, help="mm: quote at least this far from fair")
    ap.add_argument("--mm-size", type=float, default=300, help="mm: shares per quote")
    ap.add_argument("--mm-max-inventory", type=float, default=1000, help="mm: shares per market")
    ap.add_argument("--mm-take", type=float, default=0.01, help="mm: exit profit vs entry")
    ap.add_argument("--mm-max-hold", type=float, default=1800, help="mm: after this, exit at fair")
    ap.add_argument("--mm-min-spread", type=float, default=0.02, help="mm: only quote SIG spreads this wide")
    ap.add_argument("--mm-max-markets", type=int, default=8)
    ap.add_argument("--mm-min-price", type=float, default=0.10, help="mm: only quote fair values at or above")
    ap.add_argument("--mm-max-price", type=float, default=0.90, help="mm: only quote fair values at or below")
    ap.add_argument("--mm-poll", type=float, default=10, help="mm: sec between open-order/fill syncs")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    signal.signal(signal.SIGINT, _on_stop_signal)
    signal.signal(signal.SIGTERM, _on_stop_signal)

    limits = gates.load_limits(a.limits)     # malformed limits -> refuse to start
    apply_limits(a, limits)
    cli = Client(concurrency=a.concurrency)
    markets = fast_scan.load_markets(cli)
    exhaustive = load_exhaustive()
    scanner = fast_scan.TieredScanner(cli, markets, exhaustive, hot_band=a.hot_band,
                                      sweep_races=a.sweep_races, concurrency=a.concurrency)
    last_universe, full_sweep, scan = time.time(), True, {}
    risk = Risk(a)
    seen = set()
    counts = {"signals": 0, "executed": 0, "skipped": 0}
    last_exec = None
    log.info("mode=%s live=%s markets=%d max_gross=%g max_per_race=%g min_edge=%g",
             a.mode, a.live, len(markets), a.max_gross, a.max_per_race, a.min_edge)

    strategies = {x.strip() for x in a.strategy.split(",") if x.strip()}
    if not strategies <= {"arb", "fv", "ll", "mm"}:
        raise SystemExit(f"unknown --strategy {a.strategy}")
    refs = None
    ledger = fair_value.Ledger()            # also needed to reconcile holdings when fv is off
    ll_ledger = fair_value.Ledger(HERE / "logs" / "ll_positions.json")
    mm_ledger = fair_value.Ledger(HERE / "logs" / "mm_positions.json")
    ledgers = {"fv": ledger, "ll": ll_ledger, "mm": mm_ledger}
    checker = holdings_check.HoldingsCheck(ledgers, lambda why: engage_kill_switch(why, actor="holdings_check"),
                                           intent_log=INTENT_LOG, exec_log=EXEC_LOG)
    acct = {}                                # market_id -> signed YES holding (minute snapshot)
    try:
        port = cli.portfolio()
        acct = holdings_check.actual_holdings(port)
        rep = checker.check(port, cli.transactions, immediate=True)
        log.info("holdings check at start: %s", json.dumps(rep))
    except Exception as e:
        log.error("holdings check at start failed: %s", e)
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
                                 min_price=a.mm_min_price, max_price=a.mm_max_price)
        mm.enabled = False                   # until the live self-test passes
    mm_tested, mm_synced = False, 0.0
    if strategies & {"fv", "mm"}:
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

    def mm_sync(port=None):
        nonlocal mm_synced
        port = port or cli.portfolio()
        for f_ in mm.sync(port, others_expected("mm")):
            log.info("MM FILL #%s %s %g @ %s", f_["market_id"], f_["yes_side"], f_["qty"], f_["price"])
            journal_single("mm", f_["market_id"], f_["yes_side"], f_["qty"], f_["price"], "FILL", live)
        mm_synced = time.time()
        return port

    balance, balance_at = None, 0.0
    book_cache = {}
    live = False
    try:
        while True:
            t0 = time.time()
            left = cli.token_seconds_left()
            if cli.can_refresh and left is not None and left < REFRESH_AHEAD_S:
                try:
                    left = cli.refresh_session()
                    log.info("SIG session renewed; token valid %.0f min", (left or 0) / 60)
                except Exception as e:
                    log.error("SIG session renewal failed: %s", e)
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
                        balance = cli.balance() or balance
                    # One account snapshot a minute: the exposure cap counts every open position
                    # (arb and fair value, including ones from earlier runs), fair-value orders
                    # read holdings from it, and holdings are reconciled against bot records.
                    try:
                        port = cli.portfolio()
                        risk.gross = holdings_check.deployed(port)
                        acct = holdings_check.actual_holdings(port)
                        if mm is not None:
                            mm_sync(port)
                        rep = checker.check(port, cli.transactions)
                        if not rep["ok"]:
                            log.warning("holdings check: %s", json.dumps(rep))
                    except Exception as e:
                        log.warning("could not read account snapshot: %s", e)
                    balance_at = time.time()
                budget = a.budget or balance or 100_000
                scan = {}
                if mm is not None:
                    mm.live = live
                    if (kill_switch_engaged() or not live) and any(mm.open.values()):
                        with critical():
                            log.warning("mm: pulled %d quote(s) (kill switch / not live)", mm.cancel_all())
                    elif time.time() - mm_synced > a.mm_poll:
                        mm_sync()
                if feed is not None:
                    hot_mkts = set(feed.movers(min(a.ll_move, 0.01), a.ll_lookback))
                    hot_mkts |= {int(k) for k, r in ll_ledger.rows.items() if r.get("qty")}
                    if mm is not None:
                        hot_mkts |= mm.active
                    scanner.boost = {r for r in map(scanner.race_of, hot_mkts) if r}
                # Each race is evaluated, and traded, as soon as all its books arrive.
                for race, snap in scanner.stream(full=full_sweep, stats=scan):
                    for m in snap.markets:
                        book_cache[m["id"]] = (m, snap.levels[m["id"]], snap.ts)
                    # Arbs first: their profit is locked, and any other strategy trading first
                    # would consume the liquidity the arb's depth walk counted on (Nebraska, 1 Oct).
                    race_traded = False
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
                            for leg in res.get("legs", []):
                                if live and leg.get("filled") and not leg.get("dryRun"):
                                    acct[leg["market"]] = acct.get(leg["market"], 0.0) + (
                                        leg["filled"] if leg["side"] == "BUY" else -leg["filled"])
                            journal(r, res, a.mode, live)
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
                    mm_busy = set(mm.active) if mm is not None else set()
                    if "ll" in strategies:
                        for k, v in run_leadlag(cli, snap, feed, ll_ledger, a, live, risk, acct,
                                                skip=mm_busy | {int(k) for k, r in ledger.rows.items() if r.get("qty")},
                                                touched=touched).items():
                            counts[k] = counts.get(k, 0) + v
                    if "fv" in strategies:
                        fv_counts = run_fair_value(cli, snap, refs, ledger, a, live, risk, acct,
                                                   skip=mm_busy | touched, touched=touched)
                        for k, v in fv_counts.items():
                            counts[k] = counts.get(k, 0) + v
                    if mm is not None and live and not kill_switch_engaged():
                        run_market_maker(mm, snap, feed, refs, ledgers, acct, a, skip=touched)
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
                         max_per_race=a.max_per_race, min_edge=a.min_edge, counts=counts,
                         last_exec=last_exec, error=error, token_seconds_left=cli.token_seconds_left(),
                         scan=scan, strategies=sorted(strategies), holdings_check=checker.last,
                     ll={"gross": round(ll_ledger.gross(), 2), "positions": sum(1 for r in ll_ledger.rows.values() if r.get("qty")),
                         "realized": round(sum(r.get("realized", 0) for r in ll_ledger.rows.values()), 2),
                         "poly_last_poll": feed.last_poll if feed else None},
                     mm=None if mm is None else {"enabled": mm.enabled, "markets": sorted(mm.active),
                         "open_quotes": sum(len(v) for v in mm.open.values()),
                         "inventory_capital": round(mm_ledger.gross(), 2),
                         "realized": round(sum(r.get("realized", 0) for r in mm_ledger.rows.values()), 2)},
                         fv=None if refs is None else {"gross": round(ledger.gross(), 2), "max_gross": a.fv_max_gross,
                                                         "positions": sum(1 for r in ledger.rows.values() if r.get("qty")),
                                                         "references": refs.last_refresh})
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    finally:
        # Never leave resting quotes behind: re-read open orders and cancel them.
        if mm is not None:
            try:
                mm.sync(cli.portfolio(), others_expected("mm"))
                n = mm.cancel_all()
                log.info("mm: cancelled %d resting quote(s) on shutdown", n)
            except Exception as e:
                log.error("mm: could not cancel quotes on shutdown: %s", e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
