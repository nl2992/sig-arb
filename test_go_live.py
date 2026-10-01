"""go_live.py: readiness checklist and manual-order comparison (no network)."""
import base64
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import go_live
import sig_client
from test_client import client

ONE_HOUR = int(time.time()) + 3600


def jwt(exp):
    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def session_client(exp=ONE_HOUR):
    c = client("foo=1")
    c.cookie, c.access_token, c.profile_id = "foo=1", jwt(exp), "86ccda88"
    return c


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, cli):
        rows = go_live.check(offline=True, kill_switch=self.d / "KILL", capture_report=self.d / "cap.json", cli=cli)
        return {r["check"]: r for r in rows}

    def test_missing_cookie_fails_with_action(self):
        rows = self.run_check(client(""))
        self.assertEqual(rows["sig_cookie"]["status"], "FAIL")
        self.assertEqual(rows["sig_session"]["status"], "FAIL")
        self.assertIn(".env", rows["sig_cookie"]["action"])

    def test_session_expiry(self):
        self.assertEqual(self.run_check(session_client())["sig_session"]["status"], "PASS")
        self.assertEqual(self.run_check(session_client(int(time.time()) + 60))["sig_session"]["status"], "FAIL")

    def test_no_secret_in_output(self):
        c = session_client()
        text = json.dumps(self.run_check(c))
        self.assertNotIn(c.access_token, text)

    def test_capture_and_payload_flag_gate_readiness(self):
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", False):
            rows = self.run_check(session_client())
        self.assertEqual(rows["test_order_compared"]["status"], "TODO")
        self.assertEqual(rows["payload_confirmed"]["status"], "TODO")
        (self.d / "cap.json").write_text(json.dumps({"payload_match": True, "fill_fields_readable": True}))
        with mock.patch.object(sig_client, "PLACE_PAYLOAD_CONFIRMED", True):
            rows = self.run_check(session_client())
        self.assertEqual(rows["test_order_compared"]["status"], "PASS")
        self.assertEqual(rows["payload_confirmed"]["status"], "PASS")


class SetCookieTests(unittest.TestCase):
    def test_writes_env_and_keeps_other_lines(self):
        from test_client import COOKIE
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text("SIG_COOKIE=old\nSIG_TOURNAMENT=t1\n")
            go_live.set_cookie(" " + COOKIE + "\n", env)
            lines = env.read_text().splitlines()
            self.assertEqual(lines[0], "SIG_COOKIE=" + COOKIE)
            self.assertIn("SIG_TOURNAMENT=t1", lines)
            self.assertEqual(sum(l.startswith("SIG_COOKIE=") for l in lines), 1)
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_rejects_cookie_without_session(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                go_live.set_cookie("_ga=1", pathlib.Path(d) / ".env")


class CompareTests(unittest.TestCase):
    def capture(self, **response):
        body = session_client().place_raw(1042, "BUY", 0.07, -5)["body"]
        return {"payload": body, "response": response}

    def test_matching_capture(self):
        r = go_live.compare(self.capture(orderId="o1", quantityTraded=-5, avgPrice=0.07), session_client())
        self.assertTrue(r["payload_match"], r["problems"])
        self.assertTrue(r["fill_fields_readable"])

    def test_field_drift_and_unreadable_fills_are_reported(self):
        cap = self.capture(id="o1", filled=5)
        cap["payload"]["isLimitOrder"] = True
        del cap["payload"]["open"]
        r = go_live.compare(cap, session_client())
        self.assertFalse(r["payload_match"])
        self.assertFalse(r["fill_fields_readable"])
        text = " ".join(r["problems"])
        self.assertIn("isLimitOrder", text)
        self.assertIn("open", text)
        self.assertIn("quantityTraded", text)

    def test_rejects_malformed_capture(self):
        with self.assertRaises(ValueError):
            go_live.compare({"payload": []}, session_client())


if __name__ == "__main__":
    unittest.main()
