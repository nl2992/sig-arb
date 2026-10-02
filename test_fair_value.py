"""Fair-value strategy: consensus price, entry/exit rules, ledger, bot hook."""
import argparse
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import bot
bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log
import fair_value as fv
from arb_engine import Book
from signals import Snapshot


def book(mid=1, bids=(), asks=()):
    lv = [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": p, "quantity": q} for p, q in bids]
    lv += [{"exchangeId": 9, "side": "SELL", "isYes": True, "price": p, "quantity": q} for p, q in asks]
    return Book.from_levels(mid, lv)


class Refs(fv.ReferencePrices):
    def __init__(self, quotes, by_sig, **kw):
        self.by_sig, self.quotes = by_sig, {}
        self.max_age_s, self.max_spread, self.max_disagree = 300, 0.06, 0.04
        import threading
        self.lock = threading.Lock()
        now = time.monotonic()
        for k, (b, a, age) in quotes.items():
            self.quotes[k] = (b, a, now - age)


class FairTests(unittest.TestCase):
    def test_mean_of_agreeing_fresh_tight_quotes(self):
        r = Refs({("kalshi", "K"): (0.01, 0.03, 5), ("polymarket", "P"): (0.02, 0.04, 5)},
                 {1: [("kalshi", "K"), ("polymarket", "P")]})
        self.assertAlmostEqual(r.fair(1)["fair"], 0.025)

    def test_stale_wide_and_disagreeing_quotes_are_ignored(self):
        by = {1: [("kalshi", "K"), ("polymarket", "P")]}
        self.assertIsNone(Refs({("kalshi", "K"): (0.1, 0.12, 999)}, by).fair(1))                 # stale
        self.assertIsNone(Refs({("kalshi", "K"): (0.1, 0.30, 5)}, by).fair(1))                  # wide
        self.assertIsNone(Refs({("kalshi", "K"): (0.10, 0.12, 5), ("polymarket", "P"): (0.30, 0.32, 5)}, by).fair(1))
        self.assertAlmostEqual(Refs({("kalshi", "K"): (0.1, 0.30, 5), ("polymarket", "P"): (0.2, 0.22, 5)}, by).fair(1)["fair"], 0.21)
        self.assertIsNone(Refs({}, by).fair(2))


class SignalTests(unittest.TestCase):
    def test_sell_overpriced_longshot(self):
        b = book(bids=[(0.07, 100), (0.065, 200), (0.04, 500)], asks=[(0.08, 100)])
        s = fv.entry_signal(b, fair=0.02, threshold=0.03, capital_left=10_000)
        self.assertEqual((s["yes_side"], s["limit"], s["qty"]), ("SELL", 0.065, 300))
        self.assertGreater(s["edge"], 0.04)

    def test_buy_underpriced_favourite_capped_by_capital(self):
        b = book(bids=[(0.88, 100)], asks=[(0.90, 1000), (0.95, 1000)])
        s = fv.entry_signal(b, fair=0.97, threshold=0.03, capital_left=450)
        self.assertEqual((s["yes_side"], s["limit"], s["qty"]), ("BUY", 0.90, 500))
        self.assertLessEqual(s["capital"], 450)

    def test_no_signal_inside_threshold(self):
        self.assertIsNone(fv.entry_signal(book(bids=[(0.04, 100)], asks=[(0.05, 100)]), 0.03, 0.03, 1000))
        self.assertIsNone(fv.entry_signal(book(bids=[(0.10, 100)]), 0.02, 0.03, 0))

    def test_exit_when_back_near_fair(self):
        b = book(bids=[(0.025, 1000)], asks=[(0.03, 1000)])
        self.assertEqual(fv.exit_signal(b, 0.025, -300, 0.01)["yes_side"], "BUY")      # short YES -> buy back
        self.assertIsNone(fv.exit_signal(book(asks=[(0.06, 1000)]), 0.025, -300, 0.01))
        self.assertEqual(fv.exit_signal(book(bids=[(0.95, 50)]), 0.955, 200, 0.01)["qty"], 50)


class SizingTests(unittest.TestCase):
    def test_capital_scales_with_gap_and_caps(self):
        with tempfile.TemporaryDirectory() as d:
            led = fv.Ledger(pathlib.Path(d) / "l.json")
            deep = [(0.07, 100000)]
            small = fv.plan_market(book(bids=[(0.05, 100000)]), {"fair": 0.02}, led, threshold=0.03, exit_band=0.01,
                                   max_per_market=2000, gross_left=10000, unit=500)
            big = fv.plan_market(book(bids=[(0.14, 100000)]), {"fair": 0.02}, led, threshold=0.03, exit_band=0.01,
                                 max_per_market=2000, gross_left=10000, unit=500)
            capped = fv.plan_market(book(bids=[(0.40, 100000)]), {"fair": 0.02}, led, threshold=0.03, exit_band=0.01,
                                    max_per_market=2000, gross_left=10000, unit=500)
            self.assertAlmostEqual(small["capital"], 500, delta=2)        # edge 0.03 -> 1 unit
            self.assertAlmostEqual(big["capital"], 2000, delta=2)         # edge 0.12 -> 4 units
            self.assertLessEqual(capped["capital"], 2000)                 # per-market cap
            limited = fv.plan_market(book(bids=deep), {"fair": 0.02}, led, threshold=0.03, exit_band=0.01,
                                     max_per_market=2000, gross_left=300, unit=500)
            self.assertLessEqual(limited["capital"], 300)                 # account-wide room


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = fv.Ledger(pathlib.Path(self.tmp.name) / "l.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_short_yes_round_trip_realizes_pnl(self):
        self.led.record(1, "SELL", 300, 0.07)            # buy NO at 0.93
        self.assertEqual(self.led.position(1), -300)
        self.assertAlmostEqual(self.led.capital(1), 279.0)
        self.led.record(1, "BUY", 300, 0.03)             # sell NO at 0.97
        self.assertEqual(self.led.position(1), 0)
        self.assertAlmostEqual(self.led.rows[1]["realized"], 12.0)
        self.assertEqual(fv.Ledger(self.led.path).rows[1]["realized"], 12.0)   # persisted

    def test_plan_never_adds_against_position(self):
        self.led.record(1, "BUY", 100, 0.90)
        b = book(bids=[(0.99, 1000)], asks=[(0.995, 10)])
        plan = fv.plan_market(b, {"fair": 0.95}, self.led, threshold=0.03, exit_band=0.01,
                              max_per_market=1000, gross_left=1000)
        self.assertTrue(plan["exit"])                     # exit the long, never sell YES short


class BotHookTests(unittest.TestCase):
    def test_dry_run_journals_but_does_not_touch_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            led = fv.Ledger(d / "l.json")
            refs = Refs({("kalshi", "K"): (0.01, 0.03, 5)}, {386: [("kalshi", "K")]})
            snap = Snapshot("2026-10-01T00:00:00+00:00", [{"id": 386, "title": "Will the Republican Party win the Delaware Senate?"}],
                            {386: [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": 0.06, "quantity": 500}]})
            cli = mock.Mock()
            cli.place.return_value = {"dryRun": True, "orders": [], "filledQuantity": 500}
            a = argparse.Namespace(fv_threshold=0.03, fv_exit=0.01, fv_max_market=500, fv_max_gross=5000, mode="auto",
                                   fv_unit=500, fv_max_race=2500, max_gross=10000)
            with mock.patch.object(bot, "EXEC_LOG", d / "e.jsonl"), mock.patch.object(bot, "KILL_SWITCH", d / "K"):
                out = bot.run_fair_value(cli, snap, refs, led, a, live=False)
            self.assertEqual(out["fv_orders"], 1)
            args = cli.place.call_args
            self.assertEqual(args.args[2], "SELL")
            self.assertTrue(args.kwargs["dry_run"])
            self.assertEqual(led.rows, {})
            row = json.loads((d / "e.jsonl").read_text())
            self.assertEqual(row["result"]["strategy"], "fv")

    def test_kill_switch_blocks_fv_orders(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "K").write_text("x")
            refs = Refs({("kalshi", "K"): (0.01, 0.03, 5)}, {386: [("kalshi", "K")]})
            snap = Snapshot("t", [{"id": 386, "title": "Will the Republican Party win the Delaware Senate?"}],
                            {386: [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": 0.06, "quantity": 500}]})
            cli = mock.Mock()
            a = argparse.Namespace(fv_threshold=0.03, fv_exit=0.01, fv_max_market=500, fv_max_gross=5000, mode="auto",
                                   fv_unit=500, fv_max_race=2500, max_gross=10000)
            with mock.patch.object(bot, "KILL_SWITCH", d / "K"):
                out = bot.run_fair_value(cli, snap, refs, fv.Ledger(d / "l.json"), a, live=True)
            cli.place.assert_not_called()
            self.assertEqual(out["fv_skipped"], 1)


if __name__ == "__main__":
    unittest.main()


class PuntExitTests(unittest.TestCase):
    kw = dict(exit_band=0.01, tp=0.03, stop=0.05, max_slip=0.03)

    def test_short_longshot_exits(self):
        # short YES entered at 0.075 (bought NO at 0.925), fair then 0.036
        tgt = fv.exit_signal(book(asks=[(0.04, 500)]), 0.02, -300, entry=0.075, **self.kw)
        self.assertEqual(tgt["exit"], "target")                         # buy back 3.5c cheaper
        conv = fv.exit_signal(book(asks=[(0.055, 500)]), 0.05, -300, entry=0.075, **self.kw)
        self.assertEqual(conv["exit"], "converged")
        stop = fv.exit_signal(book(asks=[(0.14, 500)]), 0.13, -300, entry=0.075, **self.kw)
        self.assertEqual(stop["exit"], "stop")                          # reference now 0.13 >= 0.075 + 0.05
        self.assertIsNone(fv.exit_signal(book(asks=[(0.20, 500)]), 0.13, -300, entry=0.075, **self.kw))  # too far from fair

    def test_long_favourite_stop_and_hold(self):
        stop = fv.exit_signal(book(bids=[(0.85, 500)]), 0.86, 300, entry=0.915, **self.kw)
        self.assertEqual(stop["exit"], "stop")
        self.assertIsNone(fv.exit_signal(book(bids=[(0.90, 500)]), 0.93, 300, entry=0.915, **self.kw))    # hold
        tm = fv.exit_signal(book(bids=[(0.91, 500)]), 0.93, 300, entry=0.915, held_s=7200, max_hold_s=3600, **self.kw)
        self.assertEqual(tm["exit"], "time")
