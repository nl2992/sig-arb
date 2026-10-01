import sig_client
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from dashboard import Source, build_status, handler
from levels_store import LevelsDB


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = Source(replay='fixtures/sample_snapshot.json',
                             browser_snapshot_path=self.dir / 'browser_snapshot.json')
        self.source.kill_switch_path = self.dir / 'KILL_SWITCH'
        self.source.levels_db_path = self.dir / 'levels.sqlite3'
        self.source.reconciliation_path = self.dir / 'reconciliation.json'
        breakers = self.dir / 'breakers.json'
        breakers.write_text('{"version": 1, "events": []}')
        self.source.news_breakers_path = breakers

    def tearDown(self):
        self.tmp.cleanup()

    def gates(self, status):
        return {g['id']: g for g in status['gates']}

    def test_down_before_first_snapshot_and_no_network_fetch(self):
        status = build_status(self.source)
        self.assertEqual(status['health'], 'down')
        self.assertIsNone(self.source.snapshot)
        self.assertEqual(len(status['gates']), 13)

    def test_research_defaults_fail_closed(self):
        self.source.get()
        status = build_status(self.source)
        g = self.gates(status)
        self.assertEqual(status['data_mode'], 'replay')
        self.assertEqual(status['mode'], 'research')
        self.assertFalse(g['live_mode']['pass'])
        self.assertEqual(g['payload_verified']['pass'], sig_client.PLACE_PAYLOAD_CONFIRMED)
        self.assertFalse(g['sig_auth']['pass'])
        self.assertFalse(g['recon_clean']['pass'])
        self.assertFalse(g['quotes_fresh']['pass'])  # fixture snapshot is old
        self.assertTrue(g['risk_limits']['pass'])
        self.assertTrue(g['kill_switch_off']['pass'])
        self.assertIn('sig_books stale', status['health_reasons'])
        json.dumps(status, allow_nan=False)

    def test_kill_switch_file_engages(self):
        self.source.kill_switch_path.write_text('pre-open drill')
        status = build_status(self.source)
        self.assertTrue(status['kill_switch']['engaged'])
        self.assertEqual(status['kill_switch']['reason'], 'pre-open drill')
        self.assertFalse(self.gates(status)['kill_switch_off']['pass'])

    def test_levels_db_missing_and_present(self):
        self.assertEqual(build_status(self.source)['db']['error'], 'not found')
        db = LevelsDB(self.source.levels_db_path)
        db.ingest({'kalshi': {'markets': [], 'books': [], 'coverage': {'complete': True}}}, captured_at='2026-09-30T15:00:00+00:00')
        db.close()
        status = build_status(self.source)
        self.assertTrue(status['db']['ok'])
        self.assertEqual(status['db']['last_capture'], '2026-09-30T15:00:00+00:00')

    def test_levels_db_probe_is_read_only(self):
        sqlite3.connect(self.source.levels_db_path).close()  # empty file, no schema
        status = build_status(self.source)
        self.assertFalse(status['db']['ok'])
        conn = sqlite3.connect(self.source.levels_db_path)
        tables = conn.execute("SELECT name FROM sqlite_master").fetchall()
        conn.close()
        self.assertEqual(tables, [])

    def test_invalid_config_degrades_and_fails_gates(self):
        self.source.get()
        self.source.news_breakers_path.write_text('{"events": [{"id": "x"}]}')
        bad_limits = self.dir / 'limits.json'
        bad_limits.write_text('{"auto_hedge": true}')
        self.source.risk_limits_path = bad_limits
        status = build_status(self.source)
        self.assertEqual(status['health'], 'degraded')
        self.assertIsNotNone(status['breakers']['error'])
        self.assertFalse(self.gates(status)['risk_limits']['pass'])
        self.assertFalse(self.gates(status)['quotes_fresh']['pass'])

    def test_reconciliation_file_drives_recon_gate(self):
        self.source.reconciliation_path.write_text('{"status": "RECONCILED", "as_of": "2026-09-30T15:00:00Z"}')
        self.assertTrue(self.gates(build_status(self.source))['recon_clean']['pass'])
        self.source.reconciliation_path.write_text('{"status": "MISMATCH"}')
        self.assertFalse(self.gates(build_status(self.source))['recon_clean']['pass'])

    def test_status_endpoint_and_script_are_served(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.source, 'tok'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            bodies = {}
            for path, kind in (('/api/status', 'application/json'), ('/status.js', 'application/javascript')):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
                conn.request('GET', path)
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                self.assertEqual(resp.status, 200, path)
                self.assertTrue(resp.getheader('Content-Type').startswith(kind))
                bodies[path] = body
            self.assertEqual(len(json.loads(bodies['/api/status'])['gates']), 13)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
