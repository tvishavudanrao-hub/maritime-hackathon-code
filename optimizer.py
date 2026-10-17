"""Tideline voyage optimizer. Python standard library only.

Searches routes x speed per segment x refueling decisions over the waypoint network
in data/network.json, using the fuel model for burn and the risk model for exposure.

Hard rules remove plans: closed zones and ports, operator avoidance areas, draft
limits, tank capacity, minimum fuel reserve, hard deadlines, waves above the ship's
safe limit. Every remaining plan gets one decision score:

  cash  = fuel purchased - value of fuel left at arrival + canal tolls + port fees
          + days x daily operating cost + war-risk insurance + delay penalty
  score = cash + (time value multiplier - 1) x operating cost
          + CO2 x carbon shadow price + risk loss fraction x route risk x cargo value

The search is a label-setting search with Pareto dominance on
(time, fuel on board, cash, risk exposure, CO2) per node.
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conditions import LIVE_SOURCE, Conditions
from fuel_model import CO2_PER_T_FUEL, get_fuel_model
from risk_model import RiskModel
import live_weather
import compliance
import advisories
import insurance as cover   # module name differs from the local "insurance" cost variable
import portwatch
import uncertainty

ROOT = Path(__file__).resolve().parent
NETWORK = json.loads((ROOT / 'data' / 'network.json').read_text())
FLEET_LIST = json.loads((ROOT / 'data' / 'fleet.json').read_text())['ships']
FLEET = {s['id']: s for s in FLEET_LIST}
TODAY = json.loads((ROOT / 'data' / 'today.json').read_text())
CONDITION_MODES = ('today', 'normal')

BUNKER_HOURS = 6           # time alongside to take fuel, on top of port waiting time
IDLE_FRACTION = 0.10       # fuel burn while waiting, as a fraction of reference daily burn
REFUEL_LEVELS = (0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0)
MIN_PURCHASE_T = 50
PRUNE_FACTOR = 1.6         # keep partial plans within 60% of the best score, so alternatives survive
MAX_LABELS_PER_STATE = 150
WAR_RISK_PER_LEVEL = 0.02  # extra war-risk premium (fraction of hull value) per unit of event security bump
# Near-equal partial plans are merged so the search stays fast (dominance tolerances).
EPS_HOURS, EPS_FUEL_T, EPS_SCORE = 8.0, 40.0, 10000.0
FUEL_BUCKET_T = 250.0      # edge simulations are cached per fuel-on-board bucket

PRESETS = {
    'balanced': {'label': 'Balanced',  'time_value_mult': 1.0, 'risk_loss_frac': 0.004, 'carbon_usd_per_t': 50},
    'cheapest': {'label': 'Cheapest',  'time_value_mult': 1.0, 'risk_loss_frac': 0.001, 'carbon_usd_per_t': 0},
    'fastest':  {'label': 'Fastest',   'time_value_mult': 4.0, 'risk_loss_frac': 0.004, 'carbon_usd_per_t': 0},
    'safest':   {'label': 'Safest',    'time_value_mult': 1.0, 'risk_loss_frac': 0.02,  'carbon_usd_per_t': 50},
    'greenest': {'label': 'Greenest',  'time_value_mult': 1.0, 'risk_loss_frac': 0.004, 'carbon_usd_per_t': 200},
}
CARGO_SEGMENTS = {
    'crude':  {'label': 'Crude oil & tankers', 'news_terms': '(tanker OR crude OR oil OR LNG OR "fuel shipment")',
               'keywords': ('tanker', 'crude', 'oil', 'lng', 'lpg', 'fuel', 'vlcc', 'aframax', 'refinery', 'petroleum', 'gas carrier')},
    'retail': {'label': 'Retail & container goods', 'news_terms': '(container OR Maersk OR "MSC" OR "Hapag-Lloyd" OR CMA OR retail OR boxship)',
               'keywords': ('container', 'boxship', 'maersk', 'msc', 'hapag', 'cma cgm', 'cosco', 'retail', 'consumer goods', 'liner', 'teu')},
    'food':   {'label': 'Food & refrigerated', 'news_terms': '(grain OR wheat OR food OR reefer OR fertilizer OR "food supply")',
               'keywords': ('grain', 'wheat', 'corn', 'food', 'reefer', 'refrigerated', 'fertilizer', 'rice', 'soy', 'humanitarian')},
    'bulk':   {'label': 'Dry bulk (ore, coal)', 'news_terms': '("bulk carrier" OR "iron ore" OR coal OR bauxite OR "dry bulk")',
               'keywords': ('bulk', 'iron ore', 'coal', 'bauxite', 'capesize', 'panamax', 'ore')},
}
WEIGHT_LIMITS = {'risk_loss_frac': (0.0, 0.1), 'carbon_usd_per_t': (0.0, 1000.0), 'time_value_mult': (1.0, 10.0)}

CORRIDORS = {'suez': 'Via Suez Canal', 'cape': 'Via Cape of Good Hope', 'direct': 'Direct'}

ASSUMPTIONS = [
    'Route network, port prices, fees, waiting times, draft limits, security levels and war-risk rates are SAMPLE values.',
    'Fuel burn comes from a regression trained on SIMULATED data generated from ship physics.',
    'Within the 7-day horizon, waves, wind and currents come from live Open-Meteo forecasts; beyond it, SAMPLE seasonal wave heights. Storms can also be added as events.',
    'Risk scores are transparent assumptions, not calibrated probabilities. Not for navigation.',
]


# ---------------------------------------------------------------- geometry

def haversine_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    x = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x)))


def bearing_deg(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


SHIP_NODE = 'SHIP'


def make_edge(to, pts, zones, canal=None, exit_ok=False):
    legs = []
    for p, q in zip(pts[:-1], pts[1:]):
        d = haversine_nm(p, q)
        if d > 0:
            legs.append({'mid': ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), 'nm': d, 'bearing': bearing_deg(p, q)})
    return {'to': to, 'points': pts, 'legs': legs, 'nm': sum(l['nm'] for l in legs), 'zones': tuple(zones), 'canal': canal,
            'exit_ok': exit_ok}


def split_points(pts, fraction):
    """Split a polyline at a fraction of its length: (position, points before, points after)."""
    seg = [haversine_nm(p, q) for p, q in zip(pts[:-1], pts[1:])]
    target = sum(seg) * fraction
    for i, d in enumerate(seg):
        if target <= d or i == len(seg) - 1:
            t = 0 if d == 0 else min(1.0, target / d)
            pos = [round(pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, 4), round(pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t, 4)]
            return pos, pts[:i + 1] + [pos], [pos] + pts[i + 1:]
        target -= d
    return pts[-1], pts, [pts[-1]]


def snap_to_network(lat, lon):
    """Nearest point on the waypoint network: (from, to, fraction along edge, distance nm, point)."""
    best = None
    seen = set()
    for a, edges in GRAPH.adj.items():
        for e in edges:
            key = tuple(sorted((a, e['to'])))
            if key in seen:
                continue
            seen.add(key)
            pts = e['points']
            seg = [haversine_nm(p, q) for p, q in zip(pts[:-1], pts[1:])]
            total = sum(seg) or 1
            run = 0.0
            for i, (p, q) in enumerate(zip(pts[:-1], pts[1:])):
                # project in a local flat frame (good enough at these scales)
                kx = math.cos(math.radians((p[0] + q[0]) / 2))
                ax, ay, bx, by = p[1] * kx, p[0], q[1] * kx, q[0]
                px, py = lon * kx, lat
                dx, dy = bx - ax, by - ay
                t = 0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
                pt = [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]
                d = haversine_nm((lat, lon), pt)
                if best is None or d < best[3]:
                    best = (a, e['to'], (run + seg[i] * t) / total, d, pt)
                run += seg[i]
    return best


class Graph:
    def __init__(self, net):
        self.ports = net['ports']
        self.waypoints = net['waypoints']
        self.zones = net['zones']
        self.canals = net.get('canals', {})
        self.adj = {n: [] for n in list(self.ports) + list(self.waypoints)}
        for e in net['edges']:
            for end in (e['from'], e['to']):
                if end not in self.adj:
                    raise ValueError(f'Edge references unknown node {end}')
            for z in e.get('zones', []):
                if z not in self.zones:
                    raise ValueError(f'Edge references unknown zone {z}')
            for a, b, pts in ((e['from'], e['to'], e['points']), (e['to'], e['from'], list(reversed(e['points'])))):
                legs = []
                for p, q in zip(pts[:-1], pts[1:]):
                    d = haversine_nm(p, q)
                    if d > 0:
                        legs.append({'mid': ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), 'nm': d, 'bearing': bearing_deg(p, q)})
                self.adj[a].append({'to': b, 'points': pts, 'legs': legs, 'nm': sum(l['nm'] for l in legs),
                                    'zones': tuple(e.get('zones', [])), 'canal': e.get('canal')})

    def name(self, node):
        if node == SHIP_NODE:
            return 'Ship position'
        return (self.ports.get(node) or self.waypoints.get(node))['name']

    def edge(self, a, b):
        for e in self.adj.get(a, []):
            if e['to'] == b:
                return e
        return None

    def loc(self, node):
        return (self.ports.get(node) or self.waypoints.get(node))['loc']

    def reachable(self, start, closed_zones):
        seen, stack = {start}, [start]
        while stack:
            n = stack.pop()
            for e in self.adj[n]:
                if e['to'] not in seen and not (set(e['zones']) & closed_zones):
                    seen.add(e['to'])
                    stack.append(e['to'])
        return seen


GRAPH = Graph(NETWORK)


# ---------------------------------------------------------------- requests and events

def _parse_time(value):
    if not value:
        return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(value, name, lo=None, hi=None):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number')
    if not math.isfinite(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f'{name} is out of range')
    return x


def _normalize_resume(raw, dest, ship):
    """Where the ship is now, along the plan it has been following."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError('resume must be an object')
    route = raw.get('route')
    if not (isinstance(route, list) and 2 <= len(route) <= 40 and all(n in GRAPH.adj for n in route)):
        raise ValueError('resume.route must list the plan\'s network nodes')
    if route[-1] != dest:
        raise ValueError('resume.route must end at the destination')
    for a, b in zip(route[:-1], route[1:]):
        if GRAPH.edge(a, b) is None:
            raise ValueError(f'resume.route has no segment {a} → {b}')
    leg = int(_num(raw.get('leg_index', 0), 'resume.leg_index', 0, len(route) - 2))
    frac = _num(raw.get('fraction', 0), 'resume.fraction', 0, 1)
    if frac >= 0.999:
        leg, frac = leg + 1, 0.0
        if leg > len(route) - 2:
            raise ValueError('The ship has already arrived')
    elapsed = _num(raw.get('elapsed_h', 0), 'resume.elapsed_h', 0, 3000)
    fuel = _num(raw.get('fuel_onboard_t'), 'resume.fuel_onboard_t', 0, ship['fuel_capacity_t'])
    zones = set()
    for i in range(leg + (1 if frac > 0 else 0)):
        zones |= set(GRAPH.edge(route[i], route[i + 1])['zones'])
    if frac == 0 and leg > 0:
        zones |= set(GRAPH.edge(route[leg - 1], route[leg])['zones'])
    return {'route': route, 'leg_index': leg, 'fraction': frac, 'elapsed_h': elapsed, 'fuel_onboard_t': fuel,
            'zones_entered': frozenset(zones)}


