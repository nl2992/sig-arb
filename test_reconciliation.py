import unittest

from reconciliation import reconcile_portfolio, reconcile_positions


class ReconciliationTests(unittest.TestCase):
    def test_clean_portfolio_reconciles(self):
        report = reconcile_portfolio(
            expected_positions={'386:YES': -10}, actual_positions={'386:YES': -10},
            expected_open_orders={'o1'}, actual_open_orders={'o1'},
            expected_cash=1000, actual_cash=1000.005)
        self.assertEqual('RECONCILED', report['status'])
        self.assertFalse(report['kill_switch_required'])

    def test_position_mismatch_requires_kill_switch(self):
        report = reconcile_positions({'386:YES': -10}, {'386:YES': -9})
        self.assertEqual('MISMATCH', report['status'])
        self.assertTrue(report['kill_switch_required'])

    def test_unexpected_order_is_visible(self):
        report = reconcile_portfolio(
            expected_positions={}, actual_positions={}, expected_open_orders=set(),
            actual_open_orders={'venue-order'}, expected_cash=100, actual_cash=100)
        self.assertEqual(['venue-order'], report['checks']['orders']['unexpected'])
        self.assertTrue(report['kill_switch_required'])


if __name__ == '__main__':
    unittest.main()
