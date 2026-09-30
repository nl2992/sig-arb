import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crossvenue_adapters import KalshiAdapter, PolymarketAdapter
from crossvenue_models import MarketMatch, PriceObservation
from market_matches import approved_matches, load_registry
from movement_scanner import scan_movements
from relative_value import scan_pairs
from signals import Snapshot


class CrossVenueTests(unittest.TestCase):
    def test_registry_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "matches.json"
            p.write_text(json.dumps({"matches": [{"sig_market_id": 386, "reference_venue": "kalshi", "reference_market_id": "KX1", "status": "PROPOSED"}]}))
            self.assertEqual([], approved_matches(load_registry(p)))

    def test_movement_candidate_uses_absolute_percentage_points_and_sig_ask(self):
        ts = dt.datetime(2026, 9, 30, 2, 0, tzinfo=dt.timezone.utc)
        snap = Snapshot(ts.isoformat(), [{"id": 386, "title": "Will the Republican Party win the Synthetic Senate?"}],
                        {386: [{"exchangeId": 1, "side": "SELL", "isYes": True, "price": 0.50, "quantity": 10}]})
        obs = [
            PriceObservation("kalshi", "KX1", "YES", (ts - dt.timedelta(minutes=120)).isoformat(), last=0.50, price_basis="last"),
            PriceObservation("kalshi", "KX1", "YES", ts.isoformat(), last=0.60, price_basis="last"),
        ]
        report = scan_movements(snap, obs, [MarketMatch(386, "kalshi", "KX1", "YES", status="APPROVED", reviewed_at="2026-09-30T00:00:00+00:00", evidence="fixture-only")], min_move_pp=5, min_gap_pp=1)
        self.assertEqual(1, len(report.candidates))
        self.assertEqual(10, report.candidates[0]["movement_pp"])
        self.assertEqual("BUY_YES", report.candidates[0]["direction"])

    def test_approved_mapping_requires_review_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "matches.json"
            p.write_text(json.dumps({"matches": [{"sig_market_id": 386, "reference_venue": "kalshi", "reference_market_id": "KX1", "status": "APPROVED"}]}))
            with self.assertRaises(ValueError):
                load_registry(p)

    def test_scanner_rejects_unapproved_and_future_observations(self):
        ts = dt.datetime(2026, 9, 30, 2, 0, tzinfo=dt.timezone.utc)
        snap = Snapshot(ts.isoformat(), [{"id": 386, "title": "Q"}], {386: [{"exchangeId": 1, "side": "SELL", "isYes": True, "price": 0.50, "quantity": 10}]})
        obs = [PriceObservation("kalshi", "KX1", "YES", (ts - dt.timedelta(minutes=120)).isoformat(), last=0.4, price_basis="last"), PriceObservation("kalshi", "KX1", "YES", (ts + dt.timedelta(minutes=1)).isoformat(), last=0.8, price_basis="last")]
        report = scan_movements(snap, obs, [MarketMatch(386, "kalshi", "KX1", "YES", status="PROPOSED")])
        self.assertEqual("UNAPPROVED_MATCH", report.diagnostics[0]["reason"])

    def test_price_basis_is_honored(self):
        obs = PriceObservation("kalshi", "KX1", "YES", "2026-09-30T00:00:00+00:00", bid=0.2, ask=0.4, last=0.9, price_basis="mid")
        self.assertAlmostEqual(0.3, obs.reference_price())
        self.assertIsNone(PriceObservation("kalshi", "KX1", "YES", "2026-09-30T00:00:00+00:00", last=0.9, price_basis="unknown").reference_price())

    def test_future_review_timestamp_is_rejected(self):
        ts = dt.datetime(2026, 9, 30, 2, 0, tzinfo=dt.timezone.utc)
        snap = Snapshot(ts.isoformat(), [{"id": 386, "title": "Q"}], {386: [{"exchangeId": 1, "side": "SELL", "isYes": True, "price": 0.5, "quantity": 1}]})
        obs = [PriceObservation("kalshi", "KX1", "YES", (ts - dt.timedelta(minutes=120)).isoformat(), last=0.4, price_basis="last"), PriceObservation("kalshi", "KX1", "YES", ts.isoformat(), last=0.6, price_basis="last")]
        report = scan_movements(snap, obs, [MarketMatch(386, "kalshi", "KX1", "YES", status="APPROVED", reviewed_at="2026-09-30T03:00:00+00:00", evidence="future")])
        self.assertEqual("INCOMPLETE_REVIEW", report.diagnostics[0]["reason"])

    def test_adapters_normalize_fixtures_without_network(self):
        class Fake:
            def __init__(self, payload): self.payload = payload
            def get(self, *a, **k):
                class R:
                    def __init__(self, p): self.p = p
                    def raise_for_status(self): pass
                    def json(self): return self.p
                return R(self.payload)
        k = KalshiAdapter(session=Fake({"markets": [{"ticker": "KX1", "event_ticker": "E1", "title": "Q", "status": "active", "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.6", "last_price_dollars": "0.5"}]}))
        self.assertEqual("KX1", k.markets(1)[0].market_id)
        self.assertEqual(0.5, k.observations(1)[0].last)
        p = PolymarketAdapter(session=Fake([{"id": "P1", "question": "Q", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.4", "0.6"]', "active": True, "closed": False}]))
        self.assertEqual(2, len(p.observations(1)))

    def test_relative_value_is_review_gated_and_reports_z_score(self):
        rows = []
        for i in range(12):
            rows.append({"observed_at": f"2026-09-30T00:{i:02d}:00+00:00",
                         "sig_market_id": 386, "reference_venue": "kalshi",
                         "reference_market_id": "KX1", "reference_outcome_id": "YES",
                         "sig_price": 0.5, "reference_price": 0.6 if i == 11 else 0.5})
        match = MarketMatch(386, "kalshi", "KX1", "YES", status="APPROVED",
                            reviewed_at="2026-09-30T00:00:00+00:00", evidence="fixture")
        report = scan_pairs(rows, [match], min_points=10)
        self.assertEqual("RESEARCH_CANDIDATE", report[0]["status"])
        self.assertGreater(abs(report[0]["z_score"]), 2)
        self.assertEqual([], scan_pairs(rows, [MarketMatch(386, "kalshi", "KX1", "YES")], min_points=10))


if __name__ == "__main__":
    unittest.main()
