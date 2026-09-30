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
