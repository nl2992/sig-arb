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
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


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
    national = re.fullmatch(
        r"Will the (Republican|Democratic) Party win the U\.S\. (House|Senate)\?",
        title,
    )
    if national:
        party, chamber = (part.lower() for part in national.groups())
        party_form = "republican party" if party == "republican" else "democratic party"
        candidates = []
        for row in inventory:
            question = str(row.get("question", ""))
            question_lower = question.lower()
            if (
                party_form in question_lower
                and f"control the {chamber.lower()} after the 2026 midterm elections" in question_lower
            ):
                event = (row.get("events") or [{}])[0]
                if event.get("slug"):
                    candidates.append({
                        "id": str(row.get("id")),
                        "question": question,
                        "url": f"https://polymarket.com/event/{event['slug']}",
                    })
        return candidates
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


def _kalshi_inventory() -> list[dict]:
    """Fetch Kalshi's event feed, which contains the election markets."""
    events = []
    cursor = ""
    for _ in range(100):
        params = {"limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(f"{KALSHI_BASE}/events", params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        page = payload.get("events", [])
        events.extend(row for row in page if isinstance(row, dict))
        cursor = payload.get("cursor", "")
        if not cursor or not page:
            break
    return events


def _kalshi_candidates(title: str, inventory: list[dict]) -> list[dict]:
    national = re.fullmatch(
        r"Will the (Republican|Democratic) Party win the U\.S\. (House|Senate)\?",
        title,
    )
    if national:
        party, chamber = (part.lower() for part in national.groups())
        series = "controlh" if chamber == "house" else "controls"
        event_slug = "house-winner" if chamber == "house" else "senate-winner"
        event_ticker = f"{series.upper()}-2026"
        party_label = "republican" if party == "republican" else "democratic"
        return [{
            "id": f"{event_ticker}-{party[0].upper()}",
            "event": event_ticker,
            "url": f"https://kalshi.com/markets/{series}/{event_slug}/{event_ticker.lower()}",
        }] if any(
            event.get("event_ticker") == event_ticker
            and any(
                party_label in " ".join(str(market.get(key, "")) for key in ("yes_sub_title", "title")).lower()
                for market in event.get("markets", [])
            )
            for event in inventory
        ) else []
    match = re.fullmatch(
        r"Will the (Republican|Democratic|Independent) Party win the (.+) (Senate|Governor)\?",
        title,
    )
    if not match:
        return []
    party, state, chamber = (part.lower() for part in match.groups())
    event_title = f"{state} {chamber} winner?".lower()
    candidates = []
    for event in inventory:
        if str(event.get("title", "")).lower() != event_title:
            continue
        if "2026" not in str(event.get("sub_title", "")) and "2026" not in str(event.get("event_ticker", "")):
            continue
        for market in event.get("markets", []):
            label = " ".join(
                str(market.get(key, "")) for key in ("yes_sub_title", "title")
            ).lower()
            if party not in label:
                continue
            series = str(event.get("series_ticker", "")).lower()
            slug = re.sub(r"[^a-z0-9]+", "-", str(event.get("title", "")).lower()).strip("-")
            candidates.append({
                "id": str(market.get("ticker")),
                "event": str(event.get("event_ticker")),
                "url": f"https://kalshi.com/markets/{series}/{slug}",
            })
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/market-links.md")
    args = parser.parse_args()

    markets = sorted(Client().markets(), key=lambda row: int(row["id"]), reverse=True)
    polymarket = _polymarket_inventory()
    kalshi = _kalshi_inventory()
    polymarket_map = {
        int(market["id"]): _polymarket_candidates(str(market.get("title", "")), polymarket)
        for market in markets
    }
    kalshi_map = {
        int(market["id"]): _kalshi_candidates(str(market.get("title", "")), kalshi)
        for market in markets
    }
    pm_count = sum(len(rows) == 1 for rows in polymarket_map.values())
    kalshi_count = sum(len(rows) == 1 for rows in kalshi_map.values())
    lines = [
        "# SIG Market Links",
        "",
        "> Generated from the authenticated SIG market universe. This file is a point-in-time index; run the generator again when the universe changes.",
        "",
        f"Generated: `{dt.datetime.now(dt.timezone.utc).isoformat()}`",
        f"Market count: **{len(markets)}**",
        f"Kalshi candidate links found: **{kalshi_count}**",
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
        "Each row has the intended SIG, Kalshi, and Polymarket slots. Links marked `candidate` are title/market-structure matches found through the public APIs and still require settlement review.",
        "",
        "| SIG ID | Event | SIG | Kalshi | Polymarket | Status |",
        "|---:|---|---|---|---|---|",
    ]
    for market in markets:
        market_id = int(market["id"])
        title = str(market.get("title", "")).replace("|", "\\|")
        sig_link = f"[SIG]({SIG_BASE}/{market_id})"
        candidates = polymarket_map[market_id]
        kalshi_candidates = kalshi_map[market_id]
        if len(kalshi_candidates) == 1:
            kalshi_link = f"[candidate]({kalshi_candidates[0]['url']})"
        else:
            kalshi_link = "Not found"
        if len(candidates) == 1:
            poly_link = f"[candidate]({candidates[0]['url']})"
            status = "Both venue candidates; review"
        elif len(candidates) > 1:
            poly_link = "Ambiguous candidates"
            status = "Manual review required"
        else:
            poly_link = "Not found"
            status = "Kalshi candidate; Polymarket unresolved" if len(kalshi_candidates) == 1 else "No venue match found"
        lines.append(
            f"| {market_id} | {title} | {sig_link} | {kalshi_link} | {poly_link} | {status} |"
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
