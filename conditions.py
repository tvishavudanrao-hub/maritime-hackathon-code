"""Ocean conditions along a route, as seen by the optimizer.

Sources, in order of precedence for a given point and time:
1. Storm events supplied by the operator or extracted from reports (wave height, wind),
   when they are worse than the other sources.
2. Live Open-Meteo forecasts (waves, wind, currents; see live_weather.py), within their 7-day horizon.
3. SAMPLE seasonal conditions below: rough October typical wave heights by region,
   plus one sample current (Agulhas). These are illustrative, not climatology data.
"""
from __future__ import annotations

import math


def _haversine_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x)))


def seasonal_hs(lat, lon):
    """SAMPLE typical significant wave height (m) by region."""
    if 12 <= lat <= 30 and 32 <= lon <= 44:
        return 0.9   # Red Sea
    if 23 <= lat <= 30.5 and 47 <= lon <= 57:
        return 0.8   # Persian Gulf
    if 30 <= lat <= 46 and -5 <= lon <= 36:
        return 1.3   # Mediterranean
    if lat >= 46 and lon >= -5:
        return 2.0   # English Channel / North Sea approaches
    if lat >= 36 and lon < -5:
        return 2.8   # NE Atlantic / Bay of Biscay
    if 10 <= lat < 36 and lon < -5:
        return 1.8   # Canaries / West Africa
    if lat < -30:
        return 3.2   # Southern Ocean margin around the Cape
    if -30 <= lat < -10:
        return 2.4 if lon > 25 else 2.0
    if -10 <= lat < 10:
        return 1.4
    if lon > 100:
        return 2.0   # South / East China Sea, NE monsoon onset
    return 1.3       # Arabian Sea, Gulf of Aden, Bay of Bengal


def seasonal_current(lat, lon):
    """SAMPLE surface current: (speed kn, bearing it flows toward, deg)."""
    if -36 <= lat <= -28 and 20 <= lon <= 33:
        return 2.0, 225.0  # Agulhas Current, flowing south-west
    return 0.0, 0.0


LIVE_SOURCE = 'live forecast (Open-Meteo)'
SEASONAL_SOURCE = 'seasonal sample'


class Conditions:
    def __init__(self, storms=(), live=None, departure_epoch=None):
        self.storms = list(storms)
        self.live = live                    # live_weather.LiveField or None
        self.departure_epoch = departure_epoch

    def at(self, lat, lon, t_hours):
        hs = seasonal_hs(lat, lon)
        wind_ms, wind_from = 0.0, None
        cur_kn, cur_to = seasonal_current(lat, lon)
        source = SEASONAL_SOURCE
        if self.live is not None and self.departure_epoch is not None:
            v = self.live.at(lat, lon, self.departure_epoch + t_hours * 3600)
            if v is not None:
                hs, wind_ms, wind_from = v['hs'], v['wind_ms'], v['wind_from']
                cur_kn, cur_to = v['current_kn'], v['current_to']
                source = LIVE_SOURCE
        for s in self.storms:
            start, end = s.get('start_h', 0), s.get('end_h', 1e9)
            if start <= t_hours <= end and _haversine_nm((lat, lon), s['center']) <= s['radius_nm']:
                if s['hs_m'] > hs:
                    hs = s['hs_m']
                    wind_ms = s.get('wind_ms', 0.0)
                    wind_from = s.get('wind_from_deg')
                    source = 'event: ' + s.get('id', 'storm')
        return {'hs': hs, 'wind_ms': wind_ms, 'wind_from': wind_from,
                'current_kn': cur_kn, 'current_to': cur_to, 'source': source}
