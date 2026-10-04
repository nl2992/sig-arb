"""Lead-lag and market-making strategies (no network)."""
import argparse
import collections
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import bot
import fair_value as fv
import scalper
from arb_engine import Book
from signals import Snapshot

bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log


def book(mid=1, bids=(), asks=()):
    lv = [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": p, "quantity": q} for p, q in bids]
    lv += [{"exchangeId": 9, "side": "SELL", "isYes": True, "price": p, "quantity": q} for p, q in asks]
    return Book.from_levels(mid, lv)


def feed_with(history):
    f = scalper.PolyFeed.__new__(scalper.PolyFeed)
    import threading
    f.lock, f.max_age_s, f.hist = threading.Lock(), 30, collections.defaultdict(collections.deque)
    now = time.time()
    for sig, pts in history.items():
        for age, mid in pts:
            f.hist[sig].append((now - age, mid))
    return f


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = fv.Ledger(pathlib.Path(self.tmp.name) / "l.json")

    def tearDown(self):
        self.tmp.cleanup()


class FeedTests(unittest.TestCase):
    def test_move_and_movers(self):
        f = feed_with({1: [(400, 0.50), (250, 0.51), (5, 0.54)], 2: [(400, 0.30), (5, 0.305)], 3: [(10, 0.5)]})
        self.assertAlmostEqual(f.move(1, 300), 0.03)          # vs oldest point inside 300s
        self.assertIsNone(f.move(3, 300))                      # not enough history
        self.assertEqual(f.movers(0.02, 300), [1])
        self.assertAlmostEqual(f.mid(1), 0.54)

    def test_stale_feed_gives_nothing(self):
        f = feed_with({1: [(400, 0.5), (100, 0.6)]})
        self.assertIsNone(f.mid(1))
        self.assertIsNone(f.move(1, 300))


class LeadLagTests(Tmp):
    kw = dict(edge=0.015, move_min=0.02, unit=500, max_market=1500, gross_left=10000, tp=0.02, stop=0.03,
              max_hold_s=2700)

    def test_buy_when_reference_jumps_and_sig_lags(self):
        b = book(bids=[(0.48, 500)], asks=[(0.50, 600), (0.51, 2000), (0.53, 5000)])
        p = scalper.leadlag_plan(b, poly_mid=0.53, move=0.03, ledger=self.led, **self.kw)
        self.assertEqual((p["yes_side"], p["limit"]), ("BUY", 0.51))   # only levels <= 0.515
        self.assertLessEqual(p["capital"], 750 + 1)                     # 500 x 0.03/0.02

    def test_sell_when_reference_drops(self):
        b = book(bids=[(0.40, 1000)], asks=[(0.42, 1000)])
        p = scalper.leadlag_plan(b, poly_mid=0.37, move=-0.04, ledger=self.led, **self.kw)
        self.assertEqual(p["yes_side"], "SELL")

    def test_no_entry_without_move_or_lag(self):
        b = book(bids=[(0.48, 500)], asks=[(0.50, 600)])
        self.assertIsNone(scalper.leadlag_plan(b, 0.53, 0.01, self.led, **self.kw))     # move too small
        self.assertIsNone(scalper.leadlag_plan(b, 0.51, 0.03, self.led, **self.kw))     # SIG not behind

    def test_exits(self):
        self.led.record(1, "BUY", 100, 0.50)
        tgt = scalper.leadlag_plan(book(bids=[(0.525, 100)], asks=[(0.53, 1)]), 0.56, 0.0, self.led, **self.kw)
        self.assertEqual(tgt["exit"], "target")
        conv = scalper.leadlag_plan(book(bids=[(0.51, 100)], asks=[(0.515, 1)]), 0.512, 0.0, self.led, **self.kw)
        self.assertEqual(conv["exit"], "converged")
        stop = scalper.leadlag_plan(book(bids=[(0.46, 100)], asks=[(0.47, 1)]), 0.52, 0.0, self.led, **self.kw)
        self.assertEqual(stop["exit"], "stop")
        rev = scalper.leadlag_plan(book(bids=[(0.49, 100)], asks=[(0.495, 1)]), 0.495, 0.0, self.led, **self.kw)
        self.assertEqual(rev["exit"], "stop")                                          # reference back below entry
        self.led.rows[1]["opened_at"] = time.time() - 3000
        tm = scalper.leadlag_plan(book(bids=[(0.505, 100)], asks=[(0.51, 1)]), 0.53, 0.0, self.led, **self.kw)
        self.assertEqual(tm["exit"], "time")


class QuoteTests(unittest.TestCase):
    kw = dict(edge=0.01, size=300, max_inventory=1000, take=0.01, max_hold_s=1800, min_spread=0.02)

    def test_flat_quotes_inside_spread_but_away_from_fair(self):
        q = scalper.mm_quotes(book(bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, 0, None, 0, **self.kw)
        self.assertEqual(q, {"bid": (0.405, 300), "ask": (0.455, 300)})
        q = scalper.mm_quotes(book(bids=[(0.42, 100)], asks=[(0.44, 100)]), 0.43, 0, None, 0, **self.kw)
        self.assertEqual(q, {"bid": (0.42, 300), "ask": (0.44, 300)})                  # never inside fair +/- edge

    def test_narrow_spread_no_quotes(self):
        self.assertEqual(scalper.mm_quotes(book(bids=[(0.42, 1)], asks=[(0.43, 1)]), 0.425, 0, None, 0, **self.kw), {})

    def test_inventory_quotes_only_the_exit_priced_off_entry(self):
        b = book(bids=[(0.40, 1)], asks=[(0.46, 1)])
        q = scalper.mm_quotes(b, 0.43, 300, 0.405, 0, **self.kw)
        self.assertEqual(q, {"ask": (0.415, 300)})                                     # entry + 1c, no new bid
        q = scalper.mm_quotes(b, 0.43, -300, 0.455, 0, **self.kw)
        self.assertEqual(q, {"bid": (0.445, 300)})

    def test_exit_steps_down_to_break_even_then_flattens(self):
        kw = {**self.kw, "max_hold_s": 600, "flatten_s": 1800, "max_loss": 0.01}
        b = book(bids=[(0.40, 1)], asks=[(0.46, 1)])
        self.assertEqual(scalper.mm_quotes(b, 0.30, 300, 0.405, 900, **kw), {"ask": (0.405, 300)})   # fair ignored
        self.assertEqual(scalper.mm_quotes(b, 0.30, 300, 0.405, 2000, **kw), {"ask": (0.395, 300)})  # through the bid
        self.assertEqual(scalper.mm_quotes(b, 0.60, -300, 0.455, 2000, **kw), {"bid": (0.465, 300)})

    def test_classify_open_orders(self):
        self.assertEqual(scalper.classify_open_order({"side": "yes", "action": "buy", "priceLimit": 0.4, "quantity": 300}), ("bid", 0.4, 300))
        self.assertEqual(scalper.classify_open_order({"side": "no", "action": "buy", "priceLimit": 0.55, "quantity": -300}), ("ask", 0.45, 300))


class MarketMakerTests(Tmp):
    def mm(self, cli):
        placed = []

        def place(cli_, strategy, m, ex, side, px, qty, live, coid, holdings=None):
            placed.append((m, side, px, qty))
            return {"filledQuantity": 0}
        mm = scalper.MarketMaker(cli, self.led, live=True, place=place, requote_s=0)
        return mm, placed

    def test_quotes_then_infers_fill_from_holdings(self):
        cli = mock.Mock()
        mm, placed = self.mm(cli)
        mm.on_book(book(1, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, False, 0.0)
        self.assertEqual(sorted(placed), [(1, "BUY", 0.405, 300.0), (1, "SELL", 0.455, 300.0)])
        port = {"holdings": [{"marketId": 1, "settlementOption": "YES", "quantity": 300}], "openOrders": [
            {"id": "a1", "marketId": 1, "side": "yes", "action": "sell", "priceLimit": 0.455, "quantity": 300}]}
        fills = mm.sync(port, {})
        self.assertEqual(fills, [{"market_id": 1, "yes_side": "BUY", "qty": 300.0, "price": 0.405}])
        self.assertEqual(self.led.position(1), 300)

    def test_reference_move_pulls_quotes(self):
        cli = mock.Mock()
        mm, _ = self.mm(cli)
        mm.open = {1: [{"id": "x", "marketId": 1, "side": "yes", "action": "buy", "priceLimit": 0.4, "quantity": 300}]}
        act = mm.on_book(book(1, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, True, 0.0)
        self.assertEqual(act, {"pulled": 1})
        cli.cancel.assert_called_once_with("x", dry_run=False)

    def test_stale_quote_replaced(self):
        cli = mock.Mock()
        mm, placed = self.mm(cli)
        mm.open = {1: [{"id": "old", "marketId": 1, "side": "yes", "action": "buy", "priceLimit": 0.39, "quantity": 300}]}
        mm.on_book(book(1, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, False, 0.0)
        cli.cancel.assert_any_call("old", dry_run=False)
        self.assertIn((1, "BUY", 0.405, 300.0), placed)

    def test_unexplained_holding_is_not_absorbed(self):
        mm, _ = self.mm(mock.Mock())
        mm.active.add(5)
        fills = mm.sync({"holdings": [{"marketId": 5, "settlementOption": "YES", "quantity": 50}], "openOrders": []}, {})
        self.assertEqual(fills, [])                     # no quote of ours explains it: left to holdings_check
        self.assertEqual(self.led.position(5), 0)

    def test_self_test(self):
        cli = mock.Mock()
        mm, _ = self.mm(cli)
        order = {"id": "t1", "marketId": 1, "side": "yes", "action": "buy", "priceLimit": 0.01, "quantity": 50}
        cli.portfolio.side_effect = [{"openOrders": [order]}, {"openOrders": []}]
        with mock.patch.object(scalper.time, "sleep"):
            self.assertTrue(mm.self_test(book(1, bids=[(0.4, 1)]), 0.0))
        cli.cancel.assert_called_once_with("t1", dry_run=False)
        cli2 = mock.Mock(); mm2, _ = self.mm(cli2)
        cli2.portfolio.side_effect = [{"openOrders": [order]}, {"openOrders": [order]}]
        with mock.patch.object(scalper.time, "sleep"):
            self.assertFalse(mm2.self_test(book(1, bids=[(0.4, 1)]), 0.0))   # cancel did not work


class BotHookTests(Tmp):
    def test_leadlag_dry_run_journals_without_ledger_change(self):
        f = feed_with({1: [(200, 0.50), (2, 0.53)]})
        snap = Snapshot("t", [{"id": 1, "title": "Will the Republican Party win the Test Senate?"}],
                        {1: [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": 0.48, "quantity": 500},
                             {"exchangeId": 9, "side": "SELL", "isYes": True, "price": 0.50, "quantity": 600}]})
        cli = mock.Mock(); cli.place.return_value = {"dryRun": True, "orders": [], "filledQuantity": 600}
        a = argparse.Namespace(mode="auto", ll_lookback=300, ll_edge=0.015, ll_move=0.02, ll_unit=500, ll_max_market=1500,
                               ll_max_gross=10000, ll_tp=0.02, ll_stop=0.03, ll_max_hold=2700, max_gross=50000)
        d = pathlib.Path(self.tmp.name)
        with mock.patch.object(bot, "EXEC_LOG", d / "e.jsonl"), mock.patch.object(bot, "KILL_SWITCH", d / "K"):
            out = bot.run_leadlag(cli, snap, f, self.led, a, live=False)
        self.assertEqual(out["ll_orders"], 1)
        self.assertEqual(cli.place.call_args.args[2], "BUY")
        self.assertEqual(self.led.rows, {})


if __name__ == "__main__":
    unittest.main()


class PriceBandTests(Tmp):
    kw = dict(edge=0.01, size=300, max_inventory=1000, take=0.01, max_hold_s=1800, min_spread=0.02,
              min_price=0.10, max_price=0.90)

    def test_no_new_quotes_outside_band_but_exits_stay(self):
        lo = book(bids=[(0.005, 100)], asks=[(0.04, 100)])
        self.assertEqual(scalper.mm_quotes(lo, 0.0175, 0, None, 0, **self.kw), {})
        q = scalper.mm_quotes(book(bids=[(0.94, 100)], asks=[(0.99, 100)]), 0.975, 211, 0.95, 0, **self.kw)
        self.assertEqual(list(q), ["ask"])                               # exit for inventory only

    def test_eligibility(self):
        mm = scalper.MarketMaker(mock.Mock(), self.led, min_price=0.10, max_price=0.90)
        self.assertFalse(mm.eligible(1, 0, 0.03))
        self.assertTrue(mm.eligible(1, 0, 0.45))
        mm.active.add(2)
        self.assertTrue(mm.eligible(2, 0, 0.03))                          # still managed until flat


class WorkerTests(Tmp):
    def make(self, kill=lambda: False):
        cli = mock.Mock()
        order = {"id": "t1", "marketId": 1, "side": "yes", "action": "buy", "priceLimit": 0.01, "quantity": 50}
        cli.portfolio.side_effect = [{"openOrders": [], "holdings": []},          # first sync
                                     {"openOrders": [order]}, {"openOrders": []}]  # self-test
        placed = []

        def place(cli_, strategy, m, ex, side, px, qty, live, coid, holdings=None):
            placed.append((m, side, px))
            return {"filledQuantity": 0}
        mm = scalper.MarketMaker(cli, self.led, live=True, place=place, requote_s=0)
        return mm, cli, placed

    def wait(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def test_worker_self_tests_then_quotes(self):
        mm, cli, placed = self.make()
        with mock.patch.object(scalper.time, "sleep"):
            mm.start_worker(lambda: {}, lambda: False, poll_s=999)
            b = book(1, bids=[(0.40, 100)], asks=[(0.46, 100)])
            mm.submit(b, 0.43, False, 0.0, True)
            self.assertTrue(self.wait(lambda: mm.tested))
            self.assertTrue(mm.enabled)
            mm.submit(b, 0.43, False, 0.0, True)
            self.assertTrue(self.wait(lambda: len(placed) >= 3))      # self-test order + bid + ask
            mm.stop_worker(5)
        self.assertFalse(mm._thread.is_alive())
        self.assertIn((1, "BUY", 0.405), placed)
        self.assertIn((1, "SELL", 0.455), placed)

    def test_kill_switch_pulls_quotes_on_worker(self):
        mm, cli, _ = self.make()
        cli.portfolio.side_effect = None
        cli.portfolio.return_value = {"openOrders": [{"id": "q", "marketId": 1, "side": "yes", "action": "buy",
                                                      "priceLimit": 0.4, "quantity": 300}], "holdings": []}
        mm.start_worker(lambda: {}, lambda: True, poll_s=0)
        self.assertTrue(self.wait(lambda: cli.cancel.called))
        mm.stop_worker(5)
        cli.cancel.assert_any_call("q", dry_run=False)


class WorkerSafetyTests(WorkerTests):
    def test_transient_self_test_error_retries_and_disabled_never_quotes(self):
        mm, cli, placed = self.make()
        cli.portfolio.side_effect = None
        cli.portfolio.return_value = {"openOrders": [], "holdings": []}
        place_calls = []

        def boom(*a, **k):
            place_calls.append(a)
            raise RuntimeError("503")
        mm.place = boom
        with mock.patch.object(scalper.time, "sleep"):
            self.assertIsNone(mm.self_test(book(1, bids=[(0.4, 1)]), 0.0))   # error -> retry, not a verdict
        mm.tested, mm.enabled = True, False
        cli.portfolio.return_value = {"holdings": [], "openOrders": [
            {"id": "z", "marketId": 1, "side": "yes", "action": "buy", "priceLimit": 0.4, "quantity": 300}]}
        mm.start_worker(lambda: {}, lambda: False, poll_s=999)
        mm.live = True
        mm.submit(book(1, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, False, 0.0, True)
        self.assertTrue(self.wait(lambda: cli.cancel.called))
        mm.stop_worker(5)
        cli.cancel.assert_any_call("z", dry_run=False)                     # disabled: quotes pulled
        self.assertEqual(len(place_calls), 1)                              # and nothing new placed


class CapitalCapTests(Tmp):
    def test_at_capital_cap_only_exits_are_quoted(self):
        placed = []
        mm = scalper.MarketMaker(mock.Mock(), self.led, live=True, requote_s=0, max_capital=100,
                                 place=lambda *a, **k: placed.append((a[2], a[4], a[5])) or {"filledQuantity": 0})
        self.led.record(7, "BUY", 300, 0.40)                               # 120 of inventory >= cap
        mm.on_book(book(1, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, False, 0.0)
        self.assertEqual(placed, [])                                         # no new market
        mm.on_book(book(7, bids=[(0.40, 100)], asks=[(0.46, 100)]), 0.43, False, 300.0)
        self.assertEqual([p[1] for p in placed], ["SELL"])                  # exit only
