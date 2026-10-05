"""
fast_scan.py — tiered order-book scanning for bot.py.

SIG serves one book per request at ~1-3 s each, so a full 237-book sweep takes a minute
or more. Each tick here fetches, in this order:
  * boosted races (reference movers, market-making quotes, conviction targets, news),
  * one race of the rotating sweep, so every race is refreshed regularly,
  * the PRIORITY pool: HOT races (top-of-book edge within `hot_band` of an arb) and races
    holding arb sets (`priority`, for early exits), most deserving first: time since the
    last read, weighted x3 for races at an arb at the touch and x2 for held sets,
  * the rest of the sweep (`sweep_races` per tick).
With `books_per_tick` set (the request budget of about one tick), a pass stops adding races
once that many books are chosen, so the budget goes to the races that matter most.

stream() yields each race the moment all its books have arrived, so the bot can trade
it immediately; tick() collects a whole pass. Either way only races whose every leg was
read in that pass are returned, so signals (and orders) are never built from stale books. The market list is
cached on disk because paging it is slow.

SIG rate-limits (HTTP 429). sig_client.Pacer spaces requests just under the limit; if a 429
still comes, the tick stops sending requests and the scanner pauses 2 s, doubling per
consecutive limited tick up to 2 min.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterator, List, Optional, Tuple

from arb_engine import Book, group_markets
from sig_client import RateLimited
from signals import Snapshot

MARKETS_CACHE = pathlib.Path(__file__).with_name("logs") / "markets_cache.json"


def load_markets(cli, cache: pathlib.Path = MARKETS_CACHE, max_age_s: float = 6 * 3600,
                 refresh: bool = False) -> List[dict]:
    """Market list from the cache when fresh, else from SIG (then cached)."""
    if not refresh:
        try:
            j = json.loads(cache.read_text())
            if time.time() - j["fetched_at"] <= max_age_s and j["markets"]:
                return j["markets"]
        except (OSError, ValueError, KeyError):
            pass
    try:
        markets = [{"id": m["id"], "title": m["title"]} for m in cli.markets()]
    except Exception:
        try:                                    # SIG erroring: a stale list beats no list
            stale = json.loads(cache.read_text())["markets"]
        except (OSError, ValueError, KeyError):
            raise
        if not stale:
            raise
        return stale
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "markets": markets}))
    tmp.replace(cache)
    return markets


def top_edge(books: List[Book], exhaustive: bool) -> Optional[float]:
    """Best top-of-book edge for the race (>0 = arb at the touch); None if unpriced."""
    edges = []
    bids = [b.best_bid() for b in books]
    if all(p is not None for p in bids):
        edges.append(sum(bids) - 1)
    if exhaustive:
        asks = [b.best_ask() for b in books]
        if all(p is not None for p in asks):
            edges.append(1 - sum(asks))
    return max(edges) if edges else None


class TieredScanner:
    BACKOFF_START_S, BACKOFF_MAX_S = 2.0, 120.0

    def __init__(self, cli, markets: List[dict], exhaustive: set, hot_band: float = 0.005,
                 sweep_races: int = 8, concurrency: int = 6):
        self.cli, self.exhaustive = cli, exhaustive
        self.hot_band, self.sweep_races, self.concurrency = hot_band, sweep_races, concurrency
        self.pause_until, self.backoff = 0.0, 0.0
        self.boost: set = set()          # races to read first this pass (reference movers, MM quotes)
        self.priority: set = set()       # races holding arb sets (checked often for early exits)
        self.books_per_tick = None       # callable -> book budget of one pass; None = unlimited
        self.last_read: Dict[str, float] = {}
        self.set_markets(markets)

    def set_markets(self, markets: List[dict]):
        self.markets = {m["id"]: m for m in markets}
        self.groups = {r: legs for r, legs in group_markets(markets).items() if len(legs) >= 2}
        self.order = sorted(self.groups)
        self.edge: Dict[str, Optional[float]] = {r: getattr(self, "edge", {}).get(r) for r in self.order}
        self.cursor = getattr(self, "cursor", 0) % max(1, len(self.order))
        self.failed: Dict[int, str] = {}

    def hot(self) -> List[str]:
        """Races at or near an arb at the last read. Unread races are swept, not hot."""
        return [r for r in self.order if self.edge.get(r) is not None and self.edge[r] >= -self.hot_band]

    def paused_for(self) -> float:
        return max(0.0, self.pause_until - time.time())

    def race_of(self, market_id: int) -> Optional[str]:
        return next((r for r, legs in self.groups.items() if market_id in legs.values()), None)

    def select(self, full: bool = False) -> List[str]:
        if full:
            return list(self.order)
        boosted = [r for r in sorted(getattr(self, "boost", set())) if r in self.groups]
        held = {r for r in getattr(self, "priority", set()) if r in self.groups}
        pool = [r for r in dict.fromkeys(self.hot() + sorted(held)) if r not in boosted]
        now = time.time()

        def score(r):
            age = now - self.last_read.get(r, 0.0)
            e = self.edge.get(r)
            return age * (3 if e is not None and e >= 0 else 2 if r in held else 1)
        pool.sort(key=score, reverse=True)
        taken = set(boosted) | set(pool)
        cold = [r for r in self.order if r not in taken]
        sweep = []
        n = min(self.sweep_races, len(cold))
        if n:
            start = self.cursor % len(cold)
            sweep = (cold[start:] + cold[:start])[:n]
        chosen = boosted + sweep[:1] + pool + sweep[1:]
        budget = self.books_per_tick() if callable(getattr(self, "books_per_tick", None)) else None
        if budget:
            out, books = [], 0
            for r in chosen:
                if out and books + len(self.groups[r]) > budget:
                    break
                out.append(r)
                books += len(self.groups[r])
            chosen = out
        self.cursor = (self.cursor % len(cold) if cold else 0) + sum(1 for r in sweep if r in chosen)
        return chosen

    def stream(self, full: bool = False, stats: Optional[dict] = None) -> Iterator[Tuple[str, Snapshot]]:
        """Yield (race, snapshot of just that race) the moment all its books have arrived,
        so a race is evaluated ~one request after it was read, not one full pass later.
        `stats` (if given) is filled in when the generator finishes."""
        t0 = time.time()
        stats = stats if stats is not None else {}
        stats.update(races=0, complete=0, complete_races=[], books=0, failed_books=0,
                     hot=len(self.hot()), seconds=0.0, rate_limited=False, paused_s=round(self.paused_for(), 1))
        if self.paused_for() > 0:
            stats["rate_limited"] = True
            return
        races = self.select(full)
        stats.update(races=len(races), books=sum(len(self.groups[r]) for r in races))
        limited: List[RateLimited] = []
        pending = {r: set(self.groups[r].values()) for r in races}
        race_of = {mid: r for r in races for mid in self.groups[r].values()}
        got: Dict[int, list] = {}

        def one(mid):
            if limited:                       # stop sending once SIG has said 429
                return mid, None, "skipped after 429"
            try:
                return mid, self.cli.levels(mid), None
            except RateLimited as exc:
                limited.append(exc)
                return mid, None, str(exc)
            except Exception as exc:          # one slow/failed book never fails the pass
                return mid, None, f"{type(exc).__name__}: {str(exc)[:120]}"

        # Hot races first so they are read (and traded) before the sweep.
        ex = ThreadPoolExecutor(self.concurrency)
        try:
            futures = [ex.submit(one, mid) for r in races for mid in self.groups[r].values()]
            for fut in as_completed(futures):
                mid, levels, err = fut.result()
                r = race_of[mid]
                if err is not None:
                    self.failed[mid] = err
                    stats["failed_books"] += 1
                    pending.pop(r, None)          # race can no longer complete this pass
                    continue
                self.failed.pop(mid, None)
                got[mid] = levels
                if r not in pending:
                    continue
                pending[r].discard(mid)
                if pending[r]:
                    continue
                del pending[r]
                self.last_read[r] = time.time()
                legs = list(self.groups[r].values())
                self.edge[r] = top_edge([Book.from_levels(m, got[m]) for m in legs], r in self.exhaustive)
                stats["complete"] += 1
                stats["complete_races"].append(r)
                yield r, Snapshot(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  [{"id": m, "title": self.markets[m]["title"]} for m in legs],
                                  {m: got[m] for m in legs})
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
            if limited:
                self.backoff = min(self.BACKOFF_MAX_S, max(self.BACKOFF_START_S, self.backoff * 2,
                                                           limited[0].retry_after or 0))
                self.pause_until = time.time() + self.backoff
            elif stats["races"]:
                self.backoff = 0.0
            stats.update(hot=len(self.hot()), seconds=round(time.time() - t0, 1),
                         rate_limited=bool(limited), paused_s=round(self.paused_for(), 1))

    def tick(self, full: bool = False) -> tuple[Snapshot, dict]:
        """Whole pass at once: a snapshot of only the races completed in it."""
        stats: dict = {}
        markets, levels = [], {}
        for _, snap in self.stream(full, stats):
            markets += snap.markets
            levels.update(snap.levels)
        return Snapshot(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        sorted(markets, key=lambda m: m["id"]), levels), stats
