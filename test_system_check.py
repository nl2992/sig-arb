import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from system_check import readiness


class SystemCheckTests(unittest.TestCase):
    def test_default_readiness_is_paper_only(self):
        result = readiness()
        self.assertFalse(result["ready_for_paper"])
        self.assertFalse(result["ready_for_live"])
        self.assertEqual("BLOCKED", result["checks"]["execution_gate"]["status"])

    def test_malformed_news_ledger_blocks_paper_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.json"
            path.write_text(json.dumps({"events": [{"id": "bad"}]}))
            result = readiness(news_path=path)
        self.assertFalse(result["ready_for_paper"])
        self.assertEqual("FAIL", result["checks"]["news_ledger"]["status"])

    def test_bounded_public_inventory_is_not_complete(self):
        payload = {
            "kalshi": {"markets": [{}], "inventory_coverage": "BOUNDED_MARKET_COUNT", "book_coverage": "ALL_RETURNED_MARKETS"},
            "polymarket": {"markets": [{}], "inventory_coverage": "BOUNDED_MARKET_COUNT", "book_coverage": "ALL_RETURNED_MARKETS"},
        }
        with patch("system_check.fetch_public", return_value=payload):
            result = readiness(public=True, public_limit=2)
        self.assertFalse(result["checks"]["public_venues"]["complete"])
        self.assertFalse(result["ready_for_paper"])

    def test_sig_snapshot_must_match_fixed_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(json.dumps({"ts": "2026-09-30T00:00:00Z", "markets": [{"id": 1, "title": "wrong"}], "levels": {"1": []}}))
            result = readiness(snapshot_path=snapshot)
        self.assertEqual("FAIL", result["checks"]["sig_snapshot"]["status"])
        self.assertFalse(result["ready_for_paper"])


if __name__ == "__main__":
    unittest.main()
