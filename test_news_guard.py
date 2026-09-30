import json
import tempfile
import unittest
from pathlib import Path

from news_guard import active_breakers, load_circuit_breakers


class NewsGuardTests(unittest.TestCase):
    def test_expired_and_cleared_events_do_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({"events": [
                {"id": "expired", "market_ids": [1], "reason": "old", "status": "ACTIVE",
                 "expires_at": "2026-09-29T00:00:00Z"},
                {"id": "cleared", "market_ids": [2], "reason": "done", "status": "CLEARED"},
                {"id": "live", "market_ids": [3], "reason": "material update", "status": "ACTIVE",
                 "expires_at": "2026-10-02T00:00:00Z"},
            ]}))
            active = active_breakers(path, now=__import__('datetime').datetime.fromisoformat('2026-09-30T00:00:00+00:00'))
            self.assertEqual([], active.get(1, []))
            self.assertEqual([], active.get(2, []))
            self.assertEqual("live", active[3][0]["id"])

    def test_missing_reason_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps({"events": [{"id": "bad", "market_ids": [1]}]}))
            with self.assertRaises(ValueError):
                load_circuit_breakers(path)


if __name__ == "__main__":
    unittest.main()
