"""
news_watch.py — SIG's per-market news feed as a trigger and a guard (not a directional signal).

SIG's sidebar feed (/api/markets/[id]/news) refreshes every 7-24 hours with mostly older
polling headlines, so by the time a headline shows there Kalshi and Polymarket have usually
priced it. It is used two ways:

Trigger  a new headline in a race marks it "hot" for `boost_s`: the scanner re-reads it every
         pass, so fair value / lead-lag trade at once if the reference moved and SIG has not.
Guard    for `hold_s` after the headline, new entries in that race are allowed only in the
         direction the reference price has moved since the headline, and only after it has
         moved at least `move_min`. No move yet means no new entries there (the reference may
         not have reacted). Market making pulls its quotes in the race. Exits are never held.

Polling runs from the scan loop (step), one race at a time between book scans.
Headlines are keyed by url (or title) per race and kept in logs/news.sqlite3, so a restart
never re-fires them; the first poll of a race only records a baseline.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import queue
import sqlite3
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

log = logging.getLogger("sigarb")
ROOT = pathlib.Path(__file__).parent
DB = ROOT / "logs" / "news.sqlite3"
EVENTS = ROOT / "logs" / "news_events.jsonl"


def headline_key(h: dict) -> str:
    return str(h.get("url") or h.get("title") or json.dumps(h, sort_keys=True))


class NewsWatch:
    def __init__(self, cli, race_markets: Dict[str, List[int]], *, poll_s: float = 1200, gap_s: float = 10,
                 hold_s: float = 900, boost_s: float = 1800, move_min: float = 0.01,
                 db: pathlib.Path = DB, events_log: pathlib.Path = EVENTS):
        self.cli, self.race_markets = cli, race_markets
        self.poll_s, self.gap_s, self.hold_s, self.boost_s, self.move_min = poll_s, gap_s, hold_s, boost_s, move_min
        self.db, self.events_log = db, events_log
        self.watched: frozenset = frozenset()
        self.polled_at: Dict[str, float] = {}
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.holds: Dict[str, dict] = {}            # race -> {"at", "until", "boost_until", "f0": {market: fair}}
        self.stats = {"polls": 0, "events": 0, "errors": 0}
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS seen (race TEXT, key TEXT, first_seen TEXT, title TEXT,"
                      " PRIMARY KEY(race, key))")

    # -- polling (worker thread) -------------------------------------------------------
    def watch(self, races: Iterable[str]) -> None:
        self.watched = frozenset(r for r in races if r in self.race_markets)

    def poll_race(self, race: str) -> Optional[dict]:
        """Fetch one race's feed; returns an event dict if it carries headlines not seen before."""
        m = min(self.race_markets[race])
        payload = self.cli._get("/api/markets/[id]/news", _timeout=10, marketId=m)
        self.stats["polls"] += 1
        heads = payload.get("headlines") or []
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with sqlite3.connect(self.db) as c:
            baseline = c.execute("SELECT COUNT(*) FROM seen WHERE race=?", (race,)).fetchone()[0] == 0
            new = []
            for h in heads:
                k = headline_key(h)
                if c.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                             (race, k, now, str(h.get("title", ""))[:300])).rowcount and not baseline:
                    new.append({"title": h.get("title"), "source": h.get("source"), "url": h.get("url")})
        if not new:
            return None
        return {"race": race, "seen_at": time.time(), "headlines": new,
                "summary": (payload.get("contextSummary") or "")[:600], "feed_refreshed": payload.get("lastRefresh")}

    def due(self, now: float) -> List[str]:
        return sorted((r for r in self.watched if now - self.polled_at.get(r, 0) >= self.poll_s),
                      key=lambda r: self.polled_at.get(r, 0))

    def step(self, now: float = None) -> Optional[dict]:
        """Poll at most one due race (called from the scan loop when SIG is not rate-limiting,
        so news takes turns with the book scans instead of losing every race for the limit)."""
        now = now or time.time()
        if now < getattr(self, "_next_try", 0.0):
            return None
        races = self.due(now)
        if not races:
            return None
        race = races[0]
        try:
            ev = self.poll_race(race)
        except Exception as e:
            self.stats["errors"] += 1
            self._next_try = now + 20
            if "429" not in str(e):
                log.warning("news watch: %s on %s", e, race)
            return None
        self.polled_at[race] = now
        self._next_try = now + self.gap_s
        if ev:
            self.stats["events"] += 1
            self.events.put(ev)
        return ev

    def start(self, stop: threading.Event = None) -> threading.Thread:
        self._stop = stop or threading.Event()
        self._thread = threading.Thread(target=self._run, name="news-watch", daemon=True)
        self._thread.start()
        return self._thread

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            races = self.due(time.time())
            if not races:
                self._stop.wait(5)
                continue
            race = races[0]
            try:
                ev = self.poll_race(race)
                self.polled_at[race] = time.time()
                if ev:
                    self.stats["events"] += 1
                    self.events.put(ev)
                backoff = 0.0
            except Exception as e:                      # 429 or SIG trouble: back off, retry later
                self.stats["errors"] += 1
                backoff = min(600.0, max(30.0, backoff * 2))
                log.warning("news watch: %s on %s (backing off %.0fs)", e, race, backoff)
                self._stop.wait(backoff)
                continue
            self._stop.wait(self.gap_s)

    def stop(self) -> None:
        if getattr(self, "_stop", None):
            self._stop.set()

    # -- events, guard and boost (main thread) -------------------------------------------
    def drain(self, fair_of: Callable[[int], Optional[float]]) -> List[dict]:
        """Turn queued headlines into holds, recording each market's reference price now."""
        out = []
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return out
            race, now = ev["race"], time.time()
            f0 = {m: fair_of(m) for m in self.race_markets.get(race, [])}
            self.holds[race] = {"at": now, "until": now + self.hold_s, "boost_until": now + self.boost_s, "f0": f0}
            row = {**ev, "fair_at_news": {str(k): v for k, v in f0.items()},
                   "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            self.events_log.parent.mkdir(parents=True, exist_ok=True)
            with self.events_log.open("a") as f:
                f.write(json.dumps(row) + "\n")
            out.append(row)

    def race_of(self, m: int) -> Optional[str]:
        for race, ms in self.race_markets.items():
            if m in ms:
                return race
        return None

    def entry_ok(self, m: int, yes_side: str, fair_of: Callable[[int], Optional[float]]) -> bool:
        """New entries in a race with fresh news only go the way the reference has moved."""
        h = self.holds.get(self.race_of(m) or "")
        if not h or time.time() >= h["until"]:
            return True
        f0, f = h["f0"].get(m), fair_of(m)
        if f0 is None or f is None or abs(f - f0) < self.move_min:
            return False
        return (yes_side == "BUY") == (f > f0)

    def held_markets(self) -> frozenset:
        now = time.time()
        return frozenset(m for race, h in self.holds.items() if now < h["until"] for m in self.race_markets.get(race, []))

    def boosted_races(self) -> set:
        now = time.time()
        for race in [r for r, h in self.holds.items() if now >= max(h["until"], h["boost_until"])]:
            self.holds.pop(race)
        return {r for r, h in self.holds.items() if now < h["boost_until"]}
