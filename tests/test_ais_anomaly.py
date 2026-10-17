"""AIS anomaly rules and the chokepoint traffic log: possible stops, possible queues, possible going-dark, and traffic vs a rolling baseline."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ais_anomaly as A  # noqa: E402

NOW = 1_800_000_000.0
BOXES = {'Suez Canal': [[29.8, 32.2], [31.4, 32.7]], 'Bab el-Mandeb': [[11.5, 42.5], [13.5, 44.5]]}
PORTS = {'PSD': {'name': 'Port Said', 'loc': [31.26, 32.3]}, 'JEA': {'name': 'Jebel Ali', 'loc': [25.0, 55.0]}}
IN_BAB = (12.4, 43.6)          # inside the Bab el-Mandeb box, far from any port
NEAR_PORT_SAID = (31.30, 32.35)


def ship(mmsi, pos, still_min=None, seen_min=0, sog=0.0, name=None):
    s = {'mmsi': str(mmsi), 'lat': pos[0], 'lon': pos[1], 'sog': sog, 'seen': NOW - seen_min * 60, 'name': name}
    if still_min is not None:
        s['still_since'] = NOW - still_min * 60
    return s


def detect(ships, tracked=()):
    return A.detect(ships, NOW, BOXES, PORTS, tracked)


def kinds(found):
    return sorted(a['kind'] for a in found)


class StoppedVessel(unittest.TestCase):
    def test_a_vessel_stopped_in_a_chokepoint_for_long_enough_is_flagged(self):
        found = detect([ship(111111111, IN_BAB, still_min=45, name='TEST ONE')])
        self.assertEqual(kinds(found), ['stopped_vessel'])
        self.assertEqual(found[0]['severity'], 'info')
        self.assertEqual(found[0]['loc'], list(IN_BAB))
        self.assertIn('TEST ONE', found[0]['title'])

    def test_a_short_stop_is_not_flagged(self):
        self.assertEqual(detect([ship(111111111, IN_BAB, still_min=10)]), [])

    def test_a_moving_vessel_is_never_flagged(self):
        self.assertEqual(detect([ship(111111111, IN_BAB, sog=12.0)]), [])

    def test_a_vessel_waiting_near_a_port_is_not_flagged(self):
        self.assertEqual(detect([ship(111111111, NEAR_PORT_SAID, still_min=300)]), [])

    def test_a_vessel_outside_every_watched_area_is_not_flagged(self):
        self.assertEqual(detect([ship(111111111, (0.0, -30.0), still_min=300)]), [])

    def test_missing_fields_do_not_break_anything(self):
        self.assertEqual(detect([{'mmsi': '1'}, {'mmsi': '2', 'lat': 12.4, 'lon': 43.6}, {'mmsi': '3', 'lat': 12.4, 'lon': 43.6, 'sog': None}]), [])


class Cluster(unittest.TestCase):
    def stopped(self, n):
        return [ship(200000000 + i, (12.0 + 0.1 * i, 43.0 + 0.1 * i), still_min=60) for i in range(n)]

    def test_several_stopped_vessels_in_one_chokepoint_raise_one_queue_warning(self):
        found = detect(self.stopped(4))
        self.assertEqual(kinds(found), ['stopped_cluster'])
        self.assertEqual(found[0]['severity'], 'warning')
        self.assertIn('4', found[0]['title'])
        self.assertIn('Bab el-Mandeb', found[0]['title'])

    def test_a_few_stopped_vessels_are_reported_one_by_one(self):
        self.assertEqual(kinds(detect(self.stopped(3))), ['stopped_vessel'] * 3)

    def test_recent_stops_do_not_count_towards_a_cluster(self):
        ships = self.stopped(3) + [ship(299999999, (12.5, 43.5), still_min=5)]
        self.assertEqual(kinds(detect(ships)), ['stopped_vessel'] * 3)


class GoingDark(unittest.TestCase):
    def test_a_tracked_vessel_silent_in_a_covered_area_is_flagged(self):
        found = detect([ship(333333333, IN_BAB, seen_min=45, sog=10.0, name='TRACKED')], tracked={'333333333'})
        self.assertEqual(kinds(found), ['went_dark'])
        self.assertEqual(found[0]['severity'], 'warning')
        self.assertIn('TRACKED', found[0]['title'])

    def test_an_untracked_vessel_going_quiet_is_ordinary(self):
        self.assertEqual(detect([ship(333333333, IN_BAB, seen_min=45, sog=10.0)]), [])

    def test_a_tracked_vessel_silent_mid_ocean_is_not_flagged(self):
        self.assertEqual(detect([ship(333333333, (0.0, -30.0), seen_min=300, sog=10.0)], tracked={'333333333'}), [])

    def test_a_tracked_vessel_that_just_reported_is_fine(self):
        self.assertEqual(detect([ship(333333333, IN_BAB, seen_min=3, sog=10.0)], tracked={'333333333'}), [])

    def test_coastal_silence_near_a_port_counts_as_covered(self):
        near_jebel_ali = (25.2, 55.2)
        self.assertEqual(kinds(detect([ship(333333333, near_jebel_ali, seen_min=60, sog=10.0)], tracked={'333333333'})), ['went_dark'])


class Wording(unittest.TestCase):
    def everything(self):
        stopped = [ship(200000000 + i, (12.0 + 0.1 * i, 43.0), still_min=60) for i in range(4)]
        return detect(stopped + [ship(111111111, (30.5, 32.4), still_min=90), ship(333333333, IN_BAB, seen_min=45, sog=9.0)], tracked={'333333333'})

    def test_every_alert_says_possible_and_names_its_source(self):
        found = self.everything()
        self.assertTrue(found)
        for a in found:
            self.assertIn('possibl', (a['title'] + ' ' + a['detail']).lower(), a)
            self.assertIn('AISStream', a['source'])
            self.assertNotIn('confirmed', (a['title'] + a['detail']).lower().replace('not confirmed', ''))

    def test_ids_are_stable_between_calls_so_warnings_do_not_repeat(self):
        self.assertEqual([a['id'] for a in self.everything()], [a['id'] for a in self.everything()])
        self.assertEqual(len({a['id'] for a in self.everything()}), len(self.everything()))

    def test_alerts_carry_a_location_for_the_map(self):
        for a in self.everything():
            self.assertEqual(len(a['loc']), 2)


class Traffic(unittest.TestCase):
    HOUR = 3600.0

    def test_counts_distinct_vessels_per_window(self):
        log = A.TrafficLog(window_h=1)
        for m in ('1', '2', '2', '3'):
            log.record('Suez Canal', m, NOW)
        self.assertEqual(log.summary(NOW)['Suez Canal']['now'], 3)

    def test_a_quiet_hour_after_busy_ones_shows_as_a_low_ratio(self):
        log = A.TrafficLog(window_h=1)
        for h in range(1, 7):                      # six earlier hours with 10 vessels each
            for i in range(10):
                log.record('Suez Canal', f'm{h}-{i}', NOW - h * self.HOUR)
        for i in range(2):                          # this hour: 2 vessels
            log.record('Suez Canal', f'now{i}', NOW)
        s = log.summary(NOW)['Suez Canal']
        self.assertEqual((s['now'], s['baseline'], s['windows']), (2, 10.0, 6))
        self.assertAlmostEqual(s['ratio'], 0.2)

    def test_too_little_history_gives_no_ratio(self):
        log = A.TrafficLog(window_h=1)
        log.record('Suez Canal', '1', NOW)
        log.record('Suez Canal', '2', NOW - self.HOUR)
        s = log.summary(NOW)['Suez Canal']
        self.assertIsNone(s['ratio'])
        self.assertIn('history', s['note'].lower())

    def test_old_data_is_pruned(self):
        log = A.TrafficLog(window_h=1, keep_h=48)
        log.record('Suez Canal', 'old', NOW - 60 * self.HOUR)
        log.record('Suez Canal', 'new', NOW)
        self.assertEqual(sum(len(v) for v in log.buckets['Suez Canal'].values()), 1)

    def test_boxes_are_kept_apart(self):
        log = A.TrafficLog(window_h=1)
        log.record('Suez Canal', '1', NOW)
        log.record('Bab el-Mandeb', '2', NOW)
        log.record('Bab el-Mandeb', '3', NOW)
        s = log.summary(NOW)
        self.assertEqual((s['Suez Canal']['now'], s['Bab el-Mandeb']['now']), (1, 2))

    def test_the_summary_says_what_it_is_and_is_not(self):
        log = A.TrafficLog(window_h=1)
        log.record('Suez Canal', '1', NOW)
        s = log.summary(NOW)['Suez Canal']
        self.assertIn('AISStream', s['source'])
        self.assertIn('not a full count', s['caveat'].lower())


class TrackerIntegration(unittest.TestCase):
    """The live tracker keeps the stop timer and traffic log up to date and puts alerts in its snapshot (synthetic ships, no network)."""

    def setUp(self):
        import ais
        self.ais = ais
        self.t = ais.Tracker()

    def report(self, mmsi, lat, lon, sog):
        self.t.update(mmsi, {'mmsi': mmsi, 'lat': lat, 'lon': lon, 'sog': sog, 'name': 'TEST SHIP'})

    def test_a_stopped_ship_gets_a_timer_that_clears_when_it_moves(self):
        self.report('999000001', 12.4, 43.6, 0.0)
        self.assertIn('still_since', self.t.ships['999000001'])
        first = self.t.ships['999000001']['still_since']
        self.report('999000001', 12.4, 43.6, 0.1)
        self.assertEqual(self.t.ships['999000001']['still_since'], first)      # still stopped: the timer keeps its start
        self.report('999000001', 12.5, 43.7, 9.0)
        self.assertNotIn('still_since', self.t.ships['999000001'])

    def test_the_snapshot_carries_possible_alerts_and_the_traffic_note(self):
        self.report('999000001', 12.4, 43.6, 0.0)
        self.t.ships['999000001']['still_since'] -= 3600           # pretend it stopped an hour ago
        snap = self.t.snapshot()
        self.assertEqual([a['kind'] for a in snap['anomalies']], ['stopped_vessel'])
        self.assertIn('possibly', snap['anomalies'][0]['title'])
        self.assertIn('Bab el-Mandeb', snap['traffic'])
        self.assertEqual(snap['traffic']['Bab el-Mandeb']['now'], 1)

    def test_a_moving_ship_in_a_watched_area_counts_as_traffic_but_raises_no_alert(self):
        self.report('999000002', 12.4, 43.6, 14.0)
        snap = self.t.snapshot()
        self.assertEqual(snap['anomalies'], [])
        self.assertEqual(snap['traffic']['Bab el-Mandeb']['now'], 1)

    def test_a_tracked_ship_that_went_quiet_is_flagged(self):
        self.report('999000003', 12.4, 43.6, 12.0)
        self.t.tracked.add('999000003')
        self.t.ships['999000003']['seen'] -= 3600
        self.assertEqual([a['kind'] for a in self.t.snapshot()['anomalies']], ['went_dark'])


if __name__ == '__main__':
    unittest.main()
