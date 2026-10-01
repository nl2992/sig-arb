"""Local dashboard. Run: python3 dashboard.py --port 8765.

It never places orders. Its only writes are the kill switch (engage/release) and the
audit log; bot.py is the only process that sends orders."""
import argparse
import datetime as dt
import hmac
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from arb_engine import group_markets, scan
from sig_client import Client
from kelly import size_position
from news import fetch_news
from signals import Snapshot, load_exhaustive, scan_diagnostics
from crossvenue_adapters import fetch_public, load_market_links, load_targeted_market_ids
from crossvenue_models import PriceObservation
from market_matches import load_registry
from movement_scanner import scan_movements
from relative_value import scan_pairs
from portfolio import fetch_sig_portfolio
from news_guard import active_breakers
import gates
import sig_client
from paper import snapshot_age_seconds

ROOT = Path(__file__).parent
NEWS_BREAKERS = ROOT / 'config' / 'news_circuit_breakers.json'
KILL_SWITCH = ROOT / 'logs' / 'KILL_SWITCH'
LEVELS_DB = ROOT / 'logs' / 'levels.sqlite3'
RISK_LIMITS = ROOT / 'config' / 'risk_limits.json'
# Written by the reconciliation step; absent means reconciliation has not run.
RECONCILIATION = ROOT / 'logs' / 'reconciliation.json'
# Written by bot.py: one heartbeat file and an append-only execution journal.
BOT_STATUS = ROOT / 'logs' / 'bot_status.json'
MATCHES = ROOT / 'config' / 'market_matches.json'
BOT_BOOKS = ROOT / 'logs' / 'bot_books.json'
EXEC_LOG = ROOT / 'logs' / 'executions.jsonl'
AUDIT_LOG = ROOT / 'logs' / 'audit.jsonl'
RELEASE_PHRASE = 'RELEASE KILL SWITCH'
# Expected refresh cadence per feed in seconds; a feed is stale after twice this.
FEED_CADENCE_S = {'sig_books': 15, 'sig_account': 60, 'crossvenue': 60, 'levels_db': 60}
RELAY_PATH = '/api/browser_snapshot'
# browser_relay.js runs in the signed-in SIG page; it is the only cross-origin caller.
RELAY_ORIGIN = 'https://sig.thesuper.market'


def _snapshot_age_seconds(snapshot):
    observed = dt.datetime.fromisoformat(snapshot.ts.replace('Z', '+00:00'))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds())