def normalize_request(req):
    if not isinstance(req, dict):
        raise ValueError('Request must be a JSON object')
    ship = FLEET.get(req.get('ship_id'))
    if ship is None:
        raise ValueError(f"Unknown ship_id. Choose one of: {', '.join(FLEET)}")
    origin, dest = req.get('origin'), req.get('destination')
    for label, p in (('origin', origin), ('destination', dest)):
        if p not in GRAPH.ports:
            raise ValueError(f"Unknown {label} port. Choose one of: {', '.join(GRAPH.ports)}")
    if origin == dest:
        raise ValueError('Origin and destination must differ')
    priority = req.get('priority', 'balanced')
    if priority not in PRESETS:
        raise ValueError(f"priority must be one of: {', '.join(PRESETS)}")
    preset = dict(PRESETS[priority])
    weights = req.get('weights')
    if weights:
        if not isinstance(weights, dict):
            raise ValueError('weights must be an object')
        for k, v in weights.items():
            if k not in WEIGHT_LIMITS:
                raise ValueError(f"Unknown weight {k}. Use: {', '.join(WEIGHT_LIMITS)}")
            lo, hi = WEIGHT_LIMITS[k]
            preset[k] = _num(v, f'weights.{k}', lo, hi)
        preset['label'] = 'Custom'
    affiliations = req.get('affiliations') or []
    if not isinstance(affiliations, list) or any(a not in compliance.AFFILIATIONS for a in affiliations):
        raise ValueError(f"affiliations must be a list drawn from: {', '.join(compliance.AFFILIATIONS)}")
    cargo = req.get('cargo') or ship.get('default_cargo', 'retail')
    if cargo not in CARGO_SEGMENTS:
        raise ValueError(f"cargo must be one of: {', '.join(CARGO_SEGMENTS)}")
    deadline = req.get('deadline_days')
    deadline = None if deadline in (None, '') else _num(deadline, 'deadline_days', 0.5, 120)
    deadline_type = req.get('deadline_type', 'hard')
    if deadline_type not in ('hard', 'soft'):
        raise ValueError('deadline_type must be hard or soft')
    fuel0 = req.get('fuel_onboard_t', ship['default_fuel_onboard_t'])
    fuel0 = _num(fuel0, 'fuel_onboard_t', 0, ship['fuel_capacity_t'])
    reserve = _num(req.get('min_reserve_t', ship['min_reserve_t']), 'min_reserve_t', 0, ship['fuel_capacity_t'])
    avoid_zones = list(req.get('avoid_zones') or [])
    avoid_ports = list(req.get('avoid_ports') or [])
    for z in avoid_zones:
        if z not in GRAPH.zones:
            raise ValueError(f'Unknown zone in avoid_zones: {z}')
    for p in avoid_ports:
        if p not in GRAPH.ports:
            raise ValueError(f'Unknown port in avoid_ports: {p}')
    events = req.get('events') or []
    if not isinstance(events, list) or len(events) > 50:
        raise ValueError('events must be a list of at most 50 items')
    mode = req.get('conditions', 'today')
    if mode not in CONDITION_MODES:
        raise ValueError("conditions must be 'today' or 'normal'")
    allow_uninsured = req.get('allow_uninsured', False)
    if not isinstance(allow_uninsured, bool):
        raise ValueError('allow_uninsured must be true or false')
    live = req.get('live_weather', True)
    if not isinstance(live, bool):
        raise ValueError('live_weather must be true or false')
    berth_h = None
    if req.get('berth_ready_days') not in (None, ''):
        berth_h = _num(req.get('berth_ready_days'), 'berth_ready_days', 0.5, 150) * 24
    elif req.get('berth_ready_at'):
        try:
            berth_h = (_parse_time(req['berth_ready_at']) - _parse_time(req.get('departure'))).total_seconds() / 3600
        except ValueError:
            raise ValueError('berth_ready_at must be an ISO date-time')
        if not 12 <= berth_h <= 150 * 24:
            raise ValueError('berth_ready_at must be between 12 hours and 150 days after departure')
    resume = _normalize_resume(req.get('resume'), dest, ship)
    if resume:
        fuel0 = resume['fuel_onboard_t']
    start_at = req.get('start_at')
    if start_at:
        if resume:
            raise ValueError('Use either resume or start_at, not both')
        if not isinstance(start_at, dict):
            raise ValueError('start_at must be an object with lat and lon')
        lat, lon = _num(start_at.get('lat'), 'start_at.lat', -90, 90), _num(start_at.get('lon'), 'start_at.lon', -180, 180)
        a, b, frac, dist, pt = snap_to_network(lat, lon)
        if dist > 600:
            raise ValueError(f'That position is {dist:,.0f} nm from the nearest route on the Tideline network (limit 600 nm)')
        start_at = {'lat': lat, 'lon': lon, 'edge': (a, b), 'fraction': min(0.999, max(0.001, frac)), 'snap_nm': round(dist, 1),
                    'label': str(start_at.get('label') or 'Live ship position')[:80]}
    return {'ship': ship, 'origin': origin, 'destination': dest, 'departure': _parse_time(req.get('departure')),
            'preset': preset, 'cargo': cargo, 'affiliations': sorted(set(affiliations)),
            'live_weather': live, '_live': live_weather.current_field() if live else None, 'conditions': mode,
            'resume': resume, 'start_at': start_at, 'berth_h': berth_h, 'allow_uninsured': allow_uninsured, 'berth_source': 'operator' if berth_h else None,
            '_portwatch': portwatch.current_snapshot() if mode == 'today' else None,
            'priority': priority, 'deadline_days': deadline, 'deadline_type': deadline_type,
            'fuel_onboard_t': fuel0, 'min_reserve_t': reserve, 'avoid_zones': avoid_zones,
            'avoid_ports': avoid_ports, 'events': events}


EVENT_TYPES = ('zone_closure', 'port_closure', 'security', 'storm', 'traffic', 'news', 'fuel_price', 'port_congestion', 'cover_cancelled')


