"""Read-only public adapters for Kalshi and Polymarket market inventories.

These adapters intentionally expose metadata and indicative prices only. They
do not contain authentication or order endpoints.
"""
from __future__ import annotations

import datetime as dt
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

import requests

from crossvenue_models import MarketMetadata, PriceObservation, normalize_ts, parse_float


def load_targeted_market_ids(path="docs/market-links.csv") -> dict[str, list[str]]:
    """Load native venue IDs for the fixed SIG research universe."""
    result = {"kalshi": [], "polymarket": []}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            for venue in result:
                market_id = str(row.get(f"{venue}_market_id") or "").strip()
                if market_id and market_id not in result[venue]:
                    result[venue].append(market_id)
    return result


def load_market_links(path="docs/market-links.csv") -> list[dict]:
    with open(path, newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


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
        return self._get_url(self.base_url + path, params)

    def _get_url(self, url: str, params: dict):
        for attempt in range(4):
            response = self.session.get(url, params=params, timeout=self.timeout)
            if getattr(response, "status_code", 200) != 429:
                response.raise_for_status()
                return response.json()
            if attempt == 3:
                response.raise_for_status()
            retry_after = parse_float(getattr(response, "headers", {}).get("Retry-After"))
            time.sleep(retry_after if retry_after is not None else 2 ** attempt)


class KalshiAdapter(PublicAdapter):
    venue = "kalshi"
    DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(base_url or os.getenv("KALSHI_PUBLIC_API", self.DEFAULT_BASE_URL), **kwargs)

    def markets(self, limit: int = 1000) -> List[MarketMetadata]:
        rows, cursor = [], ""
        while limit <= 0 or len(rows) < limit:
            page_size = 200 if limit <= 0 else min(200, limit - len(rows))
            params = {"status": "open", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("/markets", **params)
            page = payload.get("markets", [])
            rows.extend(page)
            next_cursor = payload.get("cursor", "")
            if not page or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        observed = _now()
        return [self._market(item, observed) for item in (rows if limit <= 0 else rows[:limit])]

    def markets_by_ids(self, market_ids: Iterable[str]) -> List[MarketMetadata]:
        observed = _now()
        out = []
        for market_id in market_ids:
            try:
                payload = self._get(f"/markets/{market_id}")
            except requests.RequestException:
                continue
            item = payload.get("market", payload) if isinstance(payload, dict) else {}
            if item:
                out.append(self._market(item, observed))
        return out

    def observations(self, limit: int = 1000, markets: List[MarketMetadata] | None = None) -> List[PriceObservation]:
        market_rows = markets if markets is not None else self.markets(limit)
        observed = _now()
        out = []
        for market in market_rows:
            item = market.raw or {}
            out.append(PriceObservation(
                venue=self.venue, market_id=str(item.get("ticker")), outcome_id="YES",
                observed_at=observed, source_ts=normalize_ts(item.get("updated_time")),
                bid=parse_float(item.get("yes_bid_dollars")),
                ask=parse_float(item.get("yes_ask_dollars")),
                bid_size=parse_float(item.get("yes_bid_size_fp")),
                ask_size=parse_float(item.get("yes_ask_size_fp")),
                last=parse_float(item.get("last_price_dollars")),
                volume=parse_float(item.get("volume_fp")), source="kalshi-rest",
                price_basis="last" if item.get("last_price_dollars") not in (None, "") else "mid",
            ))
        return out

    @staticmethod
    def _book_levels(value: Any) -> list[dict]:
        out = []
        for level in value if isinstance(value, list) else []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price, size = parse_float(level[0]), parse_float(level[1])
            if price is not None and size is not None and 0 <= price <= 1 and size >= 0:
                out.append({"price": price, "size": size})
        return out

    def books(self, markets: List[MarketMetadata], depth: int = 100) -> List[dict]:
        """Fetch public YES/NO bid ladders and derive complementary asks."""
        def fetch_one(market):
            try:
                payload = self._get(f"/markets/{market.market_id}/orderbook", depth=depth)
            except requests.RequestException:
                return []
            orderbook = payload.get("orderbook_fp", {}) if isinstance(payload, dict) else {}
            if (not isinstance(orderbook, dict) or
                    not isinstance(orderbook.get("yes_dollars"), list) or
                    not isinstance(orderbook.get("no_dollars"), list)):
                return []
            yes_bids = self._book_levels(orderbook.get("yes_dollars"))
            no_bids = self._book_levels(orderbook.get("no_dollars"))
            observed = _now()
            rows = []

            def complement(levels):
                return [{"price": round(1 - row["price"], 10), "size": row["size"]}
                        for row in reversed(levels)]

            for outcome, bids, asks in (("YES", yes_bids, complement(no_bids)),
                                        ("NO", no_bids, complement(yes_bids))):
                bids = sorted(bids, key=lambda row: row["price"], reverse=True)
                asks = sorted(asks, key=lambda row: row["price"])
                rows.append({
                    "venue": self.venue, "market_id": market.market_id,
                    "outcome_id": outcome, "observed_at": observed,
                    "bids": bids, "asks": asks,
                    "best_bid": bids[0]["price"] if bids else None,
                    "best_ask": asks[0]["price"] if asks else None,
                    "best_bid_size": bids[0]["size"] if bids else None,
                    "best_ask_size": asks[0]["size"] if asks else None,
                    "source": "kalshi-orderbook",
                })
            return rows

        workers = max(1, int(os.getenv("VENUE_BOOK_WORKERS", "8")))
        out = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for rows in executor.map(fetch_one, markets):
                out.extend(rows)
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
    DEFAULT_CLOB_URL = "https://clob.polymarket.com"

    def __init__(self, base_url: Optional[str] = None, clob_base_url: Optional[str] = None, **kwargs):
        super().__init__(base_url or os.getenv("POLYMARKET_PUBLIC_API", self.DEFAULT_BASE_URL), **kwargs)
        self.clob_base_url = (clob_base_url or os.getenv("POLYMARKET_CLOB_API", self.DEFAULT_CLOB_URL)).rstrip("/")

    def _clob_get(self, path: str, **params):
        return self._get_url(self.clob_base_url + path, params)

    @staticmethod
    def _levels(value: Any) -> list[dict]:
        if not isinstance(value, list):
            return []
        out = []
        for level in value:
            if isinstance(level, dict):
                price, size = level.get("price"), level.get("size")
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            else:
                continue
            p, q = parse_float(price), parse_float(size)
            if p is not None and q is not None and p >= 0 and q >= 0:
                out.append({"price": p, "size": q})
        return out

    def books(self, markets: List[MarketMetadata], limit: int = 100) -> List[dict]:
        """Fetch bounded public CLOB books; failures are isolated per token."""
        selected = markets if limit <= 0 else markets[:limit]
        def fetch_one(market):
            rows = []
            raw = market.raw or {}
            token_ids = _json_list(raw.get("clobTokenIds"))
            outcomes = [x["outcome_id"] for x in market.outcomes]
            for index, token_id in enumerate(token_ids):
                try:
                    payload = self._clob_get("/book", token_id=str(token_id))
                except requests.RequestException:
                    continue
                if (not isinstance(payload, dict) or
                        not isinstance(payload.get("bids"), list) or
                        not isinstance(payload.get("asks"), list)):
                    continue
                bids = self._levels(payload.get("bids"))
                asks = self._levels(payload.get("asks"))
                bids.sort(key=lambda x: x["price"], reverse=True)
                asks.sort(key=lambda x: x["price"])
                rows.append({
                    "venue": self.venue, "market_id": market.market_id,
                    "outcome_id": outcomes[index] if index < len(outcomes) else str(index),
                    "token_id": str(token_id), "observed_at": _now(),
                    "source_ts": normalize_ts(payload.get("timestamp")),
                    "bids": bids, "asks": asks,
                    "best_bid": bids[0]["price"] if bids else None,
                    "best_ask": asks[0]["price"] if asks else None,
                    "best_bid_size": bids[0]["size"] if bids else None,
                    "best_ask_size": asks[0]["size"] if asks else None,
                    "source": "polymarket-clob",
                })
            return rows

        workers = max(1, int(os.getenv("VENUE_BOOK_WORKERS", "8")))
        out = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for rows in executor.map(fetch_one, selected):
                out.extend(rows)
        return out

    def markets(self, limit: int = 1000) -> List[MarketMetadata]:
        rows, offset = [], 0
        page_size = 1000 if limit <= 0 else min(1000, limit)
        while limit <= 0 or len(rows) < limit:
            payload = self._get("/markets", active="true", closed="false",
                                limit=page_size, offset=offset)
            page = payload if isinstance(payload, list) else payload.get("markets", [])
            rows.extend(page)
            if not page or len(page) < page_size:
                break
            offset += len(page)
        observed = _now()
        return [self._market(item, observed) for item in (rows if limit <= 0 else rows[:limit])]

    def markets_by_ids(self, market_ids: Iterable[str]) -> List[MarketMetadata]:
        observed = _now()
        out = []
        for market_id in market_ids:
            try:
                payload = self._get(f"/markets/{market_id}")
            except requests.RequestException:
                continue
            item = payload if isinstance(payload, dict) else {}
            if item:
                out.append(self._market(item, observed))
        return out

    def observations(self, limit: int = 1000, markets: List[MarketMetadata] | None = None) -> List[PriceObservation]:
        market_rows = markets if markets is not None else self.markets(limit)
        observed = _now()
        out = []
        for market in market_rows:
            item = market.raw or {}
            prices = _json_list(item.get("outcomePrices"))
            outcomes = _json_list(item.get("outcomes")) or ["Yes", "No"]
            for index, price in enumerate(prices):
                outcome = str(outcomes[index] if index < len(outcomes) else index)
                out.append(PriceObservation(
                    venue=self.venue, market_id=str(item.get("id")), outcome_id=outcome.upper(),
                    observed_at=observed, source_ts=normalize_ts(item.get("updatedAt")),
                    bid=parse_float(item.get("bestBid")), ask=parse_float(item.get("bestAsk")),
                    bid_size=parse_float(item.get("bestBidSize")), ask_size=parse_float(item.get("bestAskSize")),
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


def fetch_public(venues: Iterable[str], limit: int = 1000, market_ids: dict | None = None) -> dict:
    """Fetch public inventories and indicative observations for named venues."""
    adapters = {"kalshi": KalshiAdapter(), "polymarket": PolymarketAdapter()}
    result = {}
    for venue in venues:
        adapter = adapters[venue]
        ids = (market_ids or {}).get(venue)
        market_rows = adapter.markets_by_ids(ids) if ids is not None else adapter.markets(limit)
        observation_rows = adapter.observations(limit, market_rows)
        books = []
        if venue == "kalshi":
            depth_limit = int(os.getenv("KALSHI_DEPTH_LIMIT", "100"))
            books = adapter.books(market_rows, depth_limit)
            best = {(b["market_id"], b["outcome_id"].upper()): b for b in books}
            observation_rows = [PriceObservation(
                **{**o.to_dict(),
                   "bid": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_bid", o.bid),
                   "ask": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_ask", o.ask),
                   "bid_size": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_bid_size", o.bid_size),
                   "ask_size": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_ask_size", o.ask_size),
                   "source": "kalshi-orderbook" if (o.market_id, o.outcome_id.upper()) in best else o.source}
            ) for o in observation_rows]
        elif venue == "polymarket":
            depth_limit = int(os.getenv("POLYMARKET_DEPTH_LIMIT", "0"))
            books = adapter.books(market_rows, depth_limit)
            best = {(b["market_id"], b["outcome_id"].upper()): b for b in books}
            observation_rows = [PriceObservation(
                **{**o.to_dict(),
                   "bid": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_bid", o.bid),
                   "ask": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_ask", o.ask),
                   "bid_size": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_bid_size", o.bid_size),
                   "ask_size": best.get((o.market_id, o.outcome_id.upper()), {}).get("best_ask_size", o.ask_size),
                   "source": "polymarket-clob" if (o.market_id, o.outcome_id.upper()) in best else o.source}
            ) for o in observation_rows]
        returned_ids = {str(m.market_id) for m in market_rows}
        requested_ids = {str(value) for value in ids} if ids is not None else returned_ids
        expected_book_keys = {(str(m.market_id), str(outcome["outcome_id"]).upper())
                              for m in market_rows for outcome in m.outcomes}
        returned_book_keys = {(str(book.get("market_id")), str(book.get("outcome_id", "")).upper())
                              for book in books}
        missing_book_keys = sorted(expected_book_keys - returned_book_keys)
        book_ids = {market_id for market_id, _ in returned_book_keys}
        missing_book_ids = sorted(returned_ids - book_ids)
        scope_complete = (ids is not None and bool(ids)) or (ids is None and limit <= 0)
        result[venue] = {
            "markets": [m.to_dict() for m in market_rows],
            "observations": [o.to_dict() for o in observation_rows],
            "books": books,
            "book_coverage": "ALL_RETURNED_MARKETS" if venue == "kalshi" or depth_limit <= 0 else "BOUNDED_MARKET_COUNT",
            "inventory_coverage": "TARGETED_SIG_UNIVERSE" if ids is not None else
                                  "ALL_ACTIVE_INVENTORY" if limit <= 0 else "BOUNDED_MARKET_COUNT",
            "coverage": {
                "requested_market_count": len(requested_ids),
                "returned_market_count": len(returned_ids),
                "missing_market_ids": sorted(requested_ids - returned_ids),
                "returned_book_market_count": len(book_ids),
                "missing_book_market_ids": missing_book_ids,
                "missing_book_outcomes": [{"market_id": market_id, "outcome_id": outcome_id}
                                           for market_id, outcome_id in missing_book_keys],
                "scope_complete": scope_complete,
                "complete": scope_complete and not (requested_ids - returned_ids) and not missing_book_keys,
            },
        }
    return result
