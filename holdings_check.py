"""
holdings_check.py — make sure the account holds exactly what the bot thinks it holds.

Two pieces:

* Order intents (logs/order_intents.jsonl). Every live order is written as SENT *before*
  it goes to SIG and resolved with its result afterwards. An intent left SENT (the
  process died mid-order) or UNKNOWN (5xx) is "in doubt".

* Reconciliation, once a minute from the account snapshot the bot already reads:
    expected = arb fills (executions.jsonl) + manual closes/backfills (manual_actions.jsonl)
               + fair-value ledger (fv_positions.json)
    actual   = /api/portfolio/page-data holdings (signed YES quantity per market)
  In-doubt intents explain differences first: a fair-value fill is added to its ledger
  (price from SIG's transaction history), an arb leg is journaled and the kill switch is
  engaged so the hedge gets reviewed. A difference nothing explains for two consecutive
  checks engages the kill switch, naming the market and both quantities.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
from typing import Callable, Dict, List, Optional

ROOT = pathlib.Path(__file__).parent
INTENT_LOG = ROOT / "logs" / "order_intents.jsonl"
EXEC_LOG = ROOT / "logs" / "executions.jsonl"
MANUAL_LOG = ROOT / "logs" / "manual_actions.jsonl"
TOLERANCE = 0.5          # shares


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _append(path: pathlib.Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _rows(path: pathlib.Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ------------------------------------------------------------------ intents
def write_intent(coid: str, strategy: str, market_id: int, yes_side: str, limit: float, qty: float,
                 path: pathlib.Path = None, resting: bool = False) -> None:
    row = {"ts": _now(), "client_order_id": coid, "state": "SENT", "strategy": strategy,
           "market_id": int(market_id), "yes_side": yes_side, "limit": limit, "qty": qty}
    if resting:
        row["resting"] = True              # meant to rest: may fill long after it was placed
    _append(path or INTENT_LOG, row)


def resolve_intent(coid: str, state: str, path: pathlib.Path = None, **fields) -> None:
    _append(path or INTENT_LOG, {"ts": _now(), "client_order_id": coid, "state": state, **fields})


def in_doubt(path: pathlib.Path = None, resting_strategies: tuple = ("mm",),
             resting_window_s: float = 6 * 3600) -> List[dict]:
    """Intents that may have filled without the bot booking it, oldest first:
    latest state SENT or UNKNOWN, or a recent resting order (strategy in
    `resting_strategies` or flagged resting, DONE with less than its quantity filled),
    since an order that rested can fill later, e.g. while a restart could not cancel it."""
    latest: Dict[str, dict] = {}
    first: Dict[str, dict] = {}
    for row in _rows(path or INTENT_LOG):
        coid = row.get("client_order_id")
        if not coid:
            continue
        first.setdefault(coid, row)
        latest[coid] = row
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for c in first:
        state = latest[c]["state"]
        if state in ("SENT", "UNKNOWN"):
            out.append({**first[c], "state": state})
        elif state == "DONE" and (first[c].get("strategy") in resting_strategies or first[c].get("resting")):
            filled = float(latest[c].get("filled") or 0)
            try:
                age = (now - dt.datetime.fromisoformat(first[c]["ts"])).total_seconds()
            except (KeyError, ValueError):
                continue
            if filled < float(first[c].get("qty", 0)) and age <= resting_window_s:
                out.append({**first[c], "state": state, "qty": float(first[c]["qty"]) - filled})
    return out


# ------------------------------------------------------------------ holdings
def actual_holdings(portfolio: dict) -> Dict[int, float]:
    out: Dict[int, float] = collections.defaultdict(float)
    for h in portfolio.get("holdings", []):
        if str(h.get("settlementOption", "YES")).upper() == "YES":
            out[int(h["marketId"])] += float(h.get("quantity") or 0)
    return dict(out)


def account_avg(portfolio: dict) -> Dict[int, tuple]:
    """market_id -> (signed YES qty, averagePricePaid in the held side's terms), as SIG computes
    it from the account's own transaction history."""
    out: Dict[int, tuple] = {}
    for h in portfolio.get("holdings", []):
        if str(h.get("settlementOption", "YES")).upper() == "YES" and h.get("quantity")                 and h.get("averagePricePaid") is not None:
            out[int(h["marketId"])] = (float(h["quantity"]), float(h["averagePricePaid"]))
    return out


def deployed(portfolio: dict) -> float:
    return sum(abs(float(h.get("quantity") or 0)) * float(h.get("averagePricePaid") or 0)
               for h in portfolio.get("holdings", []))


def expected_holdings(ledger_rows: dict, exec_log: pathlib.Path = None,
                      manual_log: pathlib.Path = None) -> Dict[int, float]:
    out: Dict[int, float] = collections.defaultdict(float)
    for r in _rows(exec_log or EXEC_LOG):
        res = r.get("result") or {}
        if not r.get("live") or res.get("strategy") == "fv":
            continue
        for leg in res.get("legs", []):
            # a repair row's fill is already folded into its leg's "filled": never count it twice
            if leg.get("dryRun") or leg.get("repair") or not leg.get("filled"):
                continue
            out[int(leg["market"])] += -leg["filled"] if leg["side"] == "SELL" else leg["filled"]
    for r in _rows(manual_log or MANUAL_LOG):
        if r.get("action") == "close" and r.get("status") == "SENT":
            out[int(r["market_id"])] += float(r["after"]) - float(r["before"])
        elif r.get("action") == "journal_backfill":
            out[int(r["market_id"])] += float(r["qty"])
    for k, row in ledger_rows.items():
        out[int(k)] += float(row.get("qty", 0))
    return dict(out)


def diffs(actual: Dict[int, float], expected: Dict[int, float]) -> Dict[int, float]:
    keys = set(actual) | set(expected)
    return {k: actual.get(k, 0.0) - expected.get(k, 0.0) for k in keys
            if abs(actual.get(k, 0.0) - expected.get(k, 0.0)) > TOLERANCE}


def fill_price(transactions: List[dict], market_id: int, signed_qty: float, since: str,
               default_yes: float) -> float:
    """YES-terms price of the earliest matching SIG trade at or after `since` (NO trades
    are reported in NO terms)."""
    for t in sorted(transactions, key=lambda t: str(t.get("createdAt", ""))):
        try:
            if int(t.get("marketId")) != market_id or t.get("event_type") != "trade":
                continue
            q = float(t.get("quantity") or 0)
            if (q > 0) != (signed_qty > 0) or str(t.get("createdAt", "")) < since[:19]:
                continue
            price = float(t["price"])
            return price if q > 0 else round(1 - price, 6)
        except (TypeError, ValueError, KeyError):
            continue
    return default_yes


class HoldingsCheck:
    def __init__(self, ledger, engage: Callable[[str], None], intent_log: pathlib.Path = None,
                 exec_log: pathlib.Path = None, manual_log: pathlib.Path = None, strikes_to_kill: int = 2):
        """`ledger` is the fair-value Ledger or a {strategy: Ledger} dict (fv, ll, mm)."""
        self.ledgers = ledger if isinstance(ledger, dict) else {"fv": ledger}
        self.ledger, self.engage = self.ledgers.get("fv"), engage
        self.intent_log, self.exec_log, self.manual_log = intent_log, exec_log, manual_log
        self.strikes_to_kill = strikes_to_kill
        self.strikes: Dict[int, int] = {}
        self.last: dict = {"ok": None}

    def rows(self) -> dict:
        """All strategy ledgers summed per market (signed YES quantity)."""
        out: Dict[int, dict] = {}
        for led in self.ledgers.values():
            for k, row in led.rows.items():
                out.setdefault(int(k), {"qty": 0.0})["qty"] += float(row.get("qty", 0))
        return out

    def check(self, portfolio: dict, transactions_fn: Optional[Callable[[], List[dict]]] = None,
              immediate: bool = False) -> dict:
        """Recover in-doubt intents, then flag unexplained differences. `immediate`
        (startup) engages the kill switch on the first unexplained difference."""
        actual = actual_holdings(portfolio)
        diff = diffs(actual, expected_holdings(self.rows(), self.exec_log, self.manual_log))
        recovered, notes = [], []
        doubt = in_doubt(self.intent_log)
        txns = transactions_fn() if (doubt and transactions_fn) else []
        for it in doubt:
            m, qty = it["market_id"], float(it["qty"])
            signed = qty if it["yes_side"] == "BUY" else -qty
            d = diff.get(m, 0.0)
            if d and (d > 0) == (signed > 0) and abs(d) <= qty + TOLERANCE:
                filled = min(abs(d), qty)
                price = fill_price(txns, m, signed, it["ts"], float(it["limit"]))
                if it["strategy"] in self.ledgers:
                    self.ledgers[it["strategy"]].record(m, it["yes_side"], filled, price)
                else:
                    _append(self.manual_log or MANUAL_LOG,
                            {"ts": _now(), "action": "journal_backfill", "market_id": m,
                             "qty": filled if signed > 0 else -filled, "price_yes": price,
                             "client_order_id": it["client_order_id"], "reason": "in-doubt arb leg"})
                    notes.append(f"arb leg {it['client_order_id']} on #{m} filled {filled:g} after a crash: "
                                 "check the race is still hedged")
                diff[m] = d - (filled if signed > 0 else -filled)
                if abs(diff[m]) <= TOLERANCE:
                    diff.pop(m)
                resolve_intent(it["client_order_id"], "RECOVERED", self.intent_log, filled=filled, price_yes=price)
                recovered.append({"market_id": m, "filled": filled, "strategy": it["strategy"]})
            elif not d and it["state"] in ("SENT", "UNKNOWN"):
                resolve_intent(it["client_order_id"], "NOT_FILLED", self.intent_log)
        for m in list(self.strikes):
            if m not in diff:
                self.strikes.pop(m)
        for m in diff:
            self.strikes[m] = self.strikes.get(m, 0) + 1
        bad = {m: d for m, d in diff.items() if immediate or self.strikes[m] >= self.strikes_to_kill}
        if bad:
            exp = expected_holdings(self.rows(), self.exec_log, self.manual_log)
            notes.append("holdings differ from bot records: " + "; ".join(
                f"#{m} actual {actual.get(m, 0):g} expected {exp.get(m, 0):g}" for m in sorted(bad)))
        if notes:
            self.engage(" | ".join(notes)[:450])
        self.last = {"ok": not diff and not notes, "checked_at": _now(), "markets": len(actual),
                     "pending_differences": {str(m): round(d, 2) for m, d in diff.items()},
                     "recovered": recovered, "alerts": notes}
        return self.last
