import json
import tempfile
import unittest
from pathlib import Path

from system_check import readiness


class SystemCheckTests(unittest.TestCase):
    def test_default_readiness_is_paper_only(self):
        result = readiness()
        self.assertFalse(result["ready_for_paper"])
        self.assertFalse(result["ready_for_live"])
        self.assertEqual("BLOCKED", result["checks"]["execution_gate"]["status"])

    def test_malformed_news_ledger_blocks_paper_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.json"
            path.write_text(json.dumps({"events": [{"id": "bad"}]}))
            result = readiness(news_path=path)
        self.assertFalse(result["ready_for_paper"])
        self.assertEqual("FAIL", result["checks"]["news_ledger"]["status"])


if __name__ == "__main__":
    unittest.main()
