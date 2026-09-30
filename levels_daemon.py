"""Continuously capture targeted public levels and evaluate research alerts."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time

from crossvenue_adapters import fetch_public, load_targeted_market_ids
from levels_store import LevelsDB
from market_matches import load_registry
from movement_scanner import scan_movements
from signals import Snapshot

ROOT = pathlib.Path(__file__).parent


def run_cycle(db: LevelsDB, *, snapshot_path=None, matches_path=ROOT / "fixtures/crossvenue/matches.json",
              alert_path=ROOT / "logs/levels-alerts.jsonl", min_move_pp=5.0,
              max_snapshot_age=30.0) -> dict:
    payload = fetch_public(["kalshi", "polymarket"], market_ids=load_targeted_market_ids(ROOT / "docs/market-links.csv"))
    summaries = db.ingest(payload)
    alerts = []
    for summary in summaries:
        if not summary["complete"]:
            alerts.append({"type": "DATA_COVERAGE", "venue": summary["venue"], "details": summary})
    if snapshot_path and pathlib.Path(snapshot_path).exists():
        snapshot = Snapshot.load(snapshot_path)
        observed = dt.datetime.fromisoformat(snapshot.ts.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
        if age > max_snapshot_age:
            alerts.append({"type": "STALE_SIG_SNAPSHOT", "age_seconds": round(age, 3),
                           "max_age_seconds": max_snapshot_age})
        else:
            matches = load_registry(matches_path)
            movement = scan_movements(snapshot, db.observations(), matches, min_move_pp=min_move_pp)
            alerts.extend({"type": "MOVEMENT_CANDIDATE", "candidate": candidate}
                          for candidate in movement.candidates)
    for alert in alerts:
        db.append_alert(alert_path, alert)
        print(json.dumps(alert, separators=(",", ":")), flush=True)
    return {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "summaries": summaries, "alerts": len(alerts), "orders_sent": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=ROOT / "logs/levels.sqlite3")
    parser.add_argument("--alert-file", type=pathlib.Path, default=ROOT / "logs/levels-alerts.jsonl")
    parser.add_argument("--snapshot", type=pathlib.Path, default=ROOT / "logs/browser_snapshot.json")
    parser.add_argument("--matches", type=pathlib.Path, default=ROOT / "fixtures/crossvenue/matches.json")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--min-move-pp", type=float, default=5.0)
    parser.add_argument("--max-snapshot-age", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    db = LevelsDB(args.db)
    try:
        while True:
            started = time.time()
            try:
                print(json.dumps(run_cycle(db, snapshot_path=args.snapshot, matches_path=args.matches,
                                           alert_path=args.alert_file, min_move_pp=args.min_move_pp,
                                           max_snapshot_age=args.max_snapshot_age),
                                 separators=(",", ":")), flush=True)
            except Exception as exc:
                alert = {"type": "COLLECTOR_ERROR", "error": str(exc)}
                db.append_alert(args.alert_file, alert)
                print(json.dumps(alert, separators=(",", ":")), flush=True)
            if args.once:
                return
            time.sleep(max(0.0, args.interval - (time.time() - started)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
