import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TIDELINE_RUNTIME_DIR', tempfile.mkdtemp(prefix='tideline-test-'))
import calibration  # noqa: E402
from assign import assign  # noqa: E402
from fuel_model import get_fuel_model  # noqa: E402
from optimizer import FLEET, FLEET_LIST  # noqa: E402


class AssignTests(unittest.TestCase):
    def test_assignment_respects_compatibility_and_beats_rule_of_thumb(self):
        r = assign({'conditions': 'normal', 'live_weather': False,
                    'jobs': [{'origin': 'SHA', 'destination': 'RTM', 'cargo': 'retail'}, {'origin': 'JEA', 'destination': 'SIN', 'cargo': 'crude'}],
                    'ships': [{'ship_id': 'aurora', 'at': 'SIN'}, {'ship_id': 'borealis', 'at': 'SHA'}, {'ship_id': 'meridian', 'at': 'JEA'}]})
        got = {a['job']: a['ship_id'] for a in r['assignment']}
        self.assertEqual(got['J2'], 'meridian')            # only the tanker can carry crude
        self.assertEqual(got['J1'], 'borealis')            # already in Shanghai: no positioning
        self.assertGreaterEqual(r['saving_vs_rule_of_thumb_usd'], 0)
        self.assertFalse(next(c for c in r['matrix'] if c['ship_id'] == 'aurora' and c['job'] == 'J2')['feasible'])

    def test_validation(self):
        with self.assertRaises(ValueError):
            assign({'jobs': [], 'ships': ['aurora']})
        with self.assertRaises(ValueError):
            assign({'jobs': [{'origin': 'SIN', 'destination': 'SIN'}], 'ships': ['aurora']})


class CalibrationTests(unittest.TestCase):
    def tearDown(self):
        calibration.calibrate({'ship_id': 'aurora', 'reset': True})

    def test_noon_reports_improve_accuracy_and_change_predictions(self):
        m, ship = get_fuel_model(FLEET_LIST), FLEET['aurora']
        before = m.leg(ship, 1000, 14, 2500, 2.0, 0, 0)[1]
        r = calibration.calibrate({'ship_id': 'aurora', 'csv': calibration.sample_csv('aurora')})['model']
        self.assertLess(r['holdout_error_after'], r['holdout_error_before'])
        self.assertAlmostEqual(r['fuel_vs_prior'], 1.12, delta=0.04)
        after = m.leg(ship, 1000, 14, 2500, 2.0, 0, 0)[1]
        self.assertAlmostEqual(after / before, 1.12, delta=0.05)
        self.assertAlmostEqual(r['coefficients']['ln_speed'], 2.0, delta=0.15)

    def test_bad_files_rejected_and_constant_terms_keep_prior(self):
        with self.assertRaises(ValueError):
            calibration.calibrate({'ship_id': 'aurora', 'csv': 'a,b\n1,2\n'})
        rows = calibration.sample_csv('aurora').splitlines()
        hdr = rows[0].split(',')
        i = hdr.index('displacement_t')
        fixed = [rows[0]] + [','.join(c if k != i else '150000' for k, c in enumerate(r.split(','))) for r in rows[1:]]
        r = calibration.calibrate({'ship_id': 'aurora', 'csv': '\n'.join(fixed)})['model']
        self.assertIn('ln_displacement', r['kept_prior'])


if __name__ == '__main__':
    unittest.main()
