# Operating Guide

## Daily startup

1. Confirm the SIG session, venue API reachability, system clock, and current
   operating window.
2. Refresh the market-link index and inspect changes in `docs/market-links.md`.
3. Check that approved mappings have current review evidence. Do not promote
   discovery rows automatically.
4. Start the dashboard and the read-only minute scanner:

```bash
python3 dashboard.py --port 8765
SIG_COOKIE='...' PYTHONPATH=. python3 crossvenue.py live --interval 60
PYTHONPATH=. python3 system_check.py
```

`system_check.py` is a read-only readiness audit. Add `--sig-snapshot
logs/browser_snapshot.json --public` to verify current SIG freshness and venue
coverage; paper readiness remains false until those inputs pass. It reports
paper readiness separately from the blocked live-execution gate.

The dashboard shows SIG signals, venue coverage, mapped/approved counts,
movement candidates, and relative-value diagnostics. `/api/crossvenue` exposes
the raw public inventory for inspection. Public venue depth is explicitly
labelled in the response: the adapter fetches books for all returned Kalshi and
Polymarket markets by default and falls back to indicative observations when a
book is unavailable. The dashboard requests all active inventory by default;
bounded overrides are labelled in the payload. No cross-venue row is
execution-ready by default.

When the Python SIG client cannot read the signed-in session, use the browser
relay described in `README.md`. The dashboard writes each validated relay
payload atomically to `logs/browser_snapshot.json`. Point both read-only
workers at that file; they enforce a 30-second freshness limit:

```bash
PYTHONPATH=. python3 crossvenue.py live \
  --sig-snapshot logs/browser_snapshot.json --interval 60
PYTHONPATH=. python3 paper.py \
  --browser-snapshot logs/browser_snapshot.json --interval 60
```

If the browser tab stops refreshing, both workers report
`STALE_SIG_SNAPSHOT` and remain at zero orders. The relay snapshot is a shared
research input, not an execution credential.

For a no-order paper/shadow run, use the dedicated runner. It journals every
simulated fill and residual reconciliation, applies the default 5% ROI hurdle,
and stops immediately when `logs/KILL_SWITCH` exists:

```bash
SIG_COOKIE='...' PYTHONPATH=. python3 paper.py --interval 60
PYTHONPATH=. python3 paper.py --replay fixtures/sample_snapshot.json --once
touch logs/KILL_SWITCH   # stop new paper cycles
rm logs/KILL_SWITCH       # resume after review
```

Paper output is written to `logs/paper-runs.jsonl`. `orders_sent` must remain
zero in this stage.

## During the operating window

- Treat the dashboard as an evidence surface, not an execution authorization.
- Review stale quotes, missing bids/asks, fee assumptions, and mapping status
  before considering any candidate.
- Review related news for material events. News can pause a race, but sentiment
  is context rather than a substitute for contract rules or executable depth.
- Record material events in `config/news_circuit_breakers.json` with affected
  SIG market IDs, a reason, source, and expiry. Active records appear in
  opportunity `execution_risk` and block execution readiness; expired or
  cleared records do not. The system does not infer sentiment automatically.
- Keep a written decision record for every approved mapping, rejected signal,
  manual punt, and execution exception.
- Run paper/shadow mode before any human-confirmed orders. Use `bot.py` only
  within the execution stage authorized by `docs/systematic-execution-plan.md`.

## Alerts and kill switches

Pause new activity on stale or skewed timestamps, venue/API errors, missing
legs, changed settlement language, abnormal spreads, partial-fill residuals,
unreconciled holdings, news circuit-breaker events, or any breached risk cap.
The safe response is to preserve the evidence, cancel only orders known to be
safe to cancel, flatten residual exposure under operator review, and resume
only after the cause is documented.

## Election night and resolution

Keep the system running after November 3. Election-night calls, reported
results, certification, and each venue's contractual resolution may be
different events. Do not close the research or reconciliation process merely
because a media outlet calls a race. Record venue-specific settlement times,
final holdings, fees, payouts, and any disputes through final resolution.

## End-of-day checklist

- Save the scan output and inspect the history-point count.
- Reconcile fills, open orders, holdings, cash, and residual exposure.
- Record candidates by source: SIG arb, Kalshi movement, Polymarket movement,
  relative value, news context, and rejected-gate reason.
- Review changes to market rules, links, mappings, and venue availability.
- Leave production execution disabled unless the current stage gates are
  explicitly satisfied and an operator has acknowledged the run.
