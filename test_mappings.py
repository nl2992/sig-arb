"""Mapping registry builder: automated checks and never overwriting decisions."""
import pathlib
import tempfile
import unittest

import mappings
from market_matches import load_registry

SIG_R = "Will the Republican Party win the Rhode Island Senate?"


class CheckTests(unittest.TestCase):
    def ref(self, title, mid="X", rules="If a representative is sworn in...", status="ACTIVE"):
        return {"title": title, "market_id": mid, "rules_text": rules, "status": status}

    def test_matching_pair_passes_with_flags(self):
        r = mappings.check_pair(SIG_R, "kalshi", self.ref("Will Republicans win the Senate race in Rhode Island?", "SENATERI-26-R"))
        self.assertTrue(all(r["checks"].values()))
        self.assertTrue(any("swearing-in" in f for f in r["flags"]))

    def test_wrong_party_or_race_fails(self):
        self.assertFalse(mappings.check_pair(SIG_R, "polymarket", self.ref("Will the Democrats win the Rhode Island Senate race?"))["checks"]["party"])
        self.assertFalse(mappings.check_pair(SIG_R, "polymarket", self.ref("Will the Republicans win the Rhode Island Governor race?"))["checks"]["race"])
        self.assertFalse(mappings.check_pair(SIG_R, "polymarket", self.ref("Will the Republicans win the Vermont Senate race?"))["checks"]["race"])

    def test_house_and_chamber_races(self):
        d = "Will the Democratic Party win the AZ-01 House race?"
        self.assertTrue(mappings.check_pair(d, "polymarket", self.ref("Will the Democratic Party win the AZ-01 House seat?"))["checks"]["race"])
        self.assertTrue(mappings.check_pair(d, "kalshi", self.ref("Will Democrats win Arizona's 1st district?"))["checks"]["race"])
        self.assertFalse(mappings.check_pair(d, "kalshi", self.ref("Will Democrats win Arizona's 6th district?"))["checks"]["race"])
        u = "Will the Republican Party win the U.S. House?"
        self.assertTrue(mappings.check_pair(u, "kalshi", self.ref("Will Republicans win the House in 2026?"))["checks"]["race"])

    def test_missing_reference_fails_everything(self):
        self.assertFalse(any(mappings.check_pair(SIG_R, "kalshi", None)["checks"].values()))


class RegistryTests(unittest.TestCase):
    def test_propose_keeps_decisions_and_saves_loadable_registry(self):
        links = [{"sig_market_id": "388", "event": SIG_R, "kalshi_market_id": "SENATERI-26-R", "polymarket_market_id": "630912"}]
        refs = {"kalshi": {"SENATERI-26-R": {"title": "Will Republicans win the Senate race in Rhode Island?",
                                              "market_id": "SENATERI-26-R", "rules_text": "r", "status": "ACTIVE"}},
                "polymarket": {}}
        decided = [mappings.approve({"sig_market_id": 388, "reference_venue": "kalshi", "reference_market_id": "SENATERI-26-R",
                                     "reference_outcome_id": "YES", "status": "REVIEW_REQUIRED", "checks": {"party": True}}, "test")]
        rows = mappings.propose(links, refs, decided)
        k = next(r for r in rows if r["reference_venue"] == "kalshi")
        p = next(r for r in rows if r["reference_venue"] == "polymarket")
        self.assertEqual(k["status"], "APPROVED")
        self.assertEqual(p["status"], "REVIEW_REQUIRED")
        self.assertFalse(mappings.all_pass(p))            # reference market missing
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "m.json"
            mappings.save_rows(rows, path)
            self.assertEqual(len(load_registry(path)), 2)


if __name__ == "__main__":
    unittest.main()
