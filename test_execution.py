"""Automated execution: bot safety rails and the dashboard execution endpoints."""
import argparse
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import bot
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

    def test_quotes_use_engine_representation(self):
        cli = Recorder()
        bot.execute(cli, synthetic(), live=True, kill_switch=self.kill)
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
        a = argparse.Namespace(max_gross=60_000, max_per_race=10_000, min_edge=0.0)
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
        blockers = bot.live_blockers(a, limits, kill_switch=self.kill)
        self.assertTrue(any("manual_approval" in b for b in blockers))
        self.assertTrue(any("PLACE_PAYLOAD_CONFIRMED" in b for b in blockers))
        a = argparse.Namespace(mode="confirm", live=False)
        self.assertIn("--live not set", bot.live_blockers(a, limits, kill_switch=self.kill))


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
