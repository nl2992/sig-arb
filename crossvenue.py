"""CLI for public cross-venue inventory and read-only live research."""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import datetime as dt
from types import SimpleNamespace

from crossvenue_adapters import fetch_public, load_targeted_market_ids
from crossvenue_models import PriceObservation
from market_matches import load_registry
from movement_scanner import scan_movements
from relative_value import scan_pairs
from signals import Snapshot, generate, load_exhaustive

ROOT = pathlib.Path(__file__).parent


def _mid(book):
    bid, ask = book.best_bid(), book.best_ask()
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return ask if ask is not None else bid


def _append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _snapshot_age_seconds(snapshot):
    value = snapshot.ts.replace('Z', '+00:00')
    observed = dt.datetime.fromisoformat(value)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds())


def _live_once(args, cli, markets, matches, history, market_ids=None):
    snap = Snapshot.load(args.sig_snapshot) if args.sig_snapshot else Snapshot.fetch(cli, markets)
    age = _snapshot_age_seconds(snap)
    if args.sig_snapshot and age > args.max_snapshot_age:
        return {"ts": snap.ts, "status": "STALE_SIG_SNAPSHOT",
                "snapshot_age_seconds": round(age, 3),
                "max_snapshot_age": args.max_snapshot_age,
                "execution_enabled": False,
                "note": "Research scan blocked until the authenticated SIG snapshot refreshes."}
    public = fetch_public(["kalshi", "polymarket"], args.limit, market_ids=market_ids)
    observations = [PriceObservation(**row)
                    for venue in public.values() for row in venue["observations"]]
    wanted = {(m.reference_venue, m.reference_market_id, m.reference_outcome_id.upper())
              for m in matches}
    observations = [o for o in observations
                    if (o.venue, o.market_id, o.outcome_id.upper()) in wanted]
    by_key = {(o.venue, o.market_id, o.outcome_id.upper()): o for o in observations}
    books = snap.books()
    pair_rows = []
    for match in matches:
        ref = by_key.get((match.reference_venue, match.reference_market_id,
                          match.reference_outcome_id.upper()))
        sig_price = _mid(books[match.sig_market_id]) if match.sig_market_id in books else None
        ref_price = ref.valid_reference_price() if ref else None
        pair_rows.append({"observed_at": snap.ts, "sig_market_id": match.sig_market_id,
                          "reference_venue": match.reference_venue,
                          "reference_market_id": match.reference_market_id,
                          "reference_outcome_id": match.reference_outcome_id,
                          "sig_price": sig_price, "reference_price": ref_price})
    _append_jsonl(args.history, pair_rows)
    history.extend(pair_rows)
    historical_observations = [PriceObservation(
        venue=row["reference_venue"], market_id=row["reference_market_id"],
        outcome_id=row["reference_outcome_id"], observed_at=row["observed_at"],
        last=row["reference_price"], source="crossvenue-history", price_basis="last")
        for row in history if row.get("reference_price") is not None]
    movement = scan_movements(snap, historical_observations, matches,
                              min_move_pp=args.min_move_pp,
                              lookback_minutes=args.lookback_minutes)
    sig_args = SimpleNamespace(min_edge=args.sig_min_edge, fee=args.fee,
                               max_qty=args.max_qty, max_per_race=args.max_per_race,
                               min_pnl=args.sig_min_pnl, near=0)
    budget = args.budget
    sigs, _, _ = generate(snap, sig_args, load_exhaustive(), budget)
    sig_rows = [{"race": s.race, "direction": s.direction, "qty": s.qty,
                 "pnl": s.pnl, "capital": s.capital, "roi": s.roi,
                 "status": "SIGNAL" if s.roi >= args.sig_min_roi else "BELOW_ROI_HURDLE"}
                for s in sigs]
    return {"ts": snap.ts, "sig_markets": len(snap.markets),
            "reference_observations": len(observations),
            "sig_signals": sig_rows,
            "movement": movement.to_dict(),
            "relative_value": scan_pairs(history, matches, min_points=args.min_points),
            "execution_enabled": False,
            "note": "Research candidates only; no orders are placed."}


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
    live = sub.add_parser("live", help="run a read-only combined SIG/venue scan")
    live.add_argument("--matches", default=str(ROOT / "fixtures/crossvenue/matches.json"))
    live.add_argument("--history", type=pathlib.Path, default=ROOT / "logs/crossvenue-history.jsonl")
    live.add_argument("--interval", type=float, default=60.0)
    live.add_argument("--once", action="store_true", help="run one cycle and exit")
    live.add_argument("--sig-snapshot", type=pathlib.Path,
                      help="read SIG books from the authenticated browser relay")
    live.add_argument("--max-snapshot-age", type=float, default=30.0)
    live.add_argument("--limit", type=int, default=1000)
    live.add_argument("--targeted", action="store_true",
                      help="use native IDs from docs/market-links.csv")
    live.add_argument("--min-move-pp", type=float, default=5.0)
    live.add_argument("--lookback-minutes", type=int, default=120)
    live.add_argument("--min-points", type=int, default=10)
    live.add_argument("--sig-min-roi", type=float, default=0.05, help="default 5%% ROI hurdle")
    live.add_argument("--sig-min-edge", type=float, default=0.0)
    live.add_argument("--sig-min-pnl", type=float, default=1.0)
    live.add_argument("--fee", type=float, default=0.0)
    live.add_argument("--budget", type=float, default=None)
    live.add_argument("--max-per-race", type=float, default=None)
    live.add_argument("--max-qty", type=float, default=None)
    args = ap.parse_args(argv)
    if args.command == "fetch-public":
        payload = fetch_public(args.venues, args.limit)
        pathlib.Path(args.out).write_text(json.dumps(payload, indent=2))
        print(json.dumps({v: {k: len(rows) for k, rows in payload[v].items()} for v in payload}, indent=2))
        return
    if args.command == "live":
        from sig_client import Client
        matches = load_registry(args.matches)
        market_ids = load_targeted_market_ids(ROOT / "docs/market-links.csv") if args.targeted else None
        cli = None if args.sig_snapshot else Client()
        markets = None if args.sig_snapshot else cli.markets()
        history = _read_jsonl(args.history)
        while True:
            started = time.time()
            try:
                report = _live_once(args, cli, markets, matches, history, market_ids)
                print(json.dumps(report, indent=2), flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                  "status": "ERROR", "error": str(exc)}), flush=True)
            if args.once:
                return
            time.sleep(max(0.0, args.interval - (time.time() - started)))
            # Market membership can change during election season.
            if cli is not None:
                markets = cli.markets()

    snap = Snapshot.load(args.snapshot)
    payload = json.loads(pathlib.Path(args.observations).read_text())
    rows = payload.get("observations", payload) if isinstance(payload, dict) else payload
    observations = [PriceObservation(**row) for row in rows]
    report = scan_movements(snap, observations, load_registry(args.matches),
                            min_move_pp=args.min_move_pp, lookback_minutes=args.lookback_minutes)
    print(json.dumps(report.to_dict(), indent=2))


if __name__ == "__main__":
    main()
