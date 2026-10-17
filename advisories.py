"""Official navigational warnings from the US National Geospatial-Intelligence Agency (NGA)
Maritime Safety Information service: HYDROLANT, HYDROPAC and NAVAREA IV / XII broadcast warnings.

These are official, published warnings (unlike press headlines). Tideline keeps the active ones that
mention a risk zone on its network and are about security (attacks, hostile activity, military
operations, piracy, mines, GNSS interference). In "today" conditions each counts as a verified
signal in that zone's security score (same channel and cap as news signals) and is listed with its
number and date. Navigational notices (buoys, wrecks, cable work) are ignored.
Endpoint: https://msi.nga.mil/api/publications/broadcast-warn?output=json&status=active
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

URL = os.getenv('NGA_WARNINGS_URL', 'https://msi.nga.mil/api/publications/broadcast-warn?output=json&status=active')
SOURCE = 'NGA Maritime Safety Information, broadcast warnings (HYDROLANT/HYDROPAC/NAVAREA IV, XII)'
REFRESH_S = 3 * 3600
AREA_NAMES = {'A': 'HYDROLANT', 'P': 'HYDROPAC', '4': 'NAVAREA IV', '12': 'NAVAREA XII'}
ZONE_PATTERNS = {
    'bab': r'RED SEA|BAB[ -]?EL[ -]?MANDEB|GULF OF ADEN|YEMEN|HOUTHI',
    'hormuz': r'STRAIT OF HORMUZ|PERSIAN GULF|ARABIAN GULF|GULF OF OMAN',
    'suez': r'SUEZ',
    'malacca': r'MALACCA|SINGAPORE STRAIT',
    'gulf_guinea': r'GULF OF GUINEA',
    'cape': r'CAPE OF GOOD HOPE',
}
SECURITY = re.compile(r'ATTACK|HOSTILE|MISSILE|DRONE|UAV|UNMANNED|ARMED|PIRA(CY|TE)|HIJACK|BOARD(ED|ING)|MILITARY|NAVAL OPERATIONS|'
                      r'MINE[SD]?\b|EXPLOSIVE|GNSS|GPS (INTERFERENCE|JAMMING)|JAMMING|SPOOFING|CONFLICT|THREAT|SECURITY', re.I)
_lock = threading.Lock()
_state = {'status': 'not loaded', 'warnings': [], 'retrieved_at': None, 'error': None}


def parse(payload):
    items = payload.get('broadcast-warn') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError('unexpected NGA response')
    out = []
    for w in items:
        text = ' '.join(str(w.get('text') or '').split())
        zones = [z for z, pat in ZONE_PATTERNS.items() if re.search(pat, text, re.I)]
        if not zones or not SECURITY.search(text):
            continue
        area = AREA_NAMES.get(str(w.get('navArea')), str(w.get('navArea')))
        out.append({'id': f"{area} {w.get('msgNumber')}/{str(w.get('msgYear'))[-2:]}", 'zones': zones, 'issued': w.get('issueDate'),
                    'authority': w.get('authority'), 'text': text[:600], 'source': SOURCE,
                    'url': 'https://msi.nga.mil/NavWarnings'})
    return out


def set_warnings(warnings, status='live'):
    with _lock:
        _state.update(status=status, warnings=warnings, retrieved_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), error=None)


def refresh():
    try:
        with urlopen(Request(URL, headers={'User-Agent': 'TidelineHackathonDemo/0.3', 'Accept': 'application/json'}), timeout=40) as r:
            ws = parse(json.loads(r.read().decode('utf-8', 'replace')))
        set_warnings(ws)
        print(f'[advisories] NGA warnings loaded: {len(ws)} security warnings on Tideline risk zones', flush=True)
    except Exception as exc:
        with _lock:
            _state.update(status='unavailable' if not _state['warnings'] else 'stale', error=f'{type(exc).__name__}: {str(exc)[:160]}')
        print(f"[advisories] NGA warnings unavailable: {_state['error']}", flush=True)


def start_background_refresh():
    def loop():
        while True:
            refresh()
            time.sleep(REFRESH_S)
    threading.Thread(target=loop, name='tideline-advisories', daemon=True).start()


def current():
    with _lock:
        return dict(_state, warnings=list(_state['warnings']))


def by_zone():
    out = {}
    for w in current()['warnings']:
        for z in w['zones']:
            out.setdefault(z, []).append(w)
    return out
