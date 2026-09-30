"""Validation for the fixed, reviewed SIG research universe."""
from __future__ import annotations

import json
import pathlib

from crossvenue_adapters import load_market_links


def validate_sig_universe(markets, links_path, manifest_path):
    links = load_market_links(links_path)
    manifest = json.loads(pathlib.Path(manifest_path).read_text())
    expected = {int(row["sig_market_id"]): row["event"] for row in links}
    actual = {int(row["id"]): str(row.get("title", "")) for row in markets}
    duplicate_ids = len(expected) != len(links)
    missing_ids = sorted(set(expected) - set(actual))
    unexpected_ids = sorted(set(actual) - set(expected))
    title_mismatches = sorted(mid for mid in set(expected) & set(actual)
                              if expected[mid] != actual[mid])
    count_ok = len(expected) == int(manifest.get("expected_count", -1))
    version_ok = bool(manifest.get("version"))
    status = "PASS" if (count_ok and version_ok and not duplicate_ids and
                        not missing_ids and not unexpected_ids and not title_mismatches) else "FAIL"
    return {
        "status": status,
        "version": manifest.get("version"),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "duplicate_ids": duplicate_ids,
        "missing_ids": missing_ids,
        "unexpected_ids": unexpected_ids,
        "title_mismatches": title_mismatches,
    }
