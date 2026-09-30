"""Venue-neutral portfolio reconciliation and fail-closed controls."""
from __future__ import annotations

from typing import Mapping


def _mismatch(name: str, expected: float, actual: float, tolerance: float) -> dict | None:
    delta = actual - expected
    if abs(delta) <= tolerance:
        return None
    return {"key": name, "expected": expected, "actual": actual,
            "delta": delta, "tolerance": tolerance}


def reconcile_positions(expected: Mapping[str, float], actual: Mapping[str, float], *, tolerance: float = 1e-9) -> dict:
    """Compare normalized net positions and fail closed on any unexplained delta."""
    keys = sorted(set(expected) | set(actual))
    mismatches = [item for key in keys
                  if (item := _mismatch(key, float(expected.get(key, 0)),
                                        float(actual.get(key, 0)), tolerance))]
    return {"status": "RECONCILED" if not mismatches else "MISMATCH",
            "mismatches": mismatches, "kill_switch_required": bool(mismatches)}


def reconcile_orders(expected_open: set[str], actual_open: set[str]) -> dict:
    missing = sorted(expected_open - actual_open)
    unexpected = sorted(actual_open - expected_open)
    mismatches = [{"kind": "missing", "order_id": x} for x in missing]
    mismatches += [{"kind": "unexpected", "order_id": x} for x in unexpected]
    return {"status": "RECONCILED" if not mismatches else "MISMATCH",
            "missing": missing, "unexpected": unexpected,
            "kill_switch_required": bool(mismatches)}


def reconcile_cash(expected: float, actual: float, *, tolerance: float = 0.01) -> dict:
    mismatch = _mismatch("cash", float(expected), float(actual), tolerance)
    return {"status": "RECONCILED" if mismatch is None else "MISMATCH",
            "mismatches": [] if mismatch is None else [mismatch],
            "kill_switch_required": mismatch is not None}


def reconcile_portfolio(*, expected_positions: Mapping[str, float], actual_positions: Mapping[str, float],
                        expected_open_orders: set[str], actual_open_orders: set[str],
                        expected_cash: float, actual_cash: float,
                        position_tolerance: float = 1e-9, cash_tolerance: float = 0.01) -> dict:
    positions = reconcile_positions(expected_positions, actual_positions, tolerance=position_tolerance)
    orders = reconcile_orders(expected_open_orders, actual_open_orders)
    cash = reconcile_cash(expected_cash, actual_cash, tolerance=cash_tolerance)
    checks = {"positions": positions, "orders": orders, "cash": cash}
    return {"status": "RECONCILED" if all(item["status"] == "RECONCILED" for item in checks.values()) else "MISMATCH",
            "checks": checks,
            "kill_switch_required": any(item["kill_switch_required"] for item in checks.values())}
