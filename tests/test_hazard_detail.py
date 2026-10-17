"""Hazard overlay detail: every zone carries a score and a source for each reason, every port a problem kind."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import plan_voyage  # noqa: E402

PORT_KINDS = {'closed', 'shallow', 'unreachable', 'in_zone', 'congested'}


def plan(**kw):
    req = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal'}
    req.update(kw)
    return plan_voyage(req)


class ZoneDetail(unittest.TestCase):
    def test_every_zone_has_score_and_parts(self):
        for z in plan()['hazards']['zones']:
            self.assertIsInstance(z['score'], int, z['id'])
            self.assertTrue(0 <= z['score'] <= 100, z['id'])
            self.assertIsInstance(z['parts'], list, z['id'])

    def test_every_reason_has_a_part_with_a_source(self):
        r = plan(origin='JEA', conditions='today')
        for z in r['hazards']['zones']:
            self.assertEqual(len(z['parts']), len(z['reasons']), z['id'])
            for part in z['parts']:
                self.assertTrue(part['text'] and part['source'] and part['kind'], z)

    def test_closure_part_names_the_scenario_as_its_source(self):
        z = next(z for z in plan(events=[{'type': 'zone_closure', 'zone': 'suez'}])['hazards']['zones'] if z['id'] == 'suez')
        closed = [p for p in z['parts'] if p['kind'] == 'closure']
        self.assertTrue(closed)
        self.assertIn('SIMULATED', closed[0]['source'].upper())

    def test_security_score_rises_with_a_threat_event(self):
        base = next(z for z in plan()['hazards']['zones'] if z['id'] == 'bab')
        hot = next(z for z in plan(events=[{'type': 'security', 'zone': 'bab', 'level': 0.3}])['hazards']['zones'] if z['id'] == 'bab')
        self.assertGreater(hot['score'], base['score'])
        self.assertIn('security', {p['kind'] for p in hot['parts']})


class PortKinds(unittest.TestCase):
    def test_every_hazard_port_has_a_known_primary_kind_and_kinds_list(self):
        r = plan(origin='JEA', conditions='today', events=[{'type': 'port_closure', 'port': 'DKR'}])
        self.assertTrue(r['hazards']['ports'])
        for p in r['hazards']['ports']:
            self.assertIn(p['kind'], PORT_KINDS, p)
            self.assertIn(p['kind'], p['kinds'])
            self.assertTrue(set(p['kinds']) <= PORT_KINDS)

    def test_closed_port_is_kind_closed(self):
        r = plan(events=[{'type': 'port_closure', 'port': 'DKR'}])
        self.assertEqual(next(p for p in r['hazards']['ports'] if p['port'] == 'DKR')['kind'], 'closed')

    def test_congested_port_is_kind_congested(self):
        r = plan(events=[{'type': 'port_congestion', 'port': 'RTM', 'wait_h': 200}])
        kinds = {p['port']: p['kind'] for p in r['hazards']['ports']}
        self.assertEqual(kinds.get('RTM'), 'congested')

    def test_closed_beats_other_kinds_for_the_primary(self):
        r = plan(origin='JEA', conditions='today', events=[{'type': 'port_closure', 'port': 'JEA'}])
        jea = next(p for p in r['hazards']['ports'] if p['port'] == 'JEA')
        self.assertEqual(jea['kind'], 'closed')
        self.assertGreater(len(jea['kinds']), 1)   # JEA is also inside the high-risk Hormuz zone


if __name__ == '__main__':
    unittest.main()
