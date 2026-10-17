"""Just-in-time arrival: slow down for a late berth instead of waiting at anchorage."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import plan_voyage  # noqa: E402

BASE = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'normal', 'live_weather': False}


class JitTests(unittest.TestCase):
    def test_destination_queue_slows_the_ship(self):
        r = plan_voyage(dict(BASE, priority='fastest', events=[{'type': 'port_congestion', 'port': 'RTM', 'wait_h': 96}]))
        j = r['jit']
        self.assertEqual(j['source'], 'congestion')
        self.assertGreater(j['fuel_saved_t'], 50)
        self.assertGreater(j['cash_saved_usd'], 0)
        self.assertLess(j['just_in_time']['anchorage_h'], j['sail_then_wait']['anchorage_h'])
        self.assertEqual(r['recommended']['summary']['anchorage_hours'], j['just_in_time']['anchorage_h'])
        # the queue event is reported as affecting this voyage
        self.assertTrue(any(e['type'] == 'port_congestion' and e['impact'] == 'affects this voyage' for e in r['events']))

    def test_never_worse_than_sailing_and_waiting(self):
        for cond in ('normal', 'today'):
            j = plan_voyage(dict(BASE, conditions=cond, events=[{'type': 'port_congestion', 'port': 'RTM', 'wait_h': 72}]))['jit']
            self.assertGreaterEqual(j['cash_saved_usd'], 0)

    def test_berth_before_possible_arrival(self):
        r = plan_voyage(dict(BASE, berth_ready_days=5))
        self.assertIn('before the ship can arrive', r['jit']['note'])
        self.assertEqual(r['recommended']['summary']['anchorage_hours'], 0)

    def test_no_jit_without_berth_or_queue(self):
        self.assertNotIn('jit', plan_voyage(BASE))
        with self.assertRaises(ValueError):
            plan_voyage(dict(BASE, berth_ready_days=-3))


if __name__ == '__main__':
    unittest.main()
