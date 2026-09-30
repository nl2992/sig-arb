# Systematic Execution Plan

This plan turns the read-only research pipeline into a controlled execution
program. The default state is research-only. A mathematical opportunity is not
an order instruction until the contract mapping, settlement rules, executable
depth, fees, and execution interface have all passed review.

## Stage 0: Data and contract readiness

- Refresh the 237-instrument SIG universe and all SIG order books.
- Refresh public Kalshi and Polymarket inventories and prices.
- Keep native venue IDs, event IDs, outcome IDs, timestamps, rules text, and
  source URLs with every observation.
- Reconcile market counts, missing bids/asks, stale quotes, and duplicate IDs.
- Keep discovered mappings in `REVIEW_REQUIRED`; only manually reviewed
  mappings may enter the approved registry.

Exit gate: two consecutive clean scans with complete timestamps, no invalid
prices, and an explicit explanation for every missing-liquidity field.

## Stage 1: Paper and shadow trading

- Run `crossvenue.py live --interval 60` and the dashboard continuously.
- Run SIG arbitrage signals with the 5% net ROI hurdle.
- Record hypothetical entry, every required leg, available depth, fees,
  timestamp, quote age, and the reason a candidate was rejected.
- For cross-venue candidates, record movement, executable SIG-side gap, and
  relative-value z-score separately.
- Never call an order endpoint in this stage.

Exit gate: at least one full operating window of stable scans, replayed fills,
partial-fill tests, stale-data tests, and zero unexplained reconciliation
differences.

## Stage 2: Human-confirmed execution

- Enable only one explicitly approved venue and a small fixed gross cap.
- Require a fresh quote-age check immediately before each leg.
- Execute the thinnest or most failure-sensitive leg first, then resize later
  legs to actual fills.
- Cancel or reprice only within the configured break-even limit.
- Write every quote, order, fill, cancel, residual, and flattening action to
  the execution journal.
- A cross-venue punt remains research-only until both contracts are approved;
  movement alone never authorizes an order.

Exit gate: reviewed fills, fees, slippage, residual exposure, and settlement
outcomes agree with the journal across a representative sample.

## Stage 3: Constrained automation

Automation requires all Stage 2 gates plus authenticated order-payload
verification, idempotency, position reconciliation, and an operator-visible
kill switch. Start with one market family, low notional, and a hard daily loss
limit. The system must fail closed on stale data, missing legs, changed rules,
mapping revocation, venue errors, clock skew, or a reconciliation mismatch.

## Opportunity decision

An opportunity is actionable only when all of these are true:

1. The mapping is approved and settlement semantics are compatible.
2. Every required leg has current executable liquidity.
3. Quote age and source timestamps are within configured limits.
4. Fees, expected slippage, and capital usage leave at least 5% net ROI for
   the SIG hurdle, or the separate cross-venue research threshold is met.
5. Position, gross, per-race, daily-loss, and venue exposure limits pass.
6. No news circuit breaker, resolution uncertainty, or kill switch is active.

The dashboard may show candidates that fail one or more gates, but it must label
the failing gate and must not present them as executable orders.

## Reconciliation and shutdown

At every cycle reconcile local intended positions against venue holdings and
open orders. On mismatch: stop new orders, cancel where safe, record the
residual, and require operator review. After the election, keep monitoring
through certification and contractual resolution, then reconcile final
settlements and capital separately from election-night prices.

The normalized reconciliation contract is implemented in `reconciliation.py`.
It compares positions, open orders, and cash independently and requests the
kill switch whenever any component is outside tolerance.