def apply_events(events):
    """Turn structured events into optimizer inputs. Every event keeps its source and timestamp."""
    out = {'closed_zones': set(), 'closed_ports': set(), 'bumps': {}, 'storms': [], 'traffic': {},
           'news': {}, 'prices': {}, 'waits': {}, 'records': [], 'cover_cancelled': set()}
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValueError('Each event must be an object')
        kind = ev.get('type')
        if kind not in EVENT_TYPES:
            raise ValueError(f"Event type must be one of: {', '.join(EVENT_TYPES)}")
        eid = str(ev.get('id') or f'event-{i + 1}')[:60]
        zone, port = ev.get('zone'), ev.get('port')
        if kind in ('zone_closure', 'security', 'traffic', 'news', 'cover_cancelled') and zone not in GRAPH.zones:
            raise ValueError(f'{eid}: unknown zone {zone!r}')
        if kind in ('port_closure', 'fuel_price', 'port_congestion') and port not in GRAPH.ports:
            raise ValueError(f'{eid}: unknown port {port!r}')
        if kind == 'zone_closure':
            out['closed_zones'].add(zone)
        elif kind == 'cover_cancelled':
            out['cover_cancelled'].add(zone)   # underwriters withdrew war cover there
        elif kind == 'port_closure':
            out['closed_ports'].add(port)
        elif kind == 'security':
            out['bumps'][zone] = out['bumps'].get(zone, 0) + _num(ev.get('level', 0.2), 'level', 0, 0.9)
        elif kind == 'traffic':
            out['traffic'][zone] = _num(ev.get('ratio'), 'ratio', 0, 3)
        elif kind == 'news':
            out['news'][zone] = out['news'].get(zone, 0) + int(_num(ev.get('hits', 1), 'hits', 0, 100))
        elif kind == 'fuel_price':
            out['prices'][port] = _num(ev.get('price'), 'price', 100, 3000)
        elif kind == 'port_congestion':
            out['waits'][port] = _num(ev.get('wait_h'), 'wait_h', 0, 720)
        elif kind == 'storm':
            center = ev.get('center')
            if not (isinstance(center, list) and len(center) == 2):
                raise ValueError(f'{eid}: storm needs center [lat, lon]')
            out['storms'].append({'id': eid, 'center': [_num(center[0], 'lat', -90, 90), _num(center[1], 'lon', -180, 180)],
                                  'radius_nm': _num(ev.get('radius_nm', 200), 'radius_nm', 1, 2000),
                                  'hs_m': _num(ev.get('hs_m', 6), 'hs_m', 0, 20),
                                  'wind_ms': _num(ev.get('wind_ms', 20), 'wind_ms', 0, 80),
                                  'start_h': _num(ev.get('start_h', 0), 'start_h', -1000, 5000),
                                  'end_h': _num(ev.get('end_h', 1e6), 'end_h', -1000, 1e6)})
        out['records'].append({'id': eid, 'type': kind, 'zone': zone, 'port': port,
                               'source': str(ev.get('source') or 'manual scenario')[:200],
                               'timestamp': ev.get('timestamp'),
                               'description': str(ev.get('description') or '')[:300]})
    return out


def build_inputs(r, events):
    """Scenario events plus, in 'today' mode, the sourced conditions snapshot and live PortWatch traffic."""
    ev = apply_events(events)
    ev['premium_bumps'] = dict(ev['bumps'])   # only scenario events add war-risk premium on top of the zone rate
    ev['base_prices'] = {p: v['fuel_price'] for p, v in GRAPH.ports.items()}
    ev['war_risk'] = {z: v.get('war_risk_rate', 0.0) for z, v in GRAPH.zones.items()}
    ev['toll_mult'] = 1.0
    ev['situation'] = []
    for a in r.get('affiliations', []):
        rule = compliance.AFFILIATIONS[a]
        for z in rule['close_zones']:
            ev['closed_zones'].add(z)
            ev['records'].append({'id': f'{a}-{z}', 'type': 'compliance', 'zone': z, 'port': None,
                                  'source': rule['url'], 'timestamp': rule['date'],
                                  'description': f"{GRAPH.zones[z]['label']} excluded for {rule['label'].lower()}: {rule['rule']}"})
    if r['conditions'] != 'today':
        return ev
    sit = ev['situation']
    for p, v in TODAY['fuel_prices'].items():
        ev['base_prices'][p] = v['price']
    basis = {}
    for v in TODAY['fuel_prices'].values():
        basis[v['basis']] = basis.get(v['basis'], 0) + 1
    sit.append({'kind': 'fuel_prices', 'description': 'Fuel prices: ' + ', '.join(f'{n} {k}' for k, n in sorted(basis.items())),
                'source': 'Bunker Index and ENGINE (see data sources)', 'date': TODAY['as_of']})
    for z, v in TODAY['war_risk'].items():
        ev['war_risk'][z] = v['rate']
        sit.append({'kind': 'war_risk', 'zone': z, 'description': f"{GRAPH.zones[z]['label']} war-risk premium {v['rate']:.1%} of hull value",
                    'source': v['source'], 'url': v.get('url'), 'date': v['date'], 'stale': v.get('stale', False)})
    tm = TODAY['suez_toll_multiplier']
    ev['toll_mult'] = tm['value']
    sit.append({'kind': 'toll', 'description': f"Suez toll surcharge +{tm['value'] - 1:.0%}", 'source': tm['source'],
                'url': tm.get('url'), 'date': tm['date']})
    for z, v in TODAY['security'].items():
        ev['bumps'][z] = ev['bumps'].get(z, 0.0) + v['level']
        sit.append({'kind': 'security', 'zone': z, 'description': f"{GRAPH.zones[z]['label']} security risk raised (+{v['level']:.2f})",
                    'source': v['source'], 'url': v.get('url'), 'date': v['date']})
    ev['advisories'] = {}
    for z, ws in advisories.by_zone().items():
        if z not in GRAPH.zones:
            continue
        ev['news'][z] = ev['news'].get(z, 0) + len(ws)   # official warnings count as verified signals
        ev['advisories'][z] = ws
        sit.append({'kind': 'advisory', 'zone': z, 'verified': True,
                    'description': f"{GRAPH.zones[z]['label']}: {len(ws)} active official security warning{'s' if len(ws) > 1 else ''} ({', '.join(w['id'] for w in ws[:3])}{'…' if len(ws) > 3 else ''})",
                    'source': advisories.SOURCE, 'url': 'https://msi.nga.mil/NavWarnings', 'date': ws[0].get('issued')})
    pw = r.get('_portwatch')
    for z in GRAPH.zones:
        if z in ev['traffic']:
            continue  # a scenario event overrides
        if pw and z in pw['zones']:
            t = pw['zones'][z]
            ev['traffic'][z] = t['ratio']
            sit.append({'kind': 'traffic', 'zone': z, 'live': True,
                        'description': f"{GRAPH.zones[z]['label']}: {t['recent_daily']:g} transits/day vs {t['baseline_daily']:g} in {portwatch.BASELINE_YEAR} ({t['ratio']:.0%})",
                        'source': pw['citation'], 'url': pw['url'], 'date': pw['latest_date']})
        elif z in TODAY.get('traffic_fallback', {}):
            t = TODAY['traffic_fallback'][z]
            ev['traffic'][z] = t['ratio']
            sit.append({'kind': 'traffic', 'zone': z, 'live': False,
                        'description': f"{GRAPH.zones[z]['label']}: traffic about {t['ratio']:.0%} of normal (PortWatch unavailable; published estimate)",
                        'source': t['source'], 'url': t.get('url'), 'date': t['date']})
    return ev


# ---------------------------------------------------------------- search

class Label:
    __slots__ = ('node', 't', 'fuel', 'cash', 'co2', 'burn', 'expo', 'zones', 'visited', 'refueled', 'parent', 'step', 'dead', 'ps')

    def __init__(self, node, t, fuel, cash, co2, burn, expo, zones, visited, refueled, parent, step):
        self.node, self.t, self.fuel, self.cash, self.co2, self.burn = node, t, fuel, cash, co2, burn
        self.expo, self.zones, self.visited, self.refueled, self.parent, self.step = expo, zones, visited, refueled, parent, step
        self.dead = False
        self.ps = 0.0  # running decision score, set by the search


def speed_options(ship, fine=False):
    lo, hi = int(math.ceil(ship['min_speed_kn'])), int(math.floor(ship['max_speed_kn']))
    speeds = list(range(lo, hi + 1, 1 if fine else 2))
    if speeds[-1] != hi:
        speeds.append(hi)
    return speeds


