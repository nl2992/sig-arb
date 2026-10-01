# Wiring the operations dashboard

This guide explains how to turn the design in `docs/dashboard-design/` into
working code on top of the existing `dashboard.py` server. It follows the
stages in `systematic-execution-plan.md`: read-only first, then paper and
shadow, then reconciliation, and live last.

The design is the target. It is **not** permission to trade. Every live
control stays disabled until the backend reports that all 13 gates pass.

## 1. What is in `docs/dashboard-design/`

These are design reference files. They are not runnable pages: each
`*.dc.html` depends on the canvas runtime (`support.js`), which is not in
this repo. Use them for layout, copy, states and colours, and rebuild the
screens in `web/`. The live canvas is at
https://claude.ai/artifact/CGpDZvZVMgjg9wqaFDyLHv.

| File | Screen |
|---|---|
| `Main.dc.html` | Header, mode and risk strip, opportunity table, gates, feed freshness, block log |
| `Drawer.dc.html` | Opportunity detail: books, VWAP, fees, slippage, break-even, scenarios, settlement, history, news |
| `OrderTicket.dc.html` | Review → confirm ticket with the 13-gate checklist |
| `ArbTicket.dc.html` | Multi-leg ticket: partial fill, resize, residual, operator actions, timeline |
| `Execution.dc.html` | Routes, active tickets, pending authorizations, blotter, execution quality, gate re-checks, event tape |
| `Account.dc.html` | SIG account, reconciliation, risk limits, kill switch |
| `States.dc.html` | Empty, stale, blocked, partial, error states and button states |
| `Architecture.dc.html` | Component tree, mode matrix, opportunity and order state machines |
| `Contract.dc.html` | Data model, API contract, backend invariants |

All numbers in the design are mock data.

## 2. Stack decision

Keep the current stack: the stdlib `ThreadingHTTPServer` in `dashboard.py`
and plain JavaScript in `web/`, with no build step. The design's component
tree maps onto ES modules rather than React:

```
web/
  dashboard.html          shell: header, control bar, tabs, overlay root
  dashboard.css           tokens (colours, badges, tables) from the design
  js/api.js               fetch wrappers, one per endpoint, with timeouts
  js/state.js             single store; the server is the source of truth
  js/header.js            StatusHeader, GlobalBanner, EmergencyStop
  js/opportunities.js     OpportunityTable, FilterBar, gate/feed/block panels
  js/drawer.js            OpportunityDrawer and its panels
  js/ticket.js            OrderTicket (review → confirm)
  js/arb-ticket.js        ArbTicket
  js/execution.js         ExecutionView
  js/account.js           AccountView, RiskView
```

`dashboard.py` currently only serves `/`, `/dashboard.css` and
`/dashboard.js`. Extend the static-file branch to serve `web/js/*.js`, with a
fixed allow-list of file names and no path joins from user input.

Split `dashboard.py` once the new endpoints land: keep the HTTP handler
there, and move gates, tickets and execution into their own modules
(section 4).

## 3. Security changes to make first

These are needed before any endpoint that changes state.

**Status: done.** Implemented in `dashboard.py` and covered by
`test_dashboard_security.py`:

- **Host check.** Any request whose `Host` is not `127.0.0.1:<port>` or
  `localhost:<port>` gets a 403. This blocks DNS rebinding.
- **Relay CORS.** CORS headers are sent only to `https://sig.thesuper.market`,
  and only on `/api/browser_snapshot`. Preflight requests from any other
  origin, or to any other path, get a 403.
- **Relay origins.** The relay POST accepts no `Origin`, the SIG origin or
  the dashboard's own origin. Anything else gets a 403.
- **Action token.** `handler(source, action_token, actions)` generates a
  token when none is given and puts it in the page as
  `<meta name="action-token">`. Any path registered in `actions` requires
  both the dashboard's own origin and a matching `X-Action-Token` header,
  compared in constant time.
