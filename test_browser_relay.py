import unittest

from dashboard import Source


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


if __name__ == '__main__':
    unittest.main()