class Search:
    def __init__(self, r, ev):
        self.r, self.ev, self.ship = r, ev, r['ship']
        self.fuel_model = get_fuel_model(FLEET_LIST)
        self.live = r.get('_live')
        self.cond = Conditions(ev['storms'], self.live, r['departure'].timestamp())
        self.live_horizon_h = ((self.live.horizon_end - r['departure'].timestamp()) / 3600) if self.live else -1
        self.risk = RiskModel(GRAPH.zones, ev['bumps'], ev['news'], ev['traffic'])
        self.preset = r['preset']
        self.closed_zones = set(ev['closed_zones']) | set(r['avoid_zones'])
        # Insurance: zones the ship's policy does not cover are avoided unless the operator allows it.
        self.uninsured = cover.uninsured_zones(self.ship['id'], GRAPH.zones, ev.get('cover_cancelled', ()))
        if not r.get('allow_uninsured'):
            self.closed_zones |= self.uninsured
        r['_insurance_avoided'] = self.uninsured - set(ev['closed_zones']) - set(r['avoid_zones'])
        self.closed_ports = set(ev['closed_ports']) | set(r['avoid_ports'])
        self.prices = {p: ev['prices'].get(p, ev['base_prices'][p]) for p, v in GRAPH.ports.items() if v.get('fuel_price')}
        self.waits = {p: ev['waits'].get(p, v['wait_h']) for p, v in GRAPH.ports.items()}
        self.berth_h = r.get('berth_h')
        self.speeds = speed_options(self.ship, fine=self.berth_h is not None)
        self.opex_h = self.ship['daily_cost_usd'] / 24
        self.idle_h = self.ship['ref_daily_t'] * IDLE_FRACTION / 24
        self.min_price = min(self.prices.values())
        # Fuel left at arrival is credited at the destination price, capped at the cheapest price on the
        # network, so the optimizer cannot "profit" by carrying cheap fuel to an expensive port.
        self.dest_price = min(self.prices.get(r['destination'], self.min_price), self.min_price)
        self.deadline_h = r['deadline_days'] * 24 if r['deadline_days'] else None
        self.hard_deadline = self.deadline_h is not None and r['deadline_type'] == 'hard'
        self.labels_created = 0
        self.ets_share = compliance.ets_share(r['origin'], r['destination'])
        self.extra_adj, self.follow, self.position = {}, None, None
        res = r.get('resume')
        if res:
            route, i, f = res['route'], res['leg_index'], res['fraction']
            prev, nxt = route[i], route[i + 1]
            if f > 0:
                e = GRAPH.edge(prev, nxt)
                pos, before, after = split_points(e['points'], f)
                self.position = pos
                fwd = make_edge(nxt, after, e['zones'], e['canal'])
                edges = [fwd]
                if not e['canal']:   # a ship inside a canal cannot turn around
                    edges.append(make_edge(prev, list(reversed(before)), e['zones'], exit_ok=True))
                self.extra_adj[SHIP_NODE] = edges
                self.start_node = SHIP_NODE
                self.start_visited = frozenset(route[:i]) | {SHIP_NODE}
                path = [SHIP_NODE] + route[i + 1:]
            else:
                self.position = GRAPH.loc(prev)
                self.start_node = prev
                self.start_visited = frozenset(route[:i + 1])
                path = route[i:]
            self.start_t, self.start_zones = res['elapsed_h'], res['zones_entered']
            self.path_next = {a: b for a, b in zip(path[:-1], path[1:])}
        elif r.get('start_at'):
            sa = r['start_at']
            prev, nxt = sa['edge']
            e = GRAPH.edge(prev, nxt)
            pos, before, after = split_points(e['points'], sa['fraction'])
            self.position = [sa['lat'], sa['lon']]
            # first hop from the reported position onto the network, then along the edge either way
            edges = [make_edge(nxt, [self.position] + after, e['zones'], e['canal'])]
            if not e['canal']:
                edges.append(make_edge(prev, [self.position] + list(reversed(before)), e['zones'], exit_ok=True))
            self.extra_adj[SHIP_NODE] = edges
            self.start_node, self.start_t, self.start_zones = SHIP_NODE, 0.0, frozenset()
            self.start_visited = frozenset([SHIP_NODE])
            self.path_next = None
        else:
            self.start_node, self.start_t, self.start_zones = r['origin'], 0.0, frozenset()
            self.start_visited = frozenset([r['origin']])
            self.path_next = None
        # Time is its own dimension only when it can change feasibility or cost later on.
        self.time_matters = self.deadline_h is not None or bool(ev['storms']) or self.live is not None
        self.edge_cache = {}
        self.storm_edges = set()
        for edges in list(GRAPH.adj.values()) + list(self.extra_adj.values()):
            for e in edges:
                for st in ev['storms']:
                    if any(haversine_nm(st['center'], p) <= st['radius_nm'] + 150 for p in e['points']):
                        self.storm_edges.add(id(e))

    def can_stop(self, port):
        p = GRAPH.ports.get(port)
        return p is not None and port not in self.closed_ports and self.ship['draft_m'] <= p['max_draft_m']

    def score_partial(self, lab):
        p = self.preset
        # With a berth time, the voyage ends no earlier than the berth, so arriving early saves no time:
        # count time (and the ship's daily cost) up to the berth, which keeps this a lower bound.
        t = lab.t if self.berth_h is None else max(lab.t, self.berth_h)
        lab.ps = (lab.cash + self.opex_h * (t - lab.t) + p['carbon_usd_per_t'] * lab.co2 + (p['time_value_mult'] - 1) * self.opex_h * t
                  + p['risk_loss_frac'] * self.ship['cargo_value_usd'] * RiskModel.route_risk(lab.expo))
        return lab

    def dominates(self, a, b):
        if self.berth_h is not None:
            # Arriving early saves nothing here, so compare plans including the value of fuel still on board;
            # otherwise a faster plan within the fuel tolerance would wrongly beat a slower, cheaper one.
            return (a.ps - a.fuel * self.dest_price <= b.ps - b.fuel * self.dest_price + EPS_SCORE / 10
                    and a.fuel >= b.fuel - EPS_FUEL_T and a.zones >= b.zones and (not self.time_matters or a.t <= b.t + EPS_HOURS))
        return (a.ps <= b.ps + EPS_SCORE and a.fuel >= b.fuel - EPS_FUEL_T and a.zones >= b.zones
                and (not self.time_matters or a.t <= b.t + EPS_HOURS))

    def lower_bound(self, lab):
        cap = self.ship['fuel_capacity_t']
        return lab.ps - lab.fuel * self.dest_price - max(0.0, self.dest_price - self.min_price) * cap

    def bunker_options(self, lab):
        port = lab.node
        if lab.refueled or port == self.r['destination'] or port not in self.prices or not self.can_stop(port):
            return
        at_origin = port == self.r['origin'] and lab.parent is None
        # At the departure port there is no extra waiting unless an event (e.g. a fleet queue) says otherwise.
        hours = BUNKER_HOURS + (self.ev['waits'].get(port, 0.0) if at_origin else self.waits[port])
        idle = self.idle_h * hours
        fuel_before = lab.fuel - idle
        if fuel_before < self.r['min_reserve_t'] and not at_origin:
            return
        fee = 0 if at_origin else GRAPH.ports[port]['stop_fee']
        price = self.prices[port]
        for frac in REFUEL_LEVELS:
            target = self.ship['fuel_capacity_t'] * frac
            buy = target - fuel_before
            if buy < MIN_PURCHASE_T:
                continue
            t = lab.t + hours
            if self.hard_deadline and t > self.deadline_h:
                continue
            cost = buy * price
            step = {'kind': 'bunker', 'port': port, 'buy_t': buy, 'price': price, 'fuel_cost': cost,
                    'stop_fee': fee, 'hours': hours, 'fuel_after': target, 'at_h': lab.t}
            yield Label(port, t, target, lab.cash + cost + fee + self.opex_h * hours,
                        lab.co2 + idle * CO2_PER_T_FUEL, lab.burn + idle, lab.expo, lab.zones,
                        lab.visited, True, lab, step)

    def simulate_edge(self, e, v, fuel0, t0):
        timed = id(e) in self.storm_edges or t0 < self.live_horizon_h
        key = (id(e), v, round(fuel0 / FUEL_BUCKET_T), round(t0 / 6) if timed else None)
        if key in self.edge_cache:
            return self.edge_cache[key]
        ship = self.ship
        fuel = round(fuel0 / FUEL_BUCKET_T) * FUEL_BUCKET_T
        t, burn, expo, hours, hs_max, risk_peak, sources, wind_max = t0, 0.0, 0.0, 0.0, 0.0, 0.0, set(), 0.0
        result = None
        for leg in e['legs']:
            c = self.cond.at(leg['mid'][0], leg['mid'][1], t)
            if c['hs'] > ship['max_hs_m']:
                break
            if c['wind_from'] is None:
                headwind = 0.7 * c['wind_ms']
            else:
                headwind = c['wind_ms'] * math.cos(math.radians(c['wind_from'] - leg['bearing']))
            current = c['current_kn'] * math.cos(math.radians(c['current_to'] - leg['bearing']))
            h, f = self.fuel_model.leg(ship, leg['nm'], v, fuel, c['hs'], headwind, current)
            r_leg, _ = self.risk.leg(c['hs'], ship, e['zones'])
            expo += RiskModel.exposure(r_leg, h)
            risk_peak = max(risk_peak, r_leg)
            t += h
            fuel -= f
            burn += f
            hours += h
            hs_max = max(hs_max, c['hs'])
            wind_max = max(wind_max, c['wind_ms'])
            sources.add(c['source'])
        else:
            result = {'hours': hours, 'burn': burn, 'expo': expo, 'hs_max': hs_max, 'risk_peak': risk_peak,
                      'sources': frozenset(sources), 'wind_max': wind_max}
        self.edge_cache[key] = result
        return result

    def traverse(self, lab, e, v):
        to = e['to']
        if to in lab.visited:
            return None
        if self.closed_zones.intersection(e['zones']) and not e.get('exit_ok'):
            return None   # a ship already inside a closed zone may still sail back out the way it came
        ship = self.ship
        canal = GRAPH.canals.get(e['canal']) if e['canal'] else None
        if canal and ship['draft_m'] > canal['max_draft_m']:
            return None
        sim = self.simulate_edge(e, v, lab.fuel, lab.t)
        if sim is None:
            return None  # sea state above the ship's safe limit at that time
        sail_h, burn, hs_max, risk_peak, sources = sim['hours'], sim['burn'], sim['hs_max'], sim['risk_peak'], sim['sources']
        wind_max = sim['wind_max']
        t, fuel, expo = lab.t + sail_h, lab.fuel - burn, lab.expo + sim['expo']
        cash = lab.cash + self.opex_h * sail_h
        canal_h, toll = 0.0, 0.0
        if canal:
            canal_h = canal['wait_h']
            toll = ship['suez_toll_usd'] * self.ev['toll_mult'] if e['canal'] == 'suez' else 0.0
            idle = self.idle_h * canal_h
            fuel -= idle
            burn += idle
            t += canal_h
            cash += toll + self.opex_h * canal_h
        if fuel < self.r['min_reserve_t']:
            return None
        if self.hard_deadline and t > self.deadline_h:
            return None
        insurance = 0.0
        for z in e['zones']:
            if z not in lab.zones:
                rate = (self.ev['war_risk'][z] + WAR_RISK_PER_LEVEL * self.ev['premium_bumps'].get(z, 0.0)
                        + cover.held_rate(ship['id'], z))   # held-covered premium outside a trading warranty
                insurance += rate * ship['hull_value_usd']
        cash += insurance
        step = {'kind': 'sail', 'from': lab.node, 'to': to, 'speed_kn': v, 'nm': e['nm'], 'hours': sail_h,
                'fuel_t': burn, 'hs_max': hs_max, 'zones': list(e['zones']), 'canal': e['canal'],
                'canal_hours': canal_h, 'toll': toll, 'insurance': insurance, 'arrive_h': t,
                'fuel_arrival': fuel, 'risk_peak': risk_peak, 'weather_sources': sorted(sources), 'points': e['points'],
                'wind_max': wind_max}
        return Label(to, t, fuel, cash, lab.co2 + burn * CO2_PER_T_FUEL, lab.burn + burn, expo,
                     lab.zones | frozenset(e['zones']), lab.visited | {to}, False, lab, step)

    def finalize(self, lab):
        ship, p = self.ship, self.preset
        credit = lab.fuel * self.dest_price
        late_days = 0.0
        if self.deadline_h is not None and lab.t > self.deadline_h:
            late_days = (lab.t - self.deadline_h) / 24
        delay = late_days * ship['delay_penalty_per_day_usd'] if self.r['deadline_type'] == 'soft' else 0.0
        risk = RiskModel.route_risk(lab.expo)
        # Fuel already on board is valued at the same market price as fuel left at arrival, so the
        # cash total reflects fuel actually burned rather than treating the starting tank as free.
        opening = self.r['fuel_onboard_t'] * self.dest_price
        # Waiting at anchorage for the berth: the engine idles, the ship still costs its daily rate.
        anchor_h = max(0.0, self.berth_h - lab.t) if self.berth_h is not None else 0.0
        anchor_fuel = self.idle_h * anchor_h
        credit -= anchor_fuel * self.dest_price
        co2 = lab.co2 + anchor_fuel * CO2_PER_T_FUEL
        t_end = lab.t + anchor_h
        ets = self.ets_share * co2 * compliance.ETS_USD_PER_T   # EU allowances: a real, route-dependent cost
        cash = lab.cash + opening - credit + delay + ets + self.opex_h * anchor_h
        risk_adj = p['risk_loss_frac'] * risk * ship['cargo_value_usd']
        time_pref = (p['time_value_mult'] - 1) * self.opex_h * t_end
        carbon = p['carbon_usd_per_t'] * co2
        return {'label': lab, 'cash': cash, 'credit': credit, 'opening': opening, 'ets': ets, 'delay': delay, 'late_days': late_days, 'risk': risk,
                'risk_adj': risk_adj, 'time_pref': time_pref, 'carbon': carbon, 'anchor_h': anchor_h, 'anchor_fuel': anchor_fuel,
                'co2': co2, 't_end': t_end, 'score': cash + risk_adj + time_pref + carbon}

    def run(self):
        r = self.r
        if (not r.get('resume') and not r.get('start_at') and not self.can_stop(r['origin'])) or not self.can_stop(r['destination']):
            return []  # port closed or too shallow for this ship; explained by _explain_infeasible
        start = Label(self.start_node, self.start_t, r['fuel_onboard_t'], 0.0, 0.0, 0.0, 0.0, self.start_zones,
                      self.start_visited, False, None, None)
        counter = itertools.count()
        self.score_partial(start)
        heap = [(self.lower_bound(start), next(counter), start)]
        store = {}
        finals = []
        best = math.inf
        while heap:
            lb, _, lab = heapq.heappop(heap)
            if lab.dead:
                continue
            if best < math.inf and lb > best * PRUNE_FACTOR + 1:
                break
            if lab.node == r['destination']:
                fin = self.finalize(lab)
                finals.append(fin)
                best = min(best, fin['score'])
                continue
            children = list(self.bunker_options(lab))
            for e in self.extra_adj.get(lab.node) or GRAPH.adj[lab.node]:
                if self.follow and self.path_next and e['to'] != self.path_next.get(lab.node):
                    continue   # 'stay on course': only the committed route
                for v in self.speeds:
                    child = self.traverse(lab, e, v)
                    if child is not None:
                        children.append(child)
            for child in children:
                self.score_partial(child)
                key = (child.node, child.refueled)
                bucket = store.setdefault(key, [])
                if any(self.dominates(o, child) for o in bucket):
                    continue
                keep = []
                for o in bucket:
                    if self.dominates(child, o):
                        o.dead = True
                    else:
                        keep.append(o)
                bucket[:] = keep
                if len(bucket) >= MAX_LABELS_PER_STATE:
                    # Full: keep the better plans, never just the earliest ones.
                    worst = max(bucket, key=self.lower_bound)
                    if self.lower_bound(worst) <= self.lower_bound(child):
                        continue
                    worst.dead = True
                    bucket.remove(worst)
                bucket.append(child)
                self.labels_created += 1
                heapq.heappush(heap, (self.lower_bound(child), next(counter), child))
        return finals


