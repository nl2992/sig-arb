"""Automated execution: bot safety rails and the dashboard execution endpoints."""
import argparse
import http.client
import json
import pathlib
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import bot
bot.INTENT_LOG = pathlib.Path(tempfile.mkdtemp()) / "order_intents.jsonl"   # never the real log
import gates
from dashboard import RELEASE_PHRASE, Source, execution_actions, handler
from signals import Snapshot, generate

FIX = pathlib.Path(__file__).with_name("fixtures") / "sample_snapshot.json"
TOKEN = "test-token"


class Args:
    min_edge = 0.0; min_pnl = 1; fee = 0.0; max_qty = None; max_per_race = None; near = 0


def synthetic():
    sigs, _, _ = generate(Snapshot.load(FIX), Args, set(), budget=None)
    return next(s for s in sigs if s.race == "Synthetic Senate")


class Recorder:
    def __init__(self, fill=1.0, unknown=False):
        self.calls, self.fill, self.unknown = [], fill, unknown

    def quote(self, *a):
        self.calls.append(("quote",) + a)

    def place(self, mid, ex, side, px, q, dry_run=True, client_order_id=None):
        self.calls.append(("place", mid, client_order_id))
        resp = {"orderId": f"o{mid}", "filledQuantity": q * self.fill, "avgPrice": px}
        if self.unknown:
            resp["_unknown"] = True
        return resp

    def cancel(self, oid, dry_run=True):
        self.calls.append(("cancel", oid))


class BotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kill = pathlib.Path(self.tmp.name) / "KILL_SWITCH"

    def tearDown(self):
        self.tmp.cleanup()

    def test_kill_switch_stops_before_first_leg(self):
        bot.engage_kill_switch("test", path=self.kill)
        cli = Recorder()
        res = bot.execute(cli, synthetic(), live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "KILLED")
        self.assertEqual(cli.calls, [])

    def test_engage_is_idempotent(self):
        bot.engage_kill_switch("first", path=self.kill)
        bot.engage_kill_switch("second", path=self.kill)
        self.assertTrue(self.kill.read_text().startswith("first"))

    def test_unknown_outcome_halts_without_further_legs(self):
        cli = Recorder(unknown=True)
        res = bot.execute(cli, synthetic(), live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "UNKNOWN")
        self.assertIn(res["status"], bot.HALT_STATUSES)
        self.assertEqual(sum(1 for c in cli.calls if c[0] == "place"), 1)

    def test_each_leg_gets_its_own_client_order_id(self):
        cli = Recorder()
        res = bot.execute(cli, synthetic(), live=True, kill_switch=self.kill, run_id="r1")
        self.assertEqual(res["status"], "DONE")
        self.assertEqual([c[2] for c in cli.calls if c[0] == "place"], ["r1:0", "r1:1"])
        self.assertEqual([l["client_order_id"] for l in res["legs"]], ["r1:0", "r1:1"])

    def test_no_quote_round_trip_by_default(self):
        cli = Recorder()
        bot.execute(cli, synthetic(), live=True, kill_switch=self.kill)
        self.assertFalse([c for c in cli.calls if c[0] == "quote"])

    def test_quotes_use_engine_representation(self):
        cli = Recorder()
        bot.execute(cli, synthetic(), live=True, kill_switch=self.kill, pre_quote=True)
        quote = next(c for c in cli.calls if c[0] == "quote")
        self.assertEqual(quote[2], "BUY")          # SELL_ALL legs are bought as NO
        self.assertLess(quote[4], 0)               # negative quantity = NO shares

    def test_preview_matches_first_pass_bodies(self):
        from test_client import client
        legs = bot.preview(client("x=1"), synthetic())
        self.assertEqual(len(legs), 2)
        body = legs[0]["bodies"][0]
        self.assertEqual(body["orderType"], "BUY")
        self.assertLess(body["quantity"], 0)
        self.assertAlmostEqual(body["priceLimit"], round(1 - legs[0]["limit"], 4))

    def test_limits_only_tighten(self):
        limits = gates.load_limits("config/risk_limits.json")
        a = argparse.Namespace(max_gross=float("inf"), max_per_race=10_000, min_edge=0.0)
        bot.apply_limits(a, limits)
        self.assertEqual(a.max_gross, limits["venue_exposure"]["sig"])
        self.assertEqual(a.max_per_race, min(limits["per_trade_capital"], limits["event_exposure"]))
        self.assertEqual(a.min_edge, limits["min_net_edge"])
        a = argparse.Namespace(max_gross=100, max_per_race=50, min_edge=0.05)
        bot.apply_limits(a, limits)
        self.assertEqual((a.max_gross, a.max_per_race, a.min_edge), (100, 50, 0.05))

    def test_live_blockers(self):
        limits = {"manual_approval": True}
        a = argparse.Namespace(mode="auto", live=True)
        with unittest.mock.patch.object(bot.sig_client, "PLACE_PAYLOAD_CONFIRMED", False):
            blockers = bot.live_blockers(a, limits, kill_switch=self.kill)
        self.assertTrue(any("manual_approval" in b for b in blockers))
        self.assertTrue(any("PLACE_PAYLOAD_CONFIRMED" in b for b in blockers))
        a = argparse.Namespace(mode="confirm", live=False)
        self.assertIn("--live not set", bot.live_blockers(a, limits, kill_switch=self.kill))


class ParallelExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kill = pathlib.Path(self.tmp.name) / "KILL_SWITCH"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, fills):
        """fills: {market_id: [fraction for first order, fraction for repair order, ...]}"""
        import threading
        lock, seq = threading.Lock(), {}

        class C(Recorder):
            def place(s, mid, ex, side, px, q, dry_run=True, client_order_id=None):
                with lock:
                    n = seq.get(mid, 0); seq[mid] = n + 1
                    s.calls.append(("place", mid, client_order_id, px, q))
                return {"orderId": f"o{mid}{n}", "quantityTraded": q, "filledQuantity": q * fills[mid][n], "avgPrice": px}
        return C()

    def legs(self):
        return [l["market_id"] for l in bot.leg_plan(synthetic())]

    def test_all_legs_fill(self):
        a, b = self.legs()
        cli = self.cli({a: [1.0], b: [1.0]})
        res = bot.execute_parallel(cli, synthetic(), live=True, kill_switch=self.kill, run_id="p")
        self.assertEqual(res["status"], "DONE")
        self.assertEqual(sorted(c[2] for c in cli.calls if c[0] == "place"), ["p:0", "p:1"])

    def test_short_leg_is_repaired_within_break_even(self):
        a, b = self.legs()
        cli = self.cli({a: [1.0], b: [0.0, 1.0]})
        r = synthetic()
        res = bot.execute_parallel(cli, r, live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "DONE")
        repair = [c for c in cli.calls if c[0] == "place" and c[2].endswith("r")]
        self.assertEqual(len(repair), 1)
        first_limit = next(l["limit"] for l in bot.leg_plan(r) if l["market_id"] == b)
        be = 1 - next(l["limit"] for l in bot.leg_plan(r) if l["market_id"] == a)
        self.assertGreaterEqual(repair[0][3] + 1e-9, max(be, first_limit - 0.01))   # SELL: never below b/e

    def test_failed_repair_is_legged(self):
        a, b = self.legs()
        cli = self.cli({a: [1.0], b: [0.0, 0.0]})
        res = bot.execute_parallel(cli, synthetic(), live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "LEGGED")
        self.assertIn(res["status"], bot.HALT_STATUSES)

    def test_both_miss(self):
        a, b = self.legs()
        res = bot.execute_parallel(self.cli({a: [0.0], b: [0.0]}), synthetic(), live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "MISS")

    def test_unknown_outcome_halts(self):
        cli = Recorder(unknown=True)
        res = bot.execute_parallel(cli, synthetic(), live=True, kill_switch=self.kill)
        self.assertEqual(res["status"], "UNKNOWN")

    def test_kill_switch(self):
        bot.engage_kill_switch("t", path=self.kill)
        cli = Recorder()
        self.assertEqual(bot.execute_parallel(cli, synthetic(), live=True, kill_switch=self.kill)["status"], "KILLED")
        self.assertEqual(cli.calls, [])


class DashboardExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.source = Source(replay=str(FIX), browser_snapshot_path=d / "browser_snapshot.json")
        self.source.kill_switch_path = d / "KILL_SWITCH"
        self.source.bot_status_path = d / "bot_status.json"
        self.source.exec_log_path = d / "executions.jsonl"
        self.source.audit_log_path = d / "audit.jsonl"
        self.source.reconciliation_path = d / "reconciliation.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          handler(self.source, TOKEN, execution_actions(self.source)))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=TOKEN, origin=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Action-Token"] = token
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, data

    def test_execution_view_reads_heartbeat_and_journal(self):
        status, data = self.request("GET", "/api/execution")
        self.assertEqual(status, 200)
        self.assertIsNone(data["bot"])
        self.assertEqual(data["executions"], [])
        bot.write_status(self.source.bot_status_path, mode="auto", live=False, interval=3)
        self.source.exec_log_path.write_text(json.dumps({"race": "A", "result": {"status": "DONE"}}) + "\nnot json\n"
                                             + json.dumps({"race": "B", "result": {"status": "MISS"}}) + "\n")
        status, data = self.request("GET", "/api/execution")
        self.assertTrue(data["bot"]["running"])
        self.assertEqual([e["race"] for e in data["executions"]], ["B", "A"])

    def test_kill_switch_engage_and_release(self):
        status, _ = self.request("POST", "/api/kill-switch/engage", {"reason": "test"}, token=None)
        self.assertEqual(status, 403)
        self.assertFalse(self.source.kill_switch_path.exists())
        status, data = self.request("POST", "/api/kill-switch/engage", {"reason": "test"})
        self.assertEqual(status, 200)
        self.assertTrue(self.source.kill_switch_path.exists())
        status, _ = self.request("POST", "/api/kill-switch/release", {"confirmation": "yes"})
        self.assertEqual(status, 400)
        self.assertTrue(self.source.kill_switch_path.exists())
        self.source.reconciliation_path.write_text(json.dumps({"kill_switch_required": True}))
        status, _ = self.request("POST", "/api/kill-switch/release", {"confirmation": RELEASE_PHRASE})
        self.assertEqual(status, 409)
        self.source.reconciliation_path.write_text(json.dumps({"kill_switch_required": False}))
        status, data = self.request("POST", "/api/kill-switch/release", {"confirmation": RELEASE_PHRASE})
        self.assertEqual(status, 200)
        self.assertFalse(self.source.kill_switch_path.exists())
        events = [json.loads(l)["event"] for l in self.source.audit_log_path.read_text().splitlines()]
        self.assertEqual(events, ["kill_switch_engage", "kill_switch_release"])

    def test_order_preview_is_dry_run(self):
        q = urlencode({"race": "Synthetic Senate", "direction": "SELL_ALL", "roi": 0})
        status, data = self.request("GET", "/api/orders/preview?" + q)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["dry_run"])
        self.assertEqual(len(data["legs"]), 2)
        self.assertIn("idempotencyKey", data["legs"][0]["bodies"][0])
        status, _ = self.request("GET", "/api/orders/preview?" + urlencode({"race": "Nope", "direction": "SELL_ALL"}))
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/api/orders/preview?" + urlencode({"race": "X", "direction": "BAD"}))
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()


class SharedBooksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "bot_books.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_books_file_uses_oldest_observation(self):
        m1, m2 = {"id": 1, "title": "a"}, {"id": 2, "title": "b"}
        bot.write_books(self.path, {1: (m1, [], "2026-10-01T20:00:10+00:00"),
                                    2: (m2, [], "2026-10-01T20:00:00+00:00")})
        j = json.loads(self.path.read_text())
        self.assertEqual(j["ts"], "2026-10-01T20:00:00+00:00")
        self.assertEqual(set(j["levels"]), {"1", "2"})

    def test_dashboard_prefers_fresh_bot_books_without_sig_calls(self):
        import unittest.mock as um
        snap = Snapshot.load(FIX)
        bot.write_books(self.path, {m["id"]: (m, snap.levels[m["id"]], snap.ts) for m in snap.markets})
        source = Source()
        source.bot_books_path = self.path
        with um.patch("dashboard.Client") as client:
            got = source.get()
        client.assert_not_called()
        self.assertEqual(len(got.markets), len(snap.markets))

    def test_stale_bot_books_fall_back_to_fetch(self):
        import os, unittest.mock as um
        snap = Snapshot.load(FIX)
        bot.write_books(self.path, {m["id"]: (m, snap.levels[m["id"]], snap.ts) for m in snap.markets})
        old = time.time() - 600
        os.utime(self.path, (old, old))
        source = Source()
        source.bot_books_path = self.path
        with um.patch.object(Source, "_fetch_snapshot", return_value=snap) as fetch:
            source.get()
        fetch.assert_called_once()


class DepthRuleTests(unittest.TestCase):
    def snap(self, d_bids, r_bids):
        lv = lambda ex, bids: [{"exchangeId": ex, "side": "BUY", "isYes": True, "price": p, "quantity": q} for p, q in bids] + \
            [{"exchangeId": ex, "side": "SELL", "isYes": True, "price": 0.99, "quantity": 10}]
        return Snapshot("t", [{"id": 1, "title": "Will the Democratic Party win the VA-01 House race?"},
                              {"id": 2, "title": "Will the Republican Party win the VA-01 House race?"}],
                        {1: lv(1, d_bids), 2: lv(2, r_bids)})

    def arb(self, snap, max_qty=None):
        from arb_engine import group_markets, max_executable_arb
        books = snap.books()
        return max_executable_arb("VA-01 House race", [books[1], books[2]], "SELL_ALL", max_qty=max_qty)

    a = argparse.Namespace(min_edge=0.0, fee=0.0, min_pnl=1, max_per_race=None)

    def test_deep_legs_keep_full_size(self):
        s = self.snap([(0.47, 5000)], [(0.555, 5000)])
        r = self.arb(s, max_qty=1000)                       # capped by budget, levels 5x deeper
        self.assertEqual(bot.depth_limited(r, s, self.a, None, 2.0).qty, 1000)

    def test_thin_leg_shrinks_trade(self):
        s = self.snap([(0.47, 5000)], [(0.555, 332)])      # VA-01: R bid only 332 deep
        r = self.arb(s)
        self.assertEqual(r.qty, 332)
        sized = bot.depth_limited(r, s, self.a, None, 2.0)
        self.assertEqual(sized.qty, 166)
        self.assertGreaterEqual(min(bot.leg_depth(s.books()[l.market_id], "SELL_ALL", l.limit) for l in sized.legs), 2 * sized.qty)

    def test_too_thin_skips(self):
        s = self.snap([(0.47, 5000)], [(0.555, 15)])
        self.assertIsNone(bot.depth_limited(self.arb(s), s, self.a, None, 2.0))
