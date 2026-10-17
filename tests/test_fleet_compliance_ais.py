"""Fleet planning, compliance / EU ETS, planning from a live position, and the AIS client."""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ais  # noqa: E402
import compliance  # noqa: E402
import fake_aisstream  # noqa: E402
from fleet import fleet_plan  # noqa: E402
from optimizer import plan_voyage  # noqa: E402

BASE = {'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal'}


class ComplianceTests(unittest.TestCase):
    def test_eu_ets_cost_included_for_rotterdam(self):
        r = plan_voyage(dict(BASE, ship_id='aurora'))
        p = r['recommended']
        expected = 0.5 * p['summary']['co2_t'] * compliance.ETS_USD_PER_T
        self.assertAlmostEqual(p['costs']['eu_ets_usd'], expected, delta=expected * 0.01 + 5)
        kinds = {f['rule'] for f in p['compliance']}
        self.assertIn('EU Emissions Trading System', kinds)
        self.assertIn('Tariffs and customs', kinds)

    def test_no_ets_outside_eu(self):
        r = plan_voyage(dict(BASE, ship_id='aurora', destination='LPA'))
        self.assertEqual(r['recommended']['costs']['eu_ets_usd'], 0)

    def test_jwc_warning_on_red_sea_route(self):
        p = plan_voyage(dict(BASE, ship_id='aurora'))['recommended']
        self.assertEqual(p['corridor'], 'suez')
        self.assertTrue(any(f['rule'] == 'JWC Listed Area' and f['kind'] == 'warn' for f in p['compliance']))

    def test_saudi_linked_avoids_red_sea_and_iran_linked_cannot_leave_gulf(self):
        r = plan_voyage(dict(BASE, ship_id='aurora', affiliations=['saudi']))
        self.assertEqual(r['recommended']['corridor'], 'cape')
        self.assertTrue(any('Saudi' in f['rule'] for f in r['recommended']['compliance']))
        r = plan_voyage(dict(BASE, ship_id='meridian', origin='JEA', affiliations=['iran']))
        self.assertEqual(r['status'], 'infeasible')
        with self.assertRaises(ValueError):
            plan_voyage(dict(BASE, ship_id='aurora', affiliations=['atlantis']))


class StartAtTests(unittest.TestCase):
    def test_plan_from_live_position(self):
        r = plan_voyage(dict(BASE, ship_id='aurora', start_at={'lat': 12.8, 'lon': 48.0, 'label': 'TEST'}))
        cp = r['recommended']['checkpoints']
        self.assertEqual(cp[0]['name'], 'TEST')
        self.assertEqual(r['recommended']['corridor'], 'suez')

    def test_ship_inside_closed_zone_can_leave(self):
        r = plan_voyage(dict(BASE, ship_id='aurora', start_at={'lat': 12.8, 'lon': 48.0},
                             events=[{'type': 'zone_closure', 'zone': 'bab'}]))
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['recommended']['corridor'], 'cape')

    def test_far_from_network_rejected(self):
        with self.assertRaises(ValueError):
            plan_voyage(dict(BASE, ship_id='aurora', start_at={'lat': 60, 'lon': -150}))


class FleetTests(unittest.TestCase):
    def test_fleet_coordinates_refueling_queue(self):
        ships = [{'ship_id': s, 'fuel_onboard_t': f} for s, f in [('aurora', 900), ('borealis', 700), ('tern', 400)]]
        r = fleet_plan(dict(BASE, origin='SHA', ships=ships))
        self.assertEqual(r['totals']['ships'], 3)
        self.assertEqual(r['totals']['feasible'], 3)
        total = sum(s['result']['recommended']['costs']['cash_total_usd'] for s in r['ships'])
        self.assertAlmostEqual(r['totals']['cash_usd'], total, delta=5)
        # someone queued at Shanghai behind the first ship
        self.assertTrue(r['coordination'])
        queued = [s for s in r['ships'] if s['coordination_events']]
        self.assertTrue(queued)

    def test_fleet_validation(self):
        with self.assertRaises(ValueError):
            fleet_plan(dict(BASE, ships=[]))
        with self.assertRaises(ValueError):
            fleet_plan(dict(BASE, ships=['aurora', 'aurora']))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class AisTests(unittest.TestCase):
    def test_parse_position_report(self):
        raw = ('{"MessageType":"PositionReport","MetaData":{"MMSI":123456789,"ShipName":"TEST  ","latitude":12.5,"longitude":43.4,'
               '"time_utc":"2026-10-16 20:00:00"},"Message":{"PositionReport":{"Cog":320.5,"Sog":13.2,"TrueHeading":321}}}')
        mmsi, rec = ais.parse_message(raw)
        self.assertEqual(mmsi, '123456789')
        self.assertEqual(rec['name'], 'TEST')
        self.assertEqual((rec['lat'], rec['sog'], rec['heading']), (12.5, 13.2, 321))
        with self.assertRaises(ValueError):
            ais.parse_message('{"error":"Api Key Is Not Valid"}')

    def test_websocket_client_against_fake_stream(self):
        port, received = free_port(), []
        t = threading.Thread(target=fake_aisstream.serve, args=(port, 6, 0.05, received), daemon=True)
        t.start()
        time.sleep(0.2)
        tracker = ais.Tracker()
        ws = ais.WebSocket(f'ws://127.0.0.1:{port}')
        ws.send_text('{"APIKey":"test","BoundingBoxes":[[[11,42],[14,45]]]}')
        for _ in range(12):
            parsed = ais.parse_message(ws.recv())
            if parsed:
                tracker.update(*parsed)
        ws.close()
        snap = tracker.snapshot()
        self.assertEqual(received[0]['APIKey'], 'test')
        self.assertEqual(len(snap['ships']), 3)
        alpha = [s for s in snap['ships'] if s['name'] == 'TEST VESSEL ALPHA'][0]
        self.assertGreaterEqual(len(alpha['trail']), 2)

    def test_track_validation_and_subscription_modes(self):
        tr = ais.Tracker()
        self.assertIn('BoundingBoxes', tr.subscription('k'))
        self.assertNotIn('FiltersShipMMSI', tr.subscription('k'))
        tr.track('123456789')
        self.assertEqual(tr.subscription('k')['FiltersShipMMSI'], ['123456789'])
        with self.assertRaises(ValueError):
            tr.track('12ab')


if __name__ == '__main__':
    unittest.main()
