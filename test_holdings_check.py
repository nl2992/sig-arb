"""Write-ahead order intents and holdings reconciliation."""
import json
import pathlib
import tempfile
import unittest

import fair_value as fv
import holdings_check as hc


def holding(mid, qty, avg=0.5):
    return {"marketId": mid, "settlementOption": "YES", "quantity": qty, "averagePricePaid": avg}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.intents, self.execs, self.manual = d / "i.jsonl", d / "e.jsonl", d / "m.jsonl"
        self.ledger = fv.Ledger(d / "l.json")
        self.kills = []
        self.check = hc.HoldingsCheck(self.ledger, self.kills.append, self.intents, self.execs, self.manual)

    def tearDown(self):
        self.tmp.cleanup()

    def arb_fill(self, legs):
        with self.execs.open("a") as f:
            f.write(json.dumps({"live": True, "result": {"status": "DONE", "legs": [
                {"market": m, "side": "SELL", "filled": q, "dryRun": False} for m, q in legs]}}) + "\n")


class ReconcileTests(Base):
    def test_matching_books_are_ok(self):
        self.arb_fill([(1, 100), (2, 100)])
        self.ledger.record(3, "BUY", 50, 0.9)
        rep = self.check.check({"holdings": [holding(1, -100), holding(2, -100), holding(3, 50)]})
        self.assertTrue(rep["ok"])
        self.assertEqual(self.kills, [])

    def test_unexplained_difference_kills_on_second_strike(self):
        port = {"holdings": [holding(7, -174)]}
        self.assertFalse(self.check.check(port)["ok"])
        self.assertEqual(self.kills, [])                       # first strike: maybe still settling
        self.check.check(port)
        self.assertEqual(len(self.kills), 1)
        self.assertIn("#7 actual -174 expected 0", self.kills[0])

    def test_startup_is_immediate(self):
        self.check.check({"holdings": [holding(7, -174)]}, immediate=True)
        self.assertEqual(len(self.kills), 1)

    def test_difference_that_clears_resets_strikes(self):
        self.check.check({"holdings": [holding(7, -174)]})
        self.check.check({"holdings": []})
        self.check.check({"holdings": [holding(7, -174)]})
        self.assertEqual(self.kills, [])


class IntentRecoveryTests(Base):
    def test_in_doubt_fv_order_is_added_to_ledger_at_sig_price(self):
        hc.write_intent("fv:1", "fv", 376, "SELL", 0.075, 300, self.intents)          # process died here
        txns = [{"marketId": "376", "event_type": "trade", "quantity": -174, "price": 0.925,
                 "createdAt": "2999-01-01T00:00:00.000Z"}]
        rep = self.check.check({"holdings": [holding(376, -174)]}, lambda: txns, immediate=True)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.ledger.position(376), -174)
        self.assertAlmostEqual(self.ledger.capital(376), 174 * 0.925)
        self.assertEqual(self.kills, [])
        self.assertEqual(hc.in_doubt(self.intents), [])

    def test_in_doubt_order_that_never_filled_is_closed_out(self):
        hc.write_intent("fv:2", "fv", 5, "BUY", 0.9, 100, self.intents)
        rep = self.check.check({"holdings": []}, lambda: [], immediate=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(hc.in_doubt(self.intents), [])

    def test_in_doubt_arb_leg_is_journaled_and_kills_for_review(self):
        hc.write_intent("r:0", "arb", 9, "SELL", 0.2, 500, self.intents)
        self.check.check({"holdings": [holding(9, -500)]}, lambda: [], immediate=True)
        self.assertEqual(hc.expected_holdings(self.ledger.rows, self.execs, self.manual)[9], -500)
        self.assertEqual(len(self.kills), 1)
        self.assertIn("still hedged", self.kills[0])

    def test_resolved_intents_are_not_in_doubt(self):
        hc.write_intent("a", "fv", 1, "BUY", 0.5, 10, self.intents)
        hc.resolve_intent("a", "DONE", self.intents, filled=10)
        hc.write_intent("b", "fv", 1, "BUY", 0.5, 10, self.intents)
        hc.resolve_intent("b", "UNKNOWN", self.intents)
        self.assertEqual([i["client_order_id"] for i in hc.in_doubt(self.intents)], ["b"])

    def test_difference_larger_than_intent_is_not_absorbed(self):
        hc.write_intent("fv:3", "fv", 4, "SELL", 0.1, 100, self.intents)
        self.check.check({"holdings": [holding(4, -400)]}, lambda: [], immediate=True)
        self.assertEqual(self.ledger.position(4), 0)
        self.assertEqual(len(self.kills), 1)


class PlaceTrackedTests(unittest.TestCase):
    def test_intent_written_before_place_and_resolved_after(self):
        import bot
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "i.jsonl"
            seen = {}

            def place(*a, **k):
                seen["before"] = [json.loads(l)["state"] for l in path.read_text().splitlines()]
                return {"filledQuantity": 10, "quantityTraded": 10}
            cli = mock.Mock(place=mock.Mock(side_effect=place))
            with mock.patch.object(bot, "INTENT_LOG", path):
                bot.place_tracked(cli, "fv", 1, 9, "BUY", 0.5, 10, True, "c1", holdings=0)
                states = [json.loads(l)["state"] for l in path.read_text().splitlines()]
            self.assertEqual(seen["before"], ["SENT"])
            self.assertEqual(states, ["SENT", "DONE"])

    def test_dry_run_writes_no_intent(self):
        import bot
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "i.jsonl"
            cli = mock.Mock(place=mock.Mock(return_value={"dryRun": True}))
            with mock.patch.object(bot, "INTENT_LOG", path):
                bot.place_tracked(cli, "fv", 1, 9, "BUY", 0.5, 10, False, "c1")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()


class DeferredStopTests(unittest.TestCase):
    def test_stop_signal_waits_for_order_to_be_booked(self):
        import bot
        booked = []
        with self.assertRaises(KeyboardInterrupt):
            with bot.critical():
                bot._on_stop_signal(2, None)        # Ctrl+C arrives mid-order: deferred
                booked.append("journaled")          # ...so the fill still gets recorded
        self.assertEqual(booked, ["journaled"])
        self.assertFalse(bot._stop_pending)

    def test_stop_outside_an_order_is_immediate(self):
        import bot
        with self.assertRaises(KeyboardInterrupt):
            bot._on_stop_signal(2, None)


class SweepTests(unittest.TestCase):
    def test_cancel_all_open_orders(self):
        import bot
        from unittest import mock
        cli = mock.Mock()
        cli.portfolio.return_value = {"openOrders": [{"id": "a", "marketId": 1}, {"id": "b", "marketId": 2}]}
        cli.cancel.side_effect = [None, RuntimeError("gone")]
        self.assertEqual(bot.cancel_all_open_orders(cli), 1)
        self.assertEqual([c.args[0] for c in cli.cancel.call_args_list], ["a", "b"])
