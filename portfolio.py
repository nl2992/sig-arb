"""Read-only portfolio snapshots and normalization for reconciliation.

The adapter deliberately does not infer an account position from an unknown
venue payload. Unknown rows are retained and mark the snapshot unsafe to use
for execution decisions.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable


def _first(row: dict, names: tuple[str, ...]):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_sig_holdings(rows: Iterable[dict]) -> tuple[dict[str, float], list[dict]]:
    positions, unparsed = {}, []
    for row in rows:
        if not isinstance(row, dict):
            unparsed.append({"row": row, "reason": "not_an_object"})
            continue
        market_id = _first(row, ("marketId", "market_id", "market", "id"))
        outcome = _first(row, ("outcome", "outcomeId", "outcome_id", "side", "positionType"))
        quantity = _first(row, ("netPosition", "net_position", "quantity", "qty", "contracts", "amount"))
        qty = _number(quantity)
        if market_id is None or outcome is None or qty is None:
            unparsed.append({"row": row, "reason": "missing_market_outcome_or_quantity"})
            continue
        positions[f"sig:{market_id}:{str(outcome).upper()}"] = qty
    return positions, unparsed


def normalize_sig_orders(rows: Iterable[dict]) -> tuple[set[str], list[dict]]:
    open_orders, unparsed = set(), []
    closed = {"CANCELLED", "CANCELED", "FILLED", "SETTLED", "EXPIRED", "REJECTED"}
    for row in rows:
        if not isinstance(row, dict):
            unparsed.append({"row": row, "reason": "not_an_object"})
            continue
        order_id = _first(row, ("id", "orderId", "order_id", "exchangeId"))
        status = str(_first(row, ("status", "state")) or "OPEN").upper()
        if order_id is None:
            unparsed.append({"row": row, "reason": "missing_order_id"})
        elif status not in closed:
            open_orders.add(f"sig:{order_id}")
    return open_orders, unparsed


def normalize_sig_portfolio(*, balance: float | None, holdings: Iterable[dict], orders: Iterable[dict]) -> dict:
    positions, holding_errors = normalize_sig_holdings(holdings)
    open_orders, order_errors = normalize_sig_orders(orders)
    unparsed = holding_errors + order_errors
    return {
        "venue": "sig", "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "cash": balance, "positions": positions,
        "open_orders": sorted(open_orders), "unparsed": unparsed,
        "status": "UNVERIFIED_SCHEMA" if unparsed else "NORMALIZED",
        "kill_switch_required": bool(unparsed),
        "read_only": True,
    }


def fetch_sig_portfolio(client, markets: list[dict]) -> dict:
    """Fetch SIG account state using read-only client methods only."""
    holdings, orders = [], []
    for market in markets:
        market_id = int(market["id"])
        holdings.extend(client.holdings(market_id))
        orders.extend(client.my_orders(market_id))
    return normalize_sig_portfolio(balance=client.balance(), holdings=holdings, orders=orders)
