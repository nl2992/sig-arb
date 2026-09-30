"""Read-only opportunity, book and history views for the operations dashboard."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import gates
from dashboard import report
from market_matches import load_registry
from signals import load_exhaustive


def _json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _age(observed_at):
    if not observed_at:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return round(max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()), 3)
    except (TypeError, ValueError):
        return None


def _system(source, status):
    return dict(
        sig_session=status.get("sig_auth", {}).get("session"),
        payload_verified=status.get("sig_auth", {}).get("payload_verified", False),
        kill_switch=status.get("kill_switch", {}), recon=_read_reconciliation(source),
        mode=source.mode, limits=status.get("limits"),
        limits_error=status.get("limits_error"),
        sig_snapshot_age_s=(status.get("feeds", [{}])[0].get("age_s")
                            if status.get("feeds") else None),
        active_breakers={int(mid): True for mid in status.get("breakers", {}).get("active_markets", [])},
    )


def _read_reconciliation(source):
    try:
        return json.loads(Path(source.reconciliation_path).read_text())
    except (OSError, ValueError):
        return None


def _state(gate_list, candidate):
    failed = {g["id"] for g in gate_list if g["pass"] is False}
    research = failed & {"mapping_approved", "settlement_approved", "fees_known"}
    hard = failed - research - {"live_mode", "payload_verified"}
    if hard:
        state = "blocked"
    elif research:
        state = "research_only"
    elif failed & {"live_mode", "payload_verified"}:
        state = "paper_eligible"
    else:
        state = "exec_ready"
    return state, [g["detail"] for g in gate_list if g["pass"] is False]


def _sig_candidate(row):
    legs = []
    for leg in row.get("legs", []):
        legs.append({
            "venue": "sig", "market_id": leg["id"], "side": leg["side"],
            "qty": leg.get("qty"), "visible_qty": leg.get("qty"),
            "quote_age_s": row.get("freshness_seconds"), "fee": None,
            "venue_id_known": True,
        })
    return {
        "legs": legs, "mapping": "single_venue",
        "settlement": "approved" if row.get("direction") == "SELL_ALL" else "unreviewed",
        "capital": row.get("capital"), "slippage": None,
    }


def _decimal(value, places=6):
    return None if value is None else f"{float(value):.{places}f}"


def _normalize_signal(row, index):
    direction = row.get("direction")
    kind = "complete_set" if direction == "BUY_ALL" else "sig_arb"
    market_ids = "-".join(str(leg.get("id")) for leg in row.get("legs", []))
    source = "fixture" if row.get("source") == "fixture" else "live"
    normalized_legs = [{"venue": "sig", "market_id": leg.get("id"),
                        "outcome": "NO" if "NO" in leg.get("side", "") else "YES",
                        "side": "BUY", "qty": leg.get("qty"),
                        "vwap": _decimal(leg.get("vwap")), "worst_price": _decimal(leg.get("limit")),
                        "fee": None, "visible_qty": leg.get("qty")}
                       for leg in row.get("legs", [])]
    return {"id": f"{kind}:{direction}:{market_ids}", "strategy": kind,
            "kind": kind, "source": source, "title": row.get("race", "SIG opportunity"),
            "event": row.get("race", "SIG opportunity"), "direction": direction,
            "edge": row.get("edge"), "gross_edge": _decimal(row.get("edge")),
            "roi": None, "net_roi": None, "profit": _decimal(row.get("profit")),
            "capital": _decimal(row.get("capital")), "qty": row.get("qty"), "exec_qty": row.get("qty"),
            "legs": normalized_legs, "mapping_ids": [], "mapping": "single_venue",
            "settlement": "approved" if kind == "sig_arb" else "unreviewed",
            "news": "clear", "risk": "high",
            "candidate": _sig_candidate(row)}


def _normalize_crossvenue(row, index, kind="xv_move"):
    venue = row.get("reference_venue")
    candidate = {
        "legs": [{"venue": "sig", "market_id": row.get("sig_market_id"), "qty": 1,
                   "visible_qty": None, "quote_age_s": row.get("freshness_seconds"),
                   "fee": None, "venue_id_known": bool(row.get("sig_market_id"))},
                  {"venue": venue, "market_id": row.get("reference_market_id"), "qty": 1,
                   "visible_qty": None, "quote_age_s": row.get("freshness_seconds"),
                   "fee": None, "venue_id_known": bool(row.get("reference_market_id"))}],
        "mapping": row.get("mapping_status", "unknown").lower(),
        "settlement": "approved" if row.get("settlement_status") == "APPROVED" else "unreviewed",
        "capital": None, "slippage": None,
    }
    return {"id": f"{kind}:{row.get('sig_market_id')}:{venue}:{row.get('reference_market_id', index)}",
            "strategy": kind, "kind": kind, "source": "sqlite", "title": f"SIG {row.get('sig_market_id')} vs {venue}",
            "event": f"SIG {row.get('sig_market_id')} vs {venue}", "direction": row.get("direction"),
            "edge": row.get("gap_pp"), "gross_edge": _decimal(row.get("gap_pp")),
            "movement_pp": row.get("movement_pp"), "roi": None, "net_roi": None,
            "profit": None, "capital": None, "qty": None, "exec_qty": None,
            "legs": [{"venue": "sig", "market_id": row.get("sig_market_id"), "outcome": "YES", "side": "BUY"},
                     {"venue": venue, "market_id": row.get("reference_market_id"), "outcome": row.get("reference_outcome_id", "YES"), "side": "BUY"}],
            "mapping_ids": [row.get("sig_market_id"), row.get("reference_market_id")],
            "mapping": row.get("mapping_status", "unresolved").lower(),
            "settlement": "approved" if row.get("settlement_status") == "APPROVED" else "unreviewed",
            "news": "clear", "risk": "high", "candidate": candidate}


def list_opportunities(source, strategy=None, state=None, search=None):
    snapshot = source.get()
    crossvenue = source.get_crossvenue()
    data = report(snapshot, {"roi": ["0"], "profit": ["0"]}, crossvenue)
    status = __import__("dashboard").build_status(source)
    system = _system(source, status)
    rows = [_normalize_signal({**row, "source": "fixture" if source.replay else "live"}, index)
            for index, row in enumerate(data.get("signals", []))]
    rows += [_normalize_signal({**row, "source": "fixture" if source.replay else "live"}, index)
             for index, row in enumerate(data.get("punts", []), len(rows))]
    rows += [_normalize_crossvenue(row, index) for index, row in enumerate(
        data.get("crossvenue", {}).get("opportunities", []), len(rows))]
    rows += [_normalize_crossvenue(row, index, "rel_value") for index, row in enumerate(
        data.get("crossvenue", {}).get("relative_value", []), len(rows))
             if row.get("status") == "RESEARCH_CANDIDATE"]
    exhaustive = load_exhaustive()
    for row in rows:
        if row["strategy"] == "complete_set":
            row["settlement"] = "approved" if row.get("event") in exhaustive else "unreviewed"
            row["candidate"]["settlement"] = row["settlement"]
    result = []
    for row in rows:
        gate_list = gates.evaluate(system, row["candidate"])
        state, reasons = _state(gate_list, row["candidate"])
        result.append({k: _json(v) for k, v in row.items() if k != "candidate"} |
                      {"state": state, "block_reasons": reasons,
                       "gates": gate_list, "gate_hash": gates.gate_hash(gate_list)})
    if strategy:
        result = [row for row in result if row.get("strategy") == strategy]
    if state:
        result = [row for row in result if row.get("state") == state]
    if search:
        term = search.lower()
        result = [row for row in result if term in (row.get("title", "") + " " + row.get("event", "")).lower()]
    return {"as_of": status["as_of"], "source": "fixture" if source.replay else "live",
            "opportunities": result, "count": len(result),
            "mappings": mapping_view(source)}


def mapping_view(source):
    links = []
    try:
        from crossvenue_adapters import load_market_links
        links = load_market_links(Path(__file__).parent / "docs" / "market-links.csv")
    except (OSError, ValueError):
        pass
    try:
        registry = load_registry(Path(__file__).parent / "fixtures/crossvenue/matches.json")
    except (OSError, ValueError):
        registry = []
    approved = {(m.sig_market_id, m.reference_venue, m.reference_market_id): m for m in registry}
    rows = []
    for link in links:
        for venue in ("kalshi", "polymarket"):
            market_id = link.get(f"{venue}_market_id")
            if not market_id:
                continue
            key = (int(link["sig_market_id"]), venue, market_id)
            match = approved.get(key)
            rows.append({"sig_market_id": int(link["sig_market_id"]),
                         "reference_venue": venue, "reference_market_id": market_id,
                         "status": match.status if match else "REVIEW_REQUIRED",
                         "evidence": match.evidence if match else None})
    return rows


def get_opportunity(source, opportunity_id, requested_qty=None):
    data = list_opportunities(source)
    row = next((item for item in data["opportunities"] if item["id"] == opportunity_id), None)
    if row is None:
        return None
    books = []
    for leg in row.get("legs", []):
        books.append(read_book(source, leg.get("venue", "sig"), leg.get("id", leg.get("market_id"))))
    row["books"] = books
    row["history"] = [read_history(source, leg.get("id", leg.get("market_id")),
                                     leg.get("venue")) for leg in row.get("legs", [])]
    qty = float(requested_qty if requested_qty is not None else (row.get("qty") or 0))
    per_leg = []
    for leg, book in zip(row.get("legs", []), books):
        outcome = next((item for item in book.get("outcomes", []) if item.get("outcome_id") == leg.get("outcome", "YES")), None)
        outcome = outcome or next((item for item in book.get("outcomes", []) if item.get("outcome_id") == "YES"), {})
        if leg.get("outcome") == "YES":
            levels = outcome.get("asks", [])
        else:
            no_book = outcome if outcome.get("outcome_id") == "NO" else next(
                (item for item in book.get("outcomes", []) if item.get("outcome_id") == "NO"), None)
            if no_book:
                levels = no_book.get("asks", [])
            else:
                levels = [{"price": 1 - float(level["price"]), "size": level["size"]}
                          for level in outcome.get("bids", [])]
        remaining, cost, used = qty, 0.0, []
        for level in levels:
            take = min(remaining, float(level.get("size") or 0))
            if take > 0:
                cost += take * float(level["price"])
                used.append({"price": level["price"], "size": take})
                remaining -= take
            if remaining <= 1e-9:
                break
        per_leg.append({"market_id": leg.get("market_id"), "requested_qty": qty,
                        "executable_qty": qty - remaining,
                        "vwap": _decimal(cost / (qty - remaining)) if qty > remaining else None,
                        "worst_level": _decimal(used[-1]["price"]) if used else None,
                        "slippage": None if not used else _decimal(float(used[-1]["price"]) - float(used[0]["price"]))})
    row["requested_qty"] = qty
    row["executable_qty"] = min((item["executable_qty"] for item in per_leg), default=0)
    row["depth_walk"] = per_leg
    row["fees"] = {"status": "UNKNOWN", "per_leg": [None for _ in row.get("legs", [])]}
    row["net_roi"] = None
    row["partial_fill_scenarios"] = [{"filled_qty": item["executable_qty"],
                                      "residual_qty": qty - item["executable_qty"],
                                      "action": "review residual manually"} for item in per_leg]
    row["break_even"] = {"value": None, "status": "UNKNOWN",
                         "reason": "fees or settlement inputs are unverified"}
    row["mapping_evidence"] = {"status": row.get("mapping", "UNKNOWN"), "evidence": None}
    row["settlement_evidence"] = {"status": row.get("settlement", "UNKNOWN"), "evidence": None}
    row["freshness"] = [{"market_id": leg.get("market_id"), "source": book.get("source"),
                         "observed_at": book.get("observed_at") or next((item.get("observed_at") for item in book.get("outcomes", []) if item.get("outcome_id") == leg.get("outcome")), None),
                         "age_s": book.get("age_s") if book.get("age_s") is not None else next((item.get("age_s") for item in book.get("outcomes", []) if item.get("outcome_id") == leg.get("outcome")), None)}
                        for leg, book in zip(row.get("legs", []), books)]
    mapping_ids = set(row.get("mapping_ids", []))
    row["mapping_evidence"] = {"status": row.get("mapping", "UNKNOWN"),
                                "matches": [item for item in data.get("mappings", [])
                                            if str(item.get("sig_market_id")) in mapping_ids or
                                            str(item.get("reference_market_id")) in mapping_ids]}
    row["settlement_evidence"] = {"status": row.get("settlement", "UNKNOWN"),
                                   "evidence": "exhaustive.txt" if row.get("strategy") == "complete_set" else None}
    return row


def _connect_readonly(path):
    path = Path(path)
    if not path.exists():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def _book_row(row, levels):
    return {"venue": row[0], "market_id": row[1], "outcome_id": row[2],
            "observed_at": row[3], "age_s": _age(row[3]), "source": row[4],
            "best_bid": row[5], "best_ask": row[6], "best_bid_size": row[7],
            "best_ask_size": row[8], "bids": levels["bids"], "asks": levels["asks"]}


def read_book(source, venue, market_id):
    venue = venue.lower()
    if venue == "sig":
        snapshot = source.get()
        levels = snapshot.levels.get(int(market_id))
        if levels is None:
            return {"venue": venue, "market_id": str(market_id), "outcomes": [], "missing": True}
        from arb_engine import Book
        book = Book.from_levels(int(market_id), levels)
        yes = {"outcome_id": "YES", "best_bid": _decimal(book.best_bid()), "best_ask": _decimal(book.best_ask()),
               "bids": [{"price": _decimal(p), "size": _decimal(q, 3)} for p, q in book.bids],
               "asks": [{"price": _decimal(p), "size": _decimal(q, 3)} for p, q in book.asks]}
        no = {"outcome_id": "NO",
              "best_bid": _decimal(1 - book.best_ask()) if book.best_ask() is not None else None,
              "best_ask": _decimal(1 - book.best_bid()) if book.best_bid() is not None else None,
              "bids": [{"price": _decimal(1 - p), "size": _decimal(q, 3)} for p, q in reversed(book.asks)],
              "asks": [{"price": _decimal(1 - p), "size": _decimal(q, 3)} for p, q in reversed(book.bids)]}
        return {"venue": venue, "market_id": str(market_id), "observed_at": snapshot.ts,
                "age_s": _age(snapshot.ts), "source": "sig-snapshot", "outcomes": [yes, no], "missing": False}
    from levels_read import latest_book
    outcomes = [latest_book(source.levels_db_path, venue, market_id, outcome)
                for outcome in ("YES", "NO")]
    outcomes = [item for item in outcomes if item is not None]
    if not outcomes:
        return {"venue": venue, "market_id": str(market_id), "outcomes": [], "missing": True,
                "error": "levels database not found"}
    for item in outcomes:
        for key in ("best_bid", "best_ask"):
            item[key] = _decimal(item[key])
        for side in ("bids", "asks"):
            for level in item[side]:
                level["price"] = _decimal(level["price"])
                level["size"] = _decimal(level["size"], 3)
    return {"venue": venue, "market_id": str(market_id), "outcomes": outcomes, "missing": False}


def read_history(source, market_id, venue=None, outcome="YES", start=None, end=None, limit=300):
    from levels_read import history
    venues = [venue] if venue else ["kalshi", "polymarket"]
    output = []
    for selected in venues:
        rows = history(source.levels_db_path, selected, market_id, outcome, start, end, limit)
        if rows:
            output.extend(rows)
    return {"market_id": str(market_id), "rows": output, "missing": not bool(output),
            "gaps_are_null": True, "venue": venue, "outcome": outcome.upper(), "limit": limit}
