"""Pre-push secret scan. Standard library only.

Looks for key-like strings and files that must never be committed (.env, .venv, private keys, ...), in the files git tracks,
in the staged changes, or in the commits about to be pushed. It prints where a problem is, never the whole secret.

  python tools/secret_scan.py                 scan every tracked file (default)
  python tools/secret_scan.py --staged        scan what is staged for the next commit
  python tools/secret_scan.py --history       scan every commit on every branch
  python tools/secret_scan.py --install-hook  install a git pre-push hook that scans the commits being pushed

Exit status is 1 when something is found, so a hook or CI job can stop the push. A match is a prompt to look, not proof:
if a key was ever committed, revoke it at the provider; deleting it from the repo does not make it safe again.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys

PROVIDER_RULES = [
    ('OpenAI-style key', re.compile(r'\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}')),
    ('AWS access key id', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('Google API key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('GitHub token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('Slack token', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}')),
    ('private key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]
# A name that contains key / secret / token / password, then : or =, then a long value with no spaces or brackets.
NAME = r'(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|private[_-]?key|auth(?:orization)?|client[_-]?secret)'
ASSIGN = re.compile(r'''["']?[A-Za-z0-9_.-]*''' + NAME + r'''[A-Za-z0-9_.-]*["']?\s*[:=]\s*["']?([A-Za-z0-9/+=_.-]{16,})''', re.IGNORECASE)
PLACEHOLDER = re.compile(r'your|example|sample|dummy|placeholder|changeme|replace|xxxx|todo|insert|redacted|environ|getenv', re.IGNORECASE)
KEY_FILE_NAMES = {'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'credentials.json', 'secrets.toml', '.netrc', '.pypirc', '.npmrc'}
MAX_FILE_BYTES = 2_000_000


def redact(value: str) -> str:
    return f'{value[:4]}…({len(value)} chars)'


def forbidden_path(path: str):
    """Why this file must not be committed, or None."""
    p = path.replace('\\', '/')
    name = p.rsplit('/', 1)[-1]
    if re.fullmatch(r'\.env(\.[^/]*)?', name) and not re.search(r'\.(example|sample|template|dist)$', name):
        return 'environment file'
    if re.search(r'(^|/)(\.venv|venv|node_modules|\.vercel|\.aws|\.ssh)/', p):
        return 'environment, dependency or credential folder'
    if re.search(r'\.(pem|key|p12|pfx|jks|keystore)$', name):
        return 'key or certificate file'
    if name in KEY_FILE_NAMES or (name.startswith('service-account') and name.endswith('.json')):
        return 'credentials file'
    return None


def _finding(path, line, rule, preview):
    return {'path': path, 'line': line, 'rule': rule, 'preview': preview}


def scan_text(path: str, text: str) -> list:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        hit = False
        for rule, rx in PROVIDER_RULES:
            m = rx.search(line)
            if m:
                out.append(_finding(path, number, rule, line.strip().replace(m.group(0), redact(m.group(0)))[:140]))
                hit = True
        if hit:
            continue
        for m in ASSIGN.finditer(line):
            value = m.group(1)
            if PLACEHOLDER.search(value) or not re.search(r'\d', value) or not re.search(r'[A-Za-z]', value):
                continue
            out.append(_finding(path, number, 'secret-looking value', line.strip().replace(value, redact(value))[:140]))
            break
    return out


def scan_diff(diff: str) -> list:
    """Scan only the lines a unified diff adds, and flag files that must not be committed at all."""
    out, path, line_no, flagged = [], None, 0, set()
    for raw in diff.splitlines():
        if raw.startswith('+++ '):
            target = raw[4:].strip()
            path = target[2:] if target.startswith('b/') else None
            if path and path not in flagged:
                reason = forbidden_path(path)
                if reason:
                    out.append(_finding(path, 0, 'forbidden file', reason))
                    flagged.add(path)
        elif raw.startswith('@@'):
            m = re.search(r'\+(\d+)', raw)
            line_no = int(m.group(1)) - 1 if m else 0
        elif raw.startswith('+') and path:
            line_no += 1
            out.extend(dict(f, line=line_no) for f in scan_text(path, raw[1:]))
        elif raw.startswith(' '):
            line_no += 1
    return out


def format_finding(f: dict) -> str:
    where = f['path'] + (f":{f['line']}" if f['line'] else '')
    return f"{where}: {f['rule']}: {f['preview']}"


# ---- git plumbing -------------------------------------------------------------------------------------------

def git(*args) -> str:
    r = subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def scan_tracked() -> list:
    out = []
    for path in git('ls-files').splitlines():
        reason = forbidden_path(path)
        if reason:
            out.append(_finding(path, 0, 'forbidden file', reason))
            continue
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, encoding='utf-8') as fh:
                out.extend(scan_text(path, fh.read()))
        except (OSError, UnicodeDecodeError):
            continue   # missing or binary
    return out


def scan_pushed(stdin_lines) -> list:
    """The pre-push hook gets 'local_ref local_sha remote_ref remote_sha' per ref on stdin."""
    zero, out = '0' * 40, []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == zero:
            continue   # a deletion has nothing to scan
        local_sha, remote_sha = parts[1], parts[3]
        revs = [local_sha] if remote_sha == zero else [f'{remote_sha}..{local_sha}']
        if remote_sha == zero:
            revs += ['--not', '--remotes']   # a new branch: only commits the remote does not have yet
        out.extend(scan_diff(git('log', '-p', '-U0', '--no-color', '--format=', *revs)))
    return out


def install_hook() -> str:
    hooks = git('rev-parse', '--git-path', 'hooks').strip()
    path = os.path.join(hooks, 'pre-push')
    # Pick an interpreter that really runs: on Windows `python3` can be a Microsoft Store stub that is not Python.
    script = ('#!/bin/sh\n# installed by tools/secret_scan.py --install-hook: stops a push that contains a key or a forbidden file\n'
              'PY=""\n'
              'for candidate in python python3 py; do\n'
              '  if "$candidate" -c "import sys" >/dev/null 2>&1; then PY="$candidate"; break; fi\n'
              'done\n'
              'if [ -z "$PY" ]; then echo "pre-push: no working Python found, so the secret scan could not run. Push blocked." >&2; exit 1; fi\n'
              'exec "$PY" "$(git rev-parse --show-toplevel)/tools/secret_scan.py" --push\n')
    with open(path, 'w', newline='\n') as fh:
        fh.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Scan for secrets and files that must not be committed.')
    ap.add_argument('--staged', action='store_true', help='scan the staged changes')
    ap.add_argument('--history', action='store_true', help='scan every commit on every branch')
    ap.add_argument('--push', action='store_true', help='hook mode: scan the commits being pushed (reads refs from stdin)')
    ap.add_argument('--install-hook', action='store_true', help='install the git pre-push hook')
    a = ap.parse_args(argv)
    if a.install_hook:
        print('pre-push hook installed:', install_hook())
        return 0
    if a.push:
        found, label = scan_pushed(sys.stdin.read().splitlines()), 'commits being pushed'
    elif a.staged:
        found, label = scan_diff(git('diff', '--cached', '-U0', '--no-color')), 'staged changes'
    elif a.history:
        found, label = scan_diff(git('log', '--all', '-p', '-U0', '--no-color', '--format=')), 'full history'
    else:
        found, label = scan_tracked(), 'tracked files'
    if not found:
        print(f'secret scan: nothing found in {label}.')
        return 0
    print(f'secret scan: {len(found)} problem(s) in {label}. Nothing was pushed.', file=sys.stderr)
    for f in found:
        print('  ' + format_finding(f), file=sys.stderr)
    print('If a real key was committed, revoke it at the provider: removing it from the repo does not make it safe again.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
