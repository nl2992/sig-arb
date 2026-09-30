"""Explicit, human-reviewed news circuit-breaker records.

This module intentionally does not assign sentiment or infer trade direction.
It only blocks research candidates when an operator has recorded an active,
time-bounded event against a SIG market or race.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

STATUSES = {"ACTIVE", "CLEARED"}


def _parse(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_circuit_breakers(path: str | Path) -> list[dict]:
    payload = json.loads(Path(path).read_text())
    rows = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("news circuit-breakers must contain an events list")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("reason"):
            raise ValueError("news circuit-breaker requires id and reason")
        status = str(row.get("status", "ACTIVE")).upper()
        if status not in STATUSES:
            raise ValueError(f"invalid news circuit-breaker status: {status}")
        market_ids = row.get("market_ids", [])
        if not isinstance(market_ids, list) or any(int(mid) <= 0 for mid in market_ids):
            raise ValueError("news circuit-breaker market_ids must be positive integers")
        expires = _parse(row.get("expires_at"))
        if expires and expires.tzinfo is None:
            raise ValueError("news circuit-breaker expires_at must include timezone")
        out.append({**row, "status": status, "market_ids": [int(mid) for mid in market_ids]})
    return out


def active_breakers(path: str | Path, *, now=None) -> dict[int, list[dict]]:
    now = now or dt.datetime.now(dt.timezone.utc)
    result = {}
    for row in load_circuit_breakers(path):
        expires = _parse(row.get("expires_at"))
        if row["status"] != "ACTIVE" or expires and expires <= now:
            continue
        for market_id in row["market_ids"]:
            result.setdefault(market_id, []).append(row)
    return result
