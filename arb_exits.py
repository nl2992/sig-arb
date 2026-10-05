"""
arb_exits.py — take arb profit before settlement.

A held complete set pays a fixed amount at settlement: No on every outcome of a race
(from SELL_ALL) pays n-1 per set, Yes on every outcome (BUY_ALL) pays 1. It can also be
sold back now: No sets by buying Yes on every leg at the asks, Yes sets by selling Yes into
the bids. Unwind when that is profitable on the sets' own cost and banks at least
`min_share` of the settlement profit; the capital then goes back to new arbs.

Sets come from the arb's own fills (positions.arb_costs), capped by the account's holding on
that side (SIG nets Yes and No in one market). Every leg needs `depth_ratio` x our size at or
better than its limit, as for entries, so one competitor cannot leave the unwind one-sided.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from arb_engine import ArbResult, Book, LegFill


def complete_sets(ids: List[int], arb: Dict[int, list], acct: Dict[int, float]) -> Optional[tuple]:
    """(sign, sets, cost per set) of the complete sets held across `ids`, or None.
    sign -1: No on every leg; +1: Yes on every leg."""
    if len(ids) < 2:
        return None
    rows = [arb.get(m) for m in ids]
    if any(not r or abs(r[0]) < 1 for r in rows):
        return None
    sign = 1 if rows[0][0] > 0 else -1
    if any((r[0] > 0) != (sign > 0) for r in rows):
        return None
    sets = min(abs(r[0]) for r in rows)
    sets = min([sets] + [max(0.0, acct.get(m, 0.0) * sign) for m in ids])
    return sign, float(int(sets)), sum(r[1] / abs(r[0]) for r in rows)


def _walk(levels, qty: float) -> Optional[tuple]:
    """(vwap, worst price) of taking `qty` from a ladder, or None if it is too thin."""
    need, spent = qty, 0.0
    for p, q in levels:
        t = min(q, need)
        spent += t * p
        need -= t
        if need <= 1e-9:
            return spent / qty, p
    return None


def unwind_plan(race: str, ids: List[int], books: Dict[int, Book], arb: Dict[int, list],
                acct: Dict[int, float], *, min_share: float = 0.5, depth_ratio: float = 2.0,
                min_sets: int = 10) -> Optional[dict]:
    """The largest unwind of a race's complete sets in which every set is profitable on its
    cost and banks at least `min_share` of its settlement profit, or None."""
    held = complete_sets(ids, arb, acct)
    if not held or held[1] < min_sets:
        return None
    sign, sets, cost = held
    n = len(ids)
    settle = (n - 1) if sign < 0 else 1.0
    ladders = {m: (books[m].asks if sign < 0 else books[m].bids) for m in ids}
    floor = max(0.0, min_share * (settle - cost))

    def check(k):
        w = {m: _walk(ladders[m], k) for m in ids}
        if any(x is None for x in w.values()):
            return None
        s = sum(v for v, _ in w.values())
        value = n - s if sign < 0 else s                 # per set
        # the last set must pass on its own: one that loses on cost is worth more held
        worst = sum(x[1] for x in w.values())
        last = (n - worst if sign < 0 else worst) - cost
        return (value, w) if last > 1e-9 and last >= floor - 1e-9 else None

    if not check(min_sets):
        return None
    lo, hi = min_sets, int(sets)                         # the last set's profit only falls with size
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if check(mid) else (lo, mid - 1)
    k = lo
    for _ in range(6):
        value, w = check(k)
        cap = min(sum(q for p, q in ladders[m] if (p <= w[m][1] + 1e-9 if sign < 0 else p >= w[m][1] - 1e-9))
                  for m in ids) / depth_ratio
        if k <= cap + 1e-9:
            break
        k = int(cap)
        if k < min_sets or not check(k):
            return None
    else:
        return None
    value, w = check(k)
    return {"race": race, "sets": "NO" if sign < 0 else "YES", "qty": float(k),
            "yes_side": "BUY" if sign < 0 else "SELL", "n": n,
            "legs": [{"market_id": m, "exchange_id": books[m].exchange_id, "limit": w[m][1], "vwap": w[m][0]}
                     for m in ids],
            "cost": round(cost * k, 2), "proceeds": round(value * k, 2), "profit": round((value - cost) * k, 2),
            "settle_profit": round((settle - cost) * k, 2), "cost_per_set": cost, "settle_per_set": settle}


def as_result(plan: dict) -> ArbResult:
    """The unwind as an ArbResult, so bot.execute_parallel sends it like any arb."""
    k = plan["qty"]
    legs = [LegFill(l["market_id"], l["exchange_id"], [(l["limit"], k)]) for l in plan["legs"]]
    return ArbResult(race=plan["race"], direction="BUY_ALL" if plan["yes_side"] == "BUY" else "SELL_ALL",
                     qty=k, legs=legs, pnl=plan["profit"], capital=plan["cost"], top_edge=0.0,
                     marginal_edge=0.0, steps=[])


def repair_floor(plan: dict):
    """Worst YES price for one leg, given the other legs' fills, at which the unwind still
    breaks even on cost (the top-up of a short leg never goes past it)."""
    n, cost = plan["n"], plan["cost_per_set"]
    if plan["yes_side"] == "BUY":                        # proceeds n - sum(asks) >= cost
        return lambda others: n - cost - sum(others)
    return lambda others: cost - sum(others)             # proceeds sum(bids) >= cost
