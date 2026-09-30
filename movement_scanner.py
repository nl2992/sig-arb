"""Pure two-hour cross-venue movement diagnostics.

Results are research candidates only. The module has no order client and no
side effect beyond returning JSON-safe records.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, List, Optional

from arb_engine import Book
from crossvenue_models import MarketMatch, MovementReport, PriceObservation


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _valid_review_timestamp(value: str, now: dt.datetime) -> bool:
    try:
        parsed = _parse(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed <= now


def _sig_book(snapshot, market_id: int) -> Optional[Book]:
    levels = snapshot.levels.get(market_id)
    return Book.from_levels(market_id, levels) if levels is not None else None


def scan_movements(sig_snapshot, reference_observations: Iterable[PriceObservation],
                   approved: Iterable[MarketMatch], *, lookback_minutes: int = 120,
                   min_move_pp: float = 5.0, min_gap_pp: float = 1.0,
                   max_age_seconds: int = 120, max_skew_seconds: int = 60,
                   research_buffer_pp: float = 0.0) -> MovementReport:
    if lookback_minutes <= 0 or min_move_pp < 0 or min_gap_pp < 0 or max_age_seconds < 0 or max_skew_seconds < 0:
        raise ValueError("invalid movement scanner parameters")
    now = _parse(sig_snapshot.ts)
    observations = list(reference_observations)
    candidates, diagnostics = [], []
    for match in approved:
        if match.status != "APPROVED":
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "UNAPPROVED_MATCH"})
            continue
        if match.outcome_relation != "SAME":
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "UNSUPPORTED_RELATION"})
            continue
        if not match.evidence or not match.reviewed_at or not _valid_review_timestamp(match.reviewed_at, now):
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "INCOMPLETE_REVIEW"})
            continue
        obs = [o for o in observations
               if o.venue == match.reference_venue and o.market_id == match.reference_market_id
               and o.outcome_id.upper() == match.reference_outcome_id.upper()]
        if not obs:
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "NO_REFERENCE_DATA"})
            continue
        valid = []
        for item in obs:
            try:
                observed_at = _parse(item.observed_at)
                source_at = _parse(item.source_ts) if item.source_ts else None
            except (TypeError, ValueError):
                continue
            if observed_at.tzinfo is None or (source_at and source_at.tzinfo is None):
                continue
            if source_at and abs((observed_at - source_at).total_seconds()) > max_skew_seconds:
                continue
            if source_at and source_at > now:
                continue
            price = item.valid_reference_price()
            if price is not None:
                valid.append((item, observed_at, price))
        valid = [row for row in valid if row[1] <= now]
        if not valid:
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "INVALID_REFERENCE_DATA"})
            continue
        current_basis = max(valid, key=lambda row: row[1])[0].price_basis
        same_basis = [row for row in valid if row[0].price_basis == current_basis]
        current, current_at, current_price = max(same_basis, key=lambda row: row[1])
        baseline_cutoff = current_at - dt.timedelta(minutes=lookback_minutes)
        prior = [row for row in same_basis if baseline_cutoff - dt.timedelta(seconds=max_skew_seconds) <= row[1] <= baseline_cutoff]
        if not prior:
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "INSUFFICIENT_HISTORY"})
            continue
        baseline, baseline_at, base_price = max(prior, key=lambda row: row[1])
        age = (now - current_at).total_seconds()
        if base_price is None or age > max_age_seconds:
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "STALE_REFERENCE", "age_seconds": age})
            continue
        move_pp = round((current_price - base_price) * 100, 6)
        book = _sig_book(sig_snapshot, match.sig_market_id)
        sig_bid = book.best_bid() if book else None
        sig_ask = book.best_ask() if book else None
        direction = "BUY_YES" if move_pp >= min_move_pp else "BUY_NO" if move_pp <= -min_move_pp else None
        if direction is None:
            diagnostics.append({"sig_market_id": match.sig_market_id, "status": "REJECTED", "reason": "BELOW_MOVE_THRESHOLD", "move_pp": move_pp})
            continue
        sig_price = sig_ask if direction == "BUY_YES" else (1 - sig_bid if sig_bid is not None else None)
        ref_price = current_price if direction == "BUY_YES" else 1 - current_price
        gap_pp = round((ref_price - sig_price) * 100, 6) if sig_price is not None else None
        record = {"sig_market_id": match.sig_market_id, "reference_venue": match.reference_venue,
                  "reference_market_id": match.reference_market_id, "direction": direction,
                  "movement_pp": move_pp, "reference_price": current_price,
                  "baseline_price": base_price, "sig_price": sig_price, "gap_pp": gap_pp,
                  "status": "RESEARCH_CANDIDATE" if gap_pp is not None and gap_pp >= min_gap_pp + research_buffer_pp else "REJECTED",
                  "reason": None if gap_pp is not None and gap_pp >= min_gap_pp + research_buffer_pp else "INSUFFICIENT_SIG_GAP",
                  "observed_at": current.observed_at, "baseline_observed_at": baseline.observed_at,
                  "price_basis": current.price_basis, "research_only": True}
        diagnostics.append(record)
        if record["status"] == "RESEARCH_CANDIDATE":
            candidates.append(record)
    return MovementReport(candidates, diagnostics)
