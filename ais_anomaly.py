"""Possible-anomaly rules for live AIS, and a chokepoint traffic log. Pure Python, no network.

AIS comes from ships' own transponders, received by a limited set of receivers, so silence or a stop can have many ordinary
causes (anchoring, a queue, no receiver in range). Everything here is therefore worded as POSSIBLE and never as a confirmed
incident, and none of it closes a corridor or changes a plan: it only raises advisory warnings and a traffic note.
"""
from __future__ import annotations

import math

SOURCE = 'AISStream.io (live AIS, advisory: possible, not confirmed)'
STOPPED_KN = 0.5        # below this speed a vessel counts as stopped
STOPPED_MIN = 30        # minutes stopped before it is worth a note
CLUSTER_MIN = 4         # this many stopped vessels in one watched area read as a possible queue or blockage
DARK_MIN = 30           # minutes without a message from a tracked ship, in an area we can normally hear
PORT_NM = 30            # a stopped vessel this close to a port is probably at anchor, not stuck
COVERAGE_NM = 60        # near a port counts as covered by coastal receivers


def distance_nm(a, b) -> float:
    r = math.pi / 180
    d_lat, d_lon = (b[0] - a[0]) * r, (b[1] - a[1]) * r
    x = math.sin(d_lat / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin(d_lon / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x)))


def box_of(lat, lon, boxes):
    """Name of the watched area (a [[lat_min, lon_min], [lat_max, lon_max]] box) that contains the point, or None."""
    for name, ((la0, lo0), (la1, lo1)) in boxes.items():
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return name
    return None


def _near_port(point, ports, nm):
    return any(distance_nm(point, p['loc']) <= nm for p in ports.values())


def _alert(kind, key, severity, title, detail, loc, **extra):
    return {'id': f'{kind}:{key}', 'kind': kind, 'severity': severity, 'title': title, 'detail': detail,
            'loc': [round(loc[0], 4), round(loc[1], 4)], 'source': SOURCE, **extra}


def detect(ships, now, boxes, ports, tracked=()) -> list:
    """Possible stops, possible queues and tracked ships that may have gone dark. `ships` are tracker records
    (lat, lon, sog, still_since, seen, name, mmsi); a record missing a field is simply not considered for that rule."""
    out, stopped = [], {}
    for s in ships:
        lat, lon = s.get('lat'), s.get('lon')
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        point, name, mmsi, sog = (lat, lon), s.get('name'), str(s.get('mmsi')), s.get('sog')
        box, since = box_of(lat, lon, boxes), s.get('still_since')
        if (box and since is not None and not (isinstance(sog, (int, float)) and sog >= STOPPED_KN)
                and (now - since) / 60 >= STOPPED_MIN and not _near_port(point, ports, PORT_NM)):
            stopped.setdefault(box, []).append((s, int((now - since) / 60)))
        seen = s.get('seen')
        if mmsi in tracked and seen is not None and (now - seen) / 60 >= DARK_MIN and (box or _near_port(point, ports, COVERAGE_NM)):
            minutes = int((now - seen) / 60)
            out.append(_alert('went_dark', mmsi, 'warning', f"{name or 'Tracked vessel ' + mmsi} possibly gone dark: no AIS message for {minutes} min",
                              f"Last seen {'in ' + box if box else 'near a port'}. The ship may have switched its transponder off or moved out of receiver range; this is not confirmed.",
                              point, mmsi=mmsi))
    for box, items in stopped.items():
        if len(items) >= CLUSTER_MIN:
            lat = sum(s['lat'] for s, _ in items) / len(items)
            lon = sum(s['lon'] for s, _ in items) / len(items)
            out.append(_alert('stopped_cluster', box, 'warning', f'{len(items)} vessels possibly stopped in {box}',
                              f'All report a speed under {STOPPED_KN} kn for {STOPPED_MIN} min or more, away from ports. A queue or blockage is possible; this is not confirmed.',
                              (lat, lon), count=len(items)))
        else:
            for s, minutes in items:
                label = s.get('name') or f"Vessel {s.get('mmsi')}"
                out.append(_alert('stopped_vessel', str(s.get('mmsi')), 'info', f'{label} possibly stopped in {box} for {minutes} min',
                                  f'AIS shows a speed under {STOPPED_KN} kn, away from ports. Anchoring, waiting in a queue or an incident are all possible; not confirmed.',
                                  (s['lat'], s['lon']), mmsi=str(s.get('mmsi'))))
    return out


class TrafficLog:
    """Distinct vessels heard in each watched area, per time window, compared with the earlier windows we saw.

    Only windows in which at least one ship was heard count towards the baseline, because a gap could just as well mean we were
    not connected. This is a sample of what receivers picked up, not a transit count, so it is shown as an advisory note."""

    def __init__(self, window_h=3, baseline_h=24, keep_h=48, min_windows=3):
        self.window_s, self.baseline_h, self.keep_h, self.min_windows = window_h * 3600.0, baseline_h, keep_h, min_windows
        self.window_h = window_h
        self.buckets = {}   # box -> {window index -> set of mmsi}

    def _index(self, t):
        return int(t // self.window_s)

    def record(self, box, mmsi, t):
        windows = self.buckets.setdefault(box, {})
        windows.setdefault(self._index(t), set()).add(str(mmsi))
        oldest = self._index(t) - int(self.keep_h * 3600 / self.window_s)
        for k in [k for k in windows if k < oldest]:
            del windows[k]

    def summary(self, now):
        out, here = {}, self._index(now)
        span = max(1, int(self.baseline_h / self.window_h))
        for box, windows in self.buckets.items():
            earlier = [len(v) for k, v in windows.items() if here - span <= k < here and v]
            row = {'now': len(windows.get(here, ())), 'windows': len(earlier), 'window_h': self.window_h, 'baseline_h': self.baseline_h,
                   'source': 'AISStream.io (live AIS)',
                   'caveat': 'A sample of the ships that reported here, not a full count of transits (receiver coverage is limited, ships can be missing).'}
            if len(earlier) >= self.min_windows:
                row['baseline'] = round(sum(earlier) / len(earlier), 1)
                row['ratio'] = round(row['now'] / row['baseline'], 2)
                row['note'] = f"{row['now']} ships heard in the last {self.window_h} h against about {row['baseline']} in earlier {self.window_h} h windows."
            else:
                row['baseline'], row['ratio'] = None, None
                row['note'] = f'Not enough history yet ({len(earlier)} earlier window{"" if len(earlier) == 1 else "s"} of {self.min_windows} needed) to compare traffic.'
            out[box] = row
        return out
