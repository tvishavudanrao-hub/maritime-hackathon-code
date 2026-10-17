import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import insurance  # noqa: E402
from optimizer import plan_voyage  # noqa: E402

B = {'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal', 'live_weather': False}
rules = lambda p: [f['rule'] for f in p['compliance']]


class InsuranceTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(insurance.zone_status('aurora', 'bab')[0], insurance.ADDITIONAL)
        self.assertEqual(insurance.zone_status('aurora', 'gulf_guinea')[0], insurance.HELD)
        self.assertEqual(insurance.zone_status('tern', 'bab')[0], insurance.NOT_COVERED)
        self.assertEqual(insurance.zone_status('aurora', 'bab', {'bab'})[0], insurance.NOT_COVERED)
        self.assertEqual(insurance.zone_status('aurora', 'malacca')[0], insurance.COVERED)

    def test_ship_without_war_cover_avoids_listed_areas_unless_allowed(self):
        p = plan_voyage(dict(B, ship_id='tern'))['recommended']
        self.assertEqual(p['corridor'], 'cape')
        self.assertIn('Insurance: route restriction', rules(p))
        p = plan_voyage(dict(B, ship_id='tern', allow_uninsured=True))['recommended']
        self.assertEqual(p['corridor'], 'suez')
        self.assertIn('Insurance: NOT COVERED', rules(p))

    def test_cancelled_cover_reroutes_and_is_reported(self):
        r = plan_voyage(dict(B, ship_id='aurora', events=[{'type': 'cover_cancelled', 'zone': 'bab', 'description': 'cover cancelled'}]))
        self.assertEqual(r['recommended']['corridor'], 'cape')
        self.assertEqual(r['events'][0]['impact'], 'affects this voyage')
        bab = next(z for z in r['hazards']['zones'] if z['id'] == 'bab')
        self.assertIn("not covered by the ship's insurance", bab['reasons'])

    def test_held_covered_premium_is_charged(self):
        base = plan_voyage(dict(B, ship_id='borealis', origin='LOS'))['recommended']['costs']['war_risk_insurance_usd']
        held = plan_voyage(dict(B, ship_id='aurora', origin='LOS'))['recommended']
        self.assertIn('Insurance: held covered (warranty breach)', rules(held))
        self.assertGreater(held['costs']['war_risk_insurance_usd'], 0)
        self.assertGreaterEqual(base, 0)

    def test_infeasible_message_names_insurance(self):
        r = plan_voyage(dict(B, ship_id='tern', origin='JEA', destination='SIN'))
        self.assertEqual(r['status'], 'infeasible')
        self.assertIn('insurance', r['infeasible']['message'])


if __name__ == '__main__':
    unittest.main()
