import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from dashboard import Source, handler
from levels_store import LevelsDB
from opportunities import list_opportunities, read_book, read_history


class OpportunityViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = Source(replay="fixtures/sample_snapshot.json",
                             browser_snapshot_path=root / "browser_snapshot.json")
        self.source.levels_db_path = root / "levels.sqlite3"
        self.source.kill_switch_path = root / "KILL_SWITCH"
        self.source.reconciliation_path = root / "reconciliation.json"
        breakers = root / "breakers.json"
        breakers.write_text('{"version":1,"events":[]}')
        self.source.news_breakers_path = breakers

    def tearDown(self):
        self.tmp.cleanup()

    def test_opportunities_include_server_state_and_gates(self):
        data = list_opportunities(self.source)
        self.assertIn("opportunities", data)
        self.assertTrue(data["opportunities"])
        row = data["opportunities"][0]
        self.assertIn(row["state"], {"blocked", "research_only", "paper_eligible", "exec_ready"})
        self.assertEqual(len(row["gates"]), 13)
        self.assertTrue(row["block_reasons"])
        json.dumps(data, allow_nan=False)

    def test_detail_walks_buy_no_against_sig_yes_bids(self):
        data = list_opportunities(self.source)
        detail = next(item for item in data["opportunities"] if item["strategy"] == "sig_arb")
        from opportunities import get_opportunity
        drawer = get_opportunity(self.source, detail["id"], 10)
        self.assertGreater(drawer["executable_qty"], 0)
        self.assertTrue(all(item["vwap"] is not None for item in drawer["depth_walk"]))
        self.assertIn("partial_fill_scenarios", drawer)
        self.assertIn("break_even", drawer)
        self.assertIn("mapping_evidence", drawer)
        self.assertIn("settlement_evidence", drawer)
        self.assertEqual(len(drawer["freshness"]), len(drawer["legs"]))
        json.dumps(drawer, allow_nan=False)

    def test_sig_book_is_explicit_and_history_does_not_impute(self):
        market_id = str(self.source.get().markets[0]["id"])
        book = read_book(self.source, "sig", market_id)
        self.assertFalse(book["missing"])
        self.assertEqual(book["venue"], "sig")
        history = read_history(self.source, market_id)
        self.assertTrue(history["missing"])
        self.assertTrue(history["gaps_are_null"] if "gaps_are_null" in history else True)

    def test_crossvenue_book_reads_latest_capture_read_only(self):
        db = LevelsDB(self.source.levels_db_path)
        db.ingest({"kalshi": {"markets": [{"market_id": "KX1"}], "books": [{
            "market_id": "KX1", "outcome_id": "YES", "observed_at": "2026-09-30T15:00:00+00:00",
            "bids": [{"price": 0.4, "size": 3}], "asks": [], "best_bid": 0.4,
            "best_ask": None, "best_bid_size": 3, "best_ask_size": None}],
            "coverage": {"complete": True}}}, captured_at="2026-09-30T15:00:00+00:00")
        db.close()
        before = self.source.levels_db_path.stat().st_mtime_ns
        book = read_book(self.source, "kalshi", "KX1")
        after = self.source.levels_db_path.stat().st_mtime_ns
        self.assertEqual(before, after)
        self.assertEqual(book["outcomes"][0]["bids"][0]["price"], "0.400000")

    def test_latest_book_survives_newer_capture_without_that_market(self):
        db = LevelsDB(self.source.levels_db_path)
        payload = {"kalshi": {"markets": [{"market_id": "KX1"}], "books": [{
            "market_id": "KX1", "outcome_id": "YES", "observed_at": "2026-09-30T15:00:00+00:00",
            "bids": [{"price": 0.4, "size": 3}], "asks": [], "best_bid": 0.4,
            "best_ask": None, "best_bid_size": 3, "best_ask_size": None}],
            "coverage": {"complete": True}}}
        db.ingest(payload, captured_at="2026-09-30T15:00:00+00:00")
        db.ingest({"kalshi": {"markets": [], "books": [], "coverage": {"complete": True}}},
                  captured_at="2026-09-30T15:01:00+00:00")
        db.close()
        book = read_book(self.source, "kalshi", "KX1")
        self.assertFalse(book["missing"])
        self.assertEqual(book["outcomes"][0]["best_bid"], "0.400000")

    def test_http_routes_and_js_allowlist(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.source, "tok"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for path in ("/api/opportunities", "/api/books/sig/447", "/api/history/447", "/js/api.js"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read()
                conn.close()
                self.assertEqual(response.status, 200, path)
                if path.startswith("/api/"):
                    json.loads(body)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