# ---------------------------------------------------------------- results

def corridor_of(zones):
    if 'suez' in zones:
        return 'suez'
    if 'cape' in zones:
        return 'cape'
    return 'direct'


def _source_kind(sources):
    kinds = {'event' if x.startswith('event: ') else 'live' if x == LIVE_SOURCE else 'seasonal' for x in sources}
    if 'event' in kinds:
        return 'event'
    return kinds.pop() if len(kinds) == 1 else 'mixed'


def build_plan(fin, search):
    r, ship = search.r, search.ship
    steps = []
    lab = fin['label']
    while lab.parent is not None:
        steps.append(lab.step)
        lab = lab.parent
    steps.reverse()
    dep = r['departure']
    eta = lambda h: (dep + timedelta(hours=round(h))).replace(minute=0, second=0, microsecond=0).isoformat().replace('+00:00', 'Z')
    legs, bunkering, path = [], [], []
    if r.get('resume') or r.get('start_at'):
        checkpoints = [{'node': search.start_node, 'name': (r.get('start_at') or {}).get('label') or GRAPH.name(search.start_node), 'loc': search.position,
                        'eta': eta(search.start_t), 'fuel_t': round(r['fuel_onboard_t'], 1), 'kind': 'position'}]
    else:
        checkpoints = [{'node': r['origin'], 'name': GRAPH.name(r['origin']), 'loc': GRAPH.loc(r['origin']),
                        'eta': eta(0), 'fuel_t': round(r['fuel_onboard_t'], 1), 'kind': 'origin'}]
    totals = {'fuel_purchased_t': 0.0, 'fuel_purchased_usd': 0.0, 'port_fees_usd': 0.0, 'canal_tolls_usd': 0.0,
              'insurance_usd': 0.0, 'nm': 0.0, 'waiting_h': 0.0}
    for s in steps:
        if s['kind'] == 'bunker':
            totals['fuel_purchased_t'] += s['buy_t']
            totals['fuel_purchased_usd'] += s['fuel_cost']
            totals['port_fees_usd'] += s['stop_fee']
            totals['waiting_h'] += s['hours']
            bunkering.append({'port': s['port'], 'name': GRAPH.name(s['port']), 'buy_t': round(s['buy_t'], 1),
                              'price_usd_t': s['price'], 'cost_usd': round(s['fuel_cost']), 'stop_fee_usd': s['stop_fee'],
                              'stop_hours': round(s['hours'], 1), 'fuel_after_t': round(s['fuel_after'], 1),
                              'eta': eta(s['at_h'])})
            checkpoints[-1]['bunker_t'] = round(s['buy_t'], 1)
            checkpoints[-1]['fuel_after_bunker_t'] = round(s['fuel_after'], 1)
            continue
        totals['nm'] += s['nm']
        totals['canal_tolls_usd'] += s['toll']
        totals['insurance_usd'] += s['insurance']
        totals['waiting_h'] += s['canal_hours']
        pts = s['points']
        path.extend(pts if not path else pts[1:])
        legs.append({'from': s['from'], 'to': s['to'], 'from_name': GRAPH.name(s['from']), 'to_name': GRAPH.name(s['to']),
                     'speed_kn': s['speed_kn'], 'distance_nm': round(s['nm']), 'hours': round(s['hours'], 1),
                     'fuel_t': round(s['fuel_t'], 1), 'max_wave_m': round(s['hs_max'], 1), 'zones': s['zones'],
                     'canal': s['canal'], 'canal_wait_h': s['canal_hours'], 'toll_usd': s['toll'],
                     'war_risk_premium_usd': round(s['insurance']), 'peak_leg_risk': round(s['risk_peak'], 3),
                     'max_wind_ms': round(s['wind_max'], 1), 'weather_sources': s['weather_sources'],
                     'weather_source': _source_kind(s['weather_sources']), 'arrive_eta': eta(s['arrive_h'])})
        checkpoints.append({'node': s['to'], 'name': GRAPH.name(s['to']), 'loc': GRAPH.loc(s['to']),
                            'eta': eta(s['arrive_h']), 'fuel_t': round(s['fuel_arrival'], 1),
                            'kind': 'destination' if s['to'] == r['destination'] else
                                    ('port' if s['to'] in GRAPH.ports else 'waypoint')})
    lab = fin['label']
    days = lab.t / 24
    corridor = corridor_of(lab.zones)
    operating = search.opex_h * (fin['t_end'] - search.start_t)
    return {
        'corridor': corridor, 'corridor_label': CORRIDORS[corridor], 'zones': sorted(lab.zones),
        'summary': {'distance_nm': round(totals['nm']), 'days': round(days, 2), 'eta': eta(lab.t),
                    'fuel_burned_t': round(lab.burn + fin['anchor_fuel'], 1), 'fuel_purchased_t': round(totals['fuel_purchased_t'], 1),
                    'fuel_on_arrival_t': round(lab.fuel - fin['anchor_fuel'], 1), 'co2_t': round(fin['co2'], 1),
                    'anchorage_hours': round(fin['anchor_h'], 1), 'berth_eta': eta(fin['t_end']),
                    'waiting_hours': round(totals['waiting_h'], 1), 'route_risk': round(fin['risk'], 3),
                    'late_days': round(fin['late_days'], 2),
                    'remaining_days': round((lab.t - search.start_t) / 24, 2),
                    'speeds_kn': sorted({l['speed_kn'] for l in legs}),
                    'live_weather_legs': sum(1 for l in legs if LIVE_SOURCE in l['weather_sources']),
                    'legs': len(legs)},
        'costs': {'fuel_on_board_at_departure_usd': round(fin['opening']),
                  'fuel_purchased_usd': round(totals['fuel_purchased_usd']),
                  'fuel_left_credit_usd': -round(fin['credit']),
                  'canal_tolls_usd': round(totals['canal_tolls_usd']),
                  'port_fees_usd': round(totals['port_fees_usd']),
                  'operating_usd': round(operating),
                  'war_risk_insurance_usd': round(totals['insurance_usd']),
                  'delay_penalty_usd': round(fin['delay']),
                  'eu_ets_usd': round(fin['ets']),
                  'cash_total_usd': round(fin['cash'])},
        'decision': {'cash_total_usd': round(fin['cash']), 'risk_adjustment_usd': round(fin['risk_adj']),
                     'time_preference_usd': round(fin['time_pref']), 'carbon_shadow_usd': round(fin['carbon']),
                     'score_usd': round(fin['score'])},
        'legs': legs, 'checkpoints': checkpoints, 'bunkering': bunkering, 'path': path,
    }


