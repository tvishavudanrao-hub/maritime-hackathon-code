"""Approval, monitoring, re-planning at sea, dispatch drafts, audit chain and sanctions screening."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['TIDELINE_RUNTIME_DIR'] = tempfile.mkdtemp(prefix='tideline-test-')

import ops  # noqa: E402
import sanctions  # noqa: E402

REQ = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal', 'live_weather': False}
SDN = '\n'.join([
    '36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ',
    '9001,"SHADOW TRADER","vessel","IRAN-EO13846",-0- ,"9HA1234","Crude Oil Tanker",-0- ,"81479","Panama","SOME CO","Vessel Registration Identification IMO 9187629; MMSI 352123456."',
    '9002,"MV TERN","vessel","SDGT",-0- ,-0- ,"Container Ship",-0- ,-0- ,-0- ,-0- ,"Vessel Registration Identification IMO 9000001."',
])


class OpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sanctions.load_text(SDN, 'test fixture', '2026-10-16')

    def test_sanctions_parsing_and_matching(self):
        self.assertEqual(sanctions.status()['vessels'], 2)
        self.assertEqual(sanctions.screen_vessel(imo='9187629')['status'], 'match')
        self.assertEqual(sanctions.screen_vessel(mmsi='352123456')['matches'][0]['name'], 'SHADOW TRADER')
        self.assertEqual(sanctions.screen_vessel(name='M/V Shadow Trader')['status'], 'possible match')
        self.assertEqual(sanctions.screen_vessel(name='MV Aurora')['status'], 'clear')

    def test_approve_requires_a_person_and_screens(self):
        with self.assertRaises(ValueError):
            ops.approve({'request': REQ})
        v = ops.approve({'request': REQ, 'approved_by': 'Test Operator', 'note': 'unit test'})
        self.assertEqual(v['status'], 'approved')
        self.assertEqual(v['sanctions']['status'], 'clear')
        # a name-only hit is flagged but does not block: MV Tern shares its name with a listed vessel
        t = ops.approve({'request': dict(REQ, ship_id='tern'), 'approved_by': 'Test Operator'})
        self.assertEqual(t['sanctions']['status'], 'possible match')

    def test_monitor_replans_at_sea_and_audit_chain(self):
        v = ops.approve({'request': REQ, 'approved_by': 'Ops', 'note': ''})
        self.assertEqual(v['approved_plan']['corridor'], 'suez')
        v = ops.act(v['id'], {'action': 'simulate_hours', 'hours': 96, 'actor': 'Ops'})
        lc = v['last_check']
        self.assertEqual(v['status'], 'underway')
        self.assertEqual(lc['position_source'], 'schedule')
        self.assertEqual(lc['recommendation'], 'stay')
        self.assertLess(abs(lc['eta_drift_h']), 12)
        v = ops.act(v['id'], {'action': 'add_event', 'event': {'type': 'zone_closure', 'zone': 'bab', 'description': 'Red Sea closed'}})
        self.assertEqual(v['last_check']['recommendation'], 'change_course')
        self.assertTrue(any(a['kind'] == 'change_course' for a in v['alerts']))
        v = ops.act(v['id'], {'action': 'accept_replan', 'actor': 'Ops'})
        self.assertEqual(v['revision'], 2)
        self.assertEqual(v['committed']['plan']['corridor'], 'cape')
        self.assertNotIn('SHIP', v['committed']['route'])
        v = ops.act(v['id'], {'action': 'simulate_hours', 'hours': 48})
        self.assertEqual(v['last_check']['recommendation'], 'stay')
        o = ops.orders(v['id'])
        self.assertIn('revision 2', o['orders_text'])
        self.assertIn('Cape', o['orders_text'])
        a = ops.audit_log(voyage_id=v['id'])
        self.assertTrue(a['verification']['ok'])
        kinds = [e['action'] for e in a['entries']]
        for k in ('approved', 'simulate_hours', 'add_event', 'alert', 'accept_replan'):
            self.assertIn(k, kinds)

    def test_tampering_is_detected(self):
        ops.approve({'request': REQ, 'approved_by': 'Ops'})
        f = ops._dir() / 'audit.jsonl'
        lines = f.read_text().splitlines()
        e = json.loads(lines[0])
        e['actor'] = 'Someone else'
        lines[0] = json.dumps(e)
        original = f.read_text()
        f.write_text('\n'.join(lines) + '\n')
        try:
            self.assertFalse(ops.verify_audit()['ok'])
        finally:
            f.write_text(original)

    def test_ais_position_drives_monitoring(self):
        import ais
        v = ops.approve({'request': REQ, 'approved_by': 'Ops'})
        # a fix on the route between Colombo and the Gulf of Aden, and one far off the route
        ais.TRACKER.update('111222333', {'mmsi': '111222333', 'name': 'TEST', 'lat': 9.0, 'lon': 66.0, 'sog': 12.0, 'cog': 290.0})
        v = ops.act(v['id'], {'action': 'link_ais', 'mmsi': '111222333'})
        lc = v['last_check']
        self.assertEqual(lc['position_source'], 'AIS')
        self.assertLess(lc['off_route_nm'], 150)
        self.assertEqual(lc['status'], 'ok')
        ais.TRACKER.update('111222333', {'mmsi': '111222333', 'lat': -20.0, 'lon': 60.0})
        v = ops.act(v['id'], {'action': 'check'})
        self.assertTrue(any(a['kind'] == 'off_route' for a in v['alerts']))
        self.assertEqual(v['last_check']['recommendation'], 'replan')
        ais.TRACKER.untrack_all()

    def test_noon_report_sets_fuel(self):
        v = ops.approve({'request': REQ, 'approved_by': 'Ops'})
        v = ops.act(v['id'], {'action': 'simulate_hours', 'hours': 72})
        v = ops.act(v['id'], {'action': 'noon_report', 'fuel_t': 1500})
        self.assertEqual(v['last_check']['fuel_source'], 'noon report')
        self.assertAlmostEqual(v['last_check']['fuel_t'], 1500, delta=5)
        with self.assertRaises(ValueError):
            ops.act(v['id'], {'action': 'noon_report', 'fuel_t': 99999})


if __name__ == '__main__':
    unittest.main()


class CopilotOpsToolTests(unittest.TestCase):
    def test_gpt_reports_event_to_approved_voyages(self):
        from unittest import mock
        import ai_layer
        v = ops.approve({'request': REQ, 'approved_by': 'Ops'})
        ops.act(v['id'], {'action': 'simulate_hours', 'hours': 96})
        state = {'n': 0}

        def fake(messages, tools=None):
            state['n'] += 1
            if state['n'] == 1:
                args = {'event': {'type': 'zone_closure', 'zone': 'bab', 'source': 'user', 'description': 'Red Sea closed'}, 'voyage_ids': [v['id']]}
                return {'choices': [{'message': {'content': None, 'tool_calls': [
                    {'id': 'c1', 'type': 'function', 'function': {'name': 'report_event_to_voyages', 'arguments': json.dumps(args)}}]}}]}
            out = json.loads(messages[-1]['content'])
            return {'choices': [{'message': {'content': f"{out['updated'][0]['ship']}: {out['updated'][0]['recommendation']}"}}]}

        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), mock.patch.object(ai_layer, '_openai', fake):
            r = ai_layer.chat({'message': 'The Red Sea is closed, check my ships', 'current': REQ})
        self.assertIn('change_course', r['reply'])
        self.assertTrue(any(e['action'] == 'add_event' and 'AI' in e['actor'] for e in ops.audit_log(voyage_id=v['id'])['entries']))

    def test_offline_voyage_status(self):
        import ai_layer
        ops.approve({'request': REQ, 'approved_by': 'Ops'})
        r = ai_layer.chat({'message': 'what is the status of my voyages?', 'current': REQ})
        self.assertIn('MV Aurora', r['reply'])
