"""Research-only relative-value diagnostics for mapped binary markets.

This is deliberately a diagnostic, not an execution rule. Binary contracts
can look cointegrated while resolving on different rules, so the mapping
registry remains the authority for whether a pair may be considered at all.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

from crossvenue_models import MarketMatch


def _ols_hedge(xs: list[float], ys: list[float]) -> float:
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    return sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom if denom else 1.0


def scan_pairs(history: Iterable[dict], matches: Iterable[MarketMatch], *, min_points: int = 10,
               z_threshold: float = 2.0) -> list[dict]:
    """Return rolling spread diagnostics for approved mapped pairs.

    ``history`` rows contain ``sig_price`` and ``reference_price`` sampled in
    the same scan cycle. The spread is reference minus beta times SIG.
    """
    grouped = defaultdict(list)
    for row in history:
        key = (int(row["sig_market_id"]), str(row["reference_venue"]),
               str(row["reference_market_id"]), str(row["reference_outcome_id"]).upper())
        if row.get("sig_price") is None or row.get("reference_price") is None:
            continue
        try:
            grouped[key].append((str(row["observed_at"]), float(row["sig_price"]),
                                 float(row["reference_price"])))
        except (KeyError, TypeError, ValueError):
            continue

    out = []
    for match in matches:
        if match.status != "APPROVED" or match.outcome_relation != "SAME":
            continue
        key = (match.sig_market_id, match.reference_venue, match.reference_market_id,
               match.reference_outcome_id.upper())
        rows = sorted(grouped.get(key, []))
        if len(rows) < min_points:
            out.append({"sig_market_id": match.sig_market_id, "reference_venue": match.reference_venue,
                        "reference_market_id": match.reference_market_id, "status": "INSUFFICIENT_HISTORY",
                        "points": len(rows), "research_only": True})
            continue
        rows = rows[-min_points * 6:]
        xs = [r[1] for r in rows]
        ys = [r[2] for r in rows]
        beta = _ols_hedge(xs, ys)
        spreads = [y - beta * x for x, y in zip(xs, ys)]
        mean = sum(spreads) / len(spreads)
        variance = sum((s - mean) ** 2 for s in spreads) / max(1, len(spreads) - 1)
        std = math.sqrt(variance)
        latest = spreads[-1]
        z = (latest - mean) / std if std else 0.0
        out.append({"sig_market_id": match.sig_market_id, "reference_venue": match.reference_venue,
                    "reference_market_id": match.reference_market_id, "observed_at": rows[-1][0],
                    "points": len(rows), "beta": round(beta, 6), "spread": round(latest, 6),
                    "mean_spread": round(mean, 6), "spread_std": round(std, 6), "z_score": round(z, 6),
                    "status": "RESEARCH_CANDIDATE" if abs(z) >= z_threshold else "STABLE",
                    "research_only": True})
    return out
