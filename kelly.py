"""Single binary position sizing against depth; no existing holdings assumed."""
import math


def size_position(ladder, probability, bankroll, fraction=0.25, fee=0):
    if not all(math.isfinite(v) for v in (probability, bankroll, fraction, fee)):
        raise ValueError('Inputs must be finite')
    if not 0 < probability < 1 or bankroll <= 0 or not 0 < fraction <= 1 or fee < 0:
        raise ValueError('Use probability between 0 and 1, positive bankroll, and fraction in (0, 1]')
    # Fractional Kelly is implemented as full Kelly on a fractional risk bankroll.
    risk_bankroll = bankroll * fraction
    qty = cost = 0.0
    for price, available in sorted(ladder):
        c = price + fee
        if not 0 < c < 1 or available <= 0:
            continue
        # Stationary point of p log(B-C+Q) + (1-p) log(B-C)
        # within this constant marginal-price depth segment.
        extra = ((probability-c)*(risk_bankroll-cost)
                 - (1-probability)*c*qty) / (c*(1-c))
        limit = math.floor(available)
        candidates = {min(limit, max(0, math.floor(extra))),
                      min(limit, max(0, math.ceil(extra)))}
        def utility(take):
            lose = risk_bankroll-cost-take*c
            win = lose+qty+take
            return (probability*math.log(win)+(1-probability)*math.log(lose)
                    if lose > 0 and win > 0 else -math.inf)
        take = max(candidates, key=lambda take: (utility(take), -take))
        qty += take
        cost += take*c
        if take < math.floor(available):
            break
    expected = probability*qty-cost
    growth = (probability*math.log1p((qty-cost)/bankroll)
              + (1-probability)*math.log1p(-cost/bankroll)) if qty else 0
    return dict(qty=qty, capital=cost, expected_profit=expected,
                vwap=cost/qty-fee if qty else None, growth=growth,
                bankroll_fraction=cost/bankroll)