def _read_history(path=ROOT / 'logs' / 'crossvenue-history.jsonl'):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _crossvenue_report(snapshot, payload, matches_path=None):
    payload = payload or {}
    matches_path = matches_path or MATCHES
    observations = [PriceObservation(**row)
                    for venue in payload.values()
                    for row in venue.get('observations', [])]
    try:
        matches = load_registry(matches_path)
    except (OSError, ValueError):
        matches = []
    history = _read_history()
    historical = [PriceObservation(
        venue=row['reference_venue'], market_id=row['reference_market_id'],
        outcome_id=row['reference_outcome_id'], observed_at=row['reference_observed_at'],
        source_ts=row.get('reference_source_ts'), bid=row.get('reference_bid'),
        ask=row.get('reference_ask'), last=row.get('reference_price'),
        source=row.get('reference_source', 'crossvenue-history'),
        price_basis=row.get('reference_price_basis', 'last'))
        for row in history if row.get('reference_price') is not None and row.get('reference_observed_at')]
    all_observations = historical + observations
    movement = scan_movements(snapshot, all_observations, matches).to_dict()
    relative = scan_pairs(history, matches)
    market_index = {(venue, str(row.get('market_id'))): row
                    for venue, data in payload.items()
                    for row in data.get('markets', [])}
    observation_index = {(row.venue, row.market_id, row.outcome_id.upper()): row
                         for row in observations}
    books = snapshot.books()
    breakers = active_breakers(NEWS_BREAKERS)
    enriched = []
    now = dt.datetime.now(dt.timezone.utc)
    for candidate in movement.get('candidates', []):
        match = next((m for m in matches if m.sig_market_id == candidate['sig_market_id']
                      and m.reference_venue == candidate['reference_venue']
                      and m.reference_market_id == candidate['reference_market_id']), None)
        ref = observation_index.get((candidate['reference_venue'], candidate['reference_market_id'],
                                     match.reference_outcome_id.upper() if match else 'YES'))
        metadata = market_index.get((candidate['reference_venue'], candidate['reference_market_id']), {})
        book = books.get(candidate['sig_market_id'])
        required = book.asks if candidate['direction'] == 'BUY_YES' and book else book.bids if book else []
        top_qty = required[0][1] if required else None
        try:
            age = max(0.0, (now - dt.datetime.fromisoformat(ref.observed_at.replace('Z', '+00:00'))).total_seconds()) if ref else None
        except (TypeError, ValueError):
            age = None
        candidate.update({
            'liquidity': {'sig_top_qty': top_qty, 'reference_bid_size': ref.bid_size if ref else None,
                          'reference_ask_size': ref.ask_size if ref else None},
            'freshness_seconds': age,
            'mapping_status': match.status if match else 'UNRESOLVED',
            'settlement_status': 'RULES_PRESENT_UNREVIEWED' if metadata.get('rules_text') else 'RULES_UNAVAILABLE',
            'fee_assumption': 0.0,
            'fee_status': 'UNVERIFIED_PUBLIC_SCHEDULE',
            'roi_estimate': None,
            'gross_roi_estimate': round(candidate['gap_pp'] / abs(candidate['sig_price']) / 100, 6) if candidate.get('gap_pp') and candidate.get('sig_price') else None,
            'roi_basis': 'GROSS_INDICATIVE_GAP',
            'execution_risk': ['RESEARCH_ONLY', 'MULTI_VENUE_FILL_RISK', 'FEES_UNVERIFIED'],
            'news_status': 'ACTIVE_CIRCUIT_BREAKER' if breakers.get(candidate['sig_market_id']) else 'CLEAR',
            'execution_ready': False,
        })
        if breakers.get(candidate['sig_market_id']):
            candidate['execution_risk'].append('NEWS_CIRCUIT_BREAKER')
        enriched.append(candidate)
    market_counts = {venue: len(data.get('markets', [])) for venue, data in payload.items()}
    observation_counts = {venue: len(data.get('observations', [])) for venue, data in payload.items()}
    approved = [m for m in matches if m.status == 'APPROVED']
    link_rows = load_market_links(ROOT / 'docs' / 'market-links.csv')
    return {
        'market_counts': market_counts,
        'observation_counts': observation_counts,
        'book_counts': {venue: len(data.get('books', [])) for venue, data in payload.items()},
        'mapping_counts': {'discovered': len(link_rows), 'registry_rows': len(matches), 'approved': len(approved)},
        'mapping_candidates': [{k: row.get(k) for k in (
            'sig_market_id', 'event', 'sig_url', 'kalshi_market_id', 'kalshi_url',
            'polymarket_market_id', 'polymarket_url', 'status')}
            for row in link_rows],
        'movement': movement,
        'opportunities': enriched,
        'relative_value': relative,
        'history_points': len(history),
        'depth_coverage': 'SIG_FULL_RELAY_BOOKS_PLUS_ALL_RETURNED_KALSHI_AND_POLYMARKET_BOOKS',
        'book_coverage': {venue: data.get('book_coverage', 'UNKNOWN') for venue, data in payload.items()},
        'coverage': {venue: data.get('coverage', {}) for venue, data in payload.items()},
        'fees': {'sig': 'dashboard input', 'kalshi': 'unverified', 'polymarket': 'unverified'},
        'research_only': True,
    }


def scan_params(params):
    """Validated scanner filters shared by /api/signals and /api/orders/preview."""
    def number(name, default):
        value = float(params.get(name, [default])[0])
        if not math.isfinite(value) or value < 0:
            raise ValueError(name + " must be a finite nonnegative number")
        return value

    fee = number("fee", 0)
    budget = number("budget", 0)
    overall = number("capital", 0)
    cap_pct = number("cap_pct", 5) / 100
    if overall and cap_pct:
        per_punt = overall * cap_pct
        budget = min(budget, per_punt) if budget else per_punt
    return dict(fee=fee, budget=budget, overall=overall, cap_pct=cap_pct, edge=number("edge", 0),
                minimum=number("profit", 1), min_roi=number("roi", 5) / 100)


