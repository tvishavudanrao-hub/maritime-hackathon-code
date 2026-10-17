"""Live chokepoint traffic from IMF PortWatch (free, public ArcGIS feature service).

PortWatch counts daily ship transits through 28 chokepoints from satellite and terrestrial
AIS data (UN Global Platform). For each of our zones we compare the last 7 days with the same
calendar weeks of 2023, before the Red Sea crisis, and pass the ratio to the risk model:

    ratio = mean daily transits, last 7 days / mean daily transits, same weeks of 2023

Citation required by PortWatch: "Sources: UN Global Platform; IMF PortWatch (portwatch.imf.org)."
Runs in the background (every 6 hours); the optimizer only reads the latest snapshot.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

QUERY_URL = ('https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/'
             'Daily_Chokepoints_Data/FeatureServer/0/query?')
CITATION = 'Sources: UN Global Platform; IMF PortWatch (portwatch.imf.org)'
REFRESH_SECONDS = 6 * 3600
BASELINE_YEAR = 2023
# zone id -> keywords that identify the PortWatch chokepoint name
ZONE_KEYWORDS = {'suez': ('suez',), 'bab': ('mandeb',), 'hormuz': ('hormuz',),
                 'cape': ('good hope',), 'malacca': ('malacca',)}


def _query(where, fetch=None):
    params = {'where': where, 'outFields': 'portname,date,n_total', 'f': 'json',
              'orderByFields': 'date', 'resultRecordCount': 2000, 'returnGeometry': 'false'}
    url = QUERY_URL + urlencode(params)
    if fetch is not None:
        return fetch(url)
    req = Request(url, headers={'User-Agent': 'TidelineHackathonDemo/0.2', 'Accept': 'application/json'})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read(10_000_000).decode('utf-8'))


def _rows(payload):
    if 'error' in payload:
        raise ValueError(str(payload['error'])[:200])
    out = []
    for f in payload.get('features') or []:
        a = f.get('attributes') or {}
        name, d, n = a.get('portname'), a.get('date'), a.get('n_total')
        if not name or d is None or not isinstance(n, (int, float)):
            continue
        day = datetime.fromtimestamp(d / 1000, timezone.utc).date() if isinstance(d, (int, float)) else \
            datetime.fromisoformat(str(d)[:10]).date()
        out.append((str(name), day, float(n)))
    return out


def _zone_of(name):
    low = name.lower()
    for zone, words in ZONE_KEYWORDS.items():
        if any(w in low for w in words):
            return zone
    return None


def fetch_snapshot(today=None, fetch=None):
    today = today or datetime.now(timezone.utc).date()
    recent_from = today - timedelta(days=21)
    recent = _rows(_query(f"date >= DATE '{recent_from.isoformat()}'", fetch))
    if not recent:
        raise ValueError('PortWatch returned no recent rows')
    latest = max(d for _, d, _ in recent)
    window_start = latest - timedelta(days=6)
    base_end = latest.replace(year=BASELINE_YEAR)
    base_start = base_end - timedelta(days=13)
    base = _rows(_query(f"date >= DATE '{base_start.isoformat()}' AND date <= DATE '{base_end.isoformat()}'", fetch))
    zones = {}
    for zone in ZONE_KEYWORDS:
        r_vals = [n for name, d, n in recent if _zone_of(name) == zone and d >= window_start]
        b_vals = [n for name, d, n in base if _zone_of(name) == zone]
        names = sorted({name for name, _, _ in recent if _zone_of(name) == zone})
        if not r_vals or not b_vals:
            continue
        r_mean, b_mean = sum(r_vals) / len(r_vals), sum(b_vals) / len(b_vals)
        if b_mean <= 0:
            continue
        zones[zone] = {'chokepoint': names[0] if names else zone, 'recent_daily': round(r_mean, 1),
                       'baseline_daily': round(b_mean, 1), 'ratio': round(r_mean / b_mean, 3),
                       'window': f'{window_start.isoformat()} to {latest.isoformat()}',
                       'baseline_window': f'{base_start.isoformat()} to {base_end.isoformat()}'}
    if not zones:
        raise ValueError('PortWatch data did not match any monitored chokepoint')
    return {'status': 'live', 'source': 'IMF PortWatch daily chokepoint transits', 'citation': CITATION,
            'url': 'https://portwatch.imf.org/', 'latest_date': latest.isoformat(),
            'retrieved_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'zones': zones}


_LOCK = threading.Lock()
_SNAP = None
_STARTED = False
_LAST_ERROR = None


def set_snapshot(snap):
    global _SNAP
    with _LOCK:
        _SNAP = snap


def current_snapshot():
    with _LOCK:
        return _SNAP


def status():
    with _LOCK:
        if _SNAP:
            return _SNAP
        return {'status': 'loading' if _STARTED and not _LAST_ERROR else ('unavailable' if _LAST_ERROR else 'off'),
                'source': 'IMF PortWatch daily chokepoint transits', 'error': _LAST_ERROR, 'citation': CITATION}


def start_background_refresh(interval=REFRESH_SECONDS):
    global _STARTED
    if _STARTED:
        return
    _STARTED = True

    def loop():
        global _LAST_ERROR
        while True:
            try:
                snap = fetch_snapshot()
                set_snapshot(snap)
                _LAST_ERROR = None
                print('[portwatch] ' + ', '.join(f"{z} {v['ratio']:.0%} of 2023" for z, v in snap['zones'].items()), flush=True)
            except Exception as exc:
                _LAST_ERROR = f'{type(exc).__name__}: {str(exc)[:160]}'
                print(f'[portwatch] unavailable: {_LAST_ERROR}', flush=True)
            time.sleep(interval if _LAST_ERROR is None else 900)

    threading.Thread(target=loop, name='tideline-portwatch', daemon=True).start()