- **Response headers.** Every response carries `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
  `Vary: Origin`.

The original plan for this step follows.

1. **CORS.** `send_body` sets `Access-Control-Allow-Origin: *` on every
   response. That is acceptable for read-only JSON. It is not acceptable
   once there are POST endpoints for tickets or the kill switch, because
   any website open in your browser could call them on `127.0.0.1`.
   - Allow `https://sig.thesuper.market` only, and only on
     `/api/browser_snapshot` (needed by `browser_relay.js`).
   - Send no CORS header on any other endpoint.
   - On every other POST or PUT, reject requests whose `Origin` is not
     `http://127.0.0.1:<port>`.
2. **Local action token.** On startup, generate a random token and put it
   in the served HTML. Mutating endpoints then require it in an
   `X-Action-Token` header. This stops cross-site form posts.
3. **Bind address.** Keep `127.0.0.1`. Do not add a `--host 0.0.0.0` option.
4. **Cookie.** The UI never asks for, shows or stores `SIG_COOKIE`. The
   auth-error state tells the operator to update `.env`.

## 4. Backend modules to add

### `gates.py`: the single gate list

**Status: done (step 2).** `gates.py` evaluates all 13 gates at system scope
or for one candidate. Any gate it cannot evaluate fails, and gates that need
an opportunity report `pass: null` at system scope, which callers must treat
as not passing. `load_limits` validates `config/risk_limits.json` and
refuses `auto_hedge: true`. `gate_hash` covers `(id, pass)` only, so live
quote ages do not count as drift.

`GET /api/status` (`dashboard.build_status`) reads only caches and local
files, never the network. It returns:

- health and data mode (live or replay), the execution mode, and the last
  good snapshot;
- kill switch state from `logs/KILL_SWITCH`;
- SIG auth: session and `PLACE_PAYLOAD_CONFIRMED`;
- a read-only probe of `levels.sqlite3`;
- ages of the 4 feeds;
- active news breakers and the loaded limits;
- the gates, their summary and their hash.

`web/status.js` renders the ops strip and the gate panel. Reconciliation is
read from `logs/reconciliation.json`, which step 5 must write as
`{"status": "RECONCILED" | "MISMATCH", "as_of": ...}`. The execution mode is
`Source.mode` and is fixed to `research` until a mode endpoint exists.
Tests: `test_gates.py`, `test_status.py`.

The original plan follows.

This is one function that everything calls: the dashboard, the ticket and
the confirm endpoint.

```python
GATES = ("sig_auth", "payload_verified", "venue_ids", "fees_known",
         "mapping_approved", "settlement_approved", "quotes_fresh",
         "liquidity", "no_breaker", "risk_limits", "recon_clean",
         "kill_switch_off", "live_mode")

def evaluate(ticket_or_opportunity, system) -> list[dict]:
    """Return [{id, pass, detail, at}] for all 13 gates, in this order."""

def gate_hash(gates) -> str:
    """sha256 of the (id, pass) pairs; changes only when a gate flips."""
```

Where each gate comes from in the existing code:

| Gate | Source |
|---|---|
| `sig_auth` | `Client()._get` succeeds; `PermissionError` means expired. `Source.get_portfolio()` already maps this to `AUTH_REQUIRED`. |
| `payload_verified` | `sig_client.PLACE_PAYLOAD_CONFIRMED`. Only a code change flips it, never the UI. |
| `venue_ids` | Market ids exist in `client.markets()`; Kalshi/Polymarket ids come from `docs/market-links.csv` and `load_targeted_market_ids`. |
| `fees_known` | A pinned fee schedule per venue (add `config/fees.json` with a version and date). A missing entry means unknown. It never defaults to 0. |
| `mapping_approved` | `market_matches.load_registry()`, approved entries only. Discovered matches stay `REVIEW_REQUIRED`. |
| `settlement_approved` | SELL_ALL: mutual exclusivity. BUY_ALL: race listed in `exhaustive.txt` (`load_exhaustive`). Cross-venue: a reviewer decision per attribute. Also respect `control_model.WORDING_REGISTER`: unresolved wording blocks. |
| `quotes_fresh` | `paper.snapshot_age_seconds()` for SIG; per-venue observation timestamps for Kalshi/Polymarket. Compare against the max quote age limit. |
| `liquidity` | Visible depth at or inside the limit is at least the qty, for every leg. Never impute depth. |
| `no_breaker` | `news_guard.active_breakers(config/news_circuit_breakers.json)`. |
| `risk_limits` | Per-trade, per-venue, per-event, daily loss, max slippage, max residual (section 6). |
| `recon_clean` | Last `reconciliation.reconcile_portfolio()` result is `RECONCILED`, recent, with zero unmatched fills. |
| `kill_switch_off` | `logs/KILL_SWITCH` does not exist (`paper.kill_switch_active`). |
| `live_mode` | Server-held mode is `human_confirmed` or `constrained_live`. |

