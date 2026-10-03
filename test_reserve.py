"""High-EV reserve: capital above the account cap, open only to high expected-return entries;
and one-sided slippage guards on stops."""
import argparse
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import bot
bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log
import conviction as cv
import fair_value as fv
import gates
from arb_engine import Book
from signals import Snapshot
from test_fair_value import Refs, book

DELAWARE = [{"id": 386, "title": "Will the Republican Party win the Delaware Senate?"}]


def args(**kw):
    base = dict(fv_threshold=0.03, fv_exit=0.01, fv_max_market=500, fv_max_gross=5000, mode="auto",
                fv_unit=500, fv_max_race=2500, max_gross=10000, reserve=2000.0, reserve_min_roi=0.05)
    base.update(kw)
    return argparse.Namespace(**base)


def run_fv(bid, a, gross):
    """Fair 0.02 (Kalshi 0.01/0.03); one SIG bid. Returns the order sent, or None."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        refs = Refs({("kalshi", "K"): (0.01, 0.03, 5)}, {386: [("kalshi", "K")]})
        snap = Snapshot("t", DELAWARE, {386: [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": bid,
                                               "quantity": 500}]})
        cli = mock.Mock()
        cli.place.return_value = {"dryRun": True, "orders": [], "filledQuantity": 500}
        risk = bot.Risk(a)
        risk.gross = gross
        with mock.patch.object(bot, "EXEC_LOG", d / "e.jsonl"), mock.patch.object(bot, "KILL_SWITCH", d / "K"):
            bot.run_fair_value(cli, snap, refs, fv.Ledger(d / "l.json"), a, live=False, risk=risk)
        if not cli.place.called:
            return None
        row = json.loads((d / "e.jsonl").read_text())
        return row["result"]["plan"]


class FairValueReserveTests(unittest.TestCase):
    def test_at_the_cap_a_high_return_entry_uses_the_reserve(self):
        plan = run_fv(0.09, args(), gross=10000)          # sell YES 0.09 vs fair 0.02: ~7.7% on 0.91 a share
        self.assertTrue(plan["reserve"])
        self.assertGreaterEqual(bot.roi(plan), 0.05)

    def test_at_the_cap_an_ordinary_entry_waits(self):
        self.assertIsNone(run_fv(0.055, args(), gross=10000))   # 3.5c edge on 0.945: ~3.7%

    def test_reserve_has_its_own_limit(self):
        self.assertIsNone(run_fv(0.09, args(), gross=12000))    # reserve fully used

    def test_without_a_reserve_the_cap_is_hard(self):
        self.assertIsNone(run_fv(0.09, args(reserve=0.0), gross=10000))

    def test_under_the_cap_entries_are_unchanged(self):
        plan = run_fv(0.055, args(), gross=0)
        self.assertNotIn("reserve", plan)

    def test_reserve_ignores_the_strategy_cap(self):
        plan = run_fv(0.09, args(fv_max_gross=0), gross=0)      # fair-value cap used up, account is not
        self.assertTrue(plan["reserve"])


class ReservePlanTests(unittest.TestCase):
    def test_exits_pass_through(self):
        ex = {"exit": "stop", "qty": 10}
        self.assertIs(bot.reserve_plan(ex, lambda room: self.fail("no re-plan for exits"), args(), 0, 2000), ex)

    def test_keeps_the_normal_plan_when_the_reserve_adds_nothing(self):
        normal = {"qty": 100, "capital": 90, "expected_pnl": 2}
        self.assertIs(bot.reserve_plan(normal, lambda room: self.fail("no re-plan"), args(), 5000, 2000), normal)


class ArbReserveTests(unittest.TestCase):
    def arb(self, pnl, capital):
        return mock.Mock(marginal_edge=0.02, legs=[1, 2], capital=capital, pnl=pnl, race="R")

    def risk(self, gross, **kw):
        a = args(min_edge=0.015, min_edge_3leg=0.02, max_per_race=2000, cooldown=0, **kw)
        r = bot.Risk(a)
        r.gross = gross
        return r

    def test_high_return_arb_uses_the_reserve(self):
        self.assertEqual(self.risk(10000).ok(self.arb(pnl=60, capital=1000)), (True, ""))

    def test_ordinary_arb_still_stops_at_the_cap(self):
        self.assertEqual(self.risk(10000).ok(self.arb(pnl=20, capital=1000)), (False, "gross cap"))

    def test_arb_beyond_the_reserve_is_refused(self):
        self.assertEqual(self.risk(11500).ok(self.arb(pnl=60, capital=1000)), (False, "gross cap"))


class ConvictionReserveTests(unittest.TestCase):
    def test_top_up_at_the_cap_only_for_high_return(self):
        with tempfile.TemporaryDirectory() as d:
            led = fv.Ledger(pathlib.Path(d) / "cv.json")
            b = book(asks=[(0.30, 5000)])                  # fair 0.40: 10c on 0.30 a share, ~33%
            a = args(cv_min_edge=0.02, cv_max_bet=1000, cv_max_gross=0, cv_stop=0.10)
            risk = bot.Risk(a)
            risk.gross = 10000

            def replan(room):
                return cv.plan(b, 0.40, led, min_edge=0.02, max_bet=1000, gross_left=room, stop=0.10, max_slip=0.03)
            self.assertIsNone(replan(0))
            plan = bot.reserve_plan(None, replan, a, 0, bot.account_room(a, risk, high_ev=True))
            self.assertTrue(plan["reserve"])
            self.assertLessEqual(plan["capital"], 1000)


class LimitsTests(unittest.TestCase):
    def limits(self, **extra):
        base = json.loads((pathlib.Path(__file__).parent / "config" / "risk_limits.json").read_text())
        base.pop("high_ev_reserve", None)
        base.update(extra)
        p = pathlib.Path(tempfile.mkdtemp()) / "l.json"
        p.write_text(json.dumps(base))
        return p

    def test_reserve_is_optional_and_read_into_args(self):
        a = bot.apply_limits(argparse.Namespace(max_gross=float("inf"), max_per_race=1e9, min_edge=0),
                             gates.load_limits(self.limits()))
        self.assertEqual(a.reserve, 0.0)
        a = bot.apply_limits(argparse.Namespace(max_gross=float("inf"), max_per_race=1e9, min_edge=0),
                             gates.load_limits(self.limits(high_ev_reserve={"capital": 8000, "min_roi": 0.05})))
        self.assertEqual((a.reserve, a.reserve_min_roi), (8000.0, 0.05))

    def test_malformed_reserve_is_refused(self):
        for bad in ({"capital": -1, "min_roi": 0.05}, {"capital": 8000}, [8000], {"capital": True, "min_roi": 0.05}):
            with self.assertRaises(ValueError):
                gates.load_limits(self.limits(high_ev_reserve=bad))


class StopGuardTests(unittest.TestCase):
    kw = dict(exit_band=0.01, tp=0.03, stop=0.05, max_slip=0.03)

    def test_slip_guard_is_one_sided(self):
        self.assertTrue(fv.slip_ok(0.30, 0.20, selling=True, max_slip=0.03))     # selling 10c above fair
        self.assertFalse(fv.slip_ok(0.16, 0.20, selling=True, max_slip=0.03))    # selling 4c below fair
        self.assertTrue(fv.slip_ok(0.10, 0.20, selling=False, max_slip=0.03))    # buying 10c below fair
        self.assertFalse(fv.slip_ok(0.24, 0.20, selling=False, max_slip=0.03))   # buying 4c above fair

    def test_fv_stop_takes_a_lagging_price(self):
        # short YES from 0.075; the reference jumps to 0.13 while SIG still offers 0.08
        ex = fv.exit_signal(book(asks=[(0.08, 500)]), 0.13, -300, entry=0.075, **self.kw)
        self.assertEqual((ex["exit"], ex["yes_side"], ex["limit"]), ("stop", "BUY", 0.08))

    def test_cv_stop_takes_a_lagging_price(self):
        with tempfile.TemporaryDirectory() as d:
            led = fv.Ledger(pathlib.Path(d) / "cv.json")
            led.record(1, "BUY", 1000, 0.32)
            kw = dict(min_edge=0.04, max_bet=4000, gross_left=0, stop=0.10, max_slip=0.03)
            # fair fell to 0.21 (thesis broken) but SIG still bids 0.28: take it (refused before)
            p = cv.plan(book(bids=[(0.28, 5000)], asks=[(0.30, 1)]), 0.21, led, **kw)
            self.assertEqual((p["exit"], p["yes_side"], p["limit"]), ("stop", "SELL", 0.28))
            # SIG bid 4c below fair: wait rather than dump into a thin book
            self.assertIsNone(cv.plan(book(bids=[(0.17, 5000)], asks=[(0.30, 1)]), 0.21, led, **kw))


if __name__ == "__main__":
    unittest.main()
