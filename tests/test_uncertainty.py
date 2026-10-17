import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from optimizer import plan_voyage  # noqa: E402

BASE = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal', 'live_weather': False}


class UncertaintyTests(unittest.TestCase):
    def test_ranges_bracket_the_plan_and_are_reproducible(self):
        r = plan_voyage(BASE)
        u, s = r['uncertainty'], r['recommended']['summary']
        self.assertLessEqual(u['days']['p10'], u['days']['p50'])
        self.assertLessEqual(u['days']['p50'], u['days']['p90'])
        self.assertLess(u['fuel_burned_t']['p10'], s['fuel_burned_t'])
        self.assertGreater(u['fuel_burned_t']['p90'], s['fuel_burned_t'])
        self.assertEqual(plan_voyage(BASE)['uncertainty'], u)   # same inputs, same numbers
        self.assertIsNone(u['prob_late'])

    def test_tight_deadline_reports_risk_of_lateness(self):
        r = plan_voyage(dict(BASE, deadline_days=29.3))
        self.assertIsNotNone(r['uncertainty']['prob_late'])

    def test_storm_raises_hold_risk(self):
        calm = plan_voyage(dict(BASE, avoid_zones=['suez']))['uncertainty']['prob_weather_hold']
        storm = plan_voyage(dict(BASE, avoid_zones=['suez'], events=[{'type': 'storm', 'id': 's', 'center': [-35, 22], 'radius_nm': 300, 'hs_m': 6.0, 'wind_ms': 20}]))
        self.assertGreater(storm['uncertainty']['prob_weather_hold'], calm)


if __name__ == '__main__':
    unittest.main()