def _search(r, ev):
    s = Search(r, ev)
    return s, s.run()


def _search_all_corridors(r, ev):
    """One open search, plus one search per major corridor so an alternative is always shown."""
    s, finals = _search(r, ev)
    found = {corridor_of(f['label'].zones) for f in finals}
    for corridor, block in (('suez', 'cape'), ('cape', 'suez')):
        if corridor not in found and block not in r['avoid_zones']:
            _, extra = _search(dict(r, avoid_zones=list(r['avoid_zones']) + [block]), ev)
            finals.extend(f for f in extra if corridor_of(f['label'].zones) == corridor)
    return s, finals


def _choose(finals):
    """Best overall plan plus the best plan in each other corridor."""
    by_corridor = {}
    for f in finals:
        c = corridor_of(f['label'].zones)
        if c not in by_corridor or f['score'] < by_corridor[c]['score']:
            by_corridor[c] = f
    return sorted(by_corridor.values(), key=lambda f: f['score'])


def _explain_infeasible(r, ev, closed_zones, closed_ports):
    origin, dest, ship = r['origin'], r['destination'], r['ship']
    reasons = []
    for label, p in (('Origin', origin), ('Destination', dest)):
        if p in closed_ports:
            reasons.append(f'{label} port {GRAPH.name(p)} is closed.')
        if ship['draft_m'] > GRAPH.ports[p]['max_draft_m']:
            reasons.append(f"{ship['name']} draws {ship['draft_m']} m; {GRAPH.name(p)} accepts at most {GRAPH.ports[p]['max_draft_m']} m.")
    if reasons:
        return {'kind': 'port', 'message': ' '.join(reasons)}
    reach = GRAPH.reachable(origin, closed_zones)
    if dest not in reach:
        blocking = sorted({z for n in reach for e in GRAPH.adj[n] if e['to'] not in reach for z in e['zones'] if z in closed_zones})
        uninsured = cover.uninsured_zones(ship['id'], GRAPH.zones, ev.get('cover_cancelled', ())) if not r.get('allow_uninsured') else set()
        names = ', '.join(GRAPH.zones[z]['label'] + (" (not covered by the ship's insurance)" if z in uninsured and z not in ev['closed_zones'] else '')
                          for z in blocking)
        dest_side = GRAPH.reachable(dest, closed_zones)
        alt = [p for p in GRAPH.ports if p in dest_side and p not in closed_ports and p != dest]
        alt.sort(key=lambda p: haversine_nm(GRAPH.loc(p), GRAPH.loc(origin)))
        msg = f'Every sea route from {GRAPH.name(origin)} passes through {names}, which is closed to this ship. No sea-only plan exists.'
        if set(blocking) & uninsured:
            msg += ' Buy separate war-risk cover (then allow uninsured routes in Advanced) or use another ship.'
        if alt:
            msg += f' The nearest open port beyond the closure is {GRAPH.name(alt[0])}; moving cargo there overland would need separate planning.'
        return {'kind': 'closure', 'message': msg, 'blocking_zones': blocking,
                'nearest_open_port': alt[0] if alt else None}
    if ev['storms']:
        calm = dict(ev, storms=[])
        _, finals = _search_all_corridors(r, calm)
        if finals:
            best = min(finals, key=lambda f: f['score'])
            path = []
            lab = best['label']
            while lab.parent is not None:
                if lab.step['kind'] == 'sail':
                    path.extend(lab.step['points'])
                lab = lab.parent
            blocking = [st['id'] for st in ev['storms']
                        if any(haversine_nm(st['center'], p) <= st['radius_nm'] + 60 for p in path)] or [st['id'] for st in ev['storms']]
            worst = max(st['hs_m'] for st in ev['storms'] if st['id'] in blocking)
            return {'kind': 'weather', 'blocking_storms': blocking,
                    'message': f"Every open route crosses storm conditions ({worst:g} m waves) above {ship['name']}'s "
                               f"{ship['max_hs_m']:g} m safe limit. Without the storm, a plan exists. Hold in port or "
                               f"re-plan once the storm forecast changes."}
    if r.get('_live') is not None:
        _, finals = _search_all_corridors(dict(r, _live=None), ev)
        if finals:
            return {'kind': 'weather', 'blocking_storms': [], 'live_forecast': True,
                    'message': f"The live Open-Meteo forecast shows waves above {ship['name']}'s {ship['max_hs_m']:g} m "
                               f"safe limit on every open route within the next 7 days. With seasonal conditions a plan "
                               f"exists. Hold in port, or re-plan when the forecast updates."}
    if r['deadline_days'] and r['deadline_type'] == 'hard':
        relaxed = dict(r, deadline_days=None, priority='fastest')
        s, finals = _search(relaxed, ev)
        if finals:
            fastest = min(f['label'].t for f in finals) / 24
            return {'kind': 'deadline', 'fastest_days': round(fastest, 1),
                    'message': f"No plan arrives within {r['deadline_days']:g} days. The fastest feasible plan takes {fastest:.1f} days."}
    return {'kind': 'constraints', 'message': 'Routes exist, but none satisfies fuel reserve, tank capacity or sea-state limits. '
                                              'Try more fuel on board, a lower reserve, or waiting for weather to clear.'}


IMPACT_MAIN = 'affects this voyage'
IMPACT_ALT = 'affects an alternative route'
IMPACT_NONE = 'no impact on any route considered'


def _touches(rec, plan, storms):
    if plan is None:
        return False
    if rec['type'] == 'storm':
        st = storms.get(rec['id'])
        return bool(st) and any(haversine_nm(st['center'], pt) <= st['radius_nm'] + 60 for pt in plan['path'])
    if rec['zone']:
        return rec['zone'] in plan['zones']
    return rec['port'] in {c['node'] for c in plan['checkpoints']}


