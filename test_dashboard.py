import unittest
from unittest.mock import patch

from dashboard import report
from signals import Snapshot


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot.load('fixtures/sample_snapshot.json')

    def test_depth_weighted_prices_and_profit(self):
        rows = report(self.snapshot, {'roi': ['0']})['signals']
        self.assertEqual(rows[0]['race'], 'Delaware Senate')
        synthetic = next(r for r in rows if r['race'] == 'Synthetic Senate')
        self.assertEqual(synthetic['qty'], 700)
        self.assertAlmostEqual(synthetic['vwap'], 676/700)
        self.assertAlmostEqual(synthetic['profit'], 24)
        self.assertAlmostEqual(synthetic['edge'], 24/700)
        self.assertGreater(synthetic['top_edge'], synthetic['edge'])
        self.assertTrue(all(l['limit'] >= l['vwap'] for l in synthetic['legs']))

    def test_budget_and_fees(self):
        rows = report(self.snapshot, {'budget': ['100'], 'fee': ['0.001'], 'roi': ['0']})['signals']
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(r['capital'], 100)
            self.assertAlmostEqual(r['capital'], r['qty']*(r['vwap']+len(r['legs'])*0.001))
        self.assertEqual(report(self.snapshot, {'fee': ['1']})['signals'], [])
        near = report(self.snapshot, {'fee': ['0.02']})['near']
        de = next(r for r in near if r['race'] == 'Delaware Senate')
        self.assertAlmostEqual(de['edge'], -0.01)

    def test_invalid_filters(self):
        for value in ('nan', 'inf', '-1', 'bad'):
            with self.assertRaises(ValueError):
                report(self.snapshot, {'fee': [value]})

    def test_net_roi_hurdle(self):
        self.assertEqual(report(self.snapshot, {})['signals'], [])
        rows = report(self.snapshot, {'roi': ['3.1']})['signals']
        self.assertEqual([r['race'] for r in rows], ['Synthetic Senate'])
        self.assertEqual(report(self.snapshot, {'roi': ['3.1'], 'fee': ['0.01']})['signals'], [])

    def test_buy_yes_requires_exhaustive_allowlist(self):
        with patch('dashboard.load_exhaustive', return_value=set()):
            self.assertTrue(all(r['direction'] == 'SELL_ALL' for r in report(self.snapshot, {'roi': ['0']})['signals']))


if __name__ == '__main__':
    unittest.main()
