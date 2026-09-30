"""
signals.py — local signal generator for SIG Super.Market complement arbs.
Produces *manual order tickets*; never sends orders.

    python signals.py                       # one scan, print signals + near-misses
    python signals.py --watch 5             # rescan every 5s, alert on new/changed signals
    python signals.py --watch 5 --bell      # ...with terminal bell
    python signals.py --dump snap.json      # save raw books for later replay
    python signals.py --replay snap.json    # run offline on a saved snapshot
    python signals.py --min-edge 0.005 --min-pnl 5 --budget 20000 --near 10

Every run appends signals to logs/signals.csv (for later P&L / fill analysis).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from arb_engine import (ArbResult, Book, TITLE_RE, breakeven_limit,
                        group_markets, max_executable_arb, scan)

HERE = pathlib.Path(__file__).parent
SITE = "https://sig.thesuper.market/markets/"
PARTY = {"R": "Republican", "D": "Democratic", "I": "Independent"}

# Races where you are confident the listed parties cover every possible winner.
# BUY_ALL (sum of asks < 1) is only signalled for these. One race per line.
EXHAUSTIVE_FILE = HERE / "exhaustive.txt"
NEVER_EXHAUSTIVE = {"Idaho Senate", "South Dakota Senate"}


def load_exhaustive() -> set:
    if not EXHAUSTIVE_FILE.exists():
        return set()
    s = {l.strip() for l in EXHAUSTIVE_FILE.read_text().splitlines()
         if l.strip() and not l.startswith("#")}
    return s - NEVER_EXHAUSTIVE


# -------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    ts: str
    markets: List[dict]                 # [{id,title}]
    levels: Dict[int, List[dict]]       # market_id -> API levels

    @classmethod
    def fetch(cls, client, markets: List[dict]) -> "Snapshot":
        ids = [m["id"] for m in markets]
        levels = client.all_levels(ids)
        return cls(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   [{"id": m["id"], "title": m["title"]} for m in markets], levels)

    def dump(self, path):
        pathlib.Path(path).write_text(json.dumps(
            {"ts": self.ts, "markets": self.markets,
             "levels": {str(k): v for k, v in self.levels.items()}}, indent=1))

    @classmethod
    def load(cls, path) -> "Snapshot":
        j = json.loads(pathlib.Path(path).read_text())
        return cls(j["ts"], j["markets"], {int(k): v for k, v in j["levels"].items()})

    def books(self) -> Dict[int, Book]:
        return {i: Book.from_levels(i, L) for i, L in self.levels.items()}


@dataclass
class ScanReport:
    """Read-only scan output, including rejected opportunities."""
    opportunities: List[ArbResult]
    diagnostics: List[dict]
    liquidity: List[dict]


# --------------------------------------------------------------- signals
def leg_label(mid: int, titles: Dict[int, str]) -> str:
    m = TITLE_RE.match(titles.get(mid, ""))
    return f"{PARTY[m.group(1)[0]]:<11}" if m else str(mid)


def near_misses(groups, books: Dict[int, Book], exhaustive: set, n: int) -> List[dict]:
    """Top-of-book edge for every race (negative = distance to an arb)."""
    rows = []
    for race, legs in groups.items():
        bks = [books[m] for m in legs.values() if m in books]
        if len(bks) != len(legs):
            continue
        bids = [b.best_bid() for b in bks]
        asks = [b.best_ask() for b in bks]
        # Missing liquidity is unavailable, not a zero/one price. Each
        # direction only depends on its own required side.
        if all(p is not None for p in bids):
            sb = sum(bids)
            rows.append({"race": race, "dir": "SELL_ALL", "edge": sb - 1, "sum": sb})
        if race in exhaustive:
            if all(p is not None for p in asks):
                sa = sum(asks)
                rows.append({"race": race, "dir": "BUY_ALL", "edge": 1 - sa, "sum": sa})
    rows.sort(key=lambda r: -r["edge"])
    return [r for r in rows if r["edge"] <= 0][:n]


def scan_diagnostics(snapshot: Snapshot, exhaustive: set, *, min_edge: float = 0.0,
                     fee_per_share: float = 0.0, cash: Optional[float] = None,
                     max_qty: Optional[float] = None) -> ScanReport:
    """Scan one snapshot and retain machine-readable reasons for rejection."""
    groups = group_markets(snapshot.markets)
    books = snapshot.books()
    opportunities = scan(
        groups, books, exhaustive, min_edge=min_edge,
        fee_per_share=fee_per_share, cash=cash, max_qty=max_qty,
    )
    diagnostics = []
    liquidity = []
    for market in snapshot.markets:
        mid = market["id"]
        book = books.get(mid)
        liquidity.append({
            "market_id": mid,
            "has_book": mid in snapshot.levels,
            "bid": book.best_bid() if book else None,
            "ask": book.best_ask() if book else None,
            "bid_available": bool(book and book.bids),
            "ask_available": bool(book and book.asks),
        })

    for race, legs in groups.items():
        market_ids = list(legs.values())
        for direction in ("SELL_ALL", "BUY_ALL"):
            required = "bid" if direction == "SELL_ALL" else "ask"
            record = {
                "race": race, "direction": direction, "market_ids": market_ids,
                "status": "REJECTED", "reasons": [], "missing_market_ids": [],
                "required_side": required, "top_edge": None, "top_price_sum": None,
                "quantity": 0, "depth_adjusted_pnl": None,
            }
            if len(market_ids) < 2:
                record["reasons"] = ["INSUFFICIENT_LEGS"]
                diagnostics.append(record)
                continue
            if direction == "BUY_ALL" and race not in exhaustive:
                record["status"] = "RULE_BLOCKED"
                record["reasons"] = ["NOT_EXHAUSTIVE"]
                diagnostics.append(record)
                continue
            missing_books = [mid for mid in market_ids if mid not in books]
            if missing_books:
                record["missing_market_ids"] = missing_books
                record["reasons"] = ["MISSING_BOOK"]
                diagnostics.append(record)
                continue
            missing_side = [mid for mid in market_ids
                            if not getattr(books[mid], required + "s")]
            if missing_side:
                record["missing_market_ids"] = missing_side
                record["reasons"] = ["MISSING_BID" if required == "bid" else "MISSING_ASK"]
                diagnostics.append(record)
                continue
            prices = [getattr(books[mid], "best_" + required)() for mid in market_ids]
            top_sum = sum(prices)
            edge = ((top_sum - 1.0) if direction == "SELL_ALL" else (1.0 - top_sum)) - len(market_ids) * fee_per_share
            record["top_edge"] = edge
            record["top_price_sum"] = top_sum
            if edge <= 0:
                record["reasons"] = ["NON_POSITIVE_EDGE"]
                diagnostics.append(record)
                continue
            if edge < min_edge:
                record["reasons"] = ["BELOW_MIN_EDGE"]
                diagnostics.append(record)
                continue
            result = max_executable_arb(
                race, [books[mid] for mid in market_ids], direction,
                min_edge=min_edge, fee_per_share=fee_per_share,
                cash=cash, max_qty=max_qty,
            )
            if result is None:
                record["reasons"] = ["NO_DEPTH_AT_LIMITS"]
                diagnostics.append(record)
                continue
            record.update(status="ELIGIBLE", quantity=result.qty,
                          depth_adjusted_pnl=result.pnl)
            diagnostics.append(record)
    return ScanReport(opportunities, diagnostics, liquidity)


def tickets(r: ArbResult, titles: Dict[int, str], fee: float = 0.0) -> List[str]:
    """Human order tickets, thinnest leg first, with UI-equivalent wording."""
    order = sorted(r.legs, key=lambda l: min(q for _, q in l.fills))
    out = []
    for k, l in enumerate(order, 1):
        name = leg_label(l.market_id, titles)
        if r.direction == "SELL_ALL":
            ui = (f"BUY NO  limit {1 - l.limit:.3f}  (avg {1 - l.vwap:.4f})"
                  f"   [= SELL YES >= {l.limit:.3f}]")
        else:
            ui = f"BUY YES limit {l.limit:.3f}  (avg {l.vwap:.4f})"
        out.append(f"   {k}. {name} #{l.market_id:<4} {ui}  x {l.qty:g}   {SITE}{l.market_id}")
    if len(order) >= 2:
        first = [l.vwap for l in order[:-1]]
        be = breakeven_limit(r.direction, first, fee, len(order))
        last = order[-1]
        if r.direction == "SELL_ALL":
            out.append(f"      last leg break-even: BUY NO <= {1 - be:.3f} on #{last.market_id} "
                       f"(if earlier legs fill at their avg)")
        else:
            out.append(f"      last leg break-even: BUY YES <= {be:.3f} on #{last.market_id}")
    return out


def render(snap: Snapshot, sigs: List[ArbResult], near: List[dict], titles,
           budget: Optional[float]) -> str:
    L = [f"\n=== {snap.ts}  |  {len(snap.markets)} markets  |  "
         f"{len(sigs)} signal(s)" + (f"  |  budget {budget:,.0f}" if budget else "") + " ==="]
    if sigs:
        L.append(f"{'#':>2} {'race':<26} {'dir':<8} {'qty':>6} {'pnl':>8} {'capital':>9} "
                 f"{'roi':>6}  edge top/avg/marg")
        for i, r in enumerate(sigs, 1):
            L.append(f"{i:>2} {r.race:<26} {r.direction:<8} {r.qty:>6g} {r.pnl:>8.2f} "
                     f"{r.capital:>9.2f} {r.roi:>6.2%}  "
                     f"{r.top_edge:.3f}/{r.avg_edge:.3f}/{r.marginal_edge:.3f}")
            L += tickets(r, titles)
    else:
        L.append("   no executable arbs above thresholds")
    if near:
        L.append("   near misses (top-of-book edge; 0 = arb):")
        for r in near:
            L.append(f"     {r['race']:<26} {r['dir']:<8} sum={r['sum']:.3f}  edge={r['edge']:+.3f}")
    return "\n".join(L)


def sig_key(r: ArbResult) -> tuple:
    return (r.race, r.direction, r.qty, tuple(round(l.limit, 4) for l in r.legs))


def log_csv(snap: Snapshot, sigs: List[ArbResult], path=HERE / "logs" / "signals.csv"):
    path.parent.mkdir(exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "race", "dir", "qty", "pnl", "capital", "roi", "top_edge",
                        "avg_edge", "marg_edge", "legs"])
        for r in sigs:
            w.writerow([snap.ts, r.race, r.direction, r.qty, round(r.pnl, 4),
                        round(r.capital, 4), round(r.roi, 6), round(r.top_edge, 4),
                        round(r.avg_edge, 4), round(r.marginal_edge, 4),
                        ";".join(f"{l.market_id}:{l.qty:g}@{l.vwap:.4f}/{l.limit:.3f}"
                                 for l in r.legs)])


def generate(snap: Snapshot, a, exhaustive: set, budget: Optional[float]):
    titles = {m["id"]: m["title"] for m in snap.markets}
    groups = group_markets(snap.markets)
    books = snap.books()
    kw = dict(min_edge=a.min_edge, fee_per_share=a.fee)
    if a.max_qty:
        kw["max_qty"] = a.max_qty
    if budget:
        kw["cash"] = min(budget, a.max_per_race) if a.max_per_race else budget
    sigs = [s for s in scan(groups, books, exhaustive, **kw) if s.pnl >= a.min_pnl]
    return sigs, near_misses(groups, books, exhaustive, a.near), titles


# ------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--watch", type=float, metavar="SEC", help="rescan every SEC seconds")
    ap.add_argument("--bell", action="store_true", help="terminal bell on new signal")
    ap.add_argument("--dump", metavar="FILE", help="save raw snapshot to FILE")
    ap.add_argument("--replay", metavar="FILE", help="run on a saved snapshot (offline)")
    ap.add_argument("--min-edge", type=float, default=0.0, help="marginal edge floor (default 0)")
    ap.add_argument("--min-pnl", type=float, default=1.0, help="min locked PnL (SUSQies)")
    ap.add_argument("--fee", type=float, default=0.0, help="fee per share per leg")
    ap.add_argument("--budget", type=float, help="cap capital per signal (default: live balance)")
    ap.add_argument("--max-per-race", type=float, default=None)
    ap.add_argument("--max-qty", type=float, default=None)
    ap.add_argument("--near", type=int, default=8, help="show N closest non-arbs")
    ap.add_argument("--refresh-universe", type=float, default=600, help="sec between market-list refresh")
    a = ap.parse_args(argv)
    exhaustive = load_exhaustive()

    if a.replay:
        snap = Snapshot.load(a.replay)
        sigs, near, titles = generate(snap, a, exhaustive, a.budget)
        print(render(snap, sigs, near, titles, a.budget))
        return

    from sig_client import Client
    cli = Client()
    markets, last_uni = cli.markets(), time.time()
    budget = a.budget or cli.balance()
    print(f"universe: {len(markets)} markets, {len(group_markets(markets))} races, "
          f"exhaustive list: {len(exhaustive)}, budget: {budget}")

    seen: set = set()
    while True:
        t0 = time.time()
        try:
            if time.time() - last_uni > a.refresh_universe:
                markets, last_uni = cli.markets(), time.time()
            snap = Snapshot.fetch(cli, markets)
            if a.dump:
                snap.dump(a.dump)
            sigs, near, titles = generate(snap, a, exhaustive, budget)
            keys = {sig_key(s) for s in sigs}
            fresh = keys - seen
            if not a.watch or fresh or (seen and keys != seen):
                print(render(snap, sigs, near if not a.watch or not sigs else [], titles, budget))
                if fresh and a.bell:
                    print("\a", end="")
                log_csv(snap, [s for s in sigs if sig_key(s) in fresh])
            else:
                print(f"\r{snap.ts}  no change  ({len(sigs)} live, scan {time.time()-t0:.1f}s)  ",
                      end="", flush=True)
            seen = keys
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n[{dt.datetime.now():%H:%M:%S}] scan error: {e}", file=sys.stderr)
        if not a.watch:
            break
        time.sleep(max(0.0, a.watch - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
