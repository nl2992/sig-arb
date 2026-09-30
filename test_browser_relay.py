import unittest

from dashboard import Source
from signals import Snapshot, scan_diagnostics


class BrowserRelayTests(unittest.TestCase):
    def test_accepts_browser_snapshot(self):
        source = Source()
        result = source.accept_browser_snapshot({
            'ts': '2026-09-30T00:00:00Z',
            'markets': [{'id': 1, 'title': 'Will the Republican Party win the Test Senate?'}],
            'levels': {'1': [{'price': 0.4, 'quantity': 10, 'side': 'BUY', 'isYes': True}]},
        })
        self.assertEqual(result['markets'], 1)
        self.assertEqual(source.get().markets[0]['id'], 1)

    def test_one_sided_browser_snapshot_matches_loaded_snapshot(self):
        payload = {
            'ts': '2026-09-30T00:00:00Z',
            'markets': [
                {'id': 1, 'title': 'Will the Republican Party win the Test Senate?'},
                {'id': 2, 'title': 'Will the Democratic Party win the Test Senate?'},
            ],
            'levels': {
                '1': [{'price': 0.7, 'quantity': 10, 'side': 'BUY', 'isYes': True}],
                '2': [],
            },
        }
        source = Source()
        source.accept_browser_snapshot(payload)
        browser_report = scan_diagnostics(source.get(), set())
        loaded_report = scan_diagnostics(
            Snapshot(payload['ts'], payload['markets'], {1: payload['levels']['1'], 2: []}), set())
        self.assertEqual(browser_report.diagnostics, loaded_report.diagnostics)
        sell = next(d for d in browser_report.diagnostics if d['direction'] == 'SELL_ALL')
        self.assertEqual(sell['reasons'], ['MISSING_BID'])


if __name__ == '__main__':
    unittest.main()
