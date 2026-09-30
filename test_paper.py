import tempfile
import unittest
from pathlib import Path

from paper import run_cycle, simulate_result, snapshot_age_seconds
from signals import Snapshot, generate, load_exhaustive


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot.load('fixtures/sample_snapshot.json')

    def test_paper_cycle_has_five_percent_default_and_sends_no_orders(self):
        with tempfile.TemporaryDirectory() as d:
            report = run_cycle(self.snapshot, kill_switch=Path(d) / 'STOP')
        self.assertEqual('PAPER', report['status'])
        self.assertEqual(0, report['orders_sent'])
        self.assertEqual(0.05, report['min_roi'])
        self.assertTrue(report['research_only'])

    def test_partial_fill_is_reconciled_as_residual(self):
        class Args:
            min_edge = 0.0
            fee = 0.0
            max_qty = None
            max_per_race = None
            min_pnl = 1.0
            near = 0
        results, _, _ = generate(self.snapshot, Args(), load_exhaustive(), None)
        result = next(r for r in results if r.race == 'Synthetic Senate')
        simulated = simulate_result(result, leg_fill_ratios=[1.0, 0.5])
        self.assertEqual('PARTIAL_RESIDUAL', simulated['status'])
        self.assertTrue(simulated['residual'])

    def test_kill_switch_blocks_before_signal_generation(self):
        with tempfile.TemporaryDirectory() as d:
            switch = Path(d) / 'KILL_SWITCH'
            switch.touch()
            report = run_cycle(self.snapshot, kill_switch=switch)
        self.assertEqual('KILL_SWITCH', report['status'])
        self.assertEqual(0, report['orders_sent'])

    def test_snapshot_age_parser_handles_utc_z(self):
        snapshot = Snapshot('2026-09-30T00:00:00Z', [], {})
        self.assertGreaterEqual(snapshot_age_seconds(snapshot), 0)


if __name__ == '__main__':
    unittest.main()
