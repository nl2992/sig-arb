"""Guardrails and transparent bounds for national control markets.

These are probability bounds, not a fair-value model. A fair value requires a
joint distribution for race outcomes; marginal race prices alone are not enough.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

RACE_RE = re.compile(r"^Will the (Republican|Democratic|Independent) Party win the (.+)\?$")


@dataclass(frozen=True)
class WordingCase:
    name: str
    status: str
    question: str
    consequence: str


WORDING_REGISTER = (
    WordingCase("Senate 50-50", "UNRESOLVED",
                "Does 'majority of seats' award a 50-50 Senate to the party whose VP breaks ties?",
                "YES prices cannot be treated as a clean control arb until answered."),
    WordingCase("Independent winner", "UNRESOLVED",
                "If an Independent wins a listed race, do both Republican and Democratic contracts resolve NO?",
                "Both sides may lose; complement arb assumptions fail."),
    WordingCase("Ohio special election", "CHECK INVENTORY",
                "Which contract, if any, represents the Ohio special election?",
                "A seat-count model using listed races may omit Ohio."),
)


def senate_inventory(markets: Iterable[dict]) -> dict:
    rows = [m for m in markets if "Senate" in m.get("title", "")]
    state_races = {m["title"].split(" win the ", 1)[-1].rstrip("?") for m in rows
                   if "U.S. Senate" not in m.get("title", "")}
    return {"market_count": len(rows), "race_count": len(state_races),
            "control_markets": [m["id"] for m in rows if "U.S. Senate" in m.get("title", "")],
            "state_races": sorted(state_races)}


def probability_bounds(race_win_probabilities: Iterable[float]) -> tuple[float, float]:
    """Fréchet bounds for the probability that at least one race wins.

    For a control threshold, use this only as a coarse sanity guard. It does
    not model the probability of reaching a seat count.
    """
    ps = list(race_win_probabilities)
    if not ps or any(p < 0 or p > 1 for p in ps):
        raise ValueError("probabilities must be between 0 and 1")
    return max(0.0, sum(ps) - len(ps) + 1), min(1.0, sum(ps))


def control_readiness(markets: Iterable[dict]) -> dict:
    inv = senate_inventory(markets)
    inv["wording_register"] = [c.__dict__ for c in WORDING_REGISTER]
    inv["ready_for_riskless_control_arb"] = False
    inv["reason"] = "50-50 and Independent-winner settlement wording require organizer confirmation."
    return inv


if __name__ == "__main__":
    from sig_client import Client
    import json
    print(json.dumps(control_readiness(Client().markets()), indent=2))
