# sig_arb — SIG Super.Market complement-arb signals → systematic trading

Finds races where the mutually-exclusive markets (R / D / I) are priced inconsistently:
- **SELL_ALL:** Σ YES bids > 1. Buy NO on every leg. This is safe as long as at most one party can win.
- **BUY_ALL:** Σ YES asks < 1. Buy YES on every leg. Only safe for races listed in `exhaustive.txt`.

Each signal is sized by walking the depth of every book (VWAP, maximum executable size).

## 1. Setup (once)
```bash
cd sig_arb
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```
Next, get your cookie. With **sig.thesuper.market** open and logged in, open DevTools → **Network**, reload the page, and click any `/api/...` request. Under **Request Headers**, copy the whole `cookie:` value and paste it after `SIG_COOKIE=` in `.env`.
The cookie is your login session. Keep `.env` private (it is in `.gitignore`). If you start getting 401/403 errors, copy a fresh cookie.

Check the setup offline:
```bash
python test_engine.py && python test_system.py
python signals.py --replay fixtures/sample_snapshot.json
```

## 2. Stage 1: signal generation, manual orders
```bash
python signals.py                        # one scan
python signals.py --watch 5 --bell       # rescan every 5s; prints and beeps on new or changed signals
python signals.py --watch 5 --min-edge 0.005 --min-pnl 5 --budget 20000
python signals.py --dump snap.json       # save raw books (replay them later with --replay)
```
Each signal prints **order tickets in the site's own wording**, thinnest leg first:
```
 1 Delaware Senate   SELL_ALL   1000   30.00   970.00  3.09%  0.030/0.030/0.030
   1. Republican  #386  BUY NO  limit 0.920 (avg 0.9200) [= SELL YES >= 0.080] x 1000  <link>
   2. Democratic  #353  BUY NO  limit 0.050 (avg 0.0500) [= SELL YES >= 0.950] x 1000  <link>
      last leg break-even: BUY NO <= 0.080 on #353
```
How to place a signal by hand:
1. Open leg 1's link and choose **Limit**, **Buy No**, the limit price and the quantity.
2. Place leg 2 straight away. Never pay more than the break-even price shown for the last leg.
3. If leg 2 only fills partly, either work the remainder or sell back the excess on leg 1.

Every new signal is appended to `logs/signals.csv`. Use it to compare signals with your fills later.

Tickets only show when the edge is positive. The **near misses** section shows races that are close (edge 0 means an arb).

## 3. Stage 2: semi-systematic (you approve, the script sends)
Do this once, after trading opens on 1 Oct at 12:00 ET:
1. Place one small order by hand with DevTools open.
2. Under Network, find `/api/trading/orders/place` and look at its Payload and Response.
3. Compare them with `Client.place()` in `sig_client.py` and fix any field names. Also fix the fill fields read by `_fill_of()` in `bot.py`.
4. Set `PLACE_PAYLOAD_CONFIRMED = True`.

Then:
```bash
python bot.py --mode confirm --interval 5            # prompts y/N, runs as DRY RUN
python bot.py --mode confirm --interval 5 --live     # prompts y/N, sends real orders
```
How execution works:
- It calls `quote` on each leg, then places a limit order at the walk's worst price.
- It cancels any unfilled remainder.
- Later legs are sized to what actually filled.
- The last leg may move its price by up to `--chase-ticks` but never past break-even.
- Results go to `logs/executions.jsonl`, with status `DONE`, `IMBALANCED` (shows the uneven quantity), `LEGGED` or `MISS`.

## 4. Stage 3: fully systematic
```bash
python bot.py --mode auto --live --interval 3 --min-edge 0.005 --min-edge-3leg 0.01 \
              --max-per-race 10000 --max-gross 60000 --cooldown 30
```
Risk checks run before every trade: marginal edge floor (stricter for 3-leg races), minimum PnL, capital cap per race, total capital cap, and a per-race cooldown. `IMBALANCED` or `LEGGED` results are **not** fixed automatically yet. Watch the log and flatten them by hand. Adding an automatic unwind is the next step.

## Files
| File | Role |
|---|---|
| `arb_engine.py` | Pure maths: normalise books, group races, depth walk, VWAP, break-even |
| `sig_client.py` | HTTP client (read books, balance, quote, place, cancel) and `.env` loading |
| `signals.py` | Signal generator: once, watch, dump, replay, tickets, CSV log |
| `bot.py` | Systematic loop: signal, confirm or auto modes, risk checks, legged execution, journal |
| `scan_console.js` | Browser-console fallback, if Python requests get blocked |
| `exhaustive.txt` | Your list of races that are safe for BUY_ALL |
| `fixtures/sample_snapshot.json` | Offline test data (real Delaware book plus a synthetic multi-level race) |

## Troubleshooting
- **401/403 on reads:** your cookie has expired or wasn't copied in full. Copy it again. If Python is still blocked (bot protection), use `scan_console.js` in the browser.
- **"Tournament has not started yet":** `quote` and `place` only work from 1 Oct 12:00 ET. Reading books works now.
- **Slow scans:** 237 books take about 2 s with 8 threads. Don't go below about a 3 s interval, and don't raise concurrency much. Being polite with request volume keeps your account safe.
## Local Dashboard

Run `python3 dashboard.py --port 8876` and open http://127.0.0.1:8876.
The read-only dashboard refreshes every 30 seconds and ranks opportunities by
estimated total profit, VWAP edge, ROI, or executable quantity. Expand a race
for per-leg VWAP, worst execution price, market links, and cumulative profit
across order-book depth. Capital caps apply independently to each race, not to
the portfolio. Blank or zero capital cap means uncapped.

