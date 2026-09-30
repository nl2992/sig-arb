"""Generate a current Markdown index of SIG markets and reviewed venue mappings."""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib

from sig_client import Client


SIG_BASE = "https://sig.thesuper.market/markets"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/market-links.md")
    args = parser.parse_args()

    markets = sorted(Client().markets(), key=lambda row: int(row["id"]), reverse=True)
    lines = [
        "# SIG Market Links",
        "",
        "> Generated from the authenticated SIG market universe. This file is a point-in-time index; run the generator again when the universe changes.",
        "",
        f"Generated: `{dt.datetime.now(dt.timezone.utc).isoformat()}`  ",
        f"Market count: **{len(markets)}**  ",
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
        "## Production Mappings",
        "",
        "No production mappings are currently approved. No Kalshi or Polymarket link is presented as an equivalent contract until title, outcome semantics, close time, resolution authority, and settlement rules have been reviewed.",
        "",
        "## SIG Events",
        "",
        "| SIG ID | Event | Settlement date | SIG link |",
        "|---:|---|---|---|",
    ]
    for market in markets:
        market_id = int(market["id"])
        title = str(market.get("title", "")).replace("|", "\\|")
        settlement = market.get("rootSettlementDate") or "Not supplied"
        link = f"{SIG_BASE}/{market_id}"
        lines.append(f"| {market_id} | {title} | `{settlement}` | [Open SIG market]({link}) |")

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
