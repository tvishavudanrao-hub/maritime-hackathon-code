"""Plan cache: same request -> same answer without re-planning, one plan at a time, no stale-forever entries."""
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plan_cache import PlanCache, plan_key  # noqa: E402

REQ = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'conditions': 'today', 'live_weather': True,
       'events': [{'type': 'zone_closure', 'zone': 'suez', 'id': 'suez-closed', 'timestamp': '2026-10-17T20:40:50.195Z'}]}
T0 = datetime(2026, 10, 17, 20, 40, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Keys(unittest.TestCase):
    def test_timestamps_and_key_order_do_not_matter(self):
        other = {'live_weather': True, 'conditions': 'today', 'destination': 'RTM', 'origin': 'SIN', 'ship_id': 'aurora',
                 'events': [{'timestamp': 'a different time', 'id': 'suez-closed', 'zone': 'suez', 'type': 'zone_closure'}]}
        self.assertEqual(plan_key(REQ, T0), plan_key(other, T0))

    def test_missing_and_empty_events_are_the_same_request(self):
        a = {k: v for k, v in REQ.items() if k != 'events'}
        self.assertEqual(plan_key(a, T0), plan_key(dict(a, events=[]), T0))

    def test_different_requests_get_different_keys(self):
        base = plan_key(REQ, T0)
        self.assertNotEqual(base, plan_key(dict(REQ, ship_id='meridian'), T0))
        self.assertNotEqual(base, plan_key(dict(REQ, events=[]), T0))
        self.assertNotEqual(base, plan_key(dict(REQ, events=[dict(REQ['events'][0], zone='bab')]), T0))
        self.assertNotEqual(base, plan_key(dict(REQ, live_weather=False), T0))

    def test_without_a_departure_the_key_changes_each_hour(self):
        later = datetime(2026, 10, 17, 21, 5, tzinfo=timezone.utc)
        self.assertNotEqual(plan_key(REQ, T0), plan_key(REQ, later))
        self.assertEqual(plan_key(REQ, T0), plan_key(REQ, T0.replace(minute=59)))

    def test_with_a_departure_the_key_does_not_depend_on_the_hour(self):
        dep = dict(REQ, departure='2026-10-20T00:00:00Z')
        later = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(plan_key(dep, T0), plan_key(dep, later))


class Caching(unittest.TestCase):
    def test_second_call_is_a_hit_and_does_not_recompute(self):
        cache, calls = PlanCache(), []
        value, hit = cache.get_or_compute('k', lambda: calls.append(1) or {'x': 1})
        again, hit2 = cache.get_or_compute('k', lambda: calls.append(1) or {'x': 2})
        self.assertEqual((value, hit, again, hit2, len(calls)), ({'x': 1}, False, {'x': 1}, True, 1))

    def test_entries_expire(self):
        clock = FakeClock()
        cache = PlanCache(ttl_s=60, clock=clock)
        cache.get_or_compute('k', lambda: 1)
        clock.t = 59
        self.assertTrue(cache.get_or_compute('k', lambda: 2)[1])
        clock.t = 61
        value, hit = cache.get_or_compute('k', lambda: 3)
        self.assertEqual((value, hit), (3, False))

    def test_least_recently_used_entry_is_evicted(self):
        cache = PlanCache(max_entries=2)
        cache.get_or_compute('a', lambda: 1)
        cache.get_or_compute('b', lambda: 2)
        cache.get_or_compute('a', lambda: 9)          # touch a, so b is now the oldest
        cache.get_or_compute('c', lambda: 3)
        self.assertTrue(cache.get_or_compute('a', lambda: 9)[1])
        self.assertFalse(cache.get_or_compute('b', lambda: 9)[1])

    def test_errors_are_not_cached(self):
        cache, n = PlanCache(), [0]

        def flaky():
            n[0] += 1
            if n[0] == 1:
                raise ValueError('boom')
            return 'ok'
        with self.assertRaises(ValueError):
            cache.get_or_compute('k', flaky)
        self.assertEqual(cache.get_or_compute('k', flaky), ('ok', False))

    def test_copy_option_stops_callers_changing_the_cached_value(self):
        cache = PlanCache(copy_result=True)
        first, _ = cache.get_or_compute('k', lambda: {'plans': [1, 2]})
        first['plans'].append('mutated')
        second, hit = cache.get_or_compute('k', lambda: {'plans': 'recomputed'})
        self.assertTrue(hit)
        self.assertEqual(second, {'plans': [1, 2]})


class Concurrency(unittest.TestCase):
    def test_same_key_computed_once_while_others_wait(self):
        cache, calls = PlanCache(), []

        def slow():
            calls.append(1)
            time.sleep(0.3)
            return 'v'
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get_or_compute('k', slow))) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(calls), 1)
        self.assertEqual({v for v, _ in results}, {'v'})
        self.assertEqual(sum(1 for _, hit in results if not hit), 1)   # exactly one caller did the work

    def run_parallel(self, slots, n=4):
        cache, state = PlanCache(slots=slots), {'now': 0, 'max': 0}
        lock = threading.Lock()

        def work():
            with lock:
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
            time.sleep(0.15)
            with lock:
                state['now'] -= 1
            return 1
        threads = [threading.Thread(target=cache.get_or_compute, args=(f'k{i}', work)) for i in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return state['max']

    def test_only_one_plan_is_computed_at_a_time_by_default(self):
        self.assertEqual(self.run_parallel(slots=1), 1)

    def test_slots_can_be_raised(self):
        self.assertEqual(self.run_parallel(slots=2), 2)

    def test_exclusive_shares_the_same_slots(self):
        cache, state = PlanCache(slots=1), {'now': 0, 'max': 0}
        lock = threading.Lock()

        def work():
            with lock:
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
            time.sleep(0.1)
            with lock:
                state['now'] -= 1
        threads = [threading.Thread(target=cache.exclusive, args=(work,)) for _ in range(3)]
        threads += [threading.Thread(target=cache.get_or_compute, args=('k', lambda: work() or 1))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(state['max'], 1)

    def test_a_failing_leader_does_not_leave_waiters_hanging(self):
        cache, n, out = PlanCache(), [0], []

        def flaky():
            n[0] += 1
            time.sleep(0.2)
            if n[0] == 1:
                raise ValueError('boom')
            return 'ok'

        def call():
            try:
                out.append(cache.get_or_compute('k', flaky)[0])
            except ValueError:
                out.append('error')
        threads = [threading.Thread(target=call) for _ in range(3)]
        [t.start() for t in threads]
        [t.join(timeout=5) for t in threads]
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(out), 3)
        self.assertIn('ok', out)


if __name__ == '__main__':
    unittest.main()
