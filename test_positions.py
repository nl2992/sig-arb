"""positions.build: strategy split, locked arb value, race worst cases and stress tests."""
import pathlib
import tempfile
import unittest

import fair_value as fv
import positions


def h(mid, party, race, qty, avg, cp):
    return {"marketId": mid, "title": f"Will the {party} Party win the {race}?", "settlementOption": "YES",
            "quantity": qty, "averagePricePaid": avg, "currentPrice": cp}


class PositionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.leds = {"fv": fv.Ledger(d / "f.json"), "ll": fv.Ledger(d / "l.json"), "mm": fv.Ledger(d / "m.json")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_report(self):
        # Nevada: arb, NO on both at 0.46 + 0.53 (locked +10.1 on 1010 sets)
        # Delaware: fair value short the Republican long shot, 500 NO at 0.95
        self.leds["fv"].record(3, "SELL", 500, 0.05)
        port = {"cashBalance": 98515.1, "dailyPnL": {"value": -5}, "openOrders": [],
                "holdings": [h(1, "Republican", "Nevada Governor", -1010, 0.46, 0.55),
                             h(2, "Democratic", "Nevada Governor", -1010, 0.53, 0.47),
                             h(3, "Republican", "Delaware Senate", -500, 0.95, 0.04)]}
        rep = positions.build(port, self.leds, lambda m: {3: 0.01}.get(m), arb_cost_fn=dict)
        self.assertAlmostEqual(rep["strategies"]["arb"]["ev"], 10.1, places=1)       # locked
        self.assertAlmostEqual(rep["strategies"]["fv"]["ev"], 500 * 0.99 - 475, places=1)
        races = {r["race"]: r for r in rep["races"]}
        self.assertTrue(races["Nevada Governor"]["hedged"])
        self.assertAlmostEqual(races["Delaware Senate"]["worst"], -475, places=1)   # Republican upset
        self.assertFalse(races["Delaware Senate"]["hedged"])
        self.assertAlmostEqual(rep["risk"]["if_republicans_sweep"], 10.1 - 475, places=1)
        self.assertAlmostEqual(rep["risk"]["if_democrats_sweep"], 10.1 + 25, places=1)
        self.assertEqual(rep["risk"]["unhedged_races"], 1)
        self.assertAlmostEqual(rep["account"]["realized"], 98515.1 + 464.6 + 535.3 + 475 - 100000, places=1)


class NettingTests(PositionsTests):
    def test_opposite_strategy_legs_are_valued_net(self):
        # arb holds D NO 2043, fv bought D YES 1110 against it: the account holds D NO 933
        self.leds["fv"].record(370, "BUY", 1110, 0.92)
        port = {"cashBalance": 0, "openOrders": [], "holdings": [
            h(370, "Democratic", "Massachusetts Governor", -933, 0.053, 0.95),
            h(371, "Republican", "Massachusetts Governor", -2043, 0.925, 0.10)]}
        rep = positions.build(port, self.leds, lambda m: None, arb_cost_fn=dict)
        self.assertAlmostEqual(rep["account"]["open_cost"], 933 * 0.053 + 2043 * 0.925, places=1)
        self.assertEqual(rep["netted"], [{"strategy": "fv", "market_id": 370, "qty": 1110.0}])
        race = rep["races"][0]
        self.assertAlmostEqual(race["if_dem"], 2043 - (933 * 0.053 + 2043 * 0.925), places=1)
        self.assertAlmostEqual(race["if_rep"], 933 - (933 * 0.053 + 2043 * 0.925), places=1)


class ArbCostTests(PositionsTests):
    def test_arb_leg_cost_from_its_own_fills_not_net_average(self):
        # Nebraska #281: arb holds R NO 1041 bought at 0.30; fv holds R YES 2000 -> net long YES 959 @ 0.715
        self.leds["fv"].record(281, "BUY", 2000, 0.715)
        port = {"cashBalance": 0, "openOrders": [], "holdings": [
            h(281, "Republican", "Nebraska Senate", 959, 0.715, 0.72),
            h(279, "Democratic", "Nebraska Senate", -1041, 0.953, 0.03),
            h(280, "Independent", "Nebraska Senate", -1041, 0.726, 0.27)]}
        costs = {281: [-1041.0, 1041 * 0.30], 279: [-1041.0, 1041 * 0.953], 280: [-1041.0, 1041 * 0.726]}
        rep = positions.build(port, self.leds, lambda m: None, arb_cost_fn=lambda: costs)
        self.assertAlmostEqual(rep["strategies"]["arb"]["ev"], 2 * 1041 - 1041 * (0.30 + 0.953 + 0.726), places=1)


if __name__ == "__main__":
    unittest.main()
