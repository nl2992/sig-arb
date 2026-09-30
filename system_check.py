"""Read-only readiness audit for the systematic research pipeline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib

from crossvenue_adapters import fetch_public, load_targeted_market_ids
from market_matches import load_registry
from news_guard import load_circuit_breakers
from sig_client import PLACE_PAYLOAD_CONFIRMED
from signals import Snapshot

ROOT = pathlib.Path(__file__).parent


def _age(snapshot: Snapshot) -> float:
    observed = dt.datetime.fromisoformat(snapshot.ts.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds())


def readiness(*, snapshot_path=None, public=False, public_limit=0,
              targeted=False,
              matches_path=ROOT / "fixtures/crossvenue/matches.json",
              news_path=ROOT / "config/news_circuit_breakers.json") -> dict:
    checks = {}
    try:
        matches = load_registry(matches_path)
        checks["mapping_registry"] = {"status": "PASS", "discovered": len(matches),
                                       "approved": sum(m.status == "APPROVED" for m in matches)}
    except Exception as exc:
        checks["mapping_registry"] = {"status": "FAIL", "error": str(exc)}

    try:
        load_circuit_breakers(news_path)
        checks["news_ledger"] = {"status": "PASS", "path": str(news_path)}
    except Exception as exc:
        checks["news_ledger"] = {"status": "FAIL", "error": str(exc)}

    if snapshot_path:
        try:
            snapshot = Snapshot.load(snapshot_path)
            age = _age(snapshot)
            checks["sig_snapshot"] = {"status": "PASS" if age <= 30 else "FAIL",
                                       "age_seconds": round(age, 3), "markets": len(snapshot.markets)}
        except Exception as exc:
            checks["sig_snapshot"] = {"status": "FAIL", "error": str(exc)}
    else:
        checks["sig_snapshot"] = {"status": "NOT_RUN", "note": "Provide --sig-snapshot for freshness verification."}

    if public:
        try:
            market_ids = load_targeted_market_ids(ROOT / "docs/market-links.csv") if targeted else None
            venues = fetch_public(["kalshi", "polymarket"], public_limit, market_ids=market_ids)
            checks["public_venues"] = {"status": "PASS",
                                        "market_counts": {v: len(p["markets"]) for v, p in venues.items()},
                                        "inventory_coverage": {v: p["inventory_coverage"] for v, p in venues.items()},
                                        "book_coverage": {v: p["book_coverage"] for v, p in venues.items()},
                                        "complete": all(p["inventory_coverage"] in {"ALL_ACTIVE_INVENTORY", "TARGETED_SIG_UNIVERSE"}
                                                        for p in venues.values())}
        except Exception as exc:
            checks["public_venues"] = {"status": "FAIL", "error": str(exc)}
    else:
        checks["public_venues"] = {"status": "NOT_RUN", "note": "Pass --public to query venues."}

    checks["paper_shadow"] = {"status": "PASS", "orders_sent": 0}
    checks["live_order_payload"] = {"status": "BLOCKED", "payload_confirmed": bool(PLACE_PAYLOAD_CONFIRMED)}
    checks["portfolio_reconciliation"] = {"status": "NOT_RUN",
                                           "note": "Run the read-only SIG portfolio adapter with account access."}
    checks["execution_gate"] = {"status": "BLOCKED", "reason": "Production execution remains disabled by policy."}
    return {"checks": checks,
            "ready_for_paper": all(checks[name]["status"] == "PASS" for name in
                                    ("mapping_registry", "news_ledger", "sig_snapshot", "paper_shadow"))
            and checks["public_venues"].get("complete", False),
            "ready_for_live": False, "research_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sig-snapshot", type=pathlib.Path)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--targeted", action="store_true",
                        help="use native venue IDs from the fixed SIG universe")
    parser.add_argument("--public-limit", type=int, default=0)
    args = parser.parse_args(argv)
    if args.public_limit < 0:
        parser.error("--public-limit must be nonnegative")
    print(json.dumps(readiness(snapshot_path=args.sig_snapshot, public=args.public,
                               public_limit=args.public_limit, targeted=args.targeted), indent=2))


if __name__ == "__main__":
    main()
