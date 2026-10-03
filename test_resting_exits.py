"""Resting take-profit orders for fair-value and conviction positions, and the conviction
take-profit rule."""
import argparse
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import bot
bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log
import conviction as cv
import fair_value as fv
import holdings_check as hc
import resting_exits as rx
from signals import Snapshot
from test_fair_value import Refs, book

FV = {"tp": 0.015, "band": 0.01, "stop": 0.05}
CV = {"band": 0.005, "stop": 0.10, "floor_at_entry": True}


class FakeCli:
    def __init__(self):
        self.placed, self.cancelled, self.open = [], [], []
        self.next_id = 100

    def portfolio(self):
        return {"openOrders": list(self.open), "holdings": []}

    def cancel(self, oid, dry_run=True):
        self.cancelled.append(oid)
        self.open = [o for o in self.open if o["id"] != oid]


def fake_place(cli, strategy, m, exchange_id, yes_side, limit, qty, live, coid, holdings=None, resting=False):
    assert resting
    cli.next_id += 1
    cli.placed.append({"strategy": strategy, "market": m, "yes_side": yes_side, "limit": limit, "qty": qty,
                       "holdings": holdings, "id": cli.next_id})
    # SIG lists a YES ask as side yes/sell; a YES bid on a short as side no/sell at 1 - price
    if yes_side == "SELL":
        cli.open.append({"id": cli.next_id, "marketId": str(m), "side": "yes", "action": "sell", "quantity": qty,
                         "priceLimit": limit})
    else:
        cli.open.append({"id": cli.next_id, "marketId": str(m), "side": "no", "action": "sell", "quantity": qty,
                         "priceLimit": round(1 - limit, 4)})
    return {"orders": [{"id": cli.next_id}], "filledQuantity": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.intents = d / "intents.jsonl"
        self.fv, self.cv = fv.Ledger(d / "fv.json"), fv.Ledger(d / "cv.json")
        self.cli = FakeCli()
        self.ex = rx.RestingExits(self.cli, {"fv": self.fv, "cv": self.cv}, fake_place, rules={"fv": FV, "cv": CV},
                                  live=True, requote_s=60, grace_s=180, intent_log=self.intents)

    def tearDown(self):
        self.tmp.cleanup()

    def bk(self, m=1, bids=((0.79, 500),), asks=((0.84, 500),)):
        return book(m, bids=bids, asks=asks)

    def portfolio(self, holdings):
        return {"openOrders": list(self.cli.open),
                "holdings": [{"marketId": m, "quantity": q, "settlementOption": "YES"} for m, q in holdings.items()]}

    def expected(self):
        rows = {}
        for led in (self.fv, self.cv):
            for k, r in led.rows.items():
                rows.setdefault(int(k), {"qty": 0.0})["qty"] += float(r.get("qty", 0))
        return lambda: {k: v["qty"] for k, v in rows.items()}


class PriceTests(unittest.TestCase):
    def test_fair_value_prices_match_the_exit_rules(self):
        self.assertEqual(rx.exit_price(FV, 2475, 0.805, 0.845), 0.82)     # target (entry + 1.5c) binds
        self.assertEqual(rx.exit_price(FV, -2741, 0.36, 0.325), 0.345)    # short: buy back 1.5c under entry
        self.assertEqual(rx.exit_price(FV, 100, 0.80, 0.81), 0.80)        # converged (fair - 1c) binds

    def test_conviction_takes_profit_at_fair_never_below_entry(self):
        self.assertEqual(rx.exit_price(CV, 3562, 0.712, 0.74), 0.735)
        self.assertEqual(rx.exit_price(CV, -5333, 0.25, 0.225), 0.23)
        self.assertEqual(rx.exit_price(CV, 3562, 0.71, 0.66), 0.71)       # losing: only at break-even

    def test_prices_outside_the_book_are_refused(self):
        self.assertIsNone(rx.exit_price({"tp": 0.02, "band": 0.0}, 100, 0.99, 1.0))   # would sell at 1.00


class WantTests(Base):
    def test_sized_to_the_account_holding(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        w = self.ex.want(1, {"fv": 0.84}, holding=600, busy=False)
        self.assertEqual((w["strategy"], w["yes_side"], w["price"], w["qty"]), ("fv", "SELL", 0.815, 600))

    def test_no_order_when_busy_netted_stopped_shared_or_unpriced(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.assertIsNone(self.ex.want(1, {"fv": 0.84}, 1000, busy=True))
        self.assertIsNone(self.ex.want(1, {"fv": 0.84}, -200, busy=False))     # account nets the other way
        self.assertIsNone(self.ex.want(1, {"fv": 0.74}, 1000, busy=False))     # stop: fair 6c under entry
        self.assertIsNone(self.ex.want(1, {"fv": None}, 1000, busy=False))
        self.cv.record(1, "BUY", 100, 0.80)
        self.assertIsNone(self.ex.want(1, {"fv": 0.84, "cv": 0.84}, 1100, busy=False))   # two strategies


class OrderTests(Base):
    def test_place_once_then_no_churn(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        act = self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        self.assertEqual(act["placed"], ("fv", "SELL", 0.815, 1000.0))
        self.assertEqual(self.ex.on_book(self.bk(), {"fv": 0.85}, 1000, False), {})   # same price: target binds
        self.assertEqual(len(self.cli.placed), 1)
        self.assertIn(1, self.ex.takers_held)

    def test_reprice_waits_for_requote_interval_but_shrinks_at_once(self):
        self.cv.record(2, "BUY", 1000, 0.70)
        self.ex.on_book(self.bk(2), {"cv": 0.74}, 1000, False)
        self.assertEqual(self.ex.on_book(self.bk(2), {"cv": 0.76}, 1000, False), {})   # fair moved: wait
        act = self.ex.on_book(self.bk(2), {"cv": 0.76}, 400, False)                     # account holds less
        self.assertEqual(act["placed"][3], 400.0)
        self.assertEqual(self.cli.cancelled, [self.cli.placed[0]["id"]])

    def test_cancel_when_nothing_is_wanted_and_hold_takers_until_a_snapshot(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        self.assertEqual(self.ex.on_book(self.bk(), {"fv": 0.74}, 1000, False), {"cancelled": 1})   # stop hit
        self.assertNotIn(1, self.ex.markets_snapshot)
        self.assertIn(1, self.ex.takers_held)                 # the taker stop waits for the next snapshot
        self.ex.sync(self.portfolio({1: 1000}), self.expected(), fetched_at=time.time() + 5)
        self.assertNotIn(1, self.ex.takers_held)

    def test_vanished_order_is_cancelled_before_a_replacement(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        first = self.cli.placed[0]["id"]
        self.cli.open = []                                    # SIG no longer lists it
        self.ex.sync(self.portfolio({1: 1000}), self.expected(), fetched_at=time.time() + 30)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        self.assertEqual(self.cli.cancelled, [first])
        self.assertEqual(len(self.cli.placed), 2)


class ClaimTests(Base):
    def test_taker_cannot_claim_a_market_a_resting_exit_works(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        self.assertFalse(self.ex.claim_taker(1))
        self.assertTrue(self.ex.claim_taker(2))

    def test_after_a_taker_claim_no_resting_exit_until_the_snapshot(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.assertTrue(self.ex.claim_taker(1))
        self.assertEqual(self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False), {})
        self.ex.sync(self.portfolio({1: 1000}), self.expected(), fetched_at=time.time() + 5)
        self.assertIn("placed", self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False))

    def test_a_failed_placement_is_found_and_cancelled_before_retrying(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        def boom(*a, **k):
            raise RuntimeError("503")
        self.ex.place = boom
        with self.assertRaises(RuntimeError):
            self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        self.assertIn(1, self.ex.takers_held)                 # it may be resting: takers stay out
        self.ex.place = fake_place
        self.assertIn("placed", self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False))
        self.assertEqual(len(self.cli.placed), 1)


class FillTests(Base):
    def test_partial_fill_booked_to_the_strategy_at_the_order_price(self):
        self.fv.record(1, "SELL", 1000, 0.36)                 # short YES (holds NO)
        self.ex.on_book(self.bk(), {"fv": 0.325}, -1000, False)
        fills = self.ex.sync(self.portfolio({1: -600}), self.expected(), fetched_at=time.time())
        self.assertEqual(fills, [{"market_id": 1, "strategy": "fv", "yes_side": "BUY", "qty": 400.0, "price": 0.345}])
        self.assertEqual(self.fv.position(1), -600)
        self.assertAlmostEqual(self.fv.rows[1]["realized"], 400 * 0.015)
        self.assertEqual(self.ex.sync(self.portfolio({1: -600}), self.expected(), fetched_at=time.time()), [])

    def test_fill_capped_at_the_order_and_wrong_direction_ignored(self):
        self.fv.record(1, "BUY", 1000, 0.80)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 600, False)                 # rests 600
        self.assertEqual(self.ex.sync(self.portfolio({1: 1200}), self.expected(), time.time()), [])   # more YES: not ours
        fills = self.ex.sync(self.portfolio({1: 0}), self.expected(), time.time())
        self.assertEqual(fills[0]["qty"], 600.0)              # the other 400 is for reconciliation to flag
        self.assertEqual(self.fv.position(1), 400)

    def test_fill_found_after_cancel_within_grace(self):
        self.cv.record(2, "BUY", 1000, 0.70)
        self.ex.on_book(self.bk(2), {"cv": 0.74}, 1000, False)
        self.ex.on_book(self.bk(2), {"cv": 0.60}, 1000, False)               # stop: cancelled
        fills = self.ex.sync(self.portfolio({2: 700}), self.expected(), time.time())   # 300 filled before the cancel
        self.assertEqual((fills[0]["strategy"], fills[0]["qty"], fills[0]["price"]), ("cv", 300.0, 0.735))
        self.ex.gone[2][0]["until"] = 0                       # grace over
        self.ex.sync(self.portfolio({2: 700}), self.expected(), time.time())
        self.assertNotIn(2, self.ex.gone)
        last = [json.loads(x) for x in self.intents.read_text().splitlines()][-1]
        self.assertEqual((last["state"], last["filled"]), ("CLOSED", 300.0))

    def test_reconciliation_backstop_sees_resting_intents(self):
        hc.write_intent("fvx:1:1", "fv", 1, "SELL", 0.82, 500, self.intents, resting=True)
        hc.resolve_intent("fvx:1:1", "DONE", self.intents, filled=0)
        self.assertEqual([d["qty"] for d in hc.in_doubt(self.intents)], [500.0])
        hc.resolve_intent("fvx:1:1", "DONE", self.intents, filled=200)
        self.assertEqual([d["qty"] for d in hc.in_doubt(self.intents)], [300.0])
        hc.resolve_intent("fvx:1:1", "CLOSED", self.intents, filled=200)
        self.assertEqual(hc.in_doubt(self.intents), [])


class MarketMakerSeparationTests(Base):
    def test_mm_does_not_claim_a_fill_in_a_market_resting_exits_work(self):
        import scalper
        mm_led = fv.Ledger(pathlib.Path(self.tmp.name) / "mm.json")
        mm_led.record(1, "BUY", 100, 0.80)
        mm_led.record(1, "SELL", 100, 0.81)                   # old MM round trip: row left at zero
        mm = scalper.MarketMaker(self.cli, mm_led)
        mm.last_quote_px[(1, "ask")] = 0.81
        self.fv.record(1, "BUY", 1000, 0.80)
        self.ex.on_book(self.bk(), {"fv": 0.84}, 1000, False)
        mm.skip_sync = lambda: self.ex.claimed
        port = self.portfolio({1: 600})                        # 400 of the resting exit filled
        self.assertEqual(mm.sync(port, {1: 1000.0}), [])
        self.assertEqual(self.ex.sync(port, self.expected(), time.time())[0]["qty"], 400.0)


class ConvictionTakeProfitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = fv.Ledger(pathlib.Path(self.tmp.name) / "cv.json")
        self.kw = dict(min_edge=0.02, max_bet=4000, gross_left=0, stop=0.10, max_slip=0.03, exit_band=0.005)

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_takes_profit_once_sig_reaches_fair(self):
        self.led.record(1, "BUY", 1000, 0.712)
        p = cv.plan(book(bids=[(0.74, 300)], asks=[(0.75, 10)]), 0.74, self.led, **self.kw)
        self.assertEqual((p["exit"], p["yes_side"], p["limit"], p["qty"]), ("converged", "SELL", 0.74, 300.0))
        self.assertIsNone(cv.plan(book(bids=[(0.725, 300)], asks=[(0.75, 10)]), 0.74, self.led, **self.kw))

    def test_never_takes_profit_below_the_entry(self):
        self.led.record(1, "BUY", 1000, 0.712)
        self.assertIsNone(cv.plan(book(bids=[(0.70, 300)], asks=[(0.75, 10)]), 0.70, self.led, **self.kw))

    def test_short_takes_profit(self):
        self.led.record(1, "SELL", 1000, 0.25)
        p = cv.plan(book(bids=[(0.20, 10)], asks=[(0.225, 500)]), 0.225, self.led, **self.kw)
        self.assertEqual((p["exit"], p["yes_side"], p["limit"]), ("converged", "BUY", 0.225))


class BotHookTests(unittest.TestCase):
    def test_fair_value_taker_exits_wait_where_a_resting_exit_works(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            led = fv.Ledger(d / "l.json")
            led.record(386, "SELL", 500, 0.075)                # short; fair 0.02, SIG ask 0.03: target hit
            refs = Refs({("kalshi", "K"): (0.01, 0.03, 5)}, {386: [("kalshi", "K")]})
            snap = Snapshot("t", [{"id": 386, "title": "Will the Republican Party win the Delaware Senate?"}],
                            {386: [{"exchangeId": 9, "side": "SELL", "isYes": True, "price": 0.03, "quantity": 500}]})
            cli = mock.Mock()
            cli.place.return_value = {"dryRun": True, "orders": [], "filledQuantity": 500}
            a = argparse.Namespace(fv_threshold=0.03, fv_exit=0.01, fv_max_market=500, fv_max_gross=5000, mode="auto",
                                   fv_unit=500, fv_max_race=2500, max_gross=10000, fv_tp=0.015, fv_stop=0.05)
            with mock.patch.object(bot, "EXEC_LOG", d / "e.jsonl"), mock.patch.object(bot, "KILL_SWITCH", d / "K"):
                held = bot.run_fair_value(cli, snap, refs, led, a, live=False, exits_claim=lambda m: False)
                free = bot.run_fair_value(cli, snap, refs, led, a, live=False, exits_claim=lambda m: True)
            self.assertEqual((held["fv_orders"], free["fv_orders"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