def _event_impacts(records, main_plans, alt_plans, storms, blocking_storms):
    """Main = the recommended plan and the original plan; alternatives = every other corridor considered."""
    out = []
    for rec in records:
        if rec['type'] == 'storm' and rec['id'] in blocking_storms:
            impact = IMPACT_MAIN
        elif any(_touches(rec, p, storms) for p in main_plans):
            impact = IMPACT_MAIN
        elif any(_touches(rec, p, storms) for p in alt_plans):
            impact = IMPACT_ALT
        else:
            impact = IMPACT_NONE
        out.append(dict(rec, impact=impact))
    return out


def _hazards(r, ev):
    """Zones and ports to highlight on the map, from the same inputs the optimizer used."""
    ship = r['ship']
    closed_z = set(ev['closed_zones']) | set(r['avoid_zones'])
    closed_p = set(ev['closed_ports']) | set(r['avoid_ports'])
    uninsured = cover.uninsured_zones(ship['id'], GRAPH.zones, ev.get('cover_cancelled', ()))
    if not r.get('allow_uninsured'):
        closed_z |= uninsured
    risk = RiskModel(GRAPH.zones, ev['bumps'], ev['news'], ev['traffic'])
    zones = []
    for z, info in GRAPH.zones.items():
        sec, tr, wr = risk.security([z]), ev['traffic'].get(z), ev['war_risk'].get(z, 0.0)
        reasons = []
        if z in closed_z:
            level = 'closed'
            reasons.append('closed' if z in ev['closed_zones'] else "not covered by the ship's insurance" if z in uninsured and z not in r['avoid_zones'] else 'avoided by operator')
        else:
            high = sec >= 0.25 or wr >= 0.005 or (tr is not None and tr < 0.5)
            elevated = sec >= 0.1 or wr > 0 or (tr is not None and tr < 0.8)
            level = 'high' if high else 'elevated' if elevated else 'normal'
        if sec >= 0.1:
            reasons.append(f'security risk {sec:.2f}')
        if wr > 0:
            reasons.append(f'war-risk premium {wr:.2%} of hull')
        if tr is not None and tr < 0.8:
            reasons.append(f'traffic {tr:.0%} of normal')
        adv = len((ev.get('advisories') or {}).get(z, []))
        if adv:
            reasons.append(f"{adv} official NGA security warning(s)")
        if ev['news'].get(z, 0) - adv > 0:
            reasons.append(f"{ev['news'][z] - adv} disruption headline(s)")
        zones.append({'id': z, 'label': info['label'], 'location': info['location'], 'radiusKm': info['radiusKm'],
                      'level': level, 'reasons': reasons})
    zlevel = {z['id']: z['level'] for z in zones}
    ports = []
    for code, p in GRAPH.ports.items():
        reasons = []
        if code in closed_p:
            reasons.append('closed' if code in ev['closed_ports'] else 'avoided by operator')
        if ship['draft_m'] > p['max_draft_m']:
            reasons.append(f"too shallow for {ship['name']} ({p['max_draft_m']:g} m limit, ship draws {ship['draft_m']:g} m)")
        for z, info in GRAPH.zones.items():
            if zlevel[z] in ('high', 'closed') and haversine_nm(p['loc'], info['location']) * 1.852 <= info['radiusKm'] * 1.5:
                reasons.append(f"inside {info['label']} ({zlevel[z]} risk)")
        gate = {z for z in GRAPH.adj[code][0]['zones']} if GRAPH.adj[code] else set()
        for e in GRAPH.adj[code][1:]:
            gate &= set(e['zones'])
        for z in gate:
            if zlevel[z] in ('high', 'closed', 'elevated'):
                reasons.append(f"every sea route passes {GRAPH.zones[z]['label']} ({zlevel[z]})")
        wait = ev['waits'].get(code)
        if wait is not None and wait > p['wait_h'] * 1.5:
            reasons.append(f'congested: {wait:g} h expected wait')
        if reasons:
            severe = any(x.startswith(('closed', 'too shallow', 'avoided')) or 'closed)' in x or 'high)' in x or '(high risk)' in x for x in reasons)
            ports.append({'port': code, 'name': p['name'], 'loc': p['loc'], 'level': 'severe' if severe else 'caution', 'reasons': reasons})
    return {'zones': zones, 'ports': ports}


CHANGE_COURSE_THRESHOLD_USD = 25000   # below this, a different route is not worth the disruption


def _route_nodes(plan):
    return [c['node'] for c in plan['checkpoints']]


def _resume_decision(r, ev, plans):
    """Compare the best plan from the ship's position with simply staying on the committed route."""
    s = Search(r, ev)
    s.follow = True
    cont_finals = s.run()
    cont = build_plan(min(cont_finals, key=lambda f: f['score']), s) if cont_finals else None
    res = r['resume']
    info = {'position': s.position, 'elapsed_days': round(res['elapsed_h'] / 24, 2), 'fuel_onboard_t': round(res['fuel_onboard_t'], 1),
            'next_node': res['route'][res['leg_index'] + 1], 'next_name': GRAPH.name(res['route'][res['leg_index'] + 1]),
            'continue': cont, 'threshold_usd': CHANGE_COURSE_THRESHOLD_USD}
    best = plans[0] if plans else None
    if not best:
        info.update(recommendation='no_plan', reason='No feasible plan from the current position.')
        return info, plans
    if cont is None:
        route, i = res['route'], res['leg_index']
        ahead = set()
        for a, b in zip(route[i:-1], route[i + 1:]):
            ahead |= set(GRAPH.edge(a, b)['zones'])
        closed = sorted(ahead & s.closed_zones)
        why = (f"The committed route ahead passes {', '.join(GRAPH.zones[z]['label'] for z in closed)}, which is now closed."
               if closed else
               (f"The committed route ahead runs into the reported storm ({max(st['hs_m'] for st in ev['storms']):g} m waves), above {r['ship']['name']}'s "
                f"{r['ship']['max_hs_m']:g} m safe limit." if ev['storms'] and max(st['hs_m'] for st in ev['storms']) > r['ship']['max_hs_m'] else
                'The committed route is no longer feasible from here (fuel reserve, deadline or sea state).'))
        info.update(recommendation='change_course', saving_score_usd=None, reason=why)
        return info, plans
    same = _route_nodes(best) == _route_nodes(cont)
    saving = cont['decision']['score_usd'] - best['decision']['score_usd']
    info.update(saving_score_usd=round(saving), saving_cash_usd=cont['costs']['cash_total_usd'] - best['costs']['cash_total_usd'],
                days_difference=round(best['summary']['days'] - cont['summary']['days'], 2))
    if same:
        info.update(recommendation='stay', reason='Staying on the committed route is still the best plan (speeds may be adjusted).')
        return info, [best] + plans[1:]
    if saving < CHANGE_COURSE_THRESHOLD_USD:
        info.update(recommendation='stay', reason=f'A different route scores only ${saving:,.0f} better, below the ${CHANGE_COURSE_THRESHOLD_USD:,} threshold for changing course.')
        others = [p for p in plans if _route_nodes(p) != _route_nodes(cont)]
        return info, [cont] + others[:1]
    info.update(recommendation='change_course', reason=f'Changing course is ${saving:,.0f} better on the decision score than staying on the committed route.')
    return info, plans


def _jit_summary(r, search, rush_fin, jit_fin, dest_wait):
    """Sail at the usual speeds then wait at anchorage, versus arriving just as the berth is ready."""
    rush = search.finalize(rush_fin['label'])   # same voyage, re-costed with the berth time
    dep = r['departure']
    iso = lambda h: (dep + timedelta(hours=round(h))).isoformat().replace('+00:00', 'Z')
    side = lambda f: {'arrive_eta': iso(f['label'].t), 'anchorage_h': round(f['anchor_h'], 1),
                      'fuel_t': round(f['label'].burn + f['anchor_fuel'], 1), 'co2_t': round(f['co2'], 1),
                      'cash_usd': round(f['cash']), 'speeds_kn': sorted({s['speed_kn'] for s in _steps(f['label']) if s['kind'] == 'sail'})}
    a, b = side(rush), side(jit_fin)
    same_route = [s.get('to') for s in _steps(rush['label'])] == [s.get('to') for s in _steps(jit_fin['label'])]
    out = {'berth_ready_eta': iso(r['berth_h']), 'source': r['berth_source'], 'queue_h': dest_wait,
           'sail_then_wait': a, 'just_in_time': b, 'same_route': same_route,
           'fuel_saved_t': round(a['fuel_t'] - b['fuel_t'], 1), 'co2_saved_t': round(a['co2_t'] - b['co2_t'], 1),
           'cash_saved_usd': a['cash_usd'] - b['cash_usd'], 'anchorage_avoided_h': round(a['anchorage_h'] - b['anchorage_h'], 1)}
    if rush['anchor_h'] < 1:
        out['note'] = 'The berth is ready before the ship can arrive, so there is no waiting to avoid.'
    elif out['fuel_saved_t'] < 1:
        out['note'] = 'The ship is already at its slowest economical speeds, so it cannot absorb the wait by slowing down.'
    else:
        out['note'] = (f"Slowing down to arrive when the berth is ready avoids {out['anchorage_avoided_h']:g} h at anchorage "
                       f"and saves {out['fuel_saved_t']:g} t of fuel ({out['co2_saved_t']:g} t CO₂).")
    return out


