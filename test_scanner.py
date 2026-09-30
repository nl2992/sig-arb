import json
import unittest

from dashboard import report
from signals import Snapshot, near_misses, scan_diagnostics


def level(side, price, quantity=10):
    return {"exchangeId": 1, "side": side, "isYes": True,
            "price": price, "quantity": quantity}


class ScannerTests(unittest.TestCase):
    def snapshot(self, levels):
        markets = [
            {"id": 1, "title": "Will the Republican Party win the Test Senate?"},
            {"id": 2, "title": "Will the Democratic Party win the Test Senate?"},
        ]
        return Snapshot("2026-09-30T00:00:00Z", markets, levels)

    def test_missing_bid_is_rejected_without_fabricated_edge(self):
        snap = self.snapshot({1: [level("BUY", .7)], 2: []})
        result = scan_diagnostics(snap, set())
        sell = next(d for d in result.diagnostics if d["direction"] == "SELL_ALL")
        self.assertEqual(sell["reasons"], ["MISSING_BID"])
        self.assertIsNone(sell["top_edge"])
        self.assertEqual(result.liquidity[1]["bid"], None)

    def test_missing_book_is_distinct_from_empty_book(self):
        snap = self.snapshot({1: [level("BUY", .7)]})
        result = scan_diagnostics(snap, set())
        sell = next(d for d in result.diagnostics if d["direction"] == "SELL_ALL")
        self.assertEqual(sell["reasons"], ["MISSING_BOOK"])
        self.assertFalse(result.liquidity[1]["has_book"])

    def test_buy_all_requires_explicit_exhaustivity(self):
        snap = self.snapshot({1: [level("SELL", .4)], 2: [level("SELL", .5)]})
        result = scan_diagnostics(snap, set())
        buy = next(d for d in result.diagnostics if d["direction"] == "BUY_ALL")
        self.assertEqual(buy["status"], "RULE_BLOCKED")
        self.assertEqual(buy["reasons"], ["NOT_EXHAUSTIVE"])

    def test_approved_buy_all_preserves_depth_result(self):
        snap = self.snapshot({1: [level("SELL", .4, 10)], 2: [level("SELL", .5, 10)]})
        result = scan_diagnostics(snap, {"Test Senate"})
        buy = next(d for d in result.diagnostics if d["direction"] == "BUY_ALL")
        self.assertEqual(buy["status"], "ELIGIBLE")
        self.assertEqual(buy["quantity"], 10)
        self.assertAlmostEqual(buy["depth_adjusted_pnl"], 1)

    def test_approved_buy_all_missing_ask_is_rejected(self):
        snap = self.snapshot({1: [level("SELL", .4)], 2: []})
        result = scan_diagnostics(snap, {"Test Senate"})
        buy = next(d for d in result.diagnostics if d["direction"] == "BUY_ALL")
        self.assertEqual(buy["reasons"], ["MISSING_ASK"])
        self.assertIsNone(buy["top_edge"])
        self.assertFalse(result.opportunities)

    def test_threshold_and_limit_rejections_are_deterministic(self):
        positive = self.snapshot({1: [level("BUY", .6)], 2: [level("BUY", .45)]})
        self.assertEqual(scan_diagnostics(positive, set(), min_edge=.1).diagnostics[0]["reasons"],
                         ["BELOW_MIN_EDGE"])
        negative = self.snapshot({1: [level("BUY", .4)], 2: [level("BUY", .5)]})
        self.assertEqual(scan_diagnostics(negative, set()).diagnostics[0]["reasons"],
                         ["NON_POSITIVE_EDGE"])
        limited = scan_diagnostics(positive, set(), cash=0)
        self.assertEqual(limited.diagnostics[0]["reasons"], ["NO_DEPTH_AT_LIMITS"])

    def test_direction_checks_use_only_the_required_side(self):
        missing_asks = self.snapshot({1: [level("BUY", .7)], 2: [level("BUY", .4)]})
        result = scan_diagnostics(missing_asks, {"Test Senate"})
        sell = next(d for d in result.diagnostics if d["direction"] == "SELL_ALL")
        buy = next(d for d in result.diagnostics if d["direction"] == "BUY_ALL")
        self.assertEqual(sell["status"], "ELIGIBLE")
        self.assertEqual(buy["reasons"], ["MISSING_ASK"])

        missing_bids = self.snapshot({1: [level("SELL", .4)], 2: [level("SELL", .5)]})
        result = scan_diagnostics(missing_bids, {"Test Senate"})
        sell = next(d for d in result.diagnostics if d["direction"] == "SELL_ALL")
        buy = next(d for d in result.diagnostics if d["direction"] == "BUY_ALL")
        self.assertEqual(sell["reasons"], ["MISSING_BID"])
        self.assertEqual(buy["status"], "ELIGIBLE")

    def test_fixture_depth_results_are_reported_by_scanner(self):
        snap = Snapshot.load("fixtures/sample_snapshot.json")
        result = scan_diagnostics(snap, set())
        de = next(d for d in result.diagnostics
                  if d["race"] == "Delaware Senate" and d["direction"] == "SELL_ALL")
        synthetic = next(d for d in result.diagnostics
                         if d["race"] == "Synthetic Senate" and d["direction"] == "SELL_ALL")
        self.assertEqual(de["quantity"], 1000)
        self.assertAlmostEqual(de["depth_adjusted_pnl"], 30)
        self.assertEqual(synthetic["quantity"], 700)
        self.assertAlmostEqual(synthetic["depth_adjusted_pnl"], 24)

    def test_dashboard_output_is_json_safe_with_one_sided_books(self):
        data = report(self.snapshot({1: [level("BUY", .7)], 2: []}), {"roi": ["0"]})
        json.dumps(data, allow_nan=False)
        self.assertTrue(any(d["reasons"] == ["MISSING_BID"] for d in data["diagnostics"]))

    def test_near_misses_do_not_impute_missing_prices(self):
        snap = self.snapshot({1: [level("BUY", .7)], 2: []})
        groups = {"Test Senate": {"R": 1, "D": 2}}
        self.assertEqual(near_misses(groups, snap.books(), set(), 10), [])


if __name__ == "__main__":
    unittest.main()