Opportunity state is derived from these gates on the server, never in the
browser:

- `blocked`: a hard failure (stale quote, no depth, breaker, not exhaustive, rejected mapping).
- `research_only`: edge exists but a mapping, settlement or fee review is outstanding.
- `paper_eligible`: every gate except the live-only ones passes.
- `not_ready`: paper-eligible, but a live-only gate fails.
- `exec_ready`: all 13 pass.

### `tickets.py`: review, confirm, paper, shadow, dry-run

- **Append-only journal.** Store tickets in `logs/tickets.jsonl`: one event
  per line, with the order state, actor and gate snapshot. `logs/` is
  already git-ignored.
- **Create.** `create(opportunity_id, qty, limits)` re-reads the books,
  computes VWAP from visible levels, and stores `quote_expires_at` and
  `gate_hash`.
- **Dry-run.** Returns `Client.place(..., dry_run=True)["body"]`. That dict
  is the payload preview on the ticket, so the UI shows the fields `place()`
  would really send.
- **Paper.** Uses `paper.simulate_result` against the book at event time
  (`levels.sqlite3` for replay).
- **Shadow.** Records the dry-run body with a timestamp and never sends it.
- **Save and copy params.** Write to and read from the journal. No network.
- **Order ids.** Give every leg a `client_order_id`. `place()` already sends
  a random `idempotencyKey`; derive it from `client_order_id` instead, so
  retries are safe.

### `execution.py`: leg orchestration (live stage only)

- **Leg order.** Submit the thinnest or most failure-sensitive leg first,
  as in `systematic-execution-plan.md`, Stage 2.
- **After each fill.** Recompute the break-even for the remaining legs and
  resize them to the actual filled quantity.
- **Residual.** If residual is above the limit, halt the ticket and queue
  the leg-2 and flatten choices as **pending authorizations**. Never send
  them automatically.
- **Journal.** Write every quote, submit, ack, fill, cancel and residual to
  `logs/tickets.jsonl`. `bot.py` currently writes to
  `logs/executions.jsonl`; move it onto the same journal so the Execution
  screen sees one tape.

## 5. HTTP endpoints

Add these to `handler()` in `dashboard.py`. Keep the existing endpoints
working: `/api/signals`, `/api/crossvenue`, `/api/portfolio`, `/api/news`,
`/api/kelly` and `POST /api/browser_snapshot`.

### Phase 1: read-only (Stage 0/1)

**Status: implemented (step 3).** `opportunities.py` now normalizes the
existing SIG scanner and approved cross-venue research output into server-
decided opportunity states. `GET /api/opportunities` and its detail route
return gate snapshots and block reasons; `GET /api/books/{venue}/{market_id}`
returns the SIG snapshot or the latest read-only SQLite capture; and
`GET /api/history/{market_id}` returns stored observations with missing values
left as `null`. No endpoint writes the levels database or sends an order.
The dashboard renders the normalized state alongside the existing scanner.
Cross-venue rows remain research-only while the mapping registry is empty or
unapproved.