def report(snapshot, params, crossvenue=None):
    p = scan_params(params)
    fee, budget, overall, cap_pct = p['fee'], p['budget'], p['overall'], p['cap_pct']
    edge, minimum, min_roi = p['edge'], p['minimum'], p['min_roi']
    snapshot_age = _snapshot_age_seconds(snapshot)
    groups = group_markets(snapshot.markets)
    exhaustive = load_exhaustive()
    breakers = active_breakers(NEWS_BREAKERS)
    scan_report = scan_diagnostics(snapshot, exhaustive, fee_per_share=fee,
                                   min_edge=edge, cash=budget or None)
    results = scan_report.opportunities
    titles = {m["id"]: m["title"] for m in snapshot.markets}
    rows = []
    punts = []
    for r in results:
        no = r.direction == "SELL_ALL"
        row = dict(
            race=r.race, direction=r.direction, qty=r.qty, profit=r.pnl,
            capital=r.capital, roi=r.roi, edge=r.avg_edge,
            top_edge=r.top_edge, marginal_edge=r.marginal_edge,
            freshness_seconds=round(snapshot_age, 3),
            mapping_status='LOCAL_SIG', settlement_status='SIG_RULES_UNVERIFIED',
            fee_assumption=fee,
            execution_risk=['PARTIAL_FILL', 'QUOTE_AGE_CHECK_REQUIRED'],
            news_status='ACTIVE_CIRCUIT_BREAKER' if any(breakers.get(l.market_id) for l in r.legs) else 'CLEAR',
            execution_ready=False,
            vwap=sum(1-l.vwap if no else l.vwap for l in r.legs),
            steps=r.steps,
            legs=[dict(id=l.market_id, title=titles[l.market_id],
                       side="BUY NO" if no else "BUY YES", qty=l.qty,
                       vwap=1-l.vwap if no else l.vwap,
                       limit=1-l.limit if no else l.limit) for l in r.legs])
        if row['news_status'] == 'ACTIVE_CIRCUIT_BREAKER':
            row['execution_risk'].append('NEWS_CIRCUIT_BREAKER')
        if snapshot_age > 30:
            row['execution_risk'].append('STALE_SIG_SNAPSHOT')
        if r.pnl >= minimum and r.roi + 1e-12 >= min_roi:
            rows.append(row)
        elif r.pnl >= minimum:
            row["required_roi"] = min_roi
            punts.append(row)
    near = [dict(race=d["race"], dir=d["direction"], edge=d["top_edge"])
            for d in scan_report.diagnostics
            if d["top_edge"] is not None and d["top_edge"] <= 0]
    near.sort(key=lambda r: -r["edge"])
    return dict(ts=snapshot.ts, markets=len(snapshot.markets), races=len(groups),
                exhaustive=len(exhaustive), fee=fee, budget=budget,
                overall_capital=overall, cap_pct=cap_pct,
                signals=rows, near=near[:12],
                punts=punts[:25],
                diagnostics=scan_report.diagnostics,
                liquidity=scan_report.liquidity,
                crossvenue=_crossvenue_report(snapshot, crossvenue),
                markets_list=[dict(id=m['id'], title=m['title']) for m in snapshot.markets])


def order_preview(snapshot, params, race, direction):
    """Exact order bodies bot.execute() would send for one scanner signal. Never sends."""
    import bot
    p = scan_params(params)
    results = scan_diagnostics(snapshot, load_exhaustive(), fee_per_share=p['fee'],
                               min_edge=p['edge'], cash=p['budget'] or None).opportunities
    r = next((r for r in results if r.race == race and r.direction == direction), None)
    if r is None:
        return None
    titles = {m['id']: m['title'] for m in snapshot.markets}
    legs = bot.preview(Client(), r)
    for leg in legs:
        leg['title'] = titles.get(leg['market_id'])
        leg['limit'] = round(leg['limit'], 4)
    return {'race': r.race, 'direction': r.direction, 'qty': r.qty, 'pnl': round(r.pnl, 4),
            'capital': round(r.capital, 4), 'snapshot_ts': snapshot.ts,
            'payload_verified': sig_client.PLACE_PAYLOAD_CONFIRMED, 'dry_run': True,
            'legs': legs,
            'note': 'Dry-run bodies only. Later legs shrink to actual fills; the last leg may '
                    'chase toward break-even. profileId is null until SIG_COOKIE carries the session.'}


def _read_jsonl_tail(path, limit):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[::-1]


