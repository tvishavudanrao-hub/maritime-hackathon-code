"""Planner speed-ups must not change any answer: golden results, fewer searches, and a cheap exact reachability check.

Golden numbers were recorded from the planner before the speed-ups (live weather off, so they do not depend on the hour).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import optimizer  # noqa: E402
from optimizer import GRAPH, plan_voyage  # noqa: E402

BASE = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'priority': 'balanced', 'fuel_onboard_t': 2500,
        'live_weather': False, 'conditions': 'normal', 'cargo': 'retail'}


def closure(zone):
    return [{'type': 'zone_closure', 'zone': zone, 'id': zone + '-closed', 'source': 's', 'description': 'd'}]


STORM = [{'type': 'storm', 'id': 'cs', 'center': [-35, 22], 'radius_nm': 300, 'hs_m': 7.5, 'wind_ms': 25,
          'start_h': 0, 'end_h': 2000, 'source': 's', 'description': 'd'}]

# name: (request, status, recommended (corridor, days, cash usd), alternatives)
CASES = {
    'default': (BASE, 'ok', ('suez', 27.53, 2718164), [('cape', 43.14, 2899690)]),
    'suez closed': (dict(BASE, events=closure('suez')), 'ok', ('cape', 43.14, 2899690), []),
    'bab closed': (dict(BASE, events=closure('bab')), 'ok', ('cape', 43.14, 2899690), []),
    'cape closed': (dict(BASE, events=closure('cape')), 'ok', ('suez', 27.53, 2718164), []),
    'hormuz closed from Jebel Ali': (dict(BASE, origin='JEA', events=closure('hormuz')), 'infeasible', None, []),
    'suez and cape closed': (dict(BASE, events=closure('suez') + closure('cape')), 'infeasible', None, []),
    'cape storm': (dict(BASE, events=STORM), 'ok', ('suez', 28.92, 2707897), []),
    'tanker Shanghai to Hamburg': (dict(BASE, ship_id='meridian', origin='SHA', destination='HAM', cargo='crude'),
                                   'ok', ('suez', 43.03, 2203119), [('cape', 60.16, 2536915)]),
}
CLOSED_CORRIDOR = ('suez closed', 'bab closed', 'cape closed', 'hormuz closed from Jebel Ali', 'suez and cape closed')
CORRIDOR_POSSIBLE = ('default', 'cape storm', 'tanker Shanghai to Hamburg')


def summarise(plan):
    return (plan['corridor'], round(plan['summary']['days'], 2), round(plan['costs']['cash_total_usd']))


def run_case(name, always_search=False):
    """Plan one case; returns the result and the edge caches handed to each search. always_search=True switches the
    hopeless-corridor check off, which is how the planner behaved before the speed-up."""
    req = CASES[name][0]
    calls = []
    original, original_possible = optimizer._search, optimizer._corridor_possible

    def spy(r, ev, edge_cache=None):
        calls.append(edge_cache)
        return original(r, ev, edge_cache)
    optimizer._search = spy
    if always_search:
        optimizer._corridor_possible = lambda s, corridor, block: True
    try:
        result = plan_voyage(dict(req))
    finally:
        optimizer._search, optimizer._corridor_possible = original, original_possible
    return result, calls


class GoldenResults(unittest.TestCase):
    def test_results_are_unchanged(self):
        for name, (req, status, rec, alts) in CASES.items():
            with self.subTest(name):
                result, _ = run_case(name)
                self.assertEqual(result['status'], status)
                if rec is None:
                    self.assertIsNone(result.get('recommended'))
                    continue
                self.assertEqual(summarise(result['recommended']), rec)
                self.assertEqual([summarise(a) for a in result['alternatives']], alts)

    def test_hopeless_corridor_searches_are_skipped(self):
        for name in CLOSED_CORRIDOR:
            with self.subTest(name):
                _, fast = run_case(name)
                _, slow = run_case(name, always_search=True)
                self.assertLess(len(fast), len(slow))

    def test_searches_that_can_succeed_still_run(self):
        for name in CORRIDOR_POSSIBLE:
            with self.subTest(name):
                _, fast = run_case(name)
                _, slow = run_case(name, always_search=True)
                self.assertEqual(len(fast), len(slow))

    def test_corridor_searches_of_one_plan_share_an_edge_cache(self):
        _, calls = run_case('cape storm')   # baseline search + open search + one corridor search
        self.assertTrue(all(isinstance(c, dict) for c in calls))
        self.assertLess(len({id(c) for c in calls}), len(calls))


class BaselineCache(unittest.TestCase):
    """The plan 'as if nothing had happened' is reused across scenario toggles on the same ship and route."""

    def setUp(self):
        optimizer._BASELINE.clear()

    def count_searches(self, request):
        calls, original = [], optimizer._search

        def spy(r, ev, edge_cache=None):
            calls.append(1)
            return original(r, ev, edge_cache)
        optimizer._search = spy
        try:
            result = plan_voyage(request)
        finally:
            optimizer._search = original
        return result, len(calls)

    def test_the_second_toggle_skips_the_baseline_search(self):
        _, cold = self.count_searches(dict(BASE, events=closure('suez')))
        _, warm = self.count_searches(dict(BASE, events=closure('bab')))
        self.assertLess(warm, cold)

    def test_a_cached_baseline_gives_the_same_answer_as_a_fresh_one(self):
        req = dict(BASE, events=closure('suez'))
        cold = plan_voyage(dict(req))
        warm = plan_voyage(dict(req))
        for key in ('baseline', 'change', 'events', 'recommended', 'alternatives'):
            self.assertEqual(cold[key], warm[key], key)

    def test_callers_cannot_corrupt_the_cached_baseline(self):
        req = dict(BASE, events=closure('suez'))
        first = plan_voyage(dict(req))
        first['baseline']['checkpoints'].clear()
        first['baseline']['corridor'] = 'tampered'
        second = plan_voyage(dict(req))
        self.assertNotEqual(second['baseline']['corridor'], 'tampered')
        self.assertTrue(second['baseline']['checkpoints'])

    def test_different_ships_do_not_share_a_baseline(self):
        a = plan_voyage(dict(BASE, events=closure('suez')))
        b = plan_voyage(dict(BASE, ship_id='meridian', cargo='crude', events=closure('suez')))
        self.assertNotEqual(a['baseline']['summary']['days'], b['baseline']['summary']['days'])


class CanReachVia(unittest.TestCase):
    def test_both_corridors_exist_when_nothing_is_closed(self):
        self.assertTrue(GRAPH.can_reach_via('SIN', 'RTM', {'cape'}, 'suez'))
        self.assertTrue(GRAPH.can_reach_via('SIN', 'RTM', {'suez'}, 'cape'))

    def test_a_corridor_whose_own_zone_is_closed_is_impossible(self):
        self.assertFalse(GRAPH.can_reach_via('SIN', 'RTM', {'cape', 'suez'}, 'suez'))
        self.assertFalse(GRAPH.can_reach_via('SIN', 'RTM', {'suez', 'cape'}, 'cape'))

    def test_suez_route_needs_the_red_sea(self):
        self.assertFalse(GRAPH.can_reach_via('SIN', 'RTM', {'cape', 'bab'}, 'suez'))

    def test_hormuz_blocks_gulf_departures(self):
        self.assertFalse(GRAPH.can_reach_via('JEA', 'RTM', {'suez', 'hormuz'}, 'cape'))

    def test_it_never_rules_out_a_route_the_search_finds(self):
        # relaxation check: whenever the search finds a plan through a corridor, the check must say it is possible
        for name in ('default', 'suez closed', 'cape closed', 'tanker Shanghai to Hamburg'):
            req = CASES[name][0]
            result = plan_voyage(dict(req))
            closed = {e['zone'] for e in req.get('events', []) if e['type'] == 'zone_closure'}
            for plan in [result['recommended'], *result['alternatives']]:
                with self.subTest((name, plan['corridor'])):
                    block = {'suez': 'cape', 'cape': 'suez'}.get(plan['corridor'])
                    if block:
                        self.assertTrue(GRAPH.can_reach_via(req['origin'], req['destination'], closed | {block}, plan['corridor']))


if __name__ == '__main__':
    unittest.main()
