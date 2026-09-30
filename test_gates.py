import json
import tempfile
import unittest
from pathlib import Path

import gates

LIMITS = json.loads(Path('config/risk_limits.json').read_text())


def good_system(**overrides):
    system = dict(sig_session='NORMALIZED', payload_verified=True, kill_switch={'engaged': False},
                  recon={'status': 'RECONCILED', 'as_of': '2026-09-30T15:00:00Z'},
                  mode='human_confirmed', limits=LIMITS, sig_snapshot_age_s=1.0, active_breakers={})
    system.update(overrides)
    return system


def good_candidate(**overrides):
    candidate = dict(legs=[dict(venue='sig', market_id=447, qty=100, visible_qty=300, quote_age_s=1.0,
                                fee=0.0, venue_id_known=True),
                           dict(venue='sig', market_id=448, qty=100, visible_qty=450, quote_age_s=2.0,
                                fee=0.0, venue_id_known=True)],
                     mapping='single_venue', settlement='approved', capital=100.0, slippage=0.002)
    candidate.update(overrides)
    return candidate


def by_id(gate_list):
    return {g['id']: g for g in gate_list}


class GateTests(unittest.TestCase):
    def test_all_thirteen_pass_only_when_everything_is_verified(self):
        result = gates.evaluate(good_system(), good_candidate())
        self.assertEqual([g['id'] for g in result], list(gates.GATE_IDS))
        self.assertEqual(len(result), 13)
        self.assertTrue(gates.all_pass(result), [g for g in result if g['pass'] is not True])

    def test_each_gate_fails_independently(self):
        leg = good_candidate()['legs'][0]
        cases = {
            'sig_auth': (dict(sig_session='AUTH_REQUIRED'), {}),
            'payload_verified': (dict(payload_verified=False), {}),
            'venue_ids': ({}, dict(legs=[{**leg, 'venue_id_known': False}])),
            'fees_known': ({}, dict(legs=[{**leg, 'fee': None}])),
            'mapping_approved': ({}, dict(mapping='pending')),
            'settlement_approved': ({}, dict(settlement='unreviewed')),
            'quotes_fresh': ({}, dict(legs=[{**leg, 'quote_age_s': 9.0}])),
            'liquidity': ({}, dict(legs=[{**leg, 'visible_qty': 50}])),
            'no_breaker': (dict(active_breakers={447: [{'id': 'cb'}]}), {}),
            'risk_limits': ({}, dict(capital=5000.0)),
            'recon_clean': (dict(recon=None), {}),
            'kill_switch_off': (dict(kill_switch={'engaged': True, 'reason': 'drill'}), {}),
            'live_mode': (dict(mode='paper'), {}),
        }
        self.assertEqual(set(cases), set(gates.GATE_IDS))
        for gate_id, (system_over, cand_over) in cases.items():
            result = by_id(gates.evaluate(good_system(**system_over), good_candidate(**cand_over)))
            failing = sorted(k for k, g in result.items() if g['pass'] is not True)
            self.assertEqual(failing, [gate_id], gate_id)

    def test_unknown_values_fail(self):
        result = by_id(gates.evaluate(good_system(sig_session=None, limits=None, sig_snapshot_age_s=None),
                                      good_candidate(capital=None, slippage=None, mapping=None,
                                                     settlement=None, legs=[])))
        for gate_id in ('sig_auth', 'quotes_fresh', 'risk_limits', 'mapping_approved',
                        'settlement_approved', 'venue_ids', 'fees_known', 'liquidity', 'no_breaker'):
            self.assertIs(result[gate_id]['pass'], False, gate_id)

    def test_system_scope_marks_candidate_gates_as_not_passing(self):
        result = gates.evaluate(good_system())
        per_candidate = {g['id'] for g in result if g['pass'] is None}
        self.assertEqual(per_candidate, {'venue_ids', 'fees_known', 'mapping_approved',
                                         'settlement_approved', 'liquidity', 'no_breaker'})
        self.assertFalse(gates.all_pass(result))
        self.assertEqual(gates.summary(result), {'pass': 7, 'fail': 0, 'per_candidate': 6, 'total': 13})

    def test_fee_of_zero_is_known_but_missing_fee_is_not(self):
        leg = good_candidate()['legs'][0]
        self.assertTrue(by_id(gates.evaluate(good_system(), good_candidate()))['fees_known']['pass'])
        missing = {k: v for k, v in leg.items() if k != 'fee'}
        self.assertFalse(by_id(gates.evaluate(good_system(), good_candidate(legs=[missing])))['fees_known']['pass'])

    def test_gate_hash_changes_only_when_a_gate_flips(self):
        base = gates.gate_hash(gates.evaluate(good_system(), good_candidate()))
        self.assertEqual(base, gates.gate_hash(gates.evaluate(good_system(), good_candidate())))
        leg = good_candidate()['legs'][0]
        still_fresh = good_candidate(legs=[{**leg, 'quote_age_s': 4.0}])
        stale = good_candidate(legs=[{**leg, 'quote_age_s': 9.0}])
        self.assertEqual(base, gates.gate_hash(gates.evaluate(good_system(), still_fresh)))
        self.assertNotEqual(base, gates.gate_hash(gates.evaluate(good_system(), stale)))
        self.assertNotEqual(base, gates.gate_hash(gates.evaluate(good_system(mode='paper'), good_candidate())))

    def test_load_limits_validation(self):
        self.assertEqual(gates.load_limits('config/risk_limits.json')['max_quote_age_s'], 5.0)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'limits.json'
            for bad in ({**LIMITS, 'auto_hedge': True}, {**LIMITS, 'daily_loss': -1},
                        {**LIMITS, 'max_quote_age_s': True}, {k: v for k, v in LIMITS.items() if k != 'max_slippage'}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    gates.load_limits(path)


if __name__ == '__main__':
    unittest.main()
