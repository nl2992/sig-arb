"""
conviction.py — a few large, high-conviction directional bets (strategy "cv").

The competition pays only its top three, so this strategy trades variance for a shot at a
prize: it concentrates capital where SIG disagrees most with the Kalshi/Polymarket
consensus, in competitive races (fair probability inside [min_fair, max_fair]), and holds
to settlement.

Picker   ranks every market the bot has a recent book for by the gap between SIG's
         executable price and fair (buy YES below fair, sell YES above fair), keeps the
         top `max_bets` with gap >= `min_edge`, at most one per race.
Entry    when a target's race is read, walk SIG's book up to `max_bet` capital at prices
         still `min_edge` better than fair.
Exit     take profit once SIG reaches fair (within `exit_band`), never below the entry: the
         edge is gone, so the capital moves to the next-best bet. Stop if the consensus
         moves `stop` against the entry (thesis broken), at a SIG price no more than
         `max_slip` worse than fair. Otherwise hold to settlement.
Ledger   logs/cv_positions.json.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from arb_engine import Book, TITLE_RE
from fair_value import slip_ok


def gap(book: Book, fair: float) -> Optional[tuple]:
    """('BUY'|'SELL', edge at the touch) for the better side, or None."""
    best = None
    if book.asks:
        e = fair - book.asks[0][0]
        best = ("BUY", e)
    if book.bids:
        e = book.bids[0][0] - fair
        if best is None or e > best[1]:
            best = ("SELL", e)
    return best


def pick_targets(books: Dict[int, Book], titles: Dict[int, str], fair_fn: Callable[[int], Optional[float]],
                 ledger, *, min_edge: float, min_fair: float, max_fair: float, max_bets: int) -> Dict[int, dict]:
    """Held bets stay targets (so their exits are managed); free slots go to the largest gaps."""
    held = {m for m, r in ledger.rows.items() if r.get("qty")}
    targets = {m: {"held": True} for m in held}
    races_used = {_race(titles.get(m, "")) for m in held}
    cands = []
    for m, b in books.items():
        if m in held:
            continue
        f = fair_fn(m)
        if f is None or not (min_fair <= f <= max_fair):
            continue
        g = gap(b, f)
        if g and g[1] >= min_edge:
            cands.append((g[1], m, g[0], f))
    for edge, m, side, f in sorted(cands, reverse=True):
        if len(targets) >= max_bets:
            break
        race = _race(titles.get(m, ""))
        if race in races_used:
            continue
        races_used.add(race)
        targets[m] = {"held": False, "side": side, "edge": round(edge, 4), "fair": round(f, 4)}
    return targets


def _race(title: str) -> str:
    mt = TITLE_RE.match(title or "")
    return mt.group(2) if mt else title


def plan(book: Book, fair: Optional[float], ledger, *, min_edge: float, max_bet: float, gross_left: float,
         stop: float, max_slip: float, exit_band: Optional[float] = None,
         take: Optional[float] = None) -> Optional[dict]:
    if fair is None:
        return None
    m = book.market_id
    pos = ledger.position(m)
    if pos:
        entry = ledger.avg_yes(m)
        # Stop only when the thesis is broken, at a price no more than max_slip worse than fair.
        # SIG lagging on our side of fair is the best exit, so that is never refused.
        if pos > 0 and book.bids and fair <= entry - stop and slip_ok(book.bids[0][0], fair, selling=True,
                                                                         max_slip=max_slip):
            return {"yes_side": "SELL", "limit": book.bids[0][0], "qty": float(min(pos, book.bids[0][1])),
                    "exit": "stop", "fair": round(fair, 4), "entry": round(entry, 4)}
        if pos < 0 and book.asks and fair >= entry + stop and slip_ok(book.asks[0][0], fair, selling=False,
                                                                          max_slip=max_slip):
            return {"yes_side": "BUY", "limit": book.asks[0][0], "qty": float(min(-pos, book.asks[0][1])),
                    "exit": "stop", "fair": round(fair, 4), "entry": round(entry, 4)}
        # break-even or better: SIG pays `take` over the entry
        if take is not None and pos > 0 and book.bids and book.bids[0][0] >= entry + take - 1e-9:
            return {"yes_side": "SELL", "limit": book.bids[0][0], "qty": float(min(pos, book.bids[0][1])),
                    "exit": "target", "fair": round(fair, 4), "entry": round(entry, 4)}
        if take is not None and pos < 0 and book.asks and book.asks[0][0] <= entry - take + 1e-9:
            return {"yes_side": "BUY", "limit": book.asks[0][0], "qty": float(min(-pos, book.asks[0][1])),
                    "exit": "target", "fair": round(fair, 4), "entry": round(entry, 4)}
        # take profit: SIG has reached fair (the edge is gone) at a price no worse than the entry
        if exit_band is not None and pos > 0 and book.bids and book.bids[0][0] >= max(fair - exit_band, entry):
            return {"yes_side": "SELL", "limit": book.bids[0][0], "qty": float(min(pos, book.bids[0][1])),
                    "exit": "converged", "fair": round(fair, 4), "entry": round(entry, 4)}
        if exit_band is not None and pos < 0 and book.asks and book.asks[0][0] <= min(fair + exit_band, entry):
            return {"yes_side": "BUY", "limit": book.asks[0][0], "qty": float(min(-pos, book.asks[0][1])),
                    "exit": "converged", "fair": round(fair, 4), "entry": round(entry, 4)}
    budget = min(max_bet - ledger.capital(m), gross_left)
    if budget <= 0:
        return None
    g = gap(book, fair)
    if not g or g[1] < min_edge or (pos and (pos > 0) != (g[0] == "BUY")):
        return None
    qty = spent = notional = 0.0
    limit = None
    if g[0] == "BUY":
        for price, size in book.asks:
            if price > fair - min_edge:
                break
            take = min(size, (budget - spent) / price)
            if take <= 0:
                break
            qty += take; spent += take * price; notional += take * price; limit = price
    else:
        for price, size in book.bids:
            if price < fair + min_edge:
                break
            take = min(size, (budget - spent) / (1 - price))
            if take <= 0:
                break
            qty += take; spent += take * (1 - price); notional += take * price; limit = price
    qty = float(int(qty))
    if qty < 10 or limit is None:
        return None
    vwap = notional / qty
    edge = (fair - vwap) if g[0] == "BUY" else (vwap - fair)
    return {"yes_side": g[0], "limit": limit, "qty": qty, "fair": round(fair, 4), "edge": round(edge, 4),
            "capital": round(spent, 2), "expected_pnl": round(edge * qty, 2)}
