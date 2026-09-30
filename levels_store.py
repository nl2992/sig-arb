"""SQLite storage and local replay helpers for public venue levels."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import uuid

from crossvenue_models import PriceObservation


SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
  capture_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, venue TEXT NOT NULL,
  requested_markets INTEGER NOT NULL, returned_markets INTEGER NOT NULL,
  complete INTEGER NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
  capture_id TEXT NOT NULL, venue TEXT NOT NULL, market_id TEXT NOT NULL,
  outcome_id TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT,
  best_bid REAL, best_ask REAL, best_bid_size REAL, best_ask_size REAL,
  PRIMARY KEY (capture_id, venue, market_id, outcome_id)
);
CREATE TABLE IF NOT EXISTS book_levels (
  capture_id TEXT NOT NULL, venue TEXT NOT NULL, market_id TEXT NOT NULL,
  outcome_id TEXT NOT NULL, side TEXT NOT NULL, level_index INTEGER NOT NULL,
  price REAL NOT NULL, size REAL NOT NULL,
  PRIMARY KEY (capture_id, venue, market_id, outcome_id, side, level_index)
);
CREATE INDEX IF NOT EXISTS idx_books_lookup ON books(venue, market_id, outcome_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_levels_lookup ON book_levels(venue, market_id, outcome_id, side);
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class LevelsDB:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def ingest(self, payload: dict, captured_at: str | None = None) -> list[dict]:
        captured_at = captured_at or utc_now()
        summaries = []
        with self.conn:
            for venue, data in payload.items():
                coverage = data.get("coverage", {})
                capture_id = f"{captured_at}:{venue}:{uuid.uuid4().hex[:8]}"
                self.conn.execute(
                    "INSERT INTO captures VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (capture_id, captured_at, venue,
                     int(coverage.get("requested_market_count", len(data.get("markets", [])))),
                     int(coverage.get("returned_market_count", len(data.get("markets", [])))),
                     int(bool(coverage.get("complete"))), json.dumps(data, separators=(",", ":"))))
                for book in data.get("books", []):
                    key = (capture_id, venue, str(book["market_id"]), str(book["outcome_id"]).upper())
                    self.conn.execute(
                        "INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        key + (book.get("observed_at", captured_at), book.get("source"),
                               book.get("best_bid"), book.get("best_ask"),
                               book.get("best_bid_size"), book.get("best_ask_size")))
                    for side in ("bids", "asks"):
                        for index, level in enumerate(book.get(side) or []):
                            self.conn.execute(
                                "INSERT INTO book_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                key + (side, index, float(level["price"]), float(level["size"])))
                summaries.append({"venue": venue, "capture_id": capture_id,
                                  "markets": len(data.get("markets", [])),
                                  "books": len(data.get("books", [])),
                                  "complete": bool(coverage.get("complete"))})
        return summaries

    def observations(self, venue: str | None = None) -> list[PriceObservation]:
        query = "SELECT venue, market_id, outcome_id, observed_at, source, best_bid, best_ask, best_bid_size, best_ask_size FROM books"
        params = ()
        if venue:
            query += " WHERE venue = ?"
            params = (venue,)
        query += " ORDER BY observed_at"
        rows = self.conn.execute(query, params).fetchall()
        return [PriceObservation(venue=row[0], market_id=row[1], outcome_id=row[2],
                                 observed_at=row[3], source_ts=row[3], source="local-sqlite:" + (row[4] or "unknown"),
                                 bid=row[5], ask=row[6], bid_size=row[7], ask_size=row[8],
                                 price_basis="mid") for row in rows]

    def append_alert(self, path: str | pathlib.Path, alert: dict):
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(json.dumps({"ts": utc_now(), **alert}, separators=(",", ":")) + "\n")
