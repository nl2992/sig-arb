"""Unwinding held arb sets before settlement (no network)."""
import argparse
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import arb_exits
import bot
bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log
from arb_engine import Book
from signals import Snapshot


def book(mid, bids=(), asks=()):
    return Book(mid, 9, sorted(bids, key=lambda x: -x[0]), sorted(asks, key=lambda x: x[0]))


# 1000 No sets on a two-way race bought at bids summing 1.03: cost 0.97 a set, settlement +0.03
ARB = {1: [-1000.0, 450.0], 2: [-1000.0, 520.0]}
ACCT = {1: -1000.0, 2: -1000.0}


def plan(ask1, ask2, arb=ARB, acct=ACCT, depth=10000, **kw):
    books = {1: book(1, asks=[(ask1, depth)]), 2: book(2, asks=[(ask2, depth)])}
    return arb_exits.unwind_plan("R", [1, 2], books, arb, acct, **kw)


class PlanTests(unittest.TestCase):
    def test_unwinds_when_better_than_settlement(self):
        p = plan(0.54, 0.45)                     # asks sum 0.99: proceeds 1.01 a set
        self.assertEqual((p["yes_side"], p["qty"]), ("BUY", 1000))
        self.assertAlmostEqual(p["profit"], 40.0)
        self.assertAlmostEqual(p["settle_profit"], 30.0)

    def test_half_of_settlement_profit_is_enough(self):
        p = plan(0.565, 0.45)                    # profit 0.015 a set = 50% of 0.03
        self.assertAlmostEqual(p["profit"], 15.0)
        self.assertIsNone(plan(0.57, 0.45))       # 0.01 a set: under half
        self.assertIsNone(plan(0.60, 0.45))       # a loss on cost: never

    def test_depth_rule_shrinks_and_walk_stops_where_profit_does(self):
        books = {1: book(1, asks=[(0.54, 500), (0.60, 5000)]), 2: book(2, asks=[(0.45, 5000)])}
        p = arb_exits.unwind_plan("R", [1, 2], books, ARB, ACCT, depth_ratio=2.0)
        self.assertEqual(p["qty"], 250)          # 2x depth at the 0.54 level only
        self.assertEqual(p["legs"][0]["limit"], 0.54)

    def test_only_complete_sets_the_account_still_holds(self):
        self.assertEqual(plan(0.54, 0.45, acct={1: -1000.0, 2: -400.0})["qty"], 400)
        self.assertIsNone(plan(0.54, 0.45, arb={1: [-1000.0, 450.0]}))                 # one leg: not a set
        self.assertIsNone(plan(0.54, 0.45, arb={1: [-1000.0, 450.0], 2: [1000.0, 480.0]}))

    def test_yes_sets_sell_into_bids(self):
        arb = {1: [1000.0, 480.0], 2: [1000.0, 490.0]}               # bought at 0.97, pays 1
        books = {1: book(1, bids=[(0.50, 10000)]), 2: book(2, bids=[(0.49, 10000)])}
        p = arb_exits.unwind_plan("R", [1, 2], books, arb, {1: 1000.0, 2: 1000.0})
        self.assertEqual((p["yes_side"], p["qty"]), ("SELL", 1000))
        self.assertAlmostEqual(p["profit"], 20.0)

    def test_repair_floor_is_break_even_on_cost(self):
        p = plan(0.54, 0.45)
        self.assertAlmostEqual(arb_exits.repair_floor(p)([0.45]), 2 - 0.97 - 0.45)


class RunTests(unittest.TestCase):
    def test_live_unwind_journals_legs_books_holdings_and_frees_capital(self):
        markets = [{"id": 1, "title": "Will the Republican Party win the R?"},
                   {"id": 2, "title": "Will the Democratic Party win the R?"}]
        lv = lambda p: [{"exchangeId": 9, "side": "SELL", "isYes": True, "price": p, "quantity": 10000}]
        snap = Snapshot("t", markets, {1: lv(0.54), 2: lv(0.45)})
        a = argparse.Namespace(arb_exit_share=0.5, arb_depth_ratio=2.0, cooldown=30, mode="auto",
                               chase_ticks=2, fee=0.0)
        cli = mock.Mock()
        cli.place.return_value = {"filledQuantity": 1000}
        risk = bot.Risk(argparse.Namespace())
        risk.gross = 5000.0
        acct = dict(ACCT)
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "e.jsonl"
            with mock.patch.object(bot, "EXEC_LOG", log), mock.patch.object(bot, "KILL_SWITCH", pathlib.Path(d) / "K"):
                res = bot.run_arb_exit(cli, snap, "R", ARB, acct, a, True, risk)
                self.assertIsNone(bot.run_arb_exit(cli, snap, "R", ARB, acct, a, True, risk))   # cooldown
            row = json.loads(log.read_text())
            import positions
            after = positions.arb_costs(log, pathlib.Path(d) / "none")
        self.assertEqual(res["status"], "DONE")
        self.assertTrue(row["result"]["unwind"])
        self.assertEqual({l["side"] for l in row["result"]["legs"]}, {"BUY"})
        self.assertEqual(acct, {1: 0.0, 2: 0.0})
        self.assertAlmostEqual(risk.gross, 5000 - 970)
        self.assertEqual({m: round(q) for m, (q, _) in after.items()},
                         {1: 1000, 2: 1000})        # the journal now holds the closing legs


if __name__ == "__main__":
    unittest.main()
