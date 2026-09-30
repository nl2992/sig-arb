"""No-order paper/shadow runner with explicit reconciliation and kill switch.

This module consumes live or replayed SIG books, simulates fills from the
observed executable quantity, and journals residual exposure. It never calls
SIG quote/place/cancel endpoints and cannot enable live execution.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
from types import SimpleNamespace

from signals import Snapshot, generate, load_exhaustive
from sig_client import Client

ROOT = pathlib.Path(__file__).parent


def kill_switch_active(path: pathlib.Path) -> bool:
    return path.exists()


def simulate_result(result, fill_ratio: float = 1.0, fee: float = 0.0,
                    leg_fill_ratios: list[float] | None = None) -> dict:
    """Simulate equal fractional fills without touching an order endpoint."""
    if not 0 <= fill_ratio <= 1:
        raise ValueError("fill_ratio must be between 0 and 1")
    if leg_fill_ratios is not None and len(leg_fill_ratios) != len(result.legs):
        raise ValueError("leg_fill_ratios must match the number of legs")
    legs = []
    for index, leg in enumerate(result.legs):
        requested = result.qty
        ratio = leg_fill_ratios[index] if leg_fill_ratios is not None else fill_ratio
        if not 0 <= ratio <= 1:
            raise ValueError("leg fill ratios must be between 0 and 1")
        filled = requested * ratio
        legs.append({"market_id": leg.market_id, "side": "SELL_YES" if result.direction == "SELL_ALL" else "BUY_YES",
                     "requested": requested, "filled": filled,
                     "price": (1 - leg.vwap if result.direction == "SELL_ALL" else leg.vwap),
                     "notional": filled * (1 - leg.vwap if result.direction == "SELL_ALL" else leg.vwap)})
    hedged = min((leg["filled"] for leg in legs), default=0.0)
    residual = {str(leg["market_id"]): round(leg["filled"] - hedged, 8)
                for leg in legs if leg["filled"] - hedged > 1e-9}
    status = "RECONCILED" if not residual and hedged > 0 else "MISS" if hedged <= 0 else "PARTIAL_RESIDUAL"
    return {"status": status, "hedged_qty": hedged, "residual": residual,
            "legs": legs, "expected_pnl": result.pnl * fill_ratio,
            "fee_assumption": fee, "research_only": True, "orders_sent": 0}


def run_cycle(snapshot: Snapshot, *, min_roi: float = 0.05, min_edge: float = 0.0,
              min_pnl: float = 1.0, budget: float | None = None,
              fill_ratio: float = 1.0, fee: float = 0.0,
              max_gross: float = 60_000, kill_switch: pathlib.Path = pathlib.Path("logs/KILL_SWITCH")) -> dict:
    if kill_switch_active(kill_switch):
        return {"ts": snapshot.ts, "status": "KILL_SWITCH", "orders_sent": 0,
                "reason": f"kill switch present: {kill_switch}", "research_only": True}
    args = SimpleNamespace(min_edge=min_edge, fee=fee, max_qty=None,
                           max_per_race=None, min_pnl=min_pnl, near=0)
    signals, _, _ = generate(snapshot, args, load_exhaustive(), budget)
    accepted = [result for result in signals if result.roi >= min_roi]
    total_capital = sum(result.capital for result in accepted)
    if total_capital > max_gross:
        return {"ts": snapshot.ts, "status": "RISK_BLOCKED", "orders_sent": 0,
                "reason": "paper gross cap", "gross_capital": total_capital,
                "max_gross": max_gross, "research_only": True}
    runs = [{"race": result.race, "direction": result.direction, "roi": result.roi,
             "capital": result.capital, "simulation": simulate_result(result, fill_ratio, fee)}
            for result in accepted]
    return {"ts": snapshot.ts, "status": "PAPER", "orders_sent": 0,
            "signal_count": len(accepted), "signals": runs,
            "rejected_below_roi": len(signals) - len(accepted),
            "min_roi": min_roi, "research_only": True}


def append_journal(path: pathlib.Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(report, allow_nan=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fill-ratio", type=float, default=1.0)
    parser.add_argument("--min-roi", type=float, default=0.05)
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--min-pnl", type=float, default=1.0)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--fee", type=float, default=0.0)
    parser.add_argument("--max-gross", type=float, default=60_000)
    parser.add_argument("--kill-switch", type=pathlib.Path, default=ROOT / "logs/KILL_SWITCH")
    parser.add_argument("--journal", type=pathlib.Path, default=ROOT / "logs/paper-runs.jsonl")
    args = parser.parse_args(argv)
    client = None if args.replay else Client()
    markets = None if args.replay else client.markets()
    while True:
        started = time.time()
        try:
            if args.replay:
                snapshot = Snapshot.load(args.replay)
            else:
                snapshot = Snapshot.fetch(client, markets)
            report = run_cycle(snapshot, min_roi=args.min_roi, min_edge=args.min_edge,
                               min_pnl=args.min_pnl, budget=args.budget,
                               fill_ratio=args.fill_ratio, fee=args.fee,
                               max_gross=args.max_gross, kill_switch=args.kill_switch)
            append_journal(args.journal, report)
            print(json.dumps(report, indent=2), flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            report = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "ERROR",
                      "error": str(exc), "orders_sent": 0, "research_only": True}
            append_journal(args.journal, report)
            print(json.dumps(report), flush=True)
        if args.once:
            return
        time.sleep(max(0.0, args.interval - (time.time() - started)))
        if client is not None:
            markets = client.markets()


if __name__ == "__main__":
    main()
