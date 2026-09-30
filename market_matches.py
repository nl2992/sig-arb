"""Explicit, reviewable cross-venue match registry."""
from __future__ import annotations

import json
import pathlib
import datetime as dt
from typing import Iterable, List

from crossvenue_models import MarketMatch

STATUSES = {"PROPOSED", "REVIEW_REQUIRED", "APPROVED", "REJECTED", "REVOKED"}


def load_registry(path: str | pathlib.Path) -> List[MarketMatch]:
    payload = json.loads(pathlib.Path(path).read_text())
    if isinstance(payload, list):
        rows = payload
    else:
        rows = payload.get("matches", [])
    matches = []
    seen = set()
    for row in rows:
        status = str(row.get("status", "REVIEW_REQUIRED")).upper()
        if status not in STATUSES:
            raise ValueError(f"invalid match status: {status}")
        match = MarketMatch(
            sig_market_id=int(row["sig_market_id"]),
            reference_venue=str(row["reference_venue"]).lower(),
            reference_market_id=str(row["reference_market_id"]),
            reference_outcome_id=str(row.get("reference_outcome_id", "YES")).upper(),
            outcome_relation=str(row.get("outcome_relation", "SAME")),
            confidence=row.get("confidence"), status=status,
            reviewed_at=row.get("reviewed_at"), evidence=row.get("evidence"),
        )
        key = (match.sig_market_id, match.reference_venue, match.reference_market_id)
        if key in seen:
            raise ValueError(f"conflicting duplicate mapping: {key}")
        seen.add(key)
        if match.status == "APPROVED":
            if not match.evidence or not match.reviewed_at:
                raise ValueError("approved mapping requires evidence and reviewed_at")
            try:
                dt.datetime.fromisoformat(match.reviewed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("approved mapping has invalid reviewed_at") from exc
        matches.append(match)
    return matches


def approved_matches(matches: Iterable[MarketMatch]) -> List[MarketMatch]:
    return [m for m in matches if m.status == "APPROVED"]
