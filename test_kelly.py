import unittest
from kelly import size_position


class KellyTests(unittest.TestCase):
    def test_constant_price(self):
        r = size_position([(0.5, 10000)], 0.6, 1000, 1)
        self.assertEqual(r['qty'], 400)
        self.assertEqual(r['capital'], 200)
        self.assertEqual(size_position([(0.5, 10000)], 0.6, 1000)['qty'], 100)

    def test_depth_and_no_edge(self):
        self.assertEqual(size_position([(0.7, 1000)], 0.6, 1000)['qty'], 0)
        r = size_position([(0.4, 10), (0.8, 1000)], 0.6, 1000)
        self.assertEqual(r['qty'], 10)

    def test_matches_discrete_log_optimum(self):
        import math
        ladder = [(0.35, 30), (0.5, 200), (0.7, 200)]
        r = size_position(ladder, 0.61, 200, 1, 0.01)
        costs = [0]
        for price, q in ladder:
            for _ in range(q):
                costs.append(costs[-1]+price+0.01)
        valid = [(0.61*math.log(200-c+q)+0.39*math.log(200-c), q)
                 for q, c in enumerate(costs) if c < 200]
        self.assertEqual(r['qty'], max(valid)[1])


if __name__ == '__main__':
    unittest.main()