def execution_view(source, limit=50):
    """Bot heartbeat plus the newest execution journal rows. Local files only."""
    bot = None
    try:
        bot = json.loads(Path(source.bot_status_path).read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        bot = {'error': 'bot status unreadable'}
    if bot and bot.get('ts'):
        try:
            age = _iso_age_seconds(bot['ts'])
            bot['age_s'] = round(age, 1)
            # Three missed ticks (at least 30s) means the bot is not running.
            bot['running'] = age <= max(30.0, 3 * float(bot.get('interval') or 5))
        except (TypeError, ValueError):
            bot['running'] = False
    kill_path = Path(source.kill_switch_path)
    return {'bot': bot, 'kill_switch': {'engaged': kill_path.exists()},
            'executions': _read_jsonl_tail(source.exec_log_path, limit),
            'release_phrase': RELEASE_PHRASE}


def _audit(source, event, **fields):
    path = Path(source.audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps({'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                            'event': event, **fields}) + '\n')


def engage_kill_switch(source, reason, actor='dashboard'):
    """Always allowed and idempotent. The bot stops sending before its next leg."""
    path = Path(source.kill_switch_path)
    already = path.exists()
    if not already:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(f"{reason}\nactor={actor} at={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}\n")
        tmp.replace(path)
    _audit(source, 'kill_switch_engage', actor=actor, reason=reason, already_engaged=already)
    return {'engaged': True, 'already_engaged': already}


def release_kill_switch(source, confirmation, actor='dashboard'):
    """Needs the typed phrase and no reconciliation that demands the switch."""
    if confirmation != RELEASE_PHRASE:
        raise ValueError(f'Type "{RELEASE_PHRASE}" to release the kill switch.')
    try:
        recon = json.loads(Path(source.reconciliation_path).read_text())
    except FileNotFoundError:
        recon = None
    except (OSError, ValueError):
        raise PermissionError('Reconciliation file is unreadable; resolve it before release.')
    if recon and recon.get('kill_switch_required'):
        raise PermissionError('Reconciliation requires the kill switch; resolve the mismatch first.')
    path = Path(source.kill_switch_path)
    was = path.exists()
    path.unlink(missing_ok=True)
    _audit(source, 'kill_switch_release', actor=actor, was_engaged=was)
    return {'engaged': False, 'was_engaged': was}


def _json_body(h, limit=10_000):
    length = int(h.headers.get('Content-Length', '0') or 0)
    if length > limit:
        raise ValueError('Request body too large')
    body = json.loads(h.rfile.read(length) or b'{}') if length else {}
    if not isinstance(body, dict):
        raise ValueError('Expected a JSON object')
    return body


def execution_actions(source):
    """POST actions for the dashboard; handler() adds the origin and token checks."""
    def respond(h, fn):
        try:
            h.send_body(200, json.dumps(fn(_json_body(h))).encode(), 'application/json')
        except PermissionError as exc:
            h.send_body(409, json.dumps({'error': str(exc)}).encode(), 'application/json')
        except (ValueError, json.JSONDecodeError) as exc:
            h.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')

    return {
        '/api/kill-switch/engage': lambda h: respond(h, lambda b: engage_kill_switch(
            source, str(b.get('reason') or 'engaged from dashboard')[:300])),
        '/api/kill-switch/release': lambda h: respond(h, lambda b: release_kill_switch(
            source, b.get('confirmation'))),
    }


class Source:
    def __init__(self, replay=None, browser_snapshot_path=None):
        self.replay = replay
        self.browser_snapshot_path = Path(browser_snapshot_path or ROOT / 'logs/browser_snapshot.json')
        self.lock = threading.Lock()
        self.crossvenue_lock = threading.Lock()
        self.snapshot = None
        self.fetched = 0
        self.news_cache = {}
        self.crossvenue_cache = None
        self.crossvenue_fetched = 0
        self.portfolio_cache = None
        self.portfolio_fetched = 0
        # Execution mode is server state; there is no endpoint to change it yet.
        self.mode = 'research'
        self.kill_switch_path = KILL_SWITCH
        self.levels_db_path = LEVELS_DB
        self.risk_limits_path = RISK_LIMITS
        self.reconciliation_path = RECONCILIATION
        self.news_breakers_path = NEWS_BREAKERS
        self.bot_status_path = BOT_STATUS
        self.bot_books_path = BOT_BOOKS
        self.fetch_lock = threading.Lock()
        self.exec_log_path = EXEC_LOG
        self.audit_log_path = AUDIT_LOG

    def status_inputs(self):
        """Cached state only; never triggers a network fetch."""
        now = time.monotonic()
        with self.lock:
            snapshot = self.snapshot
            portfolio = self.portfolio_cache
            portfolio_age = now - self.portfolio_fetched if portfolio is not None else None
        with self.crossvenue_lock:
            crossvenue_age = now - self.crossvenue_fetched if self.crossvenue_cache is not None else None
        return dict(snapshot=snapshot, portfolio=portfolio, portfolio_age=portfolio_age,
                    crossvenue_age=crossvenue_age)

    def accept_browser_snapshot(self, payload):
        markets = payload.get('markets')
        levels = payload.get('levels')
        if not isinstance(markets, list) or not isinstance(levels, dict):
            raise ValueError('Snapshot must include markets and levels')
        if len(markets) > 1000 or len(levels) > 1000:
            raise ValueError('Snapshot is too large')
        if any(not isinstance(m, dict) or 'id' not in m or 'title' not in m for m in markets):
            raise ValueError('Snapshot contains an invalid market')
        if any(not isinstance(v, list) for v in levels.values()):
            raise ValueError('Snapshot contains invalid order levels')
        snapshot = Snapshot(
            payload.get('ts', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())),
            markets, {int(k): v for k, v in levels.items()})
        self.browser_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.browser_snapshot_path.with_suffix(self.browser_snapshot_path.suffix + '.tmp')
        tmp.write_text(json.dumps({'ts': snapshot.ts, 'markets': snapshot.markets,
                                   'levels': {str(k): v for k, v in snapshot.levels.items()}},
                                  allow_nan=False))
        tmp.replace(self.browser_snapshot_path)
        with self.lock:
            self.snapshot = snapshot
            self.fetched = time.monotonic()
        return {'ok': True, 'markets': len(markets), 'levels': len(levels), 'ts': snapshot.ts}

    def news(self, market_id):
        with self.lock:
            if self.replay:
                return dict(headlines=[], tradingStatus='Replay', contextSummary='News unavailable in replay mode.')
            cached = self.news_cache.get(market_id)
            if cached and time.monotonic()-cached[0] < 300:
                return cached[1]
            data = fetch_news(Client(), market_id)
            self.news_cache[market_id] = (time.monotonic(), data)
            return data

    def _bot_books(self):
        """The running bot's latest books (logs/bot_books.json) if written in the last
        minute; reading them costs no SIG requests and never competes with the bot."""
        path = Path(self.bot_books_path)
        try:
            if time.time() - path.stat().st_mtime > 60:
                return None
            j = json.loads(path.read_text())
            return Snapshot(j['ts'], j['markets'], {int(k): v for k, v in j['levels'].items()})
        except (OSError, ValueError, KeyError):
            return None

    def _fetch_snapshot(self):
        """Fallback when no bot is running: every book, tolerating individual failures."""
        from concurrent.futures import ThreadPoolExecutor
        import fast_scan
        client = Client(concurrency=4)
        markets = fast_scan.load_markets(client)

        def one(m):
            try:
                return m['id'], client.levels(m['id'])
            except Exception:
                return m['id'], None
        with ThreadPoolExecutor(4) as ex:
            got = {mid: lv for mid, lv in ex.map(one, markets) if lv is not None}
        if not got:
            raise RuntimeError('no SIG books could be read')
        return Snapshot(dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                        [m for m in markets if m['id'] in got], got)

    def get(self):
        with self.lock:
            if self.snapshot is not None and time.monotonic() - self.fetched < 15:
                return self.snapshot
            cached = self.snapshot
        if self.replay:
            snapshot = Snapshot.load(self.replay)
        else:
            snapshot = self._bot_books()
            if snapshot is None:
                # One network fetch at a time, and never while holding self.lock
                # (status and portfolio reads need it).
                if not self.fetch_lock.acquire(blocking=cached is None):
                    return cached
                try:
                    snapshot = self._fetch_snapshot()
                finally:
                    self.fetch_lock.release()
        with self.lock:
            self.snapshot, self.fetched = snapshot, time.monotonic()
            return snapshot

    def get_crossvenue(self):
        if self.replay:
            return {}
        with self.crossvenue_lock:
            if self.crossvenue_cache is not None and time.monotonic() - self.crossvenue_fetched < 60:
                return self.crossvenue_cache
        inventory_limit = int(os.environ.get('CROSSVENUE_MARKET_LIMIT', '0'))
        if inventory_limit < 0:
            raise ValueError('CROSSVENUE_MARKET_LIMIT must be nonnegative')
        targeted = load_targeted_market_ids(ROOT / 'docs' / 'market-links.csv')
        data = fetch_public(['kalshi', 'polymarket'], inventory_limit, market_ids=targeted)
        with self.crossvenue_lock:
            self.crossvenue_cache = data
            self.crossvenue_fetched = time.monotonic()
            return data

    def get_portfolio(self):
        with self.lock:
            if self.replay:
                return {'venue': 'sig', 'status': 'REPLAY', 'read_only': True,
                        'kill_switch_required': False,
                        'note': 'Account reconciliation is unavailable in replay mode.'}
            if self.portfolio_cache is not None and time.monotonic() - self.portfolio_fetched < 60:
                return self.portfolio_cache
            try:
                client = Client()
                markets = client.markets()
                data = fetch_sig_portfolio(client, markets)
            except PermissionError:
                data = {'venue': 'sig', 'status': 'AUTH_REQUIRED', 'read_only': True,
                        'kill_switch_required': True,
                        'note': 'Signed-in SIG account data is unavailable.'}
            except Exception:
                data = {'venue': 'sig', 'status': 'UNAVAILABLE', 'read_only': True,
                        'kill_switch_required': True,
                        'note': 'SIG account data could not be read.'}
            self.portfolio_cache = data
            self.portfolio_fetched = time.monotonic()
            return data


def _iso_age_seconds(value):
    observed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds())


def levels_db_status(path):
    """Read-only probe of the levels database; levels_daemon.py is the only writer."""
    path = Path(path)
    result = {'path': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
              'ok': False, 'last_capture': None, 'age_s': None, 'error': None}
    if not path.exists():
        result['error'] = 'not found'
        return result
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            last = conn.execute('SELECT MAX(captured_at) FROM captures').fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result['error'] = str(exc)
        return result
    result.update(ok=True, last_capture=last, age_s=_iso_age_seconds(last) if last else None)
    return result


def _feed(name, age, detail):
    stale = age is None or age > 2 * FEED_CADENCE_S[name]
    return {'name': name, 'age_s': None if age is None else round(age, 1), 'stale': stale, 'detail': detail}


def build_status(source):
    """System status and the 13 gates at system scope. Reads caches and local files only."""
    inputs = source.status_inputs()
    reasons = []

    kill_path = Path(source.kill_switch_path)
    kill = {'engaged': kill_path.exists(), 'reason': None, 'since': None}
    if kill['engaged']:
        try:
            kill['reason'] = kill_path.read_text()[:500].strip() or None
            kill['since'] = dt.datetime.fromtimestamp(kill_path.stat().st_mtime, dt.timezone.utc).isoformat(timespec='seconds')
        except OSError:
            kill['reason'] = 'kill switch file present but unreadable'

    limits, limits_error = None, None
    try:
        limits = gates.load_limits(source.risk_limits_path)
    except (OSError, ValueError) as exc:
        limits_error = str(exc)
        reasons.append('risk limits unavailable')

    breakers, breaker_error = {}, None
    try:
        breakers = active_breakers(source.news_breakers_path)
    except (OSError, ValueError) as exc:
        breaker_error = str(exc)
        reasons.append('news circuit-breaker config invalid')

    recon = None
    try:
        recon = json.loads(Path(source.reconciliation_path).read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        recon = {'status': 'UNREADABLE'}

    snapshot = inputs['snapshot']
    snapshot_age = snapshot_age_seconds(snapshot) if snapshot is not None else None
    portfolio = inputs['portfolio']
    session = portfolio.get('status') if portfolio else None
    db = levels_db_status(source.levels_db_path)
    data_mode = 'replay' if source.replay else 'live'

    feeds = [
        _feed('sig_books', snapshot_age, f'snapshot {snapshot.ts}' if snapshot else 'no snapshot yet'),
        _feed('sig_account', inputs['portfolio_age'], session or 'not fetched yet'),
        _feed('crossvenue', inputs['crossvenue_age'], 'Kalshi + Polymarket public' if inputs['crossvenue_age'] is not None
              else 'not fetched yet'),
        _feed('levels_db', db['age_s'], db['error'] or f"last capture {db['last_capture']}"),
    ]
    reasons += [f"{f['name']} stale" for f in feeds if f['stale']]

    system = dict(sig_session=session, payload_verified=sig_client.PLACE_PAYLOAD_CONFIRMED,
                  kill_switch=kill, recon=recon, mode=source.mode, limits=limits,
                  limits_error=limits_error, sig_snapshot_age_s=snapshot_age, active_breakers=breakers)
    gate_list = gates.evaluate(system)
    health = 'down' if snapshot is None else ('degraded' if reasons else 'ok')
    return {
        'as_of': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'health': health, 'health_reasons': reasons, 'data_mode': data_mode, 'mode': source.mode,
        'last_good_snapshot': snapshot.ts if snapshot else None,
        'kill_switch': kill,
        'sig_auth': {'session': session, 'payload_verified': sig_client.PLACE_PAYLOAD_CONFIRMED},
        'db': db, 'feeds': feeds,
        'breakers': {'active_markets': sorted(breakers), 'error': breaker_error},
        'limits': limits, 'limits_error': limits_error,
        'gates': gate_list, 'gate_summary': gates.summary(gate_list), 'gate_hash': gates.gate_hash(gate_list),
    }


def handler(source, action_token=None, actions=None):
    """Build the request handler.

    `actions` maps a POST path to a callable taking the handler. Every action
    requires this dashboard's own origin and `action_token`, which is served
    only inside the same-origin page.
    """
    action_token = action_token or secrets.token_urlsafe(32)
    actions = dict(actions or {})

    class Handler(BaseHTTPRequestHandler):
        def local_hosts(self):
            port = self.server.server_address[1]
            return {f'127.0.0.1:{port}', f'localhost:{port}'}

        def host_ok(self):
            # A foreign hostname resolving to 127.0.0.1 (DNS rebinding) is refused.
            return self.headers.get('Host', '') in self.local_hosts()

        def local_origin(self):
            return self.headers.get('Origin') in {f'http://{h}' for h in self.local_hosts()}

        def relay_cors(self):
            return urlparse(self.path).path == RELAY_PATH and self.headers.get('Origin') == RELAY_ORIGIN

        def refuse(self, message):
            self.send_body(403, json.dumps({'error': message}).encode(), 'application/json')

        def do_OPTIONS(self):
            if self.host_ok() and self.relay_cors():
                self.send_body(204, b'', 'text/plain')
            else:
                self.refuse('Cross-origin requests are not allowed.')

        def do_POST(self):
            if not self.host_ok():
                self.refuse('Unknown host.')
                return
            path = urlparse(self.path).path
            if path in actions:
                token = self.headers.get('X-Action-Token', '')
                if not (self.local_origin() and hmac.compare_digest(token, action_token)):
                    self.refuse('Action requires the dashboard origin and action token.')
                    return
                actions[path](self)
                return
            if path != RELAY_PATH:
                self.send_body(404, b'Not found', 'text/plain')
                return
            origin = self.headers.get('Origin')
            if origin is not None and origin != RELAY_ORIGIN and not self.local_origin():
                self.refuse('Snapshots are accepted only from the SIG site or this dashboard.')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length > 20_000_000:
                    raise ValueError('Snapshot too large')
                result = source.accept_browser_snapshot(json.loads(self.rfile.read(length)))
                self.send_body(200, json.dumps(result).encode(), 'application/json')
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')

        def do_GET(self):
            if not self.host_ok():
                self.refuse('Unknown host.')
                return
            url = urlparse(self.path)
            if url.path in ("/api/signals", "/api/kelly", "/api/news"):
                try:
                    params = parse_qs(url.query)
                    # Validate inputs before making external requests.
                    for values in params.values():
                        value = float(values[0])
                        if not math.isfinite(value) or value < 0:
                            raise ValueError("Invalid filter")
                    if url.path == '/api/news':
                        mid = int(params['market'][0])
                        if mid <= 0:
                            raise ValueError('Invalid market')
                        self.send_body(200, json.dumps(source.news(mid)).encode(), 'application/json')
                        return
                    snapshot = source.get()
                    if url.path == '/api/kelly':
                        mid = int(params['market'][0])
                        book = snapshot.books()[mid]
                        no = params.get('no', ['0'])[0] == '1'
                        ladder = [(1-p, q) for p, q in book.bids] if no else book.asks
                        data = size_position(ladder, float(params['probability'][0]),
                                             float(params['bankroll'][0]),
                                             float(params.get('fraction', ['0.25'])[0]),
                                             float(params.get('fee', ['0'])[0]))
                        data['ts'] = snapshot.ts
                    else:
                        data = report(snapshot, params, source.get_crossvenue())
                    data["mode"] = "replay" if source.replay else "live"
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), "application/json")
                except ValueError as exc:
                    self.send_body(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except Exception:
                    self.send_body(502, b'{"error":"Market data unavailable. Retry the scan."}', "application/json")
            elif url.path == '/api/status':
                try:
                    self.send_body(200, json.dumps(build_status(source), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(500, b'{"error":"Status unavailable."}', 'application/json')
            elif url.path == '/api/execution':
                try:
                    self.send_body(200, json.dumps(execution_view(source), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(500, b'{"error":"Execution state unavailable."}', 'application/json')
            elif url.path == '/api/orders/preview':
                try:
                    params = parse_qs(url.query)
                    race = params.pop('race', [''])[0]
                    direction = params.pop('direction', [''])[0]
                    if not race or len(race) > 120:
                        raise ValueError('Invalid race')
                    if direction not in {'SELL_ALL', 'BUY_ALL'}:
                        raise ValueError('Invalid direction')
                    data = order_preview(source.get(), params, race, direction)
                    if data is None:
                        self.send_body(404, b'{"error":"Signal not in the current snapshot."}', 'application/json')
                        return
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
                except ValueError as exc:
                    self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Order preview unavailable. Retry the scan."}', 'application/json')
            elif url.path == '/api/crossvenue':
                try:
                    self.send_body(200, json.dumps(source.get_crossvenue(), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Cross-venue data unavailable. Retry the scan."}', 'application/json')
            elif url.path == '/api/portfolio':
                try:
                    self.send_body(200, json.dumps(source.get_portfolio(), allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Portfolio state unavailable. Retry the reconciliation check."}', 'application/json')
            elif url.path == '/api/opportunities' or url.path.startswith('/api/opportunities/'):
                try:
                    from opportunities import get_opportunity, list_opportunities
                    if url.path == '/api/opportunities':
                        params = parse_qs(url.query)
                        strategy = params.get('strategy', [None])[0]
                        state = params.get('state', [None])[0]
                        search = params.get('search', [None])[0]
                        if strategy is not None and strategy not in {'sig_arb', 'complete_set', 'xv_move', 'rel_value'}:
                            raise ValueError('Unknown strategy')
                        if state is not None and state not in {'blocked', 'research_only', 'paper_eligible', 'not_ready', 'exec_ready'}:
                            raise ValueError('Unknown state')
                        if search is not None and len(search) > 120:
                            raise ValueError('Search is too long')
                        data = list_opportunities(source, strategy=strategy, state=state, search=search)
                    else:
                        opportunity_id = url.path[len('/api/opportunities/'):]
                        if not opportunity_id or '/' in opportunity_id:
                            raise ValueError('Invalid opportunity id')
                        qty = parse_qs(url.query).get('qty', [None])[0]
                        if qty is not None:
                            try:
                                qty = float(qty)
                            except ValueError as exc:
                                raise ValueError('Invalid quantity') from exc
                            if not math.isfinite(qty) or qty <= 0:
                                raise ValueError('Invalid quantity')
                        data = get_opportunity(source, opportunity_id, qty)
                        if data is None:
                            self.send_body(404, b'{"error":"Opportunity not found."}', 'application/json')
                            return
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
                except ValueError as exc:
                    self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Opportunity data unavailable. Retry the scan."}', 'application/json')
            elif url.path.startswith('/api/books/'):
                try:
                    from opportunities import read_book
                    parts = url.path.split('/')
                    if len(parts) != 5 or not parts[3] or not parts[4]:
                        raise ValueError('Expected /api/books/{venue}/{market_id}')
                    data = read_book(source, parts[3], parts[4])
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
                except ValueError as exc:
                    self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Book data unavailable. Retry the scan."}', 'application/json')
            elif url.path.startswith('/api/history/'):
                try:
                    from opportunities import read_history
                    market_id = url.path[len('/api/history/'):]
                    if not market_id or '/' in market_id:
                        raise ValueError('Invalid market id')
                    params = parse_qs(url.query)
                    venue = params.get('venue', [None])[0]
                    outcome = params.get('outcome', ['YES'])[0].upper()
                    if venue is not None and venue.lower() not in {'kalshi', 'polymarket'}:
                        raise ValueError('Invalid venue')
                    if outcome not in {'YES', 'NO'}:
                        raise ValueError('Invalid outcome')
                    try:
                        limit = int(params.get('limit', ['300'])[0])
                    except ValueError as exc:
                        raise ValueError('Invalid limit') from exc
                    if limit < 1 or limit > 1000:
                        raise ValueError('Limit must be between 1 and 1000')
                    start = params.get('from', [None])[0]
                    end = params.get('to', [None])[0]
                    for stamp in (start, end):
                        if stamp:
                            try:
                                dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                            except ValueError as exc:
                                raise ValueError('Invalid timestamp') from exc
                    data = read_history(source, market_id, venue, outcome, start, end, limit)
                    self.send_body(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
                except ValueError as exc:
                    self.send_body(400, json.dumps({'error': str(exc)}).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"History unavailable. Retry the scan."}', 'application/json')
            elif url.path == '/api/mappings':
                try:
                    from opportunities import mapping_view
                    self.send_body(200, json.dumps({'mappings': mapping_view(source)}, allow_nan=False).encode(), 'application/json')
                except Exception:
                    self.send_body(502, b'{"error":"Mappings unavailable."}', 'application/json')
            elif url.path in ("/", "/dashboard.css", "/dashboard.js", "/status.js") or url.path.startswith('/js/'):
                file = "dashboard.html" if url.path == "/" else url.path[1:]
                allowed = {"js/api.js", "js/state.js", "js/opportunities.js", "js/drawer.js",
                           "js/ticket.js", "js/arb-ticket.js", "js/execution.js", "js/account.js"}
                if file.startswith('js/') and file not in allowed:
                    self.send_body(404, b"Not found", "text/plain")
                    return
                mime = {"html": "text/html", "css": "text/css", "js": "application/javascript"}
                body = (ROOT / "web" / file).read_bytes()
                if file == "dashboard.html":
                    meta = f'<meta name="action-token" content="{action_token}"></head>'
                    body = body.replace(b"</head>", meta.encode(), 1)
                self.send_body(200, body, mime[file.split(".")[-1]])
            else:
                self.send_body(404, b"Not found", "text/plain")

        def send_body(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Vary", "Origin")
            if self.relay_cors():
                self.send_header("Access-Control-Allow-Origin", RELAY_ORIGIN)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass        # the browser gave up on this request; nothing to deliver

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--replay")
    args = parser.parse_args()
    source = Source(args.replay)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(source, actions=execution_actions(source)))
    print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
