"""Generate a current Markdown index of SIG markets and reviewed venue mappings."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re

import requests

from sig_client import Client


SIG_BASE = "https://sig.thesuper.market/markets"
POLYMARKET_BASE = "https://gamma-api.polymarket.com"


def _polymarket_inventory() -> list[dict]:
    rows = []
    for offset in range(0, 10000, 100):
        response = requests.get(
            f"{POLYMARKET_BASE}/markets",
            params={"active": "true", "closed": "false", "limit": 100, "offset": offset},
            timeout=30,
        )
        if response.status_code == 422:
            break
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or not page:
            break
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < 100:
            break
    return rows


def _polymarket_candidates(title: str, inventory: list[dict]) -> list[dict]:
    match = re.fullmatch(
        r"Will the (Republican|Democratic|Independent) Party win the (.+) (Senate|Governor)\?",
        title,
    )
    if not match:
        return []
    party, state, chamber = (part.lower() for part in match.groups())
    party_forms = {
        "republican": ("republicans", "republican"),
        "democratic": ("democrats", "democrat"),
        "independent": ("an independent", "independent"),
    }[party]
    candidates = []
    for row in inventory:
        question = str(row.get("question", ""))
        question_lower = question.lower()
        expected = re.compile(
            rf"^will (?:the )?{re.escape(party_forms[0])} win the "
            rf"{re.escape(state.lower())} {chamber.lower()} race in 2026\?$"
        )
        expected_alt = re.compile(
            rf"^will (?:the )?{re.escape(party_forms[1])} win the "
            rf"{re.escape(state.lower())} {chamber.lower()} race in 2026\?$"
        )
        if not (expected.fullmatch(question_lower) or expected_alt.fullmatch(question_lower)):
            continue
        event = (row.get("events") or [{}])[0]
        event_slug = event.get("slug")
        if event_slug:
            candidates.append({
                "id": str(row.get("id")),
                "question": question,
                "url": f"https://polymarket.com/event/{event_slug}",
            })
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/market-links.md")
    args = parser.parse_args()

    markets = sorted(Client().markets(), key=lambda row: int(row["id"]), reverse=True)
    polymarket = _polymarket_inventory()
    polymarket_map = {
        int(market["id"]): _polymarket_candidates(str(market.get("title", "")), polymarket)
        for market in markets
    }
    pm_count = sum(bool(rows) for rows in polymarket_map.values())
    lines = [
        "# SIG Market Links",
        "",
        "> Generated from the authenticated SIG market universe. This file is a point-in-time index; run the generator again when the universe changes.",
        "",
        f"Generated: `{dt.datetime.now(dt.timezone.utc).isoformat()}`",
        f"Market count: **{len(markets)}**",
        f"Polymarket candidate links found: **{pm_count}**",
        "Production cross-venue mappings: **0 approved** (the registry remains intentionally empty until settlement rules are manually verified).",
        "",
        "## Venue Links",
        "",
        "- [SIG market universe](https://sig.thesuper.market/markets)",
        "- [Kalshi](https://kalshi.com/)",
        "- [Polymarket](https://polymarket.com/)",
        "- [Kalshi public API documentation](https://docs.kalshi.com/api-reference/events/get-multivariate-events)",
        "- [Polymarket real-time market data documentation](https://docs.polymarket.com/market-data/realtime-data)",
        "",
        "## Three-Venue Mapping Table",
        "",
        "Each row has the intended SIG, Kalshi, and Polymarket slots. Polymarket links marked `candidate` are title/market-structure matches found through the public API and still require settlement review. Kalshi is shown as unresolved where no corresponding contract was found in the current public open-market inventory.",
        "",
        "| SIG ID | Event | SIG | Kalshi | Polymarket | Status |",
        "|---:|---|---|---|---|---|",
    ]
    for market in markets:
        market_id = int(market["id"])
        title = str(market.get("title", "")).replace("|", "\\|")
        sig_link = f"[SIG]({SIG_BASE}/{market_id})"
        candidates = polymarket_map[market_id]
        if len(candidates) == 1:
            poly_link = f"[candidate]({candidates[0]['url']})"
            status = "Polymarket candidate; Kalshi unresolved"
        elif len(candidates) > 1:
            poly_link = "Ambiguous candidates"
            status = "Manual review required"
        else:
            poly_link = "Not found"
            status = "No venue match found"
        lines.append(
            f"| {market_id} | {title} | {sig_link} | Not found | {poly_link} | {status} |"
        )

    lines += [
        "",
        "## Production Registry",
        "",
        "No production mappings are currently approved in `fixtures/crossvenue/matches.json`. The report links are discovery output; they do not authorize trading or mean the contracts have identical settlement rules.",
    ]

    lines += [
        "",
        "## Synthetic Test Mapping",
        "",
        "The scanner test fixture contains a synthetic Kalshi mapping for SIG market 386. It is not a live venue contract and is included only to demonstrate the scanner behavior.",
        "",
        "| SIG market | SIG link | Reference | Status |",
        "|---:|---|---|---|",
        f"| 386 | [Open SIG market]({SIG_BASE}/386) | `FIXTURE-KX1` | Synthetic fixture only |",
        "",
        "## Refresh",
        "",
        "```bash",
        "PYTHONPATH=. python3 tools/generate_market_links.py",
        "```",
        "",
    ]
    output = pathlib.Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {output} ({len(markets)} SIG markets)")


if __name__ == "__main__":
    main()
