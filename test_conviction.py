"""Conviction bets: target picking, sizing, thesis-broken stop."""
import pathlib
import tempfile
import unittest

import conviction as cv
import fair_value as fv
from arb_engine import Book


def book(mid, bids=(), asks=()):
    lv = [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": p, "quantity": q} for p, q in bids]
    lv += [{"exchangeId": 9, "side": "SELL", "isYes": True, "price": p, "quantity": q} for p, q in asks]
    return Book.from_levels(mid, lv)


T = lambda party, race: f"Will the {party} Party win the {race}?"


class ConvictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = fv.Ledger(pathlib.Path(self.tmp.name) / "cv.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_targets_largest_gaps_competitive_one_per_race(self):
        books = {1: book(1, bids=[(0.30, 1)], asks=[(0.32, 1)]),     # fair 0.40: buy gap 0.08
                 2: book(2, bids=[(0.68, 1)], asks=[(0.70, 1)]),     # sibling of 1, fair 0.60: sell gap 0.08
                 3: book(3, bids=[(0.55, 1)], asks=[(0.56, 1)]),     # fair 0.50: sell gap 0.05
                 4: book(4, bids=[(0.10, 1)], asks=[(0.11, 1)]),     # fair 0.03: outside band
                 5: book(5, bids=[(0.49, 1)], asks=[(0.50, 1)])}     # fair 0.51: gap 0.01 too small
        titles = {1: T("Democratic", "A Senate"), 2: T("Republican", "A Senate"), 3: T("Democratic", "B Senate"),
                  4: T("Democratic", "C Senate"), 5: T("Democratic", "D Senate")}
        fair = {1: 0.40, 2: 0.60, 3: 0.50, 4: 0.03, 5: 0.51}
        t = cv.pick_targets(books, titles, fair.get, self.led, min_edge=0.04, min_fair=0.15, max_fair=0.85, max_bets=6)
        self.assertEqual(len([m for m in t if m in (1, 2)]), 1)             # one bet per race
        self.assertIn(3, t)
        self.assertNotIn(4, t)
        self.assertNotIn(5, t)

    def test_plan_sizes_to_max_bet_at_good_prices_only(self):
        b = book(1, asks=[(0.32, 5000), (0.355, 5000), (0.37, 5000)])
        p = cv.plan(b, 0.40, self.led, min_edge=0.04, max_bet=4000, gross_left=10000, stop=0.10, max_slip=0.03)
        self.assertEqual((p["yes_side"], p["limit"]), ("BUY", 0.355))      # 0.37 is within 4c of fair: skipped
        self.assertLessEqual(p["capital"], 4000)
        self.assertGreater(p["edge"], 0.04)

    def test_stop_only_when_thesis_breaks(self):
        self.led.record(1, "BUY", 1000, 0.32)
        self.assertIsNone(cv.plan(book(1, bids=[(0.30, 5000)], asks=[(0.36, 1)]), 0.25, self.led, min_edge=0.04,
                                  max_bet=4000, gross_left=0, stop=0.10, max_slip=0.03))   # fair down 7c: hold
        p = cv.plan(book(1, bids=[(0.20, 5000)], asks=[(0.24, 1)]), 0.21, self.led, min_edge=0.04,
                    max_bet=4000, gross_left=0, stop=0.10, max_slip=0.03)
        self.assertEqual((p["exit"], p["yes_side"]), ("stop", "SELL"))