Bundle VWAP is the sum of the prices paid for one share on every leg; BUY NO
prices are converted from YES bids. VWAP excludes fees; profit, edge, ROI and
capital include the configured per-share, per-leg fee. Fees default to zero.
Estimates require all legs to fill; order books are fetched sequentially in
batches and are not an atomic exchange snapshot. BUY YES opportunities require
an explicit race entry in `exhaustive.txt`. This dashboard cannot place orders.

Offline preview: `python3 dashboard.py --port 8877 --replay fixtures/sample_snapshot.json`.
Dashboard checks: `python3 -m unittest test_dashboard`.

For browser-authenticated live data, run `browser_relay.js` in the SIG market
page console. It fetches the same paginated market and order endpoints as the
supplied export script, sends full levels to `/api/browser_snapshot`, and
refreshes every 15 seconds. Stop it with
`clearInterval(window.sigDashboardRelay)`. The relay only reads market data;
execution remains dry-run and requires separate order-payload verification.

### Control-market guardrails

`control_model.py` records the unresolved 50-50 Senate interpretation, the
Independent-winner case, and the possible Ohio omission. It reports the current
inventory and deliberately returns `ready_for_riskless_control_arb: false` until
the organisers answer those wording questions. Its probability-bounds helper is
only a coarse sanity check; turning race probabilities into Senate-control fair
value requires a joint simulation with a dependence model.

### Kelly and News

The probability-value calculator sizes one binary position using an explicit
probability that the selected YES or NO position pays out. It maximizes expected
log terminal wealth against available depth, fees, and whole-share granularity.
Fractional Kelly uses the selected fraction of bankroll as the risk bankroll.
It does not account for existing positions or correlations, so independent
results must not be summed into a portfolio allocation. A price is not an
independent probability forecast. No bankroll or probability is prefilled.

Related News is available through the dashboard's selected-market news panel.
It reads the platform's public `/api/markets/[id]/news?marketId=...` endpoint,
caches reads for five minutes, and archives distinct article versions with
first-seen timestamps in ignored `logs/news.sqlite3`. Summaries and probability
claims remain unverified; they never automatically populate Kelly probabilities.
The panel also reports the tournament's scheduled trading window from page data.
The scanner's quotes are indicative and do not establish permission to trade.
A useful event record needs a source URL,
publication and first-seen timestamps, affected race, deduplicated event ID,
prior/posterior probability and rationale. Evaluate forecasts with calibration
and Brier/log scores on later outcomes; evaluate trade signals against executable
prices after first-seen time, including fees and failed fills. Never backfill a
historical signal using an article's later revised contents. Track signal
persistence separately from probability: surviving scans do not prove fair value.

Kelly reference: https://theory.stanford.edu/~blynn/pr/kelly.html
Run sizing checks with `python3 -m unittest test_kelly`.

### Cross-venue research scanner

The repository includes a read-only comparison layer for public Kalshi and
Polymarket market data. It does not place, quote, cancel, or simulate orders.
It normalizes public metadata and indicative prices, while keeping approved
cross-venue mappings in an explicit review registry. The registry starts empty
because similarly worded contracts are not proof of identical settlement rules.

Fetch a public inventory snapshot:

```bash
python3 crossvenue.py fetch-public --venues kalshi polymarket --limit 1000 \
  --out /tmp/crossvenue-public.json
```

Offline movement analysis uses a SIG snapshot, timestamped reference
observations, and only mappings marked `APPROVED` in a registry:

```bash
python3 crossvenue.py scan \
  --snapshot fixtures/sample_snapshot.json \
  --observations fixtures/crossvenue/observations.json \
  --matches fixtures/crossvenue/test_matches.json \
  --lookback-minutes 120 --min-move-pp 5
```

The default movement threshold is 5 percentage points over two hours. Results
are research candidates only and require a current SIG executable side plus a
configurable price gap. Missing history, stale data, missing SIG liquidity,
and unapproved mappings are rejected explicitly.

Run the combined live, read-only scan once or every minute. It refreshes SIG
books and public Kalshi/Polymarket observations, persists mapped observations,
applies the default 5% SIG ROI hurdle, and reports movement, executable gaps,
and research-only relative-value z-scores. It never places orders:

```bash
SIG_COOKIE='...' PYTHONPATH=. python3 crossvenue.py live --once
SIG_COOKIE='...' PYTHONPATH=. python3 crossvenue.py live --interval 60
```

The production mapping registry intentionally remains empty until each
contract's settlement rules and outcome mapping are reviewed and approved.

The current point-in-time market index, including links to all SIG markets and
the review status of venue mappings, is in
[docs/market-links.md](docs/market-links.md). Regenerate it from the
authenticated SIG universe with:

```bash
PYTHONPATH=. python3 tools/generate_market_links.py
```

The generator queries Kalshi's paginated events feed with nested markets and
Polymarket's active market feed. Links are discovery candidates until contract
rules, resolution sources, timing, and outcome semantics have been reviewed.

The recurring workflow and staged execution gates are documented in
[docs/operating-guide.md](docs/operating-guide.md) and
[docs/systematic-execution-plan.md](docs/systematic-execution-plan.md).
Dashboard opportunity rows carry explicit freshness, mapping, settlement, fee,
liquidity, and execution-risk fields; `execution_ready` remains false until
the relevant gates are independently verified.
