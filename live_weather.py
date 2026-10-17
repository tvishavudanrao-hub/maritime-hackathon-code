"""Live ocean conditions along the route network, from Open-Meteo (free, no key).

* Marine API: hourly wave height, ocean current speed and direction.
* Forecast API: hourly 10 m wind speed and direction.

Sample points are the midpoints of every network segment, snapped to a 1.5 degree grid and
de-duplicated (about 100 points). They are fetched in chunks, in parallel, and refreshed in
the background every hour. The optimizer reads the latest snapshot through
`current_field()`; it never waits on the network.

Conventions (stated, not guessed silently):
* Wind direction is meteorological: the direction the wind blows FROM.
* Ocean current direction is treated as oceanographic: the direction the current flows TOWARD.
* Current speed is converted from whatever unit Open-Meteo reports in `hourly_units`.
* Beyond the forecast horizon, or where the marine model has no sea cell, the optimizer falls
  back to sample seasonal conditions and labels those segments accordingly.
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MARINE_URL = 'https://marine-api.open-meteo.com/v1/marine?'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast?'
USER_AGENT = 'TidelineHackathonDemo/0.2 (+https://open-meteo.com)'
FORECAST_DAYS = 7
GRID_DEG = 3.0   # one sample per 3-degree cell keeps calls within Open-Meteo's free daily limit
CHUNK = 40
REFRESH_SECONDS = 60 * 60
SOURCE_LABEL = 'live forecast (Open-Meteo)'

TO_KNOTS = {'km/h': 1 / 1.852, 'kmh': 1 / 1.852, 'm/s': 1.943844, 'ms': 1.943844, 'kn': 1.0, 'knots': 1.0,
            'mph': 0.868976, 'mp/h': 0.868976}


def _haversine_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x)))


def sample_points(network=None):
    """Midpoints of network lane segments, at most one per grid cell."""
    if network is None:
        network = json.loads((ROOT / 'data' / 'network.json').read_text())
    seen, points = set(), []
    for e in network['edges']:
        pts = e['points']
        for p, q in zip(pts[:-1], pts[1:]):
            mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
            key = (round(mid[0] / GRID_DEG), round(mid[1] / GRID_DEG))
            if key not in seen:
                seen.add(key)
                points.append((round(mid[0], 2), round(mid[1], 2)))   # a point on the lane itself, so at sea
    return points


def _parse_time(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).timestamp()


def _series(item, name):
    vals = (item.get('hourly') or {}).get(name) or []
    return [v if isinstance(v, (int, float)) and math.isfinite(v) else None for v in vals]


class LiveField:
    """A snapshot of forecast conditions at the sample points."""

    def __init__(self, points, marine, wind, retrieved_at, errors=()):
        self.points = points
        self.marine = marine   # list per point: {'t0', 'hs', 'cur_kn', 'cur_to'} or None
        self.wind = wind       # list per point: {'t0', 'ws', 'wd'} or None
        self.retrieved_at = retrieved_at
        self.errors = list(errors)
        self._nearest = {}
        ends = [m['t0'] + 3600 * (len(m['hs']) - 1) for m in marine if m and m['hs']]
        self.horizon_end = max(ends) if ends else None
        self.coverage = sum(1 for m in marine if m and any(v is not None for v in m['hs']))

    @property
    def usable(self):
        return self.coverage > 0 and self.horizon_end is not None

    def nearest(self, lat, lon):
        key = (round(lat, 2), round(lon, 2))
        if key not in self._nearest:
            best, best_d = None, 400.0  # nm: no point farther than this is used
            for i, p in enumerate(self.points):
                d = _haversine_nm(key, p)
                if d < best_d:
                    best, best_d = i, d
            self._nearest[key] = best
        return self._nearest[key]

    def at(self, lat, lon, epoch_s):
        """Conditions at a place and absolute time, or None if no live value applies."""
        i = self.nearest(lat, lon)
        if i is None:
            return None
        m = self.marine[i]
        if not m:
            return None
        k = int(round((epoch_s - m['t0']) / 3600))
        if k < 0 or k >= len(m['hs']) or m['hs'][k] is None:
            return None
        out = {'hs': m['hs'][k], 'current_kn': 0.0, 'current_to': 0.0, 'wind_ms': 0.0, 'wind_from': None}
        if m['cur_kn'] and k < len(m['cur_kn']) and m['cur_kn'][k] is not None and m['cur_to'][k] is not None:
            out['current_kn'], out['current_to'] = m['cur_kn'][k], m['cur_to'][k]
        w = self.wind[i] if self.wind else None
        if w:
            j = int(round((epoch_s - w['t0']) / 3600))
            if 0 <= j < len(w['ws']) and w['ws'][j] is not None and w['wd'][j] is not None:
                out['wind_ms'], out['wind_from'] = w['ws'][j], w['wd'][j]
        return out

    def status(self):
        return {'status': 'live' if self.usable else 'unavailable', 'source': 'Open-Meteo marine + forecast APIs',
                'retrieved_at': self.retrieved_at, 'points': len(self.points), 'points_with_data': self.coverage,
                'horizon_end': datetime.fromtimestamp(self.horizon_end, timezone.utc).isoformat().replace('+00:00', 'Z')
                if self.horizon_end else None,
                'variables': ['wave_height', 'ocean_current_velocity', 'ocean_current_direction',
                              'wind_speed_10m', 'wind_direction_10m'],
                'errors': self.errors[:3]}


def _fetch_json(url, timeout=20):
    req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(20_000_000).decode('utf-8'))
    return data if isinstance(data, list) else [data]


def _marine_chunk(points):
    url = MARINE_URL + urlencode({
        'latitude': ','.join(str(p[0]) for p in points), 'longitude': ','.join(str(p[1]) for p in points),
        'hourly': 'wave_height,ocean_current_velocity,ocean_current_direction',
        'forecast_days': FORECAST_DAYS, 'timezone': 'UTC', 'cell_selection': 'sea'})
    out = []
    for item in _fetch_json(url):
        times = (item.get('hourly') or {}).get('time') or []
        if not times:
            out.append(None)
            continue
        unit = str((item.get('hourly_units') or {}).get('ocean_current_velocity', 'km/h')).strip().lower()
        factor = TO_KNOTS.get(unit, 1 / 1.852)
        cur = _series(item, 'ocean_current_velocity')
        out.append({'t0': _parse_time(times[0]), 'hs': _series(item, 'wave_height'),
                    'cur_kn': [None if v is None else v * factor for v in cur],
                    'cur_to': _series(item, 'ocean_current_direction')})
    return out


def _wind_chunk(points):
    url = FORECAST_URL + urlencode({
        'latitude': ','.join(str(p[0]) for p in points), 'longitude': ','.join(str(p[1]) for p in points),
        'hourly': 'wind_speed_10m,wind_direction_10m', 'wind_speed_unit': 'ms',
        'forecast_days': FORECAST_DAYS, 'timezone': 'UTC'})
    out = []
    for item in _fetch_json(url):
        times = (item.get('hourly') or {}).get('time') or []
        if not times:
            out.append(None)
            continue
        unit = str((item.get('hourly_units') or {}).get('wind_speed_10m', 'm/s')).strip().lower()
        factor = 1.0 if unit in ('m/s', 'ms') else (TO_KNOTS.get(unit, 1.943844) / 1.943844)
        ws = _series(item, 'wind_speed_10m')
        out.append({'t0': _parse_time(times[0]), 'ws': [None if v is None else v * factor for v in ws],
                    'wd': _series(item, 'wind_direction_10m')})
    return out


def fetch_field(points=None, fetch_marine=_marine_chunk, fetch_wind=_wind_chunk):
    points = points or sample_points()
    chunks = [points[i:i + CHUNK] for i in range(0, len(points), CHUNK)]
    marine, wind, errors = [], [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        mfut = [pool.submit(fetch_marine, c) for c in chunks]
        wfut = [pool.submit(fetch_wind, c) for c in chunks]
        for c, f in zip(chunks, mfut):
            try:
                res = f.result()
                marine.extend(res if len(res) == len(c) else [None] * len(c))
            except Exception as exc:
                errors.append(f'marine: {type(exc).__name__}: {str(exc)[:120]}')
                marine.extend([None] * len(c))
        for c, f in zip(chunks, wfut):
            try:
                res = f.result()
                wind.extend(res if len(res) == len(c) else [None] * len(c))
            except Exception as exc:
                errors.append(f'wind: {type(exc).__name__}: {str(exc)[:120]}')
                wind.extend([None] * len(c))
    return LiveField(points, marine, wind, datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), errors)


_LOCK = threading.Lock()
_FIELD = None
_STARTED = False


def current_field():
    """Latest usable snapshot, or None. Never blocks on the network."""
    with _LOCK:
        return _FIELD if _FIELD is not None and _FIELD.usable else None


def current_status():
    with _LOCK:
        if _FIELD is None:
            return {'status': 'loading' if _STARTED else 'off', 'source': 'Open-Meteo marine + forecast APIs'}
        return _FIELD.status()


def set_field(field):
    """Install a snapshot (used by the refresher and by tests)."""
    global _FIELD
    with _LOCK:
        _FIELD = field


def refresh_once():
    field = fetch_field()
    with _LOCK:
        global _FIELD
        # Keep the last good snapshot if this refresh failed completely.
        if field.usable or _FIELD is None or not _FIELD.usable:
            _FIELD = field
    return field


def start_background_refresh(interval=REFRESH_SECONDS):
    global _STARTED
    if _STARTED:
        return
    _STARTED = True

    def loop():
        while True:
            try:
                f = refresh_once()
                print(f"[weather] Open-Meteo snapshot: {f.coverage}/{len(f.points)} points with data"
                      + (f"; errors: {f.errors[0]}" if f.errors else ''), flush=True)
            except Exception as exc:  # never let the refresher die
                print(f'[weather] refresh failed: {type(exc).__name__}: {exc}', flush=True)
            time.sleep(interval)

    threading.Thread(target=loop, name='tideline-weather', daemon=True).start()
