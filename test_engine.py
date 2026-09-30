from arb_engine import Book, max_executable_arb, scan, group_markets, breakeven_limit
L=lambda side,p,q,yes=True:{"exchangeId":1,"side":side,"isYes":yes,"price":p,"quantity":q}

# 1) live snapshot: Delaware
r=max_executable_arb("DE",[Book.from_levels(386,[L("BUY",.08,1000),L("SELL",.16,1000)]),
                            Book.from_levels(353,[L("BUY",.95,1000)])],"SELL_ALL")
print(r.summary()); assert r.qty==1000 and abs(r.pnl-30)<1e-6 and abs(r.capital-970)<1e-6

# 2) multi-level, edge decays with depth; min_edge cutoff
R=Book.from_levels(1,[L("BUY",.30,200),L("BUY",.28,500),L("BUY",.25,1000)])
D=Book.from_levels(2,[L("BUY",.76,300),L("BUY",.74,400),L("BUY",.70,1000)])
r=max_executable_arb("X",[R,D],"SELL_ALL")
for s in r.steps: print(s)
print(r.summary())
# steps: 200@(.30,.76)=.06, 100@(.28,.76)=.04, 400@(.28,.74)=.02, then .28+.70=-.02 stop
assert r.qty==700 and abs(r.pnl-(200*.06+100*.04+400*.02))<1e-9
r2=max_executable_arb("X",[R,D],"SELL_ALL",min_edge=0.03); assert r2.qty==300
r3=max_executable_arb("X",[R,D],"SELL_ALL",cash=100); print("cash-capped",r3.summary())
assert r3.capital<=100+1e-9
print("VWAP R",r.legs[0].vwap,"limit",r.legs[0].limit)

# 3) NO-side levels normalised (NO buy @0.30 == YES sell @0.70)
b=Book.from_levels(9,[L("BUY",.30,50,yes=False)]); assert b.asks==[(0.7,50)]

# 4) triple, buy-all only if exhaustive
books={1:Book.from_levels(1,[L("SELL",.30,100)]),2:Book.from_levels(2,[L("SELL",.40,100)]),3:Book.from_levels(3,[L("SELL",.25,60),L("SELL",.28,100)])}
groups={"Tri":{"R":1,"D":2,"I":3}}
print([x.summary() for x in scan(groups,books,exhaustive=set())])
res=scan(groups,books,exhaustive={"Tri"}); print(res[0].summary()); assert res[0].qty==100
print("breakeven leg2 sell:",breakeven_limit("SELL_ALL",[0.08]))
print("ALL TESTS PASS")