| Endpoint | Built from |
|---|---|
| `GET /api/status` | Snapshot age, `levels.sqlite3` last capture, portfolio status, `KILL_SWITCH`, `PLACE_PAYLOAD_CONFIRMED`, active breakers, mode, system-level gates, feed ages |
| `GET /api/opportunities` | `report()` signals, `scan_movements`, `scan_pairs`, normalized to the `Opportunity` shape with gates and block reasons |
| `GET /api/opportunities/{id}` | Books for every leg (all visible levels), VWAP for the requested qty, fees, slippage, break-even, partial-fill scenarios, settlement attributes, timestamps |
| `GET /api/books/{venue}/{market_id}` | SIG: `Client.levels`; others: the latest `levels.sqlite3` capture |
| `GET /api/history/{market_id}` | `levels.sqlite3` `books` table. Return gaps as explicit nulls; never interpolate. |
| `GET /api/mappings` | `load_registry()` plus the review queue |
| `GET /api/execution/*` | Read from `logs/tickets.jsonl` and `logs/paper-runs.jsonl` |

Strategy mapping from the repo to the design:

| Design | Repo |
|---|---|
| `sig_arb` | `SELL_ALL` from `arb_engine.scan` |
| `complete_set` | `BUY_ALL`, which needs `exhaustive.txt` |
| `xv_move` | `movement_scanner.scan_movements` |
| `rel_value` | `relative_value.scan_pairs` |

The design's mock rows are illustrative; this mapping wins.

Open `levels.sqlite3` read-only from the web server:
`sqlite3.connect("file:logs/levels.sqlite3?mode=ro", uri=True)`.
`levels_daemon.py` stays the only writer. If the database is locked or
missing, report it in `/api/status` and disable history. Do not fail the
whole page.

### Phase 2: tickets without sending (Stage 1)

`POST /api/tickets`, `/requote`, `/dry-run`, `/paper`, `/shadow`, `/save`,
`GET /params`, `/cancel` for paper orders.

Rules:

- **Fixture data.** Anything built from `--replay` or fixture data carries
  `source: "fixture"`. `/api/tickets` returns 400 for any non-paper mode
  when the source is a fixture.
- **Expired quotes.** An expired quote returns 409 and asks for a re-quote.

### Phase 3: reconciliation (before Stage 2)

- `POST /api/reconcile`: build the intended positions and open orders from
  `logs/tickets.jsonl`, fetch the actual ones with `fetch_sig_portfolio`,
  and run `reconcile_portfolio`.
- `kill_switch_required: true` in the result creates `logs/KILL_SWITCH`
  automatically. Engaging is always allowed; releasing never happens
  automatically.
- A fill with no matching `client_order_id` is an unmatched fill. It blocks
  `recon_clean` until someone attaches or acknowledges it.

### Phase 4: live (Stage 2 onward)

`POST /api/tickets/{id}/confirm`, `/legs/{n}/authorize`, `/flatten`.

`confirm` must:

1. Require the action token and the typed confirmation text.
2. Re-run `gates.evaluate()` on the server and compare it with the ticket's
   `gate_hash`. Return 409 on any drift.
3. Return 423 if `KILL_SWITCH` exists or `PLACE_PAYLOAD_CONFIRMED` is false.
4. Only then call `Client.place(..., dry_run=False)` for the first leg.

`authorize` and `flatten` each need their own confirmation. None of them is
triggered by another endpoint.

### Kill switch

**Status: implemented.** `POST /api/kill-switch/engage` and `/release` are dashboard
actions (origin + action token). Release needs the typed phrase and is refused while
`logs/reconciliation.json` sets `kill_switch_required`. Both write `logs/audit.jsonl`.
`bot.py` checks the switch every tick and before every leg, and engages it itself after
`UNKNOWN`, `LEGGED` or `IMBALANCED` in live mode. `GET /api/execution` serves the bot
heartbeat and execution tape; `GET /api/orders/preview` returns the exact dry-run bodies.
The remaining Phase 2–4 ticket endpoints are not built; `bot.py` is the executor.

- `POST /api/kill-switch/engage`: create `logs/KILL_SWITCH` atomically with
  the reason and actor, then request cancels for open orders. It is always
  allowed, idempotent, and needs no confirmation.
- `POST /api/kill-switch/release`: requires all non-mode gates green, a
  clean reconciliation and typed confirmation. It deletes the file and
  writes an audit line.

