import json
import tempfile
import unittest
from pathlib import Path

from levels_store import LevelsDB


class LevelsStoreTests(unittest.TestCase):
    def test_ingests_ladders_and_replays_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            db = LevelsDB(Path(directory) / "levels.sqlite3")
            payload = {"kalshi": {"markets": [], "coverage": {"requested_market_count": 1,
                        "returned_market_count": 1, "complete": True}, "books": [{
                            "market_id": "KX1", "outcome_id": "YES", "observed_at": "2026-09-30T00:00:00+00:00",
                            "source": "test", "best_bid": 0.4, "best_ask": 0.6,
                            "best_bid_size": 10, "best_ask_size": 5,
                            "bids": [{"price": 0.4, "size": 10}], "asks": [{"price": 0.6, "size": 5}]}]}}
            summary = db.ingest(payload)
            self.assertEqual(1, summary[0]["books"])
            self.assertEqual(1, len(db.observations("kalshi")))
            self.assertEqual(2, db.conn.execute("SELECT COUNT(*) FROM book_levels").fetchone()[0])
            db.close()


if __name__ == "__main__":
    unittest.main()
