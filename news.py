"""Read the same public related-news feed used by the market sidebar."""
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path


def fetch_news(client, market_id, database=None):
    payload = client._get('/api/markets/[id]/news', marketId=market_id)
    detail = client._get(f'/api/markets/{market_id}/page-data')['market']
    now = dt.datetime.now(dt.timezone.utc)
    path = Path(database or Path(__file__).parent / 'logs' / 'news.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    articles = []
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS observations (market INTEGER, digest TEXT, first_seen TEXT, payload TEXT, PRIMARY KEY(market,digest))')
        for item in payload.get('headlines', []):
            raw = json.dumps(item, sort_keys=True)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?)',
                       (market_id, digest, now.isoformat(), raw))
            first = db.execute('SELECT first_seen FROM observations WHERE market=? AND digest=?',
                               (market_id, digest)).fetchone()[0]
            articles.append({**item, 'firstSeen': first, 'version': digest})
    tournament = next((t for t in detail.get('tournaments', []) if t['id'] == client.tournament), None)
    status = 'Unknown'
    if tournament:
        start = dt.datetime.fromisoformat(tournament['start_date'].replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat(tournament['end_date'].replace('Z', '+00:00'))
        status = 'Not started' if now < start else 'Ended' if now >= end else 'Within scheduled trading window'
    return dict(market=market_id, title=detail['title'], fetchedAt=now.isoformat(),
                lastRefresh=payload.get('lastRefresh'), contextSummary=payload.get('contextSummary'),
                headlines=articles, tradingStatus=status,
                tradingStart=tournament.get('start_date') if tournament else None)
