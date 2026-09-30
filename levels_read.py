"""Bounded, read-only accessors for the levels capture database."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime, timezone


def connect(path):
    path = Path(path)
    if not path.exists():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def latest_capture(path, venue):
    conn = connect(path)
    if conn is None:
        return None
    try:
        return conn.execute("SELECT MAX(captured_at) FROM captures WHERE venue=?", (venue,)).fetchone()[0]
    finally:
        conn.close()


def _age(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except ValueError:
        return None


def _book_for_capture(conn, capture_id, venue, market_id, outcome):
    row = conn.execute("""SELECT observed_at,source,best_bid,best_ask,best_bid_size,best_ask_size
                         FROM books WHERE capture_id=? AND venue=? AND market_id=? AND outcome_id=?""",
                       (capture_id, venue, str(market_id), outcome)).fetchone()
    if row is None:
        return None
    levels = {"bids": [], "asks": []}
    for side, index, price, size in conn.execute(
            "SELECT side,level_index,price,size FROM book_levels WHERE capture_id=? AND venue=? AND market_id=? AND outcome_id=? ORDER BY side,level_index",
            (capture_id, venue, str(market_id), outcome)):
        levels[side].append({"price": price, "size": size})
    return {"venue": venue, "market_id": str(market_id), "outcome_id": outcome,
            "observed_at": row[0], "age_s": _age(row[0]), "source": row[1],
            "best_bid": row[2], "best_ask": row[3], "best_bid_size": row[4],
            "best_ask_size": row[5], "bids": levels["bids"], "asks": levels["asks"]}


def latest_book(path, venue, market_id, outcome="YES"):
    conn = connect(path)
    if conn is None:
        return None
    try:
        capture = conn.execute("""SELECT c.capture_id FROM captures c
                                  JOIN books b ON b.capture_id=c.capture_id
                                  WHERE c.venue=? AND b.venue=? AND b.market_id=? AND b.outcome_id=?
                                  ORDER BY c.captured_at DESC LIMIT 1""",
                               (venue, venue, str(market_id), outcome.upper())).fetchone()
        return _book_for_capture(conn, capture[0], venue, market_id, outcome.upper()) if capture else None
    finally:
        conn.close()


def history(path, venue, market_id, outcome="YES", start=None, end=None, limit=300):
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    conn = connect(path)
    if conn is None:
        return None
    try:
        clauses = ["venue=?"]
        params = [venue]
        if start:
            clauses.append("captured_at >= ?")
            params.append(start)
        if end:
            clauses.append("captured_at <= ?")
            params.append(end)
        captures = conn.execute(
            "SELECT capture_id,captured_at FROM captures WHERE " + " AND ".join(clauses) +
            " ORDER BY captured_at DESC LIMIT ?", (*params, limit)).fetchall()
        rows = []
        for capture_id, captured_at in reversed(captures):
            book = _book_for_capture(conn, capture_id, venue, market_id, outcome.upper())
            rows.append({"venue": venue, "market_id": str(market_id), "outcome_id": outcome.upper(),
                         "captured_at": captured_at, "observed_at": book["observed_at"] if book else None,
                         "best_bid": book["best_bid"] if book else None,
                         "best_ask": book["best_ask"] if book else None,
                         "best_bid_size": book["best_bid_size"] if book else None,
                         "best_ask_size": book["best_ask_size"] if book else None,
                         "missing": book is None})
        return rows
    finally:
        conn.close()
