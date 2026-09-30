"""
bot.py — the systematic layer on top of signals.py.

Three modes, meant to be adopted in order:

  --mode signal   (default) scan loop, print tickets, never touches order endpoints
  --mode confirm  scan loop; for each new signal, show tickets and ask y/N.
                  On "y": quote -> place legs via API (dry-run unless --live)
  --mode auto     scan loop; executes signals that pass risk checks with no prompt
                  (requires --live AND sig_client.PLACE_PAYLOAD_CONFIRMED = True)

    python bot.py --mode signal --interval 5
    python bot.py --mode confirm --interval 5 --live
    python bot.py --mode auto --interval 3 --live --min-edge 0.005 --max-gross 50000

All executions are appended to logs/executions.jsonl.
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time

from arb_engine import ArbResult, breakeven_limit
import sig_client
from sig_client import Client
from signals import HERE, Snapshot, generate, load_exhaustive, log_csv, render, sig_key, tickets

log = logging.getLogger("sigarb")
EXEC_LOG = HERE / "logs" / "executions.jsonl"


# ----------------------------------------------------------- execution
def _fill_of(resp: dict, default_qty: float, default_px: float):
    """Pull filled qty / avg price out of a place() response. Keys are a guess
    until the payload is confirmed; dry-run assumes a full fill at limit."""
    if resp.get("dryRun"):
        return default_qty, default_px, None
    q = next((resp[k] for k in ("filledQuantity", "filled", "filledQty") if k in resp), None)
    p = next((resp[k] for k in ("avgPrice", "averagePrice", "fillPrice") if k in resp), None)
    oid = resp.get("orderId") or resp.get("id")
    return float(q if q is not None else 0), float(p if p is not None else default_px), oid


def execute(cli: Client, r: ArbResult, live: bool, chase_ticks: int = 2, tick: float = 0.005,
            fee: float = 0.0) -> dict:
    """Leg the arb: thinnest leg first; later legs sized to actual fill; last leg
    may chase up to `chase_ticks` but never past break-even."""
    yes_side = "SELL" if r.direction == "SELL_ALL" else "BUY"
    order = sorted(r.legs, key=lambda l: min(q for _, q in l.fills))
    target, vwaps, rep = r.qty, [], []
    for k, leg in enumerate(order):
        limit = leg.limit
        if k == len(order) - 1 and vwaps:
            be = breakeven_limit(r.direction, vwaps, fee, len(order))
            c = chase_ticks * tick
            limit = max(be, limit - c) if yes_side == "SELL" else min(be, limit + c)
        if live:
            try:
                cli.quote(leg.exchange_id, yes_side, limit, target)
            except Exception as e:
                rep.append({"market": leg.market_id, "error": f"quote: {e}"})
                return {"status": "ABORT" if k == 0 else "LEGGED", "legs": rep}
        resp = cli.place(leg.market_id, leg.exchange_id, yes_side, limit, target, dry_run=not live)
        fq, fp, oid = _fill_of(resp, target, limit)
        if oid and fq < target:
            cli.cancel(oid, dry_run=not live)
        rep.append({"market": leg.market_id, "side": yes_side, "limit": round(limit, 4),
                    "req": target, "filled": fq, "avg": fp, "dryRun": bool(resp.get("dryRun"))})
        if fq <= 0:
            return {"status": "LEGGED" if k else "MISS", "legs": rep,
                    "action": "manually flatten earlier legs" if k else None}
        target, _ = fq, vwaps.append(fp)
    fills = [x["filled"] for x in rep]
    hedged = min(fills)
    residual = {x["market"]: x["filled"] - hedged for x in rep if x["filled"] > hedged}
    if residual:   # earlier legs over-filled vs. later ones -> naked exposure
        return {"status": "IMBALANCED", "qty": hedged, "legs": rep, "residual": residual,
                "action": "flatten residual or work the short leg manually"}
    return {"status": "DONE", "qty": hedged, "legs": rep}


# ---------------------------------------------------------- risk gate
class Risk:
    def __init__(self, a):
        self.a, self.gross, self.last_fire = a, 0.0, {}

    def ok(self, r: ArbResult) -> tuple[bool, str]:
        if r.marginal_edge < self.a.min_edge:
            return False, "edge"
        if len(r.legs) >= 3 and r.marginal_edge < self.a.min_edge_3leg:
            return False, "3-leg edge"
        if self.gross + r.capital > self.a.max_gross:
            return False, "gross cap"
        if time.time() - self.last_fire.get(r.race, 0) < self.a.cooldown:
            return False, "cooldown"
        return True, ""

    def book(self, r: ArbResult, res: dict):
        self.last_fire[r.race] = time.time()
        if res.get("status") in ("DONE", "LEGGED", "IMBALANCED"):
            self.gross += r.capital


def journal(r: ArbResult, res: dict, mode: str):
    EXEC_LOG.parent.mkdir(exist_ok=True)
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode,
                            "race": r.race, "dir": r.direction, "qty": r.qty, "pnl": r.pnl,
                            "capital": r.capital, "result": res}) + "\n")


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
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if a.mode == "auto" and not (a.live and sig_client.PLACE_PAYLOAD_CONFIRMED):
        log.warning("auto mode without --live + PLACE_PAYLOAD_CONFIRMED -> orders are DRY RUN")
    cli = Client()
    markets = cli.markets()
    exhaustive = load_exhaustive()
    risk = Risk(a)
    seen = set()
    log.info("mode=%s live=%s markets=%d", a.mode, a.live, len(markets))

    while True:
        t0 = time.time()
        try:
            budget = a.budget or cli.balance() or 100_000
            snap = Snapshot.fetch(cli, markets)
            sigs, near, titles = generate(snap, a, exhaustive, budget)
            fresh = [s for s in sigs if sig_key(s) not in seen]
            seen = {sig_key(s) for s in sigs}
            if fresh:
                print(render(snap, fresh, near, titles, budget))
                log_csv(snap, fresh)
            for r in fresh:
                if a.mode == "signal":
                    continue
                ok, why = risk.ok(r)
                if not ok:
                    log.info("skip %s (%s)", r.race, why)
                    continue
                if a.mode == "confirm":
                    print("\n".join(tickets(r, titles)))
                    if input(f"Execute {r.race} {r.direction} x{r.qty:g} "
                             f"(pnl {r.pnl:.2f})? [y/N] ").strip().lower() != "y":
                        continue
                res = execute(cli, r, live=a.live, chase_ticks=a.chase_ticks, fee=a.fee)
                risk.book(r, res)
                journal(r, res, a.mode)
                log.info("EXEC %s -> %s", r.race, json.dumps(res))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("tick failed: %s", e)
        time.sleep(max(0.0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
