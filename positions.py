"""
positions.py — account, P&L and risk by strategy and by race, for the dashboard.

    python positions.py            # print the current report (reads SIG + reference prices)

Each holding is split between strategies using their ledgers (fv, ll, mm); the remainder
is arb (complete sets). For every race it computes the P&L under each possible winner, so
the report shows a worst case per race and two stress tests (every race to the Democrats /
to the Republicans). Values:
  mark        SIG's own valuation (currentPrice), what the site shows
  settle EV   expected value at settlement using the Kalshi/Polymarket fair price
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
from typing import Callable, Dict, Optional

from arb_engine import TITLE_RE

ROOT = pathlib.Path(__file__).parent
REPORT = ROOT / "logs" / "positions.json"
START_BALANCE = 100_000.0


def _side_value(q: float, p_yes: float) -> float:
    """Value per share of a signed YES position at YES price p_yes."""
    return p_yes if q > 0 else 1 - p_yes


def arb_costs(exec_log: pathlib.Path = None, manual_log: pathlib.Path = None) -> Dict[int, list]:
    """Per market [signed YES qty, cost] of arb legs from the arb's own fills. The account's
    average price is for the NET side, which differs from an arb leg wherever another
    strategy holds the opposite side (e.g. Nebraska #281: net long YES, arb leg NO)."""
    import holdings_check as hc
    out: Dict[int, list] = {}

    def add(m, q, yes_px):
        row = out.setdefault(int(m), [0.0, 0.0])
        if row[0] and (row[0] > 0) != (q > 0):           # a reduction: release cost pro rata
            closed = min(abs(q), abs(row[0]))
            row[1] -= row[1] * closed / abs(row[0])
            row[0] += closed if q > 0 else -closed
            q = q + closed if q < 0 else q - closed
        if q:
            row[1] += abs(q) * (yes_px if q > 0 else 1 - yes_px)
            row[0] += q
    for r in hc._rows(exec_log or hc.EXEC_LOG):
        res = r.get("result") or {}
        if not r.get("live") or res.get("strategy"):
            continue
        for leg in res.get("legs", []):
            if leg.get("dryRun") or leg.get("repair") or not leg.get("filled"):
                continue
            q = leg["filled"] if leg["side"] == "BUY" else -leg["filled"]
            add(leg["market"], q, float(leg.get("avg") or leg.get("limit")))
    for r in hc._rows(manual_log or hc.MANUAL_LOG):
        if r.get("action") == "journal_backfill":
            add(r["market_id"], float(r["qty"]), float(r.get("price_yes") or 0.5))
        elif r.get("action") == "close" and r.get("status") == "SENT":
            add(r["market_id"], float(r["after"]) - float(r["before"]), float(r.get("limit") or 0.5))
    return out


def build(portfolio: dict, ledgers: Dict[str, object], fair_fn: Callable[[int], Optional[float]],
          arb_cost_fn: Optional[Callable[[], Dict[int, list]]] = None) -> dict:
    hold = {int(h["marketId"]): h for h in portfolio.get("holdings", [])
            if str(h.get("settlementOption", "YES")).upper() == "YES" and float(h.get("quantity") or 0)}
    cash = float(portfolio.get("cashBalance") or 0)
    # Value every market from its NET holding (SIG nets YES against NO in one market, so an
    # fv YES buy against an arb NO leg is a partial unwind, not a second position), then
    # attribute the net to the strategies holding the net side, pro rata to their size.
    parts = []                                   # (strategy, market, signed qty, cost)
    netted = []
    for m, h in hold.items():
        q_net, avg = float(h["quantity"]), float(h.get("averagePricePaid") or 0)
        cost_net = abs(q_net) * avg
        by = {name: led.position(m) for name, led in ledgers.items() if led.position(m)}
        rest = q_net - sum(by.values())
        if abs(rest) > 0.5:
            by["arb"] = rest
        same = {k: v for k, v in by.items() if (v > 0) == (q_net > 0)}
        for k, v in by.items():
            if k not in same:
                netted.append({"strategy": k, "market_id": m, "qty": round(v, 2)})
        total = sum(abs(v) for v in same.values()) or 1.0
        for k, v in same.items():
            share = abs(v) / total
            parts.append((k, m, q_net * share, cost_net * share))

    races: Dict[str, dict] = {}
    strat = collections.defaultdict(lambda: {"markets": 0, "cost": 0.0, "mark": 0.0, "ev": 0.0})
    rows = []
    for name, m, q, cost in parts:
        h = hold[m]
        title = h.get("title", "")
        mt = TITLE_RE.match(title)
        party, race = (mt.group(1), mt.group(2)) if mt else ("?", title)
        cp = h.get("currentPrice")
        fair = fair_fn(m)
        mark = abs(q) * _side_value(q, cp) if cp is not None else cost
        ev_price = fair if fair is not None else cp
        ev = abs(q) * _side_value(q, ev_price) - cost if ev_price is not None else 0.0
        s = strat[name]
        s["markets"] += 1; s["cost"] += cost; s["mark"] += mark; s["ev"] += ev
        rows.append({"strategy": name, "market_id": m, "race": race, "party": party, "side": "YES" if q > 0 else "NO",
                     "qty": abs(q), "avg": round(cost / abs(q), 4) if q else None,
                     "mark": round(cp, 4) if cp is not None else None, "fair": None if fair is None else round(fair, 4),
                     "cost": round(cost, 2), "mark_pnl": round(mark - cost, 2), "ev": round(ev, 2)})
        r = races.setdefault(race, {"race": race, "cost": 0.0, "positions": [], "parties": set()})
        r["cost"] += cost
        r["positions"].append((party, q, cost))
        r["parties"].add(party)

    # P&L of each race under each winner (a party outside the listed ones counts as "other")
    def race_pnl(r, winner):
        return sum(abs(q) * (1.0 if (q > 0) == (p == winner) else 0.0) - c for p, q, c in r["positions"])
    race_rows, dem_sweep, rep_sweep, worst_total = [], 0.0, 0.0, 0.0
    for r in races.values():
        outcomes = {w: race_pnl(r, w) for w in ("Democratic", "Republican", "Independent", "other")}
        worst = min(outcomes.values())
        worst_total += worst
        dem_sweep += outcomes["Democratic"]
        rep_sweep += outcomes["Republican"]
        race_rows.append({"race": r["race"], "cost": round(r["cost"], 2),
                          "if_dem": round(outcomes["Democratic"], 2), "if_rep": round(outcomes["Republican"], 2),
                          "worst": round(worst, 2), "best": round(max(outcomes.values()), 2),
                          "hedged": worst >= -0.01 * r["cost"]})
    race_rows.sort(key=lambda x: x["worst"])

    # Settlement value per strategy on its OWN (gross) positions: payoffs are linear, so
    # gross attribution is exact even where SIG nets one strategy's YES against another's NO.
    # Arbs count their locked complete-set value; the others their value at fair.
    for k in strat:
        strat[k]["ev"] = 0.0
    arb_journal = (arb_cost_fn or arb_costs)()
    arb_by_race = collections.defaultdict(list)
    for m, h in hold.items():
        q_net, avg = float(h["quantity"]), float(h.get("averagePricePaid") or 0)
        mt = TITLE_RE.match(h.get("title", ""))
        party, race = (mt.group(1), mt.group(2)) if mt else ("?", h.get("title", ""))
        fair = fair_fn(m)
        price = fair if fair is not None else h.get("currentPrice")
        rest = q_net
        for name, led in ledgers.items():
            q = led.position(m)
            if not q:
                continue
            rest -= q
            if price is not None:
                strat[name]["ev"] += abs(q) * _side_value(q, price) - led.capital(m)
        if abs(rest) > 0.5:
            journal = arb_journal.get(m)
            cost = (journal[1] * abs(rest) / abs(journal[0])
                    if journal and abs(journal[0]) > 0.5 and (journal[0] > 0) == (rest > 0) else abs(rest) * avg)
            arb_by_race[race].append((party, rest, cost))
    if arb_by_race:
        strat["arb"]["ev"] = sum(min(race_pnl({"positions": legs}, w)
                                     for w in ("Democratic", "Republican", "Independent", "other"))
                                 for legs in arb_by_race.values())

    open_cost = sum(s["cost"] for s in strat.values())
    mark_value = sum(s["mark"] for s in strat.values())
    realized = cash + sum(abs(float(h["quantity"])) * float(h.get("averagePricePaid") or 0)
                          for h in hold.values()) - START_BALANCE
    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "account": {"start": START_BALANCE, "cash": round(cash, 2), "open_cost": round(open_cost, 2),
                    "mark_value": round(mark_value, 2), "account_value": round(cash + mark_value, 2),
                    "pnl_vs_start": round(cash + mark_value - START_BALANCE, 2), "realized": round(realized, 2),
                    "settle_ev": round(sum(s["ev"] for s in strat.values()), 2),
                    "daily_pnl": (portfolio.get("dailyPnL") or {}).get("value")},
        "strategies": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                       for k, v in sorted(strat.items())},
        "risk": {"if_democrats_sweep": round(dem_sweep, 2), "if_republicans_sweep": round(rep_sweep, 2),
                 "sum_of_race_worst_cases": round(worst_total, 2), "races": len(race_rows),
                 "unhedged_races": sum(1 for r in race_rows if not r["hedged"])},
        "races": race_rows,
        "positions": sorted(rows, key=lambda x: -x["cost"]),
        "netted": netted,
        "open_orders": len(portfolio.get("openOrders", [])),
    }


def write(report: dict, path: pathlib.Path = REPORT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report))
    tmp.replace(path)


if __name__ == "__main__":
    import fair_value
    import sig_client
    c = sig_client.Client(timeout=60)
    refs = fair_value.ReferencePrices()
    refs.refresh()
    leds = {"fv": fair_value.Ledger(), "ll": fair_value.Ledger(ROOT / "logs" / "ll_positions.json"),
            "mm": fair_value.Ledger(ROOT / "logs" / "mm_positions.json")}
    rep = build(c.portfolio(), leds, lambda m: (refs.fair(m) or {}).get("fair"))
    write(rep)
    print(json.dumps({k: rep[k] for k in ("account", "strategies", "risk")}, indent=1))
    print("\nlargest race risks (worst case):")
    for r in rep["races"][:12]:
        print(f"  {r['race']:<28} cost {r['cost']:>8,.0f}  if D {r['if_dem']:>+9,.0f}  if R {r['if_rep']:>+9,.0f}  worst {r['worst']:>+9,.0f}")
