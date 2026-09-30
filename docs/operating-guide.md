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
```

The dashboard shows SIG signals, venue coverage, mapped/approved counts,
movement candidates, and relative-value diagnostics. `/api/crossvenue` exposes
the raw public inventory for inspection. Public venue depth is explicitly
labelled in the response: Kalshi may provide top-of-book sizes, while
Polymarket Gamma observations are indicative unless a CLOB book is present.
No cross-venue row is execution-ready by default.

## During the operating window

- Treat the dashboard as an evidence surface, not an execution authorization.
- Review stale quotes, missing bids/asks, fee assumptions, and mapping status
  before considering any candidate.
- Review related news for material events. News can pause a race, but sentiment
  is context rather than a substitute for contract rules or executable depth.
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
