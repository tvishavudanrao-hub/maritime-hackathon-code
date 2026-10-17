"""Backend tests: fuel model, risk model, optimizer constraints and scenarios.

Run from the project folder:  python3 -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuel_model import PhysicsFuelModel, RegressionFuelModel  # noqa: E402
from optimizer import FLEET, FLEET_LIST, GRAPH, plan_voyage, speed_options  # noqa: E402
from risk_model import RiskModel, weather_risk  # noqa: E402

AURORA = FLEET['aurora']


class FuelModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = RegressionFuelModel(FLEET_LIST)
        cls.phys = PhysicsFuelModel()

    def test_regression_rediscovers_physics(self):
        for sid, m in self.reg.info()['ships'].items():
            c = m['coefficients']
            self.assertAlmostEqual(c['ln_speed'], 2.0, delta=0.1, msg=sid)
            self.assertAlmostEqual(c['ln_displacement'], 2 / 3, delta=0.1, msg=sid)
            self.assertLess(m['holdout_mape'], 0.03, msg=sid)

    def test_cubic_law_daily_burn(self):
        d = AURORA['ref_displacement_t']
        ratio = self.phys.daily_fuel(AURORA, 20, d) / self.phys.daily_fuel(AURORA, 10, d)
        self.assertAlmostEqual(ratio, 8.0, places=6)

    def test_slower_burns_less_per_mile(self):
        _, slow = self.reg.leg(AURORA, 1000, 12, 2000)
        _, fast = self.reg.leg(AURORA, 1000, 18, 2000)
        self.assertLess(slow, fast * 0.5)

    def test_waves_and_head_current_cost_fuel(self):
        _, calm = self.phys.leg(AURORA, 1000, 16, 2000)
        _, rough = self.phys.leg(AURORA, 1000, 16, 2000, hs_m=5)
        h_with, _ = self.phys.leg(AURORA, 1000, 16, 2000, current_along_kn=2)
        h_against, f_against = self.phys.leg(AURORA, 1000, 16, 2000, current_along_kn=-2)
        self.assertGreater(rough, calm)
        self.assertGreater(f_against, calm)
        self.assertLess(h_with, h_against)


class RiskModelTests(unittest.TestCase):
    def test_weather_risk_rises_with_waves(self):
        self.assertLess(weather_risk(2, AURORA), 0.01)
        self.assertGreater(weather_risk(6.5, AURORA), 0.5)

    def test_news_bump_is_capped(self):
        rm = RiskModel(GRAPH.zones, news_hits={'bab': 1000})
        base = GRAPH.zones['bab']['security_base']
        self.assertAlmostEqual(rm.security(['bab']), base + 0.3, places=6)

    def test_noisy_or_and_exposure(self):
        rm = RiskModel(GRAPH.zones, traffic={'bab': 0.4})
        r, parts = rm.leg(1.0, AURORA, ['bab'])
        expected = 1 - (1 - parts['weather']) * (1 - parts['security']) * (1 - parts['traffic'])
        self.assertAlmostEqual(r, expected, places=9)
        self.assertAlmostEqual(parts['traffic'], 0.35 * 0.6, places=9)
        one_day = RiskModel.route_risk(RiskModel.exposure(0.2, 24))
        three_days = RiskModel.route_risk(RiskModel.exposure(0.2, 72))
        self.assertAlmostEqual(one_day, 0.2, places=9)
        self.assertAlmostEqual(three_days, 1 - 0.8 ** 3, places=9)


def plan(**kw):
    # Scenario tests run on the normal-times baseline so they don't depend on today's snapshot.
    req = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal'}
    req.update(kw)
    return plan_voyage(req)


class OptimizerTests(unittest.TestCase):
    def assert_plan_valid(self, result):
        ship = FLEET[result['ship']['id']]
        speeds = set(speed_options(ship))
        for p in [result['recommended']] + result['alternatives']:
            for c in p['checkpoints']:
                self.assertGreaterEqual(c['fuel_t'], ship['min_reserve_t'] - 1e-6, c)
                self.assertLessEqual(c.get('fuel_after_bunker_t', 0), ship['fuel_capacity_t'] + 1e-6)
            for leg in p['legs']:
                self.assertIn(leg['speed_kn'], speeds)
            for b in p['bunkering']:
                self.assertIn('fuel_price', GRAPH.ports[b['port']])
                self.assertLessEqual(ship['draft_m'], GRAPH.ports[b['port']]['max_draft_m'])
            c = p['costs']
            parts = (c['fuel_on_board_at_departure_usd'] + c['fuel_purchased_usd'] + c['fuel_left_credit_usd'] + c['eu_ets_usd'] + c['canal_tolls_usd'] + c['port_fees_usd']
                     + c['operating_usd'] + c['war_risk_insurance_usd'] + c['delay_penalty_usd'])
            self.assertLessEqual(abs(parts - c['cash_total_usd']), 10)

    def test_normal_conditions_offer_both_corridors(self):
        r = plan()
        self.assertEqual(r['status'], 'ok')
        self.assertEqual({p['corridor'] for p in [r['recommended']] + r['alternatives']}, {'suez', 'cape'})
        self.assert_plan_valid(r)

    def test_red_sea_closure_diverts_to_cape(self):
        r = plan(events=[{'type': 'zone_closure', 'zone': 'bab', 'source': 'test', 'timestamp': '2026-10-16T00:00:00Z'}])
        self.assertEqual(r['recommended']['corridor'], 'cape')
        self.assertNotIn('bab', r['recommended']['zones'])
        self.assertTrue(all('bab' not in p['zones'] for p in r['alternatives']))
        self.assertEqual(r['events'][0]['source'], 'test')
        self.assertEqual(r['events'][0]['impact'], 'affects this voyage')
        self.assertIn('change', r)
        self.assert_plan_valid(r)

    def test_hormuz_closure_from_gulf_is_infeasible(self):
        r = plan(origin='JEA', events=[{'type': 'zone_closure', 'zone': 'hormuz'}])
        self.assertEqual(r['status'], 'infeasible')
        self.assertEqual(r['infeasible']['kind'], 'closure')
        self.assertIn('hormuz', r['infeasible']['blocking_zones'])
        self.assertEqual(r['infeasible']['nearest_open_port'], 'FUJ')

    def test_low_fuel_forces_bunkering(self):
        r = plan(origin='SHA', fuel_onboard_t=900)
        self.assertEqual(r['status'], 'ok')
        self.assertTrue(r['recommended']['bunkering'])
        self.assert_plan_valid(r)

    def test_hard_deadline_respected_or_explained(self):
        r = plan(deadline_days=28)
        self.assertLessEqual(r['recommended']['summary']['days'], 28 + 1e-6)
        r = plan(deadline_days=10)
        self.assertEqual(r['status'], 'infeasible')
        self.assertEqual(r['infeasible']['kind'], 'deadline')
        self.assertGreater(r['infeasible']['fastest_days'], 10)

    def test_irrelevant_events_have_no_impact(self):
        r = plan(ship_id='tern', allow_uninsured=True, events=[   # Tern has no war cover; keep the Suez route this test is about
            {'type': 'port_closure', 'port': 'DKR'},
            {'type': 'storm', 'id': 'black-sea', 'center': [43, 35], 'radius_nm': 150, 'hs_m': 7}])
        impacts = {e['id']: e['impact'] for e in r['events']}
        self.assertEqual(impacts['black-sea'], 'no impact on any route considered')
        self.assertFalse(r['change']['route_changed'])

    def test_storm_above_limit_blocks_and_is_named(self):
        r = plan(events=[{'type': 'zone_closure', 'zone': 'bab'},
                         {'type': 'storm', 'id': 'cape-storm', 'center': [-35, 22], 'radius_nm': 300, 'hs_m': 7.5}])
        self.assertEqual(r['status'], 'infeasible')
        self.assertEqual(r['infeasible']['kind'], 'weather')
        self.assertIn('cape-storm', r['infeasible']['blocking_storms'])

    def test_draft_limit_blocks_destination(self):
        r = plan(destination='DKR')  # Dakar max draft 13 m, Aurora draws 15 m
        self.assertEqual(r['status'], 'infeasible')
        self.assertEqual(r['infeasible']['kind'], 'port')

    def test_bad_input_raises(self):
        with self.assertRaises(ValueError):
            plan_voyage({'ship_id': 'nope', 'origin': 'SIN', 'destination': 'RTM'})
        with self.assertRaises(ValueError):
            plan(events=[{'type': 'zone_closure', 'zone': 'atlantis'}])


class TodayConditionsTests(unittest.TestCase):
    def test_today_uses_sourced_prices_and_lists_sources(self):
        r = plan(conditions='today')
        items = r['conditions']['items']
        self.assertEqual(r['conditions']['mode'], 'today')
        self.assertTrue(any(i['kind'] == 'fuel_prices' for i in items))
        self.assertTrue(all(i.get('source') and i.get('date') for i in items))
        normal = plan()
        self.assertGreater(r['recommended']['costs']['cash_total_usd'], normal['recommended']['costs']['cash_total_usd'] * 0.9)

    def test_today_prefers_cape_for_red_sea_risk(self):
        today = plan(conditions='today')
        normal = plan()
        self.assertEqual(normal['recommended']['corridor'], 'suez')
        self.assertEqual(today['recommended']['corridor'], 'cape')
        suez = [p for p in today['alternatives'] if p['corridor'] == 'suez'][0]
        self.assertGreater(suez['costs']['war_risk_insurance_usd'], 0)

    def test_portwatch_snapshot_feeds_traffic(self):
        import portwatch
        portwatch.set_snapshot({'status': 'live', 'citation': 'test', 'url': 'u', 'latest_date': '2026-10-15',
                                'zones': {'bab': {'ratio': 1.0, 'recent_daily': 70, 'baseline_daily': 70}}})
        try:
            r = plan(conditions='today')
            tr = [i for i in r['conditions']['items'] if i['kind'] == 'traffic' and i['zone'] == 'bab'][0]
            self.assertTrue(tr['live'])
        finally:
            portwatch.set_snapshot(None)


if __name__ == '__main__':
    unittest.main()


class MidVoyageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.committed = plan()['recommended']   # Suez under normal times
        cls.route = [c['node'] for c in cls.committed['checkpoints']]

    def resume_at(self, leg, frac, day, fuel=2000, **kw):
        return plan(fuel_onboard_t=fuel, resume={'route': self.route, 'leg_index': leg, 'fraction': frac,
                                                 'elapsed_h': day * 24, 'fuel_onboard_t': fuel}, **kw)

    def test_no_event_stays_on_course(self):
        r = self.resume_at(1, 0.5, 8)
        self.assertEqual(r['resume']['recommendation'], 'stay')
        self.assertEqual(r['recommended']['checkpoints'][0]['node'], 'SHIP')
        self.assertGreater(r['recommended']['summary']['days'], 8)
        self.assertLess(r['recommended']['summary']['remaining_days'], r['recommended']['summary']['days'])

    def test_closure_ahead_turns_ship_around(self):
        r = self.resume_at(1, 0.8, 9, events=[{'type': 'zone_closure', 'zone': 'bab', 'source': 'test'}])
        rs = r['resume']
        self.assertEqual(rs['recommendation'], 'change_course')
        self.assertIsNone(rs['continue'])
        self.assertIn('Bab el-Mandeb', rs['reason'])
        nodes = [c['node'] for c in r['recommended']['checkpoints']]
        self.assertEqual(nodes[1], 'CMB')                 # turns back toward Colombo
        self.assertEqual(r['recommended']['corridor'], 'cape')
        self.assertEqual(r['events'][0]['impact'], 'affects this voyage')

    def test_premium_already_paid_is_not_charged_again(self):
        # Inside the Red Sea leg, the Bab el-Mandeb war-risk premium was paid on entry.
        i = self.route.index('bab')
        r = self.resume_at(i, 0.5, 15, conditions='today')
        self.assertEqual(r['recommended']['costs']['war_risk_insurance_usd'], 0)

    def test_bad_resume_rejected(self):
        with self.assertRaises(ValueError):
            plan(resume={'route': ['SIN', 'RTM'], 'leg_index': 0, 'fraction': 0.5, 'elapsed_h': 10, 'fuel_onboard_t': 1000})


class TweakTests(unittest.TestCase):
    def test_custom_weights_change_decision(self):
        low = plan(weights={'carbon_usd_per_t': 0, 'risk_loss_frac': 0.0, 'time_value_mult': 1})
        high = plan(weights={'carbon_usd_per_t': 300, 'risk_loss_frac': 0.0, 'time_value_mult': 1})
        self.assertEqual(high['preset']['label'], 'Custom')
        self.assertLess(high['recommended']['summary']['co2_t'], low['recommended']['summary']['co2_t'] + 1)
        self.assertGreater(high['recommended']['decision']['carbon_shadow_usd'], 0)
        with self.assertRaises(ValueError):
            plan(weights={'carbon_usd_per_t': -5})
        with self.assertRaises(ValueError):
            plan(weights={'banana': 1})

    def test_cargo_defaults_from_ship(self):
        self.assertEqual(plan(ship_id='meridian')['cargo']['id'], 'crude')
        self.assertEqual(plan(cargo='food')['cargo']['id'], 'food')
        with self.assertRaises(ValueError):
            plan(cargo='plutonium')

    def test_hazards_flag_zones_and_ports(self):
        r = plan(origin='JEA', conditions='today', events=[{'type': 'port_closure', 'port': 'DKR'}])
        zones = {z['id']: z['level'] for z in r['hazards']['zones']}
        ports = {p['port']: p for p in r['hazards']['ports']}
        self.assertEqual(zones['hormuz'], 'high')
        self.assertIn('JEA', ports)
        self.assertEqual(ports['DKR']['level'], 'severe')
        closed = plan(events=[{'type': 'zone_closure', 'zone': 'suez'}])
        self.assertEqual({z['id']: z['level'] for z in closed['hazards']['zones']}['suez'], 'closed')
