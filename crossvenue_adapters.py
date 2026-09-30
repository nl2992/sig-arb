"""Read-only public adapters for Kalshi and Polymarket market inventories.

These adapters intentionally expose metadata and indicative prices only. They
do not contain authentication or order endpoints.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, Iterable, List, Optional

import requests

from crossvenue_models import MarketMetadata, PriceObservation, normalize_ts, parse_float


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


class PublicAdapter:
    venue = "unknown"

    def __init__(self, base_url: str, timeout: float = 10, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.setdefault("User-Agent", "sig-arb-crossvenue/0.1")

    def _get(self, path: str, **params):
        response = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


class KalshiAdapter(PublicAdapter):
    venue = "kalshi"
    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(base_url or os.getenv("KALSHI_PUBLIC_API", self.DEFAULT_BASE_URL), **kwargs)

    def markets(self, limit: int = 1000) -> List[MarketMetadata]:
        payload = self._get("/markets", status="open", limit=limit)
        observed = _now()
        return [self._market(item, observed) for item in payload.get("markets", [])]

    def observations(self, limit: int = 1000) -> List[PriceObservation]:
        payload = self._get("/markets", status="open", limit=limit)
        observed = _now()
        out = []
        for item in payload.get("markets", []):
            out.append(PriceObservation(
                venue=self.venue, market_id=str(item.get("ticker")), outcome_id="YES",
                observed_at=observed, source_ts=normalize_ts(item.get("updated_time")),
                bid=parse_float(item.get("yes_bid_dollars")),
                ask=parse_float(item.get("yes_ask_dollars")),
                last=parse_float(item.get("last_price_dollars")),
                volume=parse_float(item.get("volume_fp")), source="kalshi-rest",
                price_basis="last" if item.get("last_price_dollars") not in (None, "") else "mid",
            ))
        return out

    def _market(self, item: dict, observed: str) -> MarketMetadata:
        return MarketMetadata(
            venue=self.venue, market_id=str(item.get("ticker")),
            event_id=item.get("event_ticker"), title=item.get("title", ""),
            outcomes=[{"outcome_id": "YES", "label": item.get("yes_sub_title", "Yes")},
                      {"outcome_id": "NO", "label": item.get("no_sub_title", "No")}],
            rules_text="\n".join(x for x in (item.get("rules_primary"), item.get("rules_secondary")) if x),
            close_ts=normalize_ts(item.get("close_time") or item.get("expiration_time")),
            resolution_ts=normalize_ts(item.get("expected_expiration_time")),
            status=str(item.get("status", "UNKNOWN")).upper(),
            volume=parse_float(item.get("volume_fp")), observed_at=observed, raw=item,
        )


class PolymarketAdapter(PublicAdapter):
    venue = "polymarket"
    DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(base_url or os.getenv("POLYMARKET_PUBLIC_API", self.DEFAULT_BASE_URL), **kwargs)

    def markets(self, limit: int = 1000) -> List[MarketMetadata]:
        payload = self._get("/markets", active="true", closed="false", limit=limit)
        observed = _now()
        return [self._market(item, observed) for item in (payload if isinstance(payload, list) else payload.get("markets", []))]

    def observations(self, limit: int = 1000) -> List[PriceObservation]:
        payload = self._get("/markets", active="true", closed="false", limit=limit)
        observed = _now()
        out = []
        for item in (payload if isinstance(payload, list) else payload.get("markets", [])):
            prices = _json_list(item.get("outcomePrices"))
            outcomes = _json_list(item.get("outcomes")) or ["Yes", "No"]
            for index, price in enumerate(prices):
                outcome = str(outcomes[index] if index < len(outcomes) else index)
                out.append(PriceObservation(
                    venue=self.venue, market_id=str(item.get("id")), outcome_id=outcome.upper(),
                    observed_at=observed, source_ts=normalize_ts(item.get("updatedAt")),
                    last=parse_float(price), volume=parse_float(item.get("volume")),
                    source="polymarket-gamma", price_basis="outcome_price",
                ))
        return out

    def _market(self, item: dict, observed: str) -> MarketMetadata:
        outcomes = _json_list(item.get("outcomes")) or ["Yes", "No"]
        return MarketMetadata(
            venue=self.venue, market_id=str(item.get("id")),
            event_id=str(item.get("conditionId")) if item.get("conditionId") else None,
            title=item.get("question", ""),
            outcomes=[{"outcome_id": str(x).upper(), "label": str(x)} for x in outcomes],
            rules_text=item.get("description"),
            close_ts=normalize_ts(item.get("endDate")),
            status="OPEN" if item.get("active") and not item.get("closed") else "CLOSED",
            volume=parse_float(item.get("volume")), observed_at=observed, raw=item,
        )


def fetch_public(venues: Iterable[str], limit: int = 1000) -> dict:
    """Fetch public inventories and indicative observations for named venues."""
    adapters = {"kalshi": KalshiAdapter(), "polymarket": PolymarketAdapter()}
    result = {}
    for venue in venues:
        adapter = adapters[venue]
        result[venue] = {
            "markets": [m.to_dict() for m in adapter.markets(limit)],
            "observations": [o.to_dict() for o in adapter.observations(limit)],
        }
    return result