`bot.py` does not check `logs/KILL_SWITCH` today. `paper.py` does. Add the
check at the top of every `bot.py` loop iteration, and before each
`execute()`, before any live use.

## 6. Risk limits

Move limits into `config/risk_limits.json`, loaded by `gates.py` and by
`bot.py`:

```json
{ "per_trade_capital": 1000, "daily_loss": 500,
  "venue_exposure": {"sig": 5000, "kalshi": 2000, "polymarket": 2000},
  "event_exposure": 1500, "max_quote_age_s": 5.0, "max_slippage": 0.01,
  "max_residual_contracts": 50, "min_net_edge": 0.005,
  "manual_approval": true, "auto_hedge": false }
```

- `bot.py --max-gross`, `--max-per-race` and `--cooldown` become overrides
  that can only tighten these limits.
- `PUT /api/risk/limits` needs the action token and confirmation. It applies
  tightening immediately and writes every change to the audit log.
- `auto_hedge` is not editable from the UI.

## 7. Frontend rules

These come from the design and apply everywhere.

- **Server decides.** The browser renders server state. It never computes
  `exec_ready`, never enables a live control on its own, and treats any gate
  field it does not recognise as failing.
- **Colour.** Green only for verified passes and the fully armed live
  button. Amber for research, paper, shadow and review. Red for blocked,
  stale, rejected or dangerous.
- **Stale data.** Stale rows stay visible with their age in red and a
  reason. "Hide blocked" is off by default.
- **Missing data.** Missing depth shows as `—` or `no depth`, never
  estimated. Missing fees show as `UNKNOWN`, never `$0`.
- **Settlement.** Differences are shown per attribute. Nothing defaults to
  "match".
- **Money.** Prices and money are formatted from decimal strings sent by the
  API. Internally the engine uses floats; round at the API boundary to the
  venue tick (0.001 for SIG) and send strings.
- **Refresh.** Poll `/api/status` every second. Poll opportunities every 5
  seconds, or at the `/api/signals` cadence. A WebSocket can replace polling
  later; the stdlib server does not provide one.
- **Errors.** Keep the existing behaviour: show the last good data, marked
  stale, and a banner naming the failed source.

## 8. Tests to add

Follow the existing `unittest` style (see `test_dashboard.py`) and run with
`python -m unittest`.

- **`test_gates.py`**
  - Each of the 13 gates fails independently.
  - Unknown fees fail.
  - Stale quotes fail.
  - BUY_ALL on a race not in `exhaustive.txt` is blocked.
  - An unresolved `WORDING_REGISTER` case blocks settlement.
  - `gate_hash` changes when any gate changes.
- **`test_tickets.py`**
  - The dry-run body equals `Client.place(dry_run=True)["body"]`.
  - Fixture data cannot be confirmed.
  - An expired quote returns 409.
  - Gate drift returns 409.
  - `KILL_SWITCH` returns 423.
  - `PLACE_PAYLOAD_CONFIRMED = False` returns 423.
  - A partial fill resizes the later legs and reports the residual.
  - Leg 2 and flatten are never sent without an authorize call.
- **`test_dashboard.py`**
  - New endpoints return the documented shapes.
  - Mutating endpoints reject a missing token or a foreign `Origin`.
  - `/api/browser_snapshot` still accepts the SIG origin.
- **`test_reconciliation.py`**
  - An unmatched fill blocks `recon_clean`.
  - A mismatch engages the kill switch.

## 9. Order of work

1. Security changes (section 3).
2. `gates.py` and `/api/status`, with the header and the gate panel.
3. `/api/opportunities`, the table and the drawer.
4. `tickets.py`: dry-run, paper and shadow tickets, plus the Execution screen.
5. Reconciliation and the Account screen; kill switch endpoints; the
   `bot.py` kill-switch check.
6. `config/risk_limits.json` and the Risk screen.
7. Capture one real SIG order in DevTools, match `Client.place()` to it, and
   only then flip `PLACE_PAYLOAD_CONFIRMED`. Then build Phase 4, behind all
   13 gates.
