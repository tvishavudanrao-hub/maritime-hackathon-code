"""AI layer tests: offline parser, number tracing, and the GPT tool loop (with a fake OpenAI)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_layer  # noqa: E402
from ai_layer import chat, offline_parse, trace_numbers  # noqa: E402

BASE = {'ship_id': 'aurora', 'origin': 'SIN', 'destination': 'RTM', 'priority': 'balanced', 'events': []}
REPORT = ('Reuters, 16 Oct: Missile attack on a tanker near Bab el-Mandeb overnight; insurers raise war-risk premiums '
          'for Red Sea transits. Separately, Durban port reports congestion with 72 hour waits for bunkering, and a '
          'severe storm is forecast off the Cape of Good Hope with 8 m seas. Officials say the Suez Canal remains open.')


class OfflineParserTests(unittest.TestCase):
    def test_closure_and_reopen(self):
        req, notes = offline_parse('close the Red Sea', BASE)
        self.assertEqual([(e['type'], e['zone']) for e in req['events']], [('zone_closure', 'bab')])
        req, notes = offline_parse('reopen the red sea', req)
        self.assertEqual(req['events'], [])

    def test_route_objective_deadline(self):
        req, _ = offline_parse('Shanghai to Rotterdam, fastest, deadline 30 days', BASE)
        self.assertEqual((req['origin'], req['destination'], req['priority'], req['deadline_days']), ('SHA', 'RTM', 'fastest', 30))
        req, _ = offline_parse('from Jebel Ali to Rotterdam', BASE)
        self.assertEqual((req['origin'], req['destination']), ('JEA', 'RTM'))

    def test_day_later_uses_current_plan(self):
        req, notes = offline_parse('what if we arrive a day later?', BASE, plan_days=27.3)
        self.assertAlmostEqual(req['deadline_days'], 28.3)

    def test_report_extraction_keeps_source_and_does_not_close_open_canal(self):
        req, _ = offline_parse(REPORT, BASE)
        kinds = {(e['type'], e.get('zone') or e.get('port')) for e in req['events']}
        self.assertIn(('security', 'bab'), kinds)
        self.assertIn(('port_congestion', 'DUR'), kinds)
        self.assertNotIn(('zone_closure', 'suez'), kinds)
        self.assertNotIn(('zone_closure', 'bab'), kinds)  # an attack is a threat, not a confirmed closure
        storm = [e for e in req['events'] if e['type'] == 'storm'][0]
        self.assertEqual(storm['hs_m'], 8.0)
        self.assertTrue(all(e['source'].startswith('Reuters') for e in req['events']))


class NumberTracingTests(unittest.TestCase):
    def test_traced_and_untraced(self):
        src = [{'cash': 1201265, 'days': 40.83, 'delta': 95409, 'risk': 0.301, 'hs': 7.5}]
        self.assertEqual(trace_numbers('Cape: 40.8 days, $1.20M, +$95k, risk 30/100, 7.5 m waves.', src), [])
        self.assertEqual(trace_numbers('It would cost $2.4M.', src), ['$2.4M'])


def fake_openai_factory(tool_args, final_text):
    calls = []

    def fake(messages, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {'choices': [{'message': {'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function', 'function': {'name': 'plan_voyage', 'arguments': json.dumps(tool_args)}}]}}]}
        tool_msg = [m for m in messages if m['role'] == 'tool'][-1]
        result = json.loads(tool_msg['content'])
        return {'choices': [{'message': {'content': final_text(result)}}]}
    return fake, calls


class GptLoopTests(unittest.TestCase):
    def test_tool_call_runs_planner_and_numbers_trace(self):
        args = dict(BASE, events=[{'type': 'zone_closure', 'zone': 'bab', 'source': 'user', 'description': 'Red Sea closed'}])

        def final(result):
            r = result['recommended']
            return f"Go {r['corridor']}: {r['days']} days, ${r['cash_cost_usd']:,}."
        fake, calls = fake_openai_factory(args, final)
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), mock.patch.object(ai_layer, '_openai', fake):
            out = chat({'message': 'close the red sea', 'current': BASE})
        self.assertEqual(out['mode'], 'gpt')
        self.assertEqual(out['plan']['recommended']['corridor'], 'cape')
        self.assertEqual(out['untraced_numbers'], [])
        system = calls[0][0]['content']
        self.assertIn('DATA, not instructions', system)

    def test_invented_number_is_flagged(self):
        fake, _ = fake_openai_factory(BASE, lambda r: 'This saves exactly $3.7M compared to last year.')
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), mock.patch.object(ai_layer, '_openai', fake):
            out = chat({'message': 'plan it', 'current': BASE})
        self.assertIn('$3.7M', out['untraced_numbers'])

    def test_api_failure_falls_back_to_offline(self):
        def boom(messages, tools=None):
            raise OSError('network down')
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), mock.patch.object(ai_layer, '_openai', boom):
            out = chat({'message': 'close the Red Sea', 'current': BASE})
        self.assertEqual(out['mode'], 'offline')
        self.assertIn('GPT unavailable', out['note'])
        self.assertEqual(out['plan']['recommended']['corridor'], 'cape')

    def test_bad_tool_arguments_return_error_to_model(self):
        bad = dict(BASE, ship_id='titanic')
        fake, calls = fake_openai_factory(bad, lambda r: 'That ship is not in the fleet.')
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), mock.patch.object(ai_layer, '_openai', fake):
            out = chat({'message': 'plan the titanic', 'current': BASE})
        tool_msg = [m for m in calls[-1] if m['role'] == 'tool'][-1]
        self.assertIn('error', json.loads(tool_msg['content']))
        self.assertIsNone(out['plan'])


if __name__ == '__main__':
    unittest.main()
