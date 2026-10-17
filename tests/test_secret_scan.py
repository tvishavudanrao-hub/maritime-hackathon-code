"""The pre-push secret scan: finds key-like strings and forbidden files, stays quiet on placeholders, never prints a whole secret.

Fake secrets are assembled at run time so this file contains nothing the scanner itself would flag.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

import secret_scan  # noqa: E402

OPENAI = 'sk-' + 'a1B2c3D4' * 4
AWS = 'AKIA' + 'ABCDEFGHIJ123456'
GITHUB = 'ghp_' + 'Zz9' * 13
AIZA = 'AIza' + 'Sy' + 'x' * 33
PRIVATE = '-----BEGIN ' + 'RSA PRIVATE KEY-----'
REAL_VALUE = 'f3a9' * 8


class TextRules(unittest.TestCase):
    def test_provider_key_formats_are_found(self):
        for label, text in (('openai', f'key = "{OPENAI}"'), ('aws', f'id: {AWS}'), ('github', f'token {GITHUB}'),
                            ('google', f'k={AIZA}'), ('private key', PRIVATE)):
            with self.subTest(label):
                self.assertTrue(secret_scan.scan_text('x.py', text), label)

    def test_named_secret_assignments_with_real_looking_values_are_found(self):
        for text in (f'AISSTREAM_API_KEY = "{REAL_VALUE}"', f"api_key: '{REAL_VALUE}'", f'OPENAI_API_KEY={REAL_VALUE}',
                     f'"password": "{REAL_VALUE}"', f'SECRET_TOKEN = "{REAL_VALUE}"'):
            with self.subTest(text[:20]):
                self.assertTrue(secret_scan.scan_text('config.py', text), text)

    def test_placeholders_and_empty_values_are_fine(self):
        for text in ('AISSTREAM_API_KEY=your-key-here', 'OPENAI_API_KEY=', 'OPENAI_API_KEY=""', 'api_key = "<paste key here>"',
                     'AISSTREAM_API_KEY=changeme', 'token = "example-token"', 'key = os.getenv("AISSTREAM_API_KEY")',
                     'AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY", "")', "set AISSTREAM_API_KEY for live ships",
                     'headers = {"Authorization": "Bearer " + key}'):
            with self.subTest(text):
                self.assertEqual(secret_scan.scan_text('x.py', text), [], text)

    def test_ordinary_code_and_hashes_are_not_flagged(self):
        sha = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        self.assertEqual(secret_scan.scan_text('audit.py', f'prev = "{sha}"'), [])
        self.assertEqual(secret_scan.scan_text('a.py', 'def tokenize(text): return text.split()'), [])
        self.assertEqual(secret_scan.scan_text('a.py', 'password_hint = "choose something long"'), [])

    def test_findings_never_contain_the_whole_secret(self):
        for secret, text in ((OPENAI, f'key = "{OPENAI}"'), (REAL_VALUE, f'API_KEY = "{REAL_VALUE}"')):
            found = secret_scan.scan_text('x.py', text)
            self.assertTrue(found)
            self.assertNotIn(secret, repr(found))
            self.assertNotIn(secret, secret_scan.format_finding(found[0]))

    def test_findings_carry_file_and_line(self):
        f = secret_scan.scan_text('src/a.py', f'x = 1\ny = "{OPENAI}"\n')[0]
        self.assertEqual((f['path'], f['line']), ('src/a.py', 2))


class Paths(unittest.TestCase):
    def test_forbidden_files(self):
        for path in ('.env', '.env.local', '.env.production', 'app/.env', 'deploy/prod.pem', 'keys/server.key', 'id_rsa',
                     '.venv/Scripts/python.exe', 'venv/lib/x.py', '.streamlit/secrets.toml', 'credentials.json',
                     'node_modules/a/b.js', '.vercel/project.json', 'cert.p12', 'service-account.json'):
            with self.subTest(path):
                self.assertIsNotNone(secret_scan.forbidden_path(path), path)

    def test_examples_and_normal_files_are_allowed(self):
        for path in ('.env.example', '.env.sample', 'README.md', 'server.py', 'data/network.json', 'tests/test_env.py',
                     'tools/secret_scan.py', 'runtime_notes.md', 'environment.md'):
            with self.subTest(path):
                self.assertIsNone(secret_scan.forbidden_path(path), path)


DIFF = '''diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1,2 +1,3 @@
 unchanged = 1
-old = "{old}"
+new_line = 2
+key = "{new}"
diff --git a/.env b/.env
new file mode 100644
--- /dev/null
+++ b/.env
@@ -0,0 +1 @@
+X=1
'''.format(old=OPENAI, new=OPENAI)


class Diffs(unittest.TestCase):
    def test_only_added_lines_are_scanned_and_files_are_checked(self):
        found = secret_scan.scan_diff(DIFF)
        self.assertEqual(sum(1 for f in found if f['path'] == 'config.py'), 1)   # the added key line, not the removed one
        self.assertTrue(any(f['path'] == '.env' and f['rule'] == 'forbidden file' for f in found))

    def test_a_clean_diff_has_no_findings(self):
        clean = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n'
        self.assertEqual(secret_scan.scan_diff(clean), [])


@unittest.skipUnless(shutil.which('git') and shutil.which('sh'), 'git and sh are needed')
class PrePushHook(unittest.TestCase):
    """End to end in a throwaway repo with a local bare remote: the installed hook lets a clean push through and stops bad ones."""

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(['git', *args], cwd=cwd or self.work, capture_output=True, text=True, check=check)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.remote, self.work = os.path.join(self.tmp.name, 'remote.git'), os.path.join(self.tmp.name, 'work')
        subprocess.run(['git', 'init', '-q', '--bare', self.remote], check=True)
        subprocess.run(['git', 'clone', '-q', self.remote, self.work], check=True, capture_output=True)
        self.git('config', 'user.name', 'tester')
        self.git('config', 'user.email', 'tester@example.com')
        os.makedirs(os.path.join(self.work, 'tools'))
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(secret_scan.__file__)), 'secret_scan.py'), os.path.join(self.work, 'tools'))
        subprocess.run([sys.executable, 'tools/secret_scan.py', '--install-hook'], cwd=self.work, check=True, capture_output=True)
        self.write('a.txt', 'hello\n')
        self.commit('clean')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.work, name), 'w') as fh:
            fh.write(text)

    def commit(self, message):
        self.git('add', '-A', '-f')
        self.git('commit', '-q', '-m', message)

    def push(self):
        return self.git('push', 'origin', 'HEAD:refs/heads/main', check=False)

    def remote_commits(self):
        r = subprocess.run(['git', '--git-dir', self.remote, 'rev-list', '--count', 'main'], capture_output=True, text=True)
        return int(r.stdout.strip() or 0)

    def test_a_clean_push_goes_through(self):
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.remote_commits(), 1)

    def test_a_forced_env_file_is_blocked(self):
        self.assertEqual(self.push().returncode, 0)
        self.write('.env', 'X=1\n')
        self.commit('oops env')
        r = self.push()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('forbidden file', r.stderr)
        self.assertEqual(self.remote_commits(), 1)

    def test_a_key_in_code_is_blocked_and_never_printed(self):
        self.assertEqual(self.push().returncode, 0)
        self.write('cfg.py', f'k = "{OPENAI}"\n')
        self.commit('oops key')
        r = self.push()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('OpenAI-style key', r.stderr)
        self.assertNotIn(OPENAI, r.stderr + r.stdout)
        self.assertEqual(self.remote_commits(), 1)

    def test_a_key_in_an_earlier_commit_is_still_caught(self):
        self.assertEqual(self.push().returncode, 0)
        self.write('cfg.py', f'k = "{OPENAI}"\n')
        self.commit('add key')
        os.remove(os.path.join(self.work, 'cfg.py'))
        self.commit('remove key again')   # gone from the tip, but still in the pushed history
        self.assertNotEqual(self.push().returncode, 0)
        self.assertEqual(self.remote_commits(), 1)


if __name__ == '__main__':
    unittest.main()
