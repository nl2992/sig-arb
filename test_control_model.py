import unittest

from control_model import control_readiness, probability_bounds, senate_inventory
from signals import Snapshot


class ControlModelTests(unittest.TestCase):
    def test_inventory_and_guard(self):
        snapshot = Snapshot.load('fixtures/sample_snapshot.json')
        result = control_readiness(snapshot.markets)
        self.assertFalse(result['ready_for_riskless_control_arb'])
        self.assertTrue(any('50-50' in x['name'] for x in result['wording_register']))

    def test_bounds(self):
        self.assertEqual(probability_bounds([0.2, 0.3]), (0.0, 0.5))
        with self.assertRaises(ValueError):
            probability_bounds([1.1])

    def test_senate_grouping(self):
        snapshot = Snapshot.load('fixtures/sample_snapshot.json')
        inv = senate_inventory(snapshot.markets)
        self.assertGreaterEqual(inv['race_count'], 2)


if __name__ == '__main__':
    unittest.main()
