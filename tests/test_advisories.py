import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import advisories  # noqa: E402
from optimizer import plan_voyage  # noqa: E402

SAMPLE = {'broadcast-warn': [
    {'msgYear': 2026, 'msgNumber': 1234, 'navArea': 'P', 'issueDate': '151200Z OCT 2026', 'authority': 'TEST',
     'text': 'RED SEA. SOUTHERN RED SEA AND BAB EL MANDEB. MISSILE AND UAV ATTACKS ON MERCHANT VESSELS REPORTED. EXERCISE CAUTION.'},
    {'msgYear': 2026, 'msgNumber': 1300, 'navArea': 'A', 'issueDate': '141000Z OCT 2026', 'authority': 'TEST',
     'text': 'NORTH SEA. NETHERLANDS. LIGHTED BUOY UNLIT.'},
    {'msgYear': 2026, 'msgNumber': 1301, 'navArea': 'P', 'issueDate': '141000Z OCT 2026', 'authority': 'TEST',
     'text': 'GULF OF OMAN. GNSS INTERFERENCE REPORTED IN VICINITY OF STRAIT OF HORMUZ.'},
]}


class AdvisoryTests(unittest.TestCase):
    def tearDown(self):
        advisories.set_warnings([], status='not loaded')

    def test_parse_keeps_security_warnings_on_network_zones(self):
        ws = advisories.parse(SAMPLE)
        self.assertEqual([w['id'] for w in ws], ['HYDROPAC 1234/26', 'HYDROPAC 1301/26'])
        self.assertEqual(ws[0]['zones'], ['bab'])
        self.assertIn('hormuz', ws[1]['zones'])

    def test_warnings_feed_today_conditions_with_source(self):
        base = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'today', 'live_weather': False}
        before = plan_voyage(base)
        advisories.set_warnings(advisories.parse(SAMPLE))
        after = plan_voyage(base)
        items = [i for i in after['conditions']['items'] if i['kind'] == 'advisory']
        self.assertTrue(items and items[0]['verified'])
        bab = next(z for z in after['hazards']['zones'] if z['id'] == 'bab')
        self.assertTrue(any('NGA' in x for x in bab['reasons']))
        self.assertNotIn('advisory', [i['kind'] for i in plan_voyage(dict(base, conditions='normal'))['conditions']['items']])
        self.assertTrue(before['status'] == after['status'] == 'ok')


if __name__ == '__main__':
    unittest.main()
