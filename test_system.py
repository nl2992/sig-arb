"""Offline end-to-end tests: signals live-path + bot execution with a fake client."""
import json, pathlib, sys
import sig_client, signals, bot
from signals import Snapshot, generate
FIX = pathlib.Path(__file__).with_name("fixtures") / "sample_snapshot.json"
snap = Snapshot.load(FIX)

class Fake:
    def __init__(self, *a, **k): self.calls = []
    def markets(self): return snap.markets
    def all_levels(self, ids): return {i: snap.levels[i] for i in ids}
    def balance(self): return 100_000
    def quote(self, *a): self.calls.append(("quote",) + a); return {}
    def place(self, mid, ex, side, px, q, dry_run=True):
        self.calls.append(("place", mid, side, px, q))
        return {"orderId": f"o{mid}", "filledQuantity": q if mid != 902 else q * 0.5, "avgPrice": px}
    def cancel(self, oid, dry_run=True): self.calls.append(("cancel", oid))

class A: min_edge=0.0; min_pnl=1; fee=0.0; max_qty=None; max_per_race=None; near=3
# 1) signal generation incl. budget cap
sigs, near, titles = generate(snap, A, set(), budget=500)
assert all(s.capital <= 500 + 1e-9 for s in sigs), [s.capital for s in sigs]
print("budget-capped:", [(s.race, s.qty, round(s.pnl,2)) for s in sigs])

# 2) live path of signals.main with fake client (one scan)
sig_client.Client = Fake
signals.main(["--near", "2"])  # prints + logs/signals.csv
assert (signals.HERE / "logs" / "signals.csv").exists()

# 3) execution: thinnest-first, leg 2 partial -> target shrinks, cancel residual
sigs, _, _ = generate(snap, A, set(), budget=None)
syn = [s for s in sigs if s.race == "Synthetic Senate"][0]
f = Fake(); res = bot.execute(f, syn, live=True)
print(json.dumps(res, indent=1)); print(f.calls)
assert res["status"] == "IMBALANCED" and res["qty"] == 350 and res["residual"] == {901: 350.0}
class Full(Fake):
    def place(self, mid, ex, side, px, q, dry_run=True): return {"orderId": "x", "filledQuantity": q, "avgPrice": px}
assert bot.execute(Full(), syn, live=True)["status"] == "DONE"
assert ("cancel", "o902") in f.calls

# 4) leg-1 miss -> MISS, leg-2 miss -> LEGGED
class Miss(Fake):
    def place(self, mid, ex, side, px, q, dry_run=True): return {"orderId": "x", "filledQuantity": 0}
assert bot.execute(Miss(), syn, live=True)["status"] == "MISS"
# 5) risk gate cooldown / gross cap
class Args: min_edge=0.005; min_edge_3leg=0.01; max_gross=900; cooldown=30
rk = bot.Risk(Args)
de = [s for s in sigs if s.race == "Delaware Senate"][0]
print("risk DE:", rk.ok(de))  # capital 970 > 900 -> gross cap
assert rk.ok(de) == (False, "gross cap")
print("ALL SYSTEM TESTS PASS")