def _steps(lab):
    out = []
    while lab.parent is not None:
        out.append(lab.step)
        lab = lab.parent
    return out[::-1]


def plan_voyage(request):
    r = normalize_request(request)
    ev = build_inputs(r, r['events'])
    run = _search if (r.get('resume') or r.get('start_at')) else _search_all_corridors
    # Just-in-time arrival: when the destination berth is only free later (an operator berth time, or a
    # congestion queue at the destination), plan first as if sailing straight in, then re-plan knowing the
    # berth time, so the ship can slow down instead of waiting at anchorage.
    rush = None
    dest_wait = ev['waits'].get(r['destination'])
    if r['berth_h'] is not None or dest_wait:
        search, finals = run(dict(r, berth_h=None), ev)
        if finals:
            rush = min(finals, key=lambda f: f['score'])
            if r['berth_h'] is None:
                r['berth_h'], r['berth_source'] = rush['label'].t + dest_wait, 'congestion'
            # Same route and refuelling ports as the best plan; re-optimize speeds (1 kn steps) for the berth time.
            jit = Search(r, ev)
            steps = [st for st in _steps(rush['label']) if st['kind'] == 'sail']
            jit.path_next = {st['from']: st['to'] for st in steps}
            jit.follow = True
            jit_finals = jit.run()
            # Every plan is re-costed with the berth time: sailing as usual and waiting stays an option.
            finals = [jit.finalize(f['label']) for f in finals] + jit_finals
            search = jit
    else:
        search, finals = run(r, ev)
    chosen = _choose(finals)
    plans = [build_plan(f, search) for f in chosen]
    resume_info = None
    if r.get('resume'):
        resume_info, plans = _resume_decision(r, ev, plans)
    for p in plans:
        p['compliance'] = compliance.screen(p, r, GRAPH.name, lambda z: GRAPH.zones[z]['label'])
        p['compliance'] += cover.findings(p, r, lambda z: GRAPH.zones[z]['label'], ev.get('cover_cancelled', ()))
    result = {
        'status': 'ok' if plans else 'infeasible',
        'ship': {k: r['ship'][k] for k in ('id', 'name', 'type', 'draft_m', 'fuel_capacity_t', 'min_reserve_t')},
        'request': {'origin': r['origin'], 'destination': r['destination'],
                    'departure': r['departure'].isoformat().replace('+00:00', 'Z'), 'priority': r['priority'],
                    'deadline_days': r['deadline_days'], 'deadline_type': r['deadline_type'],
                    'fuel_onboard_t': r['fuel_onboard_t'], 'min_reserve_t': r['min_reserve_t'],
                    'avoid_zones': r['avoid_zones'], 'avoid_ports': r['avoid_ports'], 'live_weather': r['live_weather'],
                    'conditions': r['conditions'], 'cargo': r['cargo'], 'affiliations': r['affiliations'], 'allow_uninsured': r['allow_uninsured'],
                    'start_at': {k: v for k, v in (r.get('start_at') or {}).items() if k != 'edge'} or None,
                    'berth_ready_days': round(r['berth_h'] / 24, 2) if r['berth_h'] else None, 'berth_source': r['berth_source'],
                    'weights': {k: r['preset'][k] for k in WEIGHT_LIMITS}},
        'weather': dict(live_weather.current_status(), requested=r['live_weather'], used=r['_live'] is not None),
        'conditions': {'mode': r['conditions'], 'as_of': TODAY['as_of'] if r['conditions'] == 'today' else None,
                       'items': ev['situation'], 'portwatch': portwatch.status() if r['conditions'] == 'today' else None,
                       'advisories': {k: advisories.current()[k] for k in ('status', 'retrieved_at', 'error')} if r['conditions'] == 'today' else None},
        'preset': r['preset'],
        'cargo': {'id': r['cargo'], 'label': CARGO_SEGMENTS[r['cargo']]['label']},
        'hazards': _hazards(r, ev),
        'recommended': plans[0] if plans else None,
        'alternatives': plans[1:],
        'fuel_model': {'name': search.fuel_model.name, 'description': search.fuel_model.description,
                       'ship_model': (getattr(search.fuel_model, 'ships', {}).get(r['ship']['id']) or {}).get('source', 'simulated'),
                       'reports': (getattr(search.fuel_model, 'ships', {}).get(r['ship']['id']) or {}).get('reports')},
        'assumptions': ASSUMPTIONS,
        'search': {'plans_evaluated': len(finals), 'partial_plans': search.labels_created},
    }
    if plans and rush is not None:
        result['jit'] = _jit_summary(r, search, rush, chosen[0], dest_wait)
    if plans and not r.get('resume') and request.get('uncertainty', True) is not False:
        seed = json.dumps(request, sort_keys=True, default=str)
        result['uncertainty'] = uncertainty.simulate(search, chosen[0]['label'], seed)
    if plans:
        best = plans[0]
        result['comparison'] = [{'corridor': p['corridor'], 'corridor_label': p['corridor_label'],
                                 'extra_cash_usd': p['costs']['cash_total_usd'] - best['costs']['cash_total_usd'],
                                 'extra_days': round(p['summary']['days'] - best['summary']['days'], 2),
                                 'extra_score_usd': p['decision']['score_usd'] - best['decision']['score_usd']}
                                for p in plans[1:]]
    elif r.get('resume') or r.get('start_at'):
        result['infeasible'] = {'kind': 'position', 'message': 'From the ship\'s current position no plan satisfies the constraints '
                                '(closures ahead and behind, fuel reserve, deadline or sea state). Consider a lower reserve, '
                                'a later deadline, or waiting for conditions to change.'}
    else:
        result['infeasible'] = _explain_infeasible(r, ev, search.closed_zones, search.closed_ports)

    if resume_info is not None:
        result['resume'] = resume_info
        storms = {st['id']: st for st in ev['storms']}
        res = r['resume']
        ahead_route = [SHIP_NODE] + res['route'][res['leg_index'] + 1:]
        ahead = {'zones': set(), 'checkpoints': [{'node': n} for n in ahead_route], 'path': []}
        for a_, b_ in zip(res['route'][res['leg_index']:-1], res['route'][res['leg_index'] + 1:]):
            e = GRAPH.edge(a_, b_)
            ahead['zones'] |= set(e['zones'])
            ahead['path'].extend(e['points'])
        result['events'] = _event_impacts(ev['records'], [plans[0] if plans else None, resume_info['continue'], ahead],
                                          plans[1:], storms, set())
    elif r['events']:
        base_req = dict(r, events=[])
        if r['berth_source'] == 'congestion':
            base_req['berth_h'] = None
        base_ev = build_inputs(r, [])
        bsearch, bfinals = _search_all_corridors(base_req, base_ev)
        bchosen = _choose(bfinals)
        baseline_all = [build_plan(f, bsearch) for f in bchosen]
        baseline = baseline_all[0] if baseline_all else None
        storms = {st['id']: st for st in ev['storms']}
        blocking = set((result.get('infeasible') or {}).get('blocking_storms', []))
        result['baseline'] = baseline
        result['events'] = _event_impacts(ev['records'], [plans[0] if plans else None, baseline],
                                          plans[1:] + baseline_all[1:], storms, blocking)
        if baseline and plans:
            rec = plans[0]
            result['change'] = {
                'route_changed': [c['node'] for c in rec['checkpoints']] != [c['node'] for c in baseline['checkpoints']],
                'corridor_before': baseline['corridor'], 'corridor_after': rec['corridor'],
                'extra_cash_usd': rec['costs']['cash_total_usd'] - baseline['costs']['cash_total_usd'],
                'extra_days': round(rec['summary']['days'] - baseline['summary']['days'], 2),
                'extra_fuel_t': round(rec['summary']['fuel_burned_t'] - baseline['summary']['fuel_burned_t'], 1),
            }
    return result


def conditions_payload():
    return {'today': TODAY, 'portwatch': portwatch.status(), 'weather': live_weather.current_status()}


def network_payload():
    return {'cargo_segments': {k: v['label'] for k, v in CARGO_SEGMENTS.items()}, 'weight_limits': WEIGHT_LIMITS,
            'affiliations': {k: v['label'] for k, v in compliance.AFFILIATIONS.items()},'ports': GRAPH.ports, 'waypoints': GRAPH.waypoints, 'zones': GRAPH.zones, 'canals': GRAPH.canals,
            'edges': NETWORK['edges'], 'presets': PRESETS, 'event_types': list(EVENT_TYPES), 'note': NETWORK.get('_note')}


def fleet_payload():
    return {'ships': FLEET_LIST, 'insurance': cover.POLICIES, 'speed_options': {s['id']: speed_options(s) for s in FLEET_LIST}}
