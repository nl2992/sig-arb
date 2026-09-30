import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crossvenue_adapters import KalshiAdapter, PolymarketAdapter
from crossvenue_models import MarketMatch, MarketMetadata, PriceObservation, normalize_ts
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

    def test_coverage_reports_missing_targeted_markets_and_books(self):
        from unittest.mock import patch
        with patch('crossvenue_adapters.KalshiAdapter.markets_by_ids', return_value=[]), \
             patch('crossvenue_adapters.PolymarketAdapter.markets_by_ids', return_value=[]):
            from crossvenue_adapters import fetch_public
            result = fetch_public(['kalshi', 'polymarket'], market_ids={'kalshi': ['KX1'], 'polymarket': ['P1']})
        self.assertFalse(result['kalshi']['coverage']['complete'])
        self.assertEqual(['KX1'], result['kalshi']['coverage']['missing_market_ids'])

    def test_bounded_inventory_is_not_complete_coverage(self):
        from unittest.mock import patch
        market = MarketMetadata('kalshi', 'KX1', 'E1', 'Q', [])
        with patch('crossvenue_adapters.KalshiAdapter.markets', return_value=[market]), \
             patch('crossvenue_adapters.KalshiAdapter.observations', return_value=[]), \
             patch('crossvenue_adapters.KalshiAdapter.books', return_value=[{'market_id': 'KX1', 'outcome_id': 'YES'}]):
            from crossvenue_adapters import fetch_public
            result = fetch_public(['kalshi'], limit=1)
        self.assertFalse(result['kalshi']['coverage']['complete'])
        self.assertFalse(result['kalshi']['coverage']['scope_complete'])

    def test_invalid_book_response_is_not_counted_as_empty_liquidity(self):
        class Fake:
            def get(self, *args, **kwargs):
                class Response:
                    status_code = 200
                    headers = {}
                    def raise_for_status(self): pass
                    def json(self): return {}
                return Response()
        adapter = KalshiAdapter(session=Fake())
        market = MarketMetadata('kalshi', 'KX1', 'E1', 'Q', [{'outcome_id': 'YES'}, {'outcome_id': 'NO'}])
        self.assertEqual([], adapter.books([market]))

    def test_millisecond_timestamps_are_normalized(self):
        self.assertEqual("2026-09-30T00:00:00+00:00", normalize_ts("1790726400000"))

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

    def test_kalshi_markets_follow_cursor(self):
        class Paged:
            def __init__(self): self.calls = []
            def get(self, url, *args, **kwargs):
                self.calls.append(kwargs)
                cursor = kwargs.get('params', {}).get('cursor')
                page = [{'ticker': 'KX1' if not cursor else 'KX2', 'title': 'Q', 'status': 'active'}]
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {'markets': page, 'cursor': '' if cursor else 'next'}
                return R()
        session = Paged()
        adapter = KalshiAdapter(session=session)
        self.assertEqual(['KX1', 'KX2'], [m.market_id for m in adapter.markets(2)])
        self.assertEqual(2, len(session.calls))

    def test_polymarket_markets_follow_offset(self):
        class Paged:
            def get(self, url, *args, **kwargs):
                offset = kwargs.get('params', {}).get('offset', 0)
                rows = [{'id': f'P{offset + index + 1}', 'question': 'Q', 'outcomes': '["Yes"]',
                         'outcomePrices': '["0.5"]', 'active': True, 'closed': False}
                        for index in range(2)]
                class R:
                    def raise_for_status(self): pass
                    def json(self): return rows
                return R()
        adapter = PolymarketAdapter(session=Paged())
        self.assertEqual(['P1', 'P2'], [m.market_id for m in adapter.markets(2)])

    def test_zero_market_limit_means_all_pages(self):
        class Paged:
            def get(self, url, *args, **kwargs):
                cursor = kwargs.get('params', {}).get('cursor')
                ticker = 'KX1' if not cursor else 'KX2'
                class R:
                    def raise_for_status(self): pass
                    def json(self): return {'markets': [{'ticker': ticker, 'title': 'Q', 'status': 'active'}],
                                           'cursor': '' if cursor else 'next'}
                return R()
        adapter = KalshiAdapter(session=Paged())
        self.assertEqual(['KX1', 'KX2'], [m.market_id for m in adapter.markets(0)])

    def test_public_adapter_retries_rate_limit(self):
        class RetrySession:
            def __init__(self): self.calls = 0
            def get(self, url, *args, **kwargs):
                self.calls += 1
                status = 429 if self.calls == 1 else 200
                class R:
                    status_code = status
                    headers = {}
                    def raise_for_status(self):
                        if self.status_code == 429:
                            raise RuntimeError('rate limited')
                    def json(self): return {'markets': []}
                return R()
        session = RetrySession()
        adapter = KalshiAdapter(session=session)
        self.assertEqual([], adapter.markets(1))
        self.assertEqual(2, session.calls)

    def test_targeted_market_id_inventory_uses_native_endpoints(self):
        class Fake:
            def get(self, url, *args, **kwargs):
                class R:
                    status_code = 200
                    headers = {}
                    def raise_for_status(self): pass
                    def json(self): return {'market': {'ticker': 'KX1', 'title': 'Q', 'status': 'active'}}
                return R()
        adapter = KalshiAdapter(session=Fake())
        self.assertEqual(['KX1'], [m.market_id for m in adapter.markets_by_ids(['KX1'])])

    def test_polymarket_clob_books_are_normalized(self):
        class FakeCLOB:
            def get(self, url, *args, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self):
                        if '/book' in url:
                            return {'timestamp': '1790726400000',
                                    'bids': [{'price': '0.4', 'size': '12'}],
                                    'asks': [{'price': '0.5', 'size': '8'}]}
                        return [{'id': 'P1', 'question': 'Q', 'conditionId': 'C1',
                                 'outcomes': '["Yes", "No"]',
                                 'outcomePrices': '["0.4", "0.6"]',
                                 'clobTokenIds': '["T1", "T2"]',
                                 'active': True, 'closed': False}]
                return R()
        adapter = PolymarketAdapter(session=FakeCLOB(), clob_base_url='https://clob.test')
        books = adapter.books(adapter.markets(1), 1)
        self.assertEqual(2, len(books))
        self.assertEqual(0.4, books[0]['best_bid'])
        self.assertEqual(12.0, books[0]['best_bid_size'])

    def test_polymarket_zero_limit_means_all_returned_markets(self):
        class FakeCLOB:
            def get(self, url, *args, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self):
                        if '/book' in url:
                            return {'bids': [], 'asks': []}
                        return [{'id': 'P1', 'question': 'Q1', 'outcomes': '["Yes"]',
                                 'outcomePrices': '["0.4"]', 'clobTokenIds': '["T1"]',
                                 'active': True, 'closed': False}]
                return R()
        adapter = PolymarketAdapter(session=FakeCLOB())
        markets = adapter.markets(1)
        self.assertEqual(1, len(adapter.books(markets, 0)))

    def test_kalshi_orderbook_derives_complementary_asks(self):
        class FakeKalshi:
            def get(self, url, *args, **kwargs):
                class R:
                    def raise_for_status(self): pass
                    def json(self):
                        return {'orderbook_fp': {
                            'yes_dollars': [['0.40', '12'], ['0.50', '8']],
                            'no_dollars': [['0.20', '10'], ['0.30', '5']],
                        }}
                return R()
        adapter = KalshiAdapter(session=FakeKalshi())
        market = adapter._market({'ticker': 'KX1', 'title': 'Q'}, '2026-09-30T00:00:00Z')
        books = adapter.books([market], depth=100)
        yes = next(row for row in books if row['outcome_id'] == 'YES')
        self.assertEqual(0.5, yes['best_bid'])
        self.assertEqual(0.7, yes['best_ask'])
        self.assertEqual(5.0, yes['best_ask_size'])

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
