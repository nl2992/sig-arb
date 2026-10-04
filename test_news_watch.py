"""SIG news feed as a trigger (boost the race) and a guard (entries follow the reference move)."""
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
import news_watch as nw
from signals import Snapshot
from test_fair_value import Refs

RACES = {"Texas Governor": [195, 196], "Alaska Senate": [377, 378]}


class FakeCli:
    def __init__(self):
        self.feeds = {}

    def _get(self, path, **params):
        return {"headlines": self.feeds.get(params["marketId"], []), "contextSummary": "s", "lastRefresh": "t"}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.cli = FakeCli()
        self.w = nw.NewsWatch(self.cli, RACES, hold_s=900, boost_s=1800, move_min=0.01, db=d / "n.sqlite3",
                              events_log=d / "e.jsonl")
        self.fair = {195: 0.245, 196: 0.755}

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_poll_is_a_baseline_then_only_new_headlines_fire(self):
        self.cli.feeds[195] = [{"title": "Old poll", "url": "u1"}]
        self.assertIsNone(self.w.poll_race("Texas Governor"))
        self.assertIsNone(self.w.poll_race("Texas Governor"))
        self.cli.feeds[195] = [{"title": "Old poll", "url": "u1"}, {"title": "New poll: tie", "url": "u2"}]
        ev = self.w.poll_race("Texas Governor")
        self.assertEqual([h["title"] for h in ev["headlines"]], ["New poll: tie"])
        self.assertIsNone(self.w.poll_race("Texas Governor"))

    def test_restart_does_not_refire(self):
        self.cli.feeds[195] = [{"title": "A", "url": "u1"}]
        self.w.poll_race("Texas Governor")
        again = nw.NewsWatch(self.cli, RACES, db=self.w.db, events_log=self.w.events_log)
        self.assertIsNone(again.poll_race("Texas Governor"))

    def fire(self):
        self.cli.feeds[195] = [{"title": "A", "url": "u1"}]
        self.w.poll_race("Texas Governor")
        self.cli.feeds[195].append({"title": "Shock poll", "url": "u2"})
        self.w.events.put(self.w.poll_race("Texas Governor"))
        rows = self.w.drain(self.fair.get)
        self.assertEqual(rows[0]["race"], "Texas Governor")

    def test_guard_waits_for_the_reference_then_only_follows_it(self):
        self.fire()
        self.assertFalse(self.w.entry_ok(195, "BUY", self.fair.get))     # reference has not moved yet
        self.assertFalse(self.w.entry_ok(195, "SELL", self.fair.get))
        self.fair[195] = 0.27                                               # Kalshi/Polymarket moved up 2.5c
        self.assertTrue(self.w.entry_ok(195, "BUY", self.fair.get))
        self.assertFalse(self.w.entry_ok(195, "SELL", self.fair.get))      # no bets against the move
        self.assertTrue(self.w.entry_ok(377, "SELL", self.fair.get))       # other races unaffected

    def test_hold_and_boost_expire(self):
        self.fire()
        self.assertEqual(self.w.held_markets(), frozenset({195, 196}))
        self.assertEqual(self.w.boosted_races(), {"Texas Governor"})
        h = self.w.holds["Texas Governor"]
        h["until"] = time.time() - 1
        self.assertTrue(self.w.entry_ok(195, "SELL", self.fair.get))
        self.assertEqual(self.w.held_markets(), frozenset())
        self.assertEqual(self.w.boosted_races(), {"Texas Governor"})     # still rescanned for a while
        h["boost_until"] = time.time() - 1
        self.assertEqual(self.w.boosted_races(), set())
        self.assertNotIn("Texas Governor", self.w.holds)

    def test_only_watched_races_are_due(self):
        self.w.watch(["Texas Governor", "Not a race"])
        self.assertEqual(self.w.due(time.time()), ["Texas Governor"])
        self.w.polled_at["Texas Governor"] = time.time()
        self.assertEqual(self.w.due(time.time()), [])


class StepTests(WatchTests):
    def test_step_polls_one_due_race_then_waits(self):
        self.w.watch(["Texas Governor", "Alaska Senate"])
        self.cli.feeds[195] = [{"title": "A", "url": "u1"}]
        self.w.step(now=10000.0)
        self.assertEqual(self.w.stats["polls"], 1)
        self.w.step(now=10001.0)                              # gap not over
        self.assertEqual(self.w.stats["polls"], 1)
        self.w.step(now=10011.0)
        self.assertEqual(self.w.stats["polls"], 2)

    def test_rate_limit_backs_off_quietly(self):
        self.w.watch(["Texas Governor"])
        def limited(path, **params):
            raise RuntimeError("/api/markets/[id]/news -> 429 Too Many Requests")
        self.cli._get = limited
        self.w.step(now=10000.0)
        self.w.step(now=10005.0)
        self.assertEqual(self.w.stats["errors"], 1)
        self.assertEqual(self.w.due(10005.0), ["Texas Governor"])


class BotGuardTests(unittest.TestCase):
    def test_fair_value_entries_respect_the_guard_but_exits_do_not(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            refs = Refs({("kalshi", "K"): (0.01, 0.03, 5)}, {386: [("kalshi", "K")]})
            snap = Snapshot("t", [{"id": 386, "title": "Will the Republican Party win the Delaware Senate?"}],
                            {386: [{"exchangeId": 9, "side": "BUY", "isYes": True, "price": 0.06, "quantity": 500}]})
            cli = mock.Mock()
            cli.place.return_value = {"dryRun": True, "orders": [], "filledQuantity": 500}
            a = argparse.Namespace(fv_threshold=0.03, fv_exit=0.01, fv_max_market=500, fv_max_gross=5000, mode="auto",
                                   fv_unit=500, fv_max_race=2500, max_gross=10000)
            with mock.patch.object(bot, "EXEC_LOG", d / "e.jsonl"), mock.patch.object(bot, "KILL_SWITCH", d / "K"):
                blocked = bot.run_fair_value(cli, snap, refs, fv.Ledger(d / "a.json"), a, live=False,
                                             news_ok=lambda m, s: False)
                allowed = bot.run_fair_value(cli, snap, refs, fv.Ledger(d / "b.json"), a, live=False,
                                             news_ok=lambda m, s: True)
                led = fv.Ledger(d / "c.json")
                led.record(386, "BUY", 100, 0.02)                 # long; SIG bid 0.06 >= target: exit
                exits = bot.run_fair_value(cli, snap, refs, led, a, live=False, news_ok=lambda m, s: False)
            self.assertEqual((blocked["fv_orders"], allowed["fv_orders"], exits["fv_orders"]), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
