import unittest

from portfolio import normalize_sig_portfolio


class PortfolioTests(unittest.TestCase):
    def test_normalizes_read_only_sig_state(self):
        report = normalize_sig_portfolio(
            balance=123.45,
            holdings=[{"marketId": 386, "outcome": "YES", "quantity": -10}],
            orders=[{"orderId": "o1", "status": "OPEN"},
                    {"orderId": "o2", "status": "FILLED"}],
        )
        self.assertEqual("NORMALIZED", report["status"])
        self.assertEqual({"sig:386:YES": -10.0}, report["positions"])
        self.assertEqual(["sig:o1"], report["open_orders"])
        self.assertFalse(report["kill_switch_required"])

    def test_unknown_rows_force_fail_closed_state(self):
        report = normalize_sig_portfolio(
            balance=None, holdings=[{"marketId": 386}], orders=[{"status": "OPEN"}])
        self.assertEqual("UNVERIFIED_SCHEMA", report["status"])
        self.assertTrue(report["unparsed"])
        self.assertTrue(report["kill_switch_required"])

    def test_fetch_uses_read_only_market_calls(self):
        class Client:
            concurrency = 2
            def holdings(self, market_id): return [{"marketId": market_id, "outcome": "YES", "quantity": 1}]
            def my_orders(self, market_id): return [{"orderId": f"o{market_id}", "status": "OPEN"}]
            def balance(self): return 10
        from portfolio import fetch_sig_portfolio
        report = fetch_sig_portfolio(Client(), [{"id": 1}, {"id": 2}])
        self.assertEqual("NORMALIZED", report["status"])
        self.assertEqual(2, len(report["positions"]))
        self.assertEqual(2, len(report["open_orders"]))


if __name__ == "__main__":
    unittest.main()
