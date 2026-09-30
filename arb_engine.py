"""
arb_engine.py — pure, I/O-free logic for complement-set arbitrage on
SIG Super.Market (binary YES exchanges, books quoted in YES terms).

A "group" is a set of mutually-exclusive markets for the same race
(R/D pair, or R/D/I triple). Two trades exist:

  SELL_ALL : sell YES on every leg (== buy NO on every leg).
             Needs only mutual exclusivity (at most one winner).
             Marginal edge per bundle = sum(bid_i) - 1
             Capital per bundle       = sum(1 - bid_i)

  BUY_ALL  : buy YES on every leg.
             Needs exhaustivity (exactly one listed winner).
             Marginal edge per bundle = 1 - sum(ask_i)
             Capital per bundle       = sum(ask_i)

Because each ladder is sorted best-first, the marginal edge is
non-increasing as we walk deeper, so the greedy walk below gives the
*maximum executable arb*: the largest bundle size Q whose marginal
edge at every step still meets `min_edge` (after fees), subject to a
capital budget. It returns per-leg VWAP and the worst (limit) price.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Ladder = List[Tuple[float, float]]  # [(price, qty)], best first

TITLE_RE = re.compile(r"^Will the (Republican|Democratic|Independent) Party win the (.+)\?$")


# ----------------------------------------------------------------- books
@dataclass
class Book:
    market_id: int
    exchange_id: int
    bids: Ladder  # YES bids, price desc
    asks: Ladder  # YES asks, price asc

    @classmethod
    def from_levels(cls, market_id: int, levels: List[dict]) -> "Book":
        """Normalise API `levels` into YES-terms ladders.
        A NO level at p is the YES level on the opposite side at 1-p."""
        bids: Dict[float, float] = {}
        asks: Dict[float, float] = {}
        ex_id = None
        for l in levels:
            ex_id = l.get("exchangeId", ex_id)
            p, q = float(l["price"]), float(l["quantity"])
            is_buy = l["side"] == "BUY"
            if not l.get("isYes", True):          # convert NO -> YES terms
                p, is_buy = round(1 - p, 6), not is_buy
            tgt = bids if is_buy else asks
            tgt[p] = tgt.get(p, 0) + q
        return cls(
            market_id=market_id,
            exchange_id=ex_id,
            bids=sorted(bids.items(), key=lambda x: -x[0]),
            asks=sorted(asks.items(), key=lambda x: x[0]),
        )

    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None


# ------------------------------------------------------------- grouping
def group_markets(markets: List[dict]) -> Dict[str, Dict[str, int]]:
    """{race: {'R': market_id, 'D': market_id, 'I': ...}} from titles."""
    groups: Dict[str, Dict[str, int]] = {}
    for m in markets:
        mt = TITLE_RE.match(m["title"])
        if mt:
            groups.setdefault(mt.group(2), {})[mt.group(1)[0]] = m["id"]
    return groups


# ------------------------------------------------------------ the walk
@dataclass
class LegFill:
    market_id: int
    exchange_id: int
    fills: List[Tuple[float, float]] = field(default_factory=list)  # YES price, qty

    @property
    def qty(self) -> float:
        return sum(q for _, q in self.fills)

    @property
    def vwap(self) -> float:
        return sum(p * q for p, q in self.fills) / self.qty if self.qty else float("nan")

    @property
    def limit(self) -> float:
        """Worst YES price touched = the limit price to send."""
        return self.fills[-1][0] if self.fills else float("nan")


@dataclass
class ArbResult:
    race: str
    direction: str            # SELL_ALL | BUY_ALL
    qty: float                # bundles (shares per leg)
    legs: List[LegFill]
    pnl: float                # locked profit (after fees) if all legs fill
    capital: float            # cash locked
    top_edge: float           # edge of the first share
    marginal_edge: float      # edge of the last share taken
    steps: List[dict]         # the marginal edge curve (for logs / plots)

    @property
    def avg_edge(self) -> float:
        return self.pnl / self.qty if self.qty else 0.0

    @property
    def roi(self) -> float:
        return self.pnl / self.capital if self.capital else 0.0

    def summary(self) -> str:
        legs = ", ".join(
            f"{l.market_id}: {l.qty:g}@vwap {l.vwap:.4f} (limit {l.limit:.3f})" for l in self.legs
        )
        return (f"{self.race:<28} {self.direction:<8} Q={self.qty:>7g}  pnl={self.pnl:8.2f}  "
                f"cap={self.capital:9.2f}  roi={self.roi:6.2%}  edge top/avg/marg="
                f"{self.top_edge:.3f}/{self.avg_edge:.3f}/{self.marginal_edge:.3f}  [{legs}]")


def max_executable_arb(
    race: str,
    books: List[Book],
    direction: str,
    min_edge: float = 0.0,
    fee_per_share: float = 0.0,
    cash: Optional[float] = None,
    max_qty: Optional[float] = None,
    lot: float = 1.0,
) -> Optional[ArbResult]:
    """Greedy depth walk across all legs simultaneously.

    min_edge      : required marginal edge per bundle (slippage/latency buffer)
    fee_per_share : charged per share per leg (unknown on SIG -> default 0)
    cash          : capital budget; the last step is partially filled to fit
    max_qty       : hard cap on bundle count (risk limit)
    lot           : share granularity (1 = whole shares)
    """
    n = len(books)
    if direction == "SELL_ALL":
        ladders = [b.bids for b in books]
        edge_of = lambda ps: sum(ps) - 1.0 - n * fee_per_share
        cap_of = lambda ps: sum(1.0 - p for p in ps) + n * fee_per_share
    elif direction == "BUY_ALL":
        ladders = [b.asks for b in books]
        edge_of = lambda ps: 1.0 - sum(ps) - n * fee_per_share
        cap_of = lambda ps: sum(ps) + n * fee_per_share
    else:
        raise ValueError(direction)

    if any(not ld for ld in ladders):
        return None

    idx = [0] * n
    rem = [ladders[i][0][1] for i in range(n)]
    legs = [LegFill(b.market_id, b.exchange_id) for b in books]
    Q = pnl = capital = 0.0
    steps: List[dict] = []
    top_edge = edge_of([ladders[i][0][0] for i in range(n)])

    while True:
        prices = [ladders[i][idx[i]][0] for i in range(n)]
        e = edge_of(prices)
        if e < min_edge or e <= 0:
            break
        q = min(rem)
        if max_qty is not None:
            q = min(q, max_qty - Q)
        if cash is not None:
            q = min(q, (cash - capital) / cap_of(prices))
        q = math.floor(q / lot + 1e-9) * lot
        if q <= 0:
            break
        for i in range(n):
            legs[i].fills.append((prices[i], q))
            rem[i] -= q
        Q += q
        pnl += e * q
        capital += cap_of(prices) * q
        steps.append({"prices": prices, "qty": q, "edge": e, "cum_qty": Q, "cum_pnl": pnl})
        # advance any exhausted level
        done = False
        for i in range(n):
            if rem[i] <= 1e-9:
                idx[i] += 1
                if idx[i] >= len(ladders[i]):
                    done = True
                    break
                rem[i] = ladders[i][idx[i]][1]
        if done:
            break

    if Q == 0:
        return None
    return ArbResult(race, direction, Q, legs, pnl, capital, top_edge,
                     steps[-1]["edge"], steps)


def edge_curve(race: str, books: List[Book], direction: str) -> List[dict]:
    """Full marginal-edge curve down to edge 0 (for diagnostics)."""
    r = max_executable_arb(race, books, direction, min_edge=-1.0)
    return r.steps if r else []


def breakeven_limit(direction: str, filled_vwaps: List[float], fee_per_share: float = 0.0,
                    n_legs: int = 2) -> float:
    """YES price at which the *remaining* leg makes the whole bundle break even,
    given VWAPs already achieved on the other legs. Used to chase leg 2."""
    s = sum(filled_vwaps)
    if direction == "SELL_ALL":      # need s + b >= 1 + fees
        return 1.0 - s + n_legs * fee_per_share
    return 1.0 - s - n_legs * fee_per_share   # BUY_ALL: s + a <= 1 - fees


# -------------------------------------------------------------- scanner
def scan(
    groups: Dict[str, Dict[str, int]],
    books: Dict[int, Book],
    exhaustive: set,
    **walk_kw,
) -> List[ArbResult]:
    out: List[ArbResult] = []
    for race, legs in groups.items():
        bks = [books[mid] for mid in legs.values() if mid in books]
        if len(bks) != len(legs) or len(bks) < 2:
            continue
        r = max_executable_arb(race, bks, "SELL_ALL", **walk_kw)
        if r:
            out.append(r)
        if race in exhaustive:
            r = max_executable_arb(race, bks, "BUY_ALL", **walk_kw)
            if r:
                out.append(r)
    return sorted(out, key=lambda r: (-r.pnl, -r.avg_edge))
