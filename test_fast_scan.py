"""Tiered scanning: only freshly read races reach signals; rotation covers everything."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import fast_scan
import sig_client
from test_client import client


def market(mid, party, race):
    return {"id": mid, "title": f"Will the {party} Party win the {race}?"}


RACES = [f"State{i} Senate" for i in range(6)]
MARKETS = [m for i, r in enumerate(RACES) for m in (market(10 * i + 1, "Republican", r),
                                                    market(10 * i + 2, "Democratic", r))]


def levels(bid):
    return [{"exchangeId": 1, "side": "BUY", "isYes": True, "price": bid, "quantity": 100},
            {"exchangeId": 1, "side": "SELL", "isYes": True, "price": bid + 0.02, "quantity": 100}]


class FakeCli:
    def __init__(self, bids, fail=()):
        self.bids, self.fail, self.calls = bids, set(fail), []

    def levels(self, mid):
        self.calls.append(mid)
        if mid in self.fail:
            raise TimeoutError("slow")
        return levels(self.bids.get(mid, 0.3))

    def markets(self):
        return MARKETS


class ScannerTests(unittest.TestCase):
    def scanner(self, cli, **kw):
        return fast_scan.TieredScanner(cli, MARKETS, set(), concurrency=4, **kw)

    def test_first_full_tick_reads_everything_then_only_hot_plus_sweep(self):
        # State0 sums to 1.01 (arb), State1 to 0.99 (hot), the rest to 0.60 (cold).
        bids = {1: 0.51, 2: 0.50, 11: 0.50, 12: 0.49}
        cli = FakeCli(bids)
        sc = self.scanner(cli, hot_band=0.02, sweep_races=1)
        snap, stats = sc.tick(full=True)
        self.assertEqual(stats["complete"], 6)
        self.assertEqual(sorted(sc.hot()), ["State0 Senate", "State1 Senate"])
        cli.calls.clear()
        snap, stats = sc.tick()
        self.assertEqual(stats["races"], 3)              # 2 hot + 1 swept
        self.assertEqual(len(cli.calls), 6)

    def test_rotation_covers_every_cold_race(self):
        sc = self.scanner(FakeCli({}), sweep_races=2)
        sc.tick(full=True)
        seen = set()
        for _ in range(3):
            seen |= set(sc.tick()[1]["complete_races"])
        self.assertEqual(seen, set(RACES))

    def test_race_with_failed_book_is_excluded(self):
        sc = self.scanner(FakeCli({}, fail={1}))
        snap, stats = sc.tick(full=True)
        self.assertNotIn("State0 Senate", stats["complete_races"])
        self.assertNotIn(1, snap.levels)
        self.assertNotIn(2, snap.levels)                 # sibling dropped too
        self.assertEqual(stats["failed_books"], 1)
        self.assertEqual(stats["complete"], 5)

    def test_rate_limit_stops_requests_and_backs_off(self):
        class Limited(FakeCli):
            def levels(self, mid):
                self.calls.append(mid)
                raise sig_client.RateLimited("/x")
        cli = Limited({})
        sc = fast_scan.TieredScanner(cli, MARKETS, set(), concurrency=1)
        snap, stats = sc.tick(full=True)
        self.assertTrue(stats["rate_limited"])
        self.assertEqual(len(cli.calls), 1)              # nothing sent after the first 429
        self.assertGreaterEqual(stats["paused_s"], 14)
        cli.calls.clear()
        snap, stats = sc.tick()
        self.assertEqual(cli.calls, [])                  # paused: no requests at all
        self.assertEqual(snap.markets, [])
        first = sc.backoff
        sc.pause_until = 0
        sc.tick()
        self.assertEqual(sc.backoff, first * 2)          # consecutive 429s double the pause

    def test_client_raises_rate_limited_without_retry(self):
        c = client()
        resp = mock.Mock(status_code=429, headers={"Retry-After": "30"})
        with mock.patch.object(c.s, "get", return_value=resp) as get:
            with self.assertRaises(sig_client.RateLimited) as ctx:
                c.levels(1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(ctx.exception.retry_after, 30.0)

    def test_stream_yields_each_race_with_only_its_books(self):
        sc = self.scanner(FakeCli({1: 0.51, 2: 0.50}))
        stats, got = {}, []
        for race, snap in sc.stream(full=True, stats=stats):
            ids = sorted(snap.levels)
            self.assertEqual(ids, sorted(m["id"] for m in snap.markets))
            self.assertEqual(len(ids), 2)
            got.append(race)
        self.assertEqual(sorted(got), sorted(RACES))
        self.assertEqual(stats["complete"], 6)
        self.assertAlmostEqual(sc.edge["State0 Senate"], 0.01)

    def test_stream_trades_can_start_before_the_pass_ends(self):
        # A slow book in the last race must not delay the first race being yielded.
        import threading
        release = threading.Event()

        class Slow(FakeCli):
            def levels(self, mid):
                if mid == 52:
                    release.wait(5)
                return super().levels(mid)
        sc = self.scanner(Slow({}))
        gen = sc.stream(full=True)
        first, _ = next(gen)
        self.assertNotEqual(first, "State5 Senate")
        release.set()
        list(gen)

    def test_top_edge(self):
        from arb_engine import Book
        books = [Book.from_levels(1, levels(0.51)), Book.from_levels(2, levels(0.50))]
        self.assertAlmostEqual(fast_scan.top_edge(books, False), 0.01)
        self.assertIsNone(fast_scan.top_edge([Book.from_levels(1, [])], False))


class MarketsTests(unittest.TestCase):
    def test_cache_is_used_until_stale(self):
        with tempfile.TemporaryDirectory() as d:
            cache = pathlib.Path(d) / "m.json"
            cli = mock.Mock(markets=mock.Mock(return_value=MARKETS))
            self.assertEqual(len(fast_scan.load_markets(cli, cache)), 12)
            fast_scan.load_markets(cli, cache)
            self.assertEqual(cli.markets.call_count, 1)
            j = json.loads(cache.read_text()); j["fetched_at"] = time.time() - 10 ** 6
            cache.write_text(json.dumps(j))
            fast_scan.load_markets(cli, cache)
            self.assertEqual(cli.markets.call_count, 2)

    def test_parallel_paging_collects_all_pages_once(self):
        pages = {o: {"markets": MARKETS[o:o + 4], "nextOffset": o + 4 if o + 4 < 12 else None,
                     "totalMarkets": 12} for o in (0, 4, 8)}
        c = client()
        with mock.patch.object(c, "_get", side_effect=lambda path, offset: pages[offset]) as get:
            out = c.markets()
        self.assertEqual([m["id"] for m in out], [m["id"] for m in MARKETS])
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
