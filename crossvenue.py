"""CLI for public cross-venue inventory and offline movement research."""
from __future__ import annotations

import argparse
import json
import pathlib

from crossvenue_adapters import fetch_public
from crossvenue_models import PriceObservation
from market_matches import load_registry
from movement_scanner import scan_movements
from signals import Snapshot


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-public")
    fetch.add_argument("--venues", nargs="+", default=["kalshi", "polymarket"])
    fetch.add_argument("--limit", type=int, default=1000)
    fetch.add_argument("--out", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--snapshot", required=True)
    scan.add_argument("--observations", required=True)
    scan.add_argument("--matches", required=True)
    scan.add_argument("--min-move-pp", type=float, default=5.0)
    scan.add_argument("--lookback-minutes", type=int, default=120)
    args = ap.parse_args(argv)
    if args.command == "fetch-public":
        payload = fetch_public(args.venues, args.limit)
        pathlib.Path(args.out).write_text(json.dumps(payload, indent=2))
        print(json.dumps({v: {k: len(rows) for k, rows in payload[v].items()} for v in payload}, indent=2))
        return
    snap = Snapshot.load(args.snapshot)
    payload = json.loads(pathlib.Path(args.observations).read_text())
    rows = payload.get("observations", payload) if isinstance(payload, dict) else payload
    observations = [PriceObservation(**row) for row in rows]
    report = scan_movements(snap, observations, load_registry(args.matches),
                            min_move_pp=args.min_move_pp, lookback_minutes=args.lookback_minutes)
    print(json.dumps(report.to_dict(), indent=2))


if __name__ == "__main__":
    main()
