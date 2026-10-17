"""Live weather tests with synthetic Open-Meteo responses (no network)."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_weather  # noqa: E402
from conditions import LIVE_SOURCE, Conditions, seasonal_hs  # noqa: E402
from optimizer import plan_voyage  # noqa: E402

HOURS = 168


def field(hs_fn=None, cur_kmh=1.852, cur_to=90.0, wind_ms=8.0, wind_from=270.0):
    t0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp()

    def marine(points):
        return [{'t0': t0, 'hs': [hs_fn(lat, lon, k) if hs_fn else 2.0 for k in range(HOURS)],
                 'cur_kn': [cur_kmh / 1.852] * HOURS, 'cur_to': [cur_to] * HOURS} for lat, lon in points]

    def wind(points):
        return [{'t0': t0, 'ws': [wind_ms] * HOURS, 'wd': [wind_from] * HOURS} for _ in points]
    return live_weather.fetch_field(fetch_marine=marine, fetch_wind=wind)


class LiveWeatherTests(unittest.TestCase):
    def tearDown(self):
        live_weather.set_field(None)

    def test_sample_points_cover_network(self):
        pts = live_weather.sample_points()
        self.assertGreater(len(pts), 50)
        self.assertLess(len(pts), 200)

    def test_marine_chunk_converts_current_units(self):
        item = {'hourly_units': {'ocean_current_velocity': 'km/h'},
                'hourly': {'time': ['2026-10-16T00:00'], 'wave_height': [2.5], 'ocean_current_velocity': [3.704],
                           'ocean_current_direction': [180]}}
        orig = live_weather._fetch_json
        live_weather._fetch_json = lambda url, timeout=20: [item]
        try:
            out = live_weather._marine_chunk([(0, 0)])
        finally:
            live_weather._fetch_json = orig
        self.assertAlmostEqual(out[0]['cur_kn'][0], 2.0, places=3)
        self.assertEqual(out[0]['hs'][0], 2.5)

    def test_conditions_live_inside_horizon_seasonal_after(self):
        f = field(lambda lat, lon, k: 3.3)
        dep = datetime.now(timezone.utc).timestamp()
        c = Conditions([], f, dep)
        near = c.at(5.0, 88.0, 10)
        later = c.at(5.0, 88.0, 400)
        self.assertEqual(near['source'], LIVE_SOURCE)
        self.assertAlmostEqual(near['hs'], 3.3)
        self.assertAlmostEqual(near['current_kn'], 1.0)
        self.assertEqual(later['source'], 'seasonal sample')
        self.assertAlmostEqual(later['hs'], seasonal_hs(5.0, 88.0))

    def test_plan_uses_live_and_can_be_switched_off(self):
        live_weather.set_field(field())
        r = plan_voyage({'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM'})
        self.assertTrue(r['weather']['used'])
        self.assertGreater(r['recommended']['summary']['live_weather_legs'], 0)
        off = plan_voyage({'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'live_weather': False})
        self.assertFalse(off['weather']['used'])
        self.assertEqual(off['recommended']['summary']['live_weather_legs'], 0)

    def test_head_wind_costs_fuel(self):
        live_weather.set_field(field(wind_ms=0.0))
        calm = plan_voyage({'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'priority': 'cheapest'})
        live_weather.set_field(field(wind_ms=15.0, wind_from=270.0))  # westbound ship into a west wind
        windy = plan_voyage({'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'priority': 'cheapest'})
        self.assertGreater(windy['recommended']['costs']['cash_total_usd'], calm['recommended']['costs']['cash_total_usd'])

    def test_forecast_storm_above_limit_is_explained(self):
        live_weather.set_field(field(lambda lat, lon, k: 9.0 if lon > 70 else 2.0))
        r = plan_voyage({'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM'})
        self.assertEqual(r['status'], 'infeasible')
        self.assertEqual(r['infeasible']['kind'], 'weather')
        self.assertTrue(r['infeasible'].get('live_forecast'))


if __name__ == '__main__':
    unittest.main()
