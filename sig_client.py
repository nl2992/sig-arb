"""
sig_client.py — thin HTTP client for sig.thesuper.market (read + trade).

Auth: the site uses your browser session cookie. Put the full Cookie header
value in a `.env` file next to this script:

    SIG_COOKIE=...paste from DevTools...
    SIG_TOURNAMENT=bda92870-621e-47b0-bc3c-3602c5c26f55   # optional

Never commit .env.
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import requests

from arb_engine import Book

BASE = "https://sig.thesuper.market"
DEFAULT_TOURNAMENT = "bda92870-621e-47b0-bc3c-3602c5c26f55"

# Flip to True only after you have captured ONE real order in DevTools
# (Network tab -> /api/trading/orders/place -> Payload) and checked that
# `place()` below sends the same fields.
PLACE_PAYLOAD_CONFIRMED = False


def load_env(path: str | os.PathLike = None) -> None:
    """Minimal .env loader (no python-dotenv dependency)."""
    p = pathlib.Path(path or pathlib.Path(__file__).with_name(".env"))
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Client:
    def __init__(self, cookie: str | None = None, tournament: str | None = None,
                 concurrency: int = 8, timeout: float = 10):
        load_env()
        self.cookie = cookie or os.environ.get("SIG_COOKIE", "")
        self.tournament = tournament or os.environ.get("SIG_TOURNAMENT", DEFAULT_TOURNAMENT)
        self.concurrency, self.timeout = concurrency, timeout
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "sig-arb/0.2"})
        if self.cookie:
            self.s.headers["Cookie"] = self.cookie

    # ---------------------------------------------------------------- http
    def _get(self, path: str, **params):
        r = self.s.get(BASE + path, params=params, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise PermissionError(f"{path} -> {r.status_code}. Is SIG_COOKIE set / still valid?")
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict):
        r = self.s.post(BASE + path, data=json.dumps(body), timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise RuntimeError(f"{path} {r.status_code}: {data}")
        return data

    # ---------------------------------------------------------------- read
    def markets(self) -> List[dict]:
        out, off = [], 0
        while off is not None:
            j = self._get("/api/markets/page-data", offset=off)
            out += j["markets"]
            off = j.get("nextOffset") if j["markets"] else None
        return out

    def levels(self, market_id: int) -> List[dict]:
        j = self._get(f"/api/markets/{market_id}/orders", marketId=market_id,
                      tournamentId=self.tournament)
        return j["levels"]

    def all_levels(self, ids: List[int]) -> Dict[int, List[dict]]:
        with ThreadPoolExecutor(self.concurrency) as ex:
            return dict(zip(ids, ex.map(self.levels, ids)))

    def books(self, ids: List[int]) -> Dict[int, Book]:
        return {i: Book.from_levels(i, L) for i, L in self.all_levels(ids).items()}

    def balance(self) -> float | None:
        try:
            j = self._get("/api/tournaments/member-balance", tournamentId=self.tournament)
        except Exception:
            return None
        for k in ("balance", "userBalance", "memberBalance", "cash"):
            if isinstance(j, dict) and k in j:
                return float(j[k])
        return None

    def my_orders(self, market_id: int) -> List[dict]:
        j = self._get(f"/api/markets/{market_id}/orders", marketId=market_id,
                      tournamentId=self.tournament)
        return j.get("myOrders", [])

    def holdings(self, market_id: int) -> list:
        j = self._get(f"/api/markets/{market_id}/live", tournamentId=self.tournament)
        return j.get("userHoldings", [])

    # --------------------------------------------------------------- trade
    def quote(self, exchange_id: int, order_type: str, price: float, qty: float) -> dict:
        return self._post("/api/trading/orders/quote", {
            "exchangeId": exchange_id, "orderType": order_type,
            "priceLimit": round(price, 4), "quantity": qty, "tournamentId": self.tournament})

    def place(self, market_id: int, exchange_id: int, order_type: str,
              price: float, qty: float, dry_run: bool = True) -> dict:
        """order_type BUY/SELL in YES terms (SELL YES == buy NO at 1-price).
        Payload inferred from the web client — see PLACE_PAYLOAD_CONFIRMED."""
        body = {"marketId": market_id, "exchangeId": exchange_id, "orderType": order_type,
                "priceLimit": round(price, 4), "quantity": qty, "isLimitOrder": True,
                "tournamentId": self.tournament, "idempotencyKey": str(uuid.uuid4())}
        if dry_run or not PLACE_PAYLOAD_CONFIRMED:
            return {"dryRun": True, "body": body}
        return self._post("/api/trading/orders/place", body)

    def cancel(self, order_id: str, dry_run: bool = True):
        if dry_run or not order_id:
            return {"dryRun": True}
        return self._post("/api/trading/orders/cancel",
                          {"orderId": order_id, "tournamentId": self.tournament})
