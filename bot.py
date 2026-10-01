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
import datetime as dt
import json
import logging
import os
import pathlib
import time
import uuid

from arb_engine import ArbResult, breakeven_limit
import fast_scan
import gates
import sig_client
from sig_client import Client
from signals import HERE, Snapshot, generate, load_exhaustive, log_csv, render, sig_key, tickets

log = logging.getLogger("sigarb")
EXEC_LOG = HERE / "logs" / "executions.jsonl"
KILL_SWITCH = HERE / "logs" / "KILL_SWITCH"
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
        resp = cli.place(leg["market_id"], leg["exchange_id"], yes_side, limit, target,
                         dry_run=not live, client_order_id=coid)
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
        resp = cli.place(leg["market_id"], leg["exchange_id"], leg["yes_side"], limit, qty,
                         dry_run=not live, client_order_id=coid)
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
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

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

    balance, balance_at = None, 0.0
    book_cache = {}
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
            if not a.budget and time.time() - balance_at > a.balance_every and not scanner.paused_for():
                balance, balance_at = cli.balance() or balance, time.time()
            budget = a.budget or balance or 100_000
            scan = {}
            # Each race is evaluated, and traded, as soon as all its books arrive.
            for race, snap in scanner.stream(full=full_sweep, stats=scan):
                for m in snap.markets:
                    book_cache[m["id"]] = (m, snap.levels[m["id"]], snap.ts)
                sigs, near, titles = generate(snap, a, exhaustive, budget)
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
                    if a.sequential:
                        res = execute(cli, r, live=live, chase_ticks=a.chase_ticks, fee=a.fee,
                                      pre_quote=a.pre_quote)
                    else:
                        res = execute_parallel(cli, r, live=live, chase_ticks=a.chase_ticks, fee=a.fee)
                    risk.book(r, res)
                    journal(r, res, a.mode, live)
                    counts["executed"] += 1
                    last_exec = {"race": r.race, "status": res["status"], "live": live,
                                 "book_age_s": round(time.time() - t0, 1)}
                    log.info("EXEC %s -> %s", r.race, json.dumps(res))
                    if live and res["status"] in HALT_STATUSES:
                        engage_kill_switch(f"{res['status']} on {r.race} (run {res.get('run_id')}): "
                                           f"{res.get('action') or 'operator review required'}")
                        log.error("kill switch engaged after %s on %s", res["status"], r.race)
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
                     scan=scan)
        time.sleep(max(0.0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
