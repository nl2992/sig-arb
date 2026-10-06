"""
adopt_fv_positions.py — hand one-sided SIG positions to the fair-value strategy.

Positions adopted from another machine's records (journal_backfill) sit outside every
strategy ledger, so nothing exits them. This moves each one-sided position (what is left of
a market's holding after complete No sets and the ll/mm/cv/fv ledgers) into
logs/fv_positions.json at SIG's own averagePricePaid, and writes an offsetting
journal_backfill so the holdings check still balances. The fair-value strategy then applies
its target / converged / stop exits to them.

    python tools/adopt_fv_positions.py            # show the plan, change nothing
    python tools/adopt_fv_positions.py --apply    # write it (bot must be stopped)
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot                      # noqa: E402
import fair_value               # noqa: E402
import fast_scan                # noqa: E402
import holdings_check as hc     # noqa: E402
import sig_client               # noqa: E402
import supervisor               # noqa: E402

MIN_QTY = 10                    # smaller leftovers are left alone


def plan(cli) -> list[dict]:
    port = cli.portfolio()
    avg = hc.account_avg(port)
    ledgers = [fair_value.Ledger(), *(fair_value.Ledger(ROOT / "logs" / n)
                                      for n in ("ll_positions.json", "mm_positions.json", "cv_positions.json"))]
    free = {m: q - sum(l.position(m) for l in ledgers) for m, (q, _) in avg.items()}
    covered: dict[int, float] = {}
    for legs in bot.group_markets(fast_scan.load_markets(cli)).values():
        ids = list(legs.values())
        if len(ids) >= 2 and all(free.get(m, 0.0) < 0 for m in ids):
            k = min(-free[m] for m in ids)
            for m in ids:
                covered[m] = k
    out = []
    for m, q in free.items():
        left = q + covered.get(m, 0.0) if q < 0 else q
        if abs(left) < MIN_QTY or (q > 0) != (left > 0):
            continue
        acct_q, paid = avg[m]
        if (acct_q > 0) != (left > 0):
            continue                     # another ledger holds the opposite side: leave it
        out.append({"market_id": m, "qty": float(int(left)), "paid": paid,
                    "entry_yes": paid if left > 0 else 1 - paid})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    sig_client.load_env()
    rows = plan(sig_client.Client())
    cap = sum(abs(r["qty"]) * r["paid"] for r in rows)
    for r in sorted(rows, key=lambda r: -abs(r["qty"]) * r["paid"]):
        print(f"#{r['market_id']:<5} {'YES' if r['qty'] > 0 else 'NO':3} {abs(r['qty']):7.0f}  entry {r['paid']:.4f} "
              f"(YES terms {r['entry_yes']:.4f})")
    print(f"{len(rows)} positions, capital {cap:,.2f}")
    if not a.apply:
        print("plan only; rerun with --apply while the bot is stopped")
        return 0
    if supervisor.read_pid() or supervisor.other_bots():
        print("bot is running: stop it first (python go_live.py stop)")
        return 1
    led = fair_value.Ledger()
    now = time.time()
    for r in rows:
        m = r["market_id"]
        if led.position(m):
            continue
        led.rows[m] = {"qty": r["qty"], "capital": round(abs(r["qty"]) * r["paid"], 4), "realized": 0.0,
                       "opened_at": now, "updated_at": hc._now(), "adopted": True}
        hc._append(hc.MANUAL_LOG, {"ts": hc._now(), "action": "journal_backfill", "market_id": m,
                                   "qty": -r["qty"], "price_yes": r["entry_yes"],
                                   "reason": "moved to the fair-value ledger (tools/adopt_fv_positions.py)"})
    led.save()
    print(f"adopted {len(rows)} positions into {fair_value.LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
