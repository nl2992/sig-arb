import unittest
import json
from unittest.mock import patch

from dashboard import Source, report
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
        self.assertEqual(report(self.snapshot, {'roi': ['5']})['signals'], [])
        rows = report(self.snapshot, {'roi': ['3.1']})['signals']
        self.assertEqual([r['race'] for r in rows], ['Synthetic Senate'])
        self.assertEqual(report(self.snapshot, {'roi': ['3.1'], 'fee': ['0.01']})['signals'], [])

    def test_overall_cap_limits_each_punt(self):
        rows = report(self.snapshot, {'roi': ['0'], 'capital': ['2000'], 'cap_pct': ['5']})['signals']
        self.assertTrue(rows)
        self.assertTrue(all(r['capital'] <= 100 for r in rows))

    def test_below_hurdle_is_retained_as_punt(self):
        data = report(self.snapshot, {})
        self.assertEqual(data['signals'], [])
        self.assertTrue({r['race'] for r in data['punts']} >= {'Delaware Senate', 'Synthetic Senate'})
        self.assertTrue(all(r['required_roi'] == 0.05 for r in data['punts']))

    def test_buy_yes_requires_exhaustive_allowlist(self):
        with patch('dashboard.load_exhaustive', return_value=set()):
            self.assertTrue(all(r['direction'] == 'SELL_ALL' for r in report(self.snapshot, {'roi': ['0']})['signals']))

    def test_extended_fields_preserve_compatibility_and_json_safety(self):
        data = report(self.snapshot, {'roi': ['0']})
        self.assertIn('signals', data)
        self.assertIn('near', data)
        self.assertIn('punts', data)
        self.assertIn('diagnostics', data)
        self.assertIn('liquidity', data)
        json.dumps(data, allow_nan=False)

    def test_crossvenue_coverage_is_included_in_dashboard_report(self):
        payload = {
            'kalshi': {'markets': [{'ticker': 'KX1'}], 'observations': []},
            'polymarket': {'markets': [{'id': 'P1'}], 'observations': []},
        }
        data = report(self.snapshot, {'roi': ['0']}, payload)
        self.assertEqual({'kalshi': 1, 'polymarket': 1}, data['crossvenue']['market_counts'])
        self.assertEqual({'discovered': 0, 'approved': 0}, data['crossvenue']['mapping_counts'])
        json.dumps(data, allow_nan=False)

    def test_replay_portfolio_state_is_explicit(self):
        source = Source(replay='fixtures/sample_snapshot.json')
        data = source.get_portfolio()
        self.assertEqual('REPLAY', data['status'])
        self.assertTrue(data['read_only'])

    def test_news_status_is_explicit_when_no_breaker_is_active(self):
        data = report(self.snapshot, {'roi': ['0']})
        self.assertTrue(all(row['news_status'] == 'CLEAR' for row in data['signals']))


if __name__ == '__main__':
    unittest.main()
