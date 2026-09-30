"""Small, JSON-safe records shared by the cross-venue research scanner."""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Any, Dict, List, Optional


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    return str(value)


@dataclasses.dataclass(frozen=True)
class MarketMetadata:
    venue: str
    market_id: str
    event_id: Optional[str]
    title: str
    outcomes: List[Dict[str, str]]
    rules_text: Optional[str] = None
    rules_source_url: Optional[str] = None
    close_ts: Optional[str] = None
    resolution_ts: Optional[str] = None
    status: str = "UNKNOWN"
    volume: Optional[float] = None
    observed_at: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class PriceObservation:
    venue: str
    market_id: str
    outcome_id: str
    observed_at: str
    source_ts: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    volume: Optional[float] = None
    source: str = "unknown"
    price_basis: str = "unknown"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def reference_price(self) -> Optional[float]:
        if self.price_basis == "last":
            return self.last
        if self.price_basis == "mid":
            if self.bid is None or self.ask is None:
                return None
            return (self.bid + self.ask) / 2
        if self.price_basis == "outcome_price":
            return self.last
        return None

    def valid_reference_price(self) -> Optional[float]:
        value = self.reference_price()
        return value if value is not None and math.isfinite(value) and 0 <= value <= 1 else None


@dataclasses.dataclass(frozen=True)
class MarketMatch:
    sig_market_id: int
    reference_venue: str
    reference_market_id: str
    reference_outcome_id: str
    outcome_relation: str = "SAME"
    confidence: Optional[float] = None
    status: str = "REVIEW_REQUIRED"
    reviewed_at: Optional[str] = None
    evidence: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MovementReport:
    candidates: List[dict]
    diagnostics: List[dict]

    def to_dict(self) -> dict:
        return {"candidates": self.candidates, "diagnostics": self.diagnostics}


def parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ts(value: Any) -> Optional[str]:
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            numeric = None
        if numeric is not None:
            if numeric > 100_000_000_000:
                numeric /= 1000
            return dt.datetime.fromtimestamp(numeric, dt.timezone.utc).isoformat()
    return _iso(value)
