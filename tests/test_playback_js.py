"""Runs the browser maths tests (tests/playback.test.js, tests/mapview.test.js) with Node, if Node is installed."""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
class PlaybackJs(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(['node', '--test', os.path.join(HERE, 'playback.test.js'), os.path.join(HERE, 'mapview.test.js')],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
