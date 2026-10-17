"""Operations: approve a plan, keep watching it, and keep an audit trail.

* Approve: the server re-runs the optimizer on the submitted inputs (it never trusts a plan sent by
  the browser), stores the voyage, and writes an audit entry.
* Monitor: a background thread re-checks every active voyage from where the ship is now. Position
  comes from its linked AIS transponder when available, else from the latest noon report, else
  from the approved schedule. It re-plans with the latest conditions and raises alerts when the
  plan should change (route, arrival time, fuel, feasibility).
* Dispatch: voyage orders for the master and bunker enquiries are generated from the plan's own
  numbers. Tideline drafts them; a person sends them. Nothing is booked automatically.
* Audit: every action is appended to runtime/audit.jsonl. Each entry carries the SHA-256 of the
  previous one, so any edit to the history breaks the chain and is detected.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ais
import compliance
import sanctions
from optimizer import FLEET, GRAPH, SHIP_NODE, haversine_nm, plan_voyage

ROOT = Path(__file__).resolve().parent
MONITOR_MINUTES = float(os.getenv('TIDELINE_MONITOR_MIN', '15'))
OFF_ROUTE_NM = 150          # an AIS position further than this from the approved route is "off route"
AIS_MAX_AGE_H = 6           # older AIS fixes are not used
NOON_MAX_AGE_H = 36
ETA_ALERT_H = 12
FUEL_ALERT_FRAC = 0.2
ACTIVE = ('approved', 'underway')

_lock = threading.RLock()
_thread = None


def _dir():
    d = Path(os.getenv('TIDELINE_RUNTIME_DIR', ROOT / 'runtime'))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


# ------------------------------------------------------------------ storage and audit chain

def _load():
    f = _dir() / 'voyages.json'
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except ValueError:
        return {}


def _save(voyages):
    f = _dir() / 'voyages.json'
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(voyages, indent=1))
    tmp.replace(f)


def _audit_lines():
    f = _dir() / 'audit.jsonl'
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _hash(entry):
    body = {k: v for k, v in entry.items() if k != 'hash'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def audit(action, voyage_id=None, actor='system', detail=None):
    with _lock:
        f = _dir() / 'audit.jsonl'
        prev = None
        if f.exists():
            lines = f.read_text().splitlines()
            prev = json.loads(lines[-1])['hash'] if lines else None
        entry = {'at': _iso(_now()), 'action': action, 'voyage': voyage_id, 'actor': str(actor)[:80],
                 'detail': detail or {}, 'prev': prev}
        entry['hash'] = _hash(entry)
        with f.open('a') as fh:
            fh.write(json.dumps(entry) + '\n')
        return entry


def verify_audit():
    prev = None
    lines = _audit_lines()
    for i, e in enumerate(lines):
        if e.get('prev') != prev or _hash(e) != e.get('hash'):
            return {'ok': False, 'entries': len(lines), 'broken_at': i}
        prev = e['hash']
    return {'ok': True, 'entries': len(lines)}


def audit_log(limit=200, voyage_id=None):
    lines = _audit_lines()
    if voyage_id:
        lines = [e for e in lines if e.get('voyage') == voyage_id]
    return {'entries': lines[-limit:][::-1], 'verification': verify_audit()}


# ------------------------------------------------------------------ approval

def _committed(plan, route_offset=0.0, route=None):
    route = route or [c['node'] for c in plan['checkpoints']]
    return {'route': route, 'first_offset': route_offset, 'plan': plan}


def approve(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('request'), dict):
        raise ValueError('Send {request, approved_by, note}')
    who = str(payload.get('approved_by') or '').strip()
    if not who:
        raise ValueError('approved_by is required: a person approves every voyage')
    req = copy.deepcopy(payload['request'])
    if req.get('resume') or req.get('start_at'):
        raise ValueError('Approve the plan from the departure port (reset voyage progress first). '
                         'Ships already at sea are handled by monitoring once a voyage is approved.')
    req['departure'] = req.get('departure') or _iso(_now().replace(minute=0, second=0, microsecond=0))
    res = plan_voyage(req)
    plan = res.get('recommended')
    if not plan:
        raise ValueError('There is no feasible plan to approve: ' + (res.get('infeasible') or {}).get('message', ''))
    mmsi = str(payload.get('mmsi') or '').strip() or None
    if mmsi and not (mmsi.isdigit() and len(mmsi) == 9):
        raise ValueError('MMSI must be 9 digits')
    ship = FLEET[req['ship_id']]
    screen = sanctions.screen_vessel(ship.get('name'), ship.get('imo'), mmsi)
    if screen['status'] == 'match' and not payload.get('override_sanctions'):
        raise ValueError(f"Sanctions screening found a possible match ({screen['matches'][0]['name']}). "
                         'Resolve it with compliance before approving.')
    vid = 'V-' + _now().strftime('%y%m%d') + '-' + uuid.uuid4().hex[:4].upper()
    v = {'id': vid, 'status': 'approved', 'created_at': _iso(_now()), 'approved_by': who[:80],
         'note': str(payload.get('note') or '')[:500], 'ship_id': req['ship_id'], 'ship_name': ship['name'],
         'origin': req['origin'], 'destination': req['destination'], 'departure': res['request']['departure'],
         'request': req, 'approved_plan': plan, 'committed': _committed(plan),
         'conditions_as_of': (res.get('conditions') or {}).get('as_of'), 'objective': res['preset']['label'],
         'events': res.get('events') or [], 'jit': res.get('jit'), 'mmsi': mmsi, 'clock_offset_h': 0.0,
         'noon': None, 'last_check': None, 'alerts': [], 'sanctions': screen, 'revision': 1}
    with _lock:
        voyages = _load()
        voyages[vid] = v
        _save(voyages)
    audit('approved', vid, who, {'route': plan['corridor_label'], 'eta': plan['summary']['eta'],
                                 'cash_usd': plan['costs']['cash_total_usd'], 'fuel_t': plan['summary']['fuel_burned_t'],
                                 'objective': v['objective'], 'note': v['note'], 'sanctions': screen['status'],
                                 'inputs_sha256': hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()})
    if mmsi:
        _track(mmsi)
    try:
        return check(vid, 'monitor')   # first check right away, so the card shows position and status
    except Exception:
        return v


def _track(mmsi):
    try:
        ais.TRACKER.track(mmsi)
    except ValueError:
        pass


def _brief(v):
    p = v['committed']['plan']
    lc = v.get('last_check') or {}
    return {'id': v['id'], 'status': v['status'], 'ship_id': v['ship_id'], 'ship_name': v['ship_name'],
            'origin': v['origin'], 'destination': v['destination'], 'origin_name': GRAPH.name(v['origin']),
            'destination_name': GRAPH.name(v['destination']), 'approved_by': v['approved_by'], 'created_at': v['created_at'],
            'departure': v['departure'], 'eta': p['summary']['eta'], 'corridor': p['corridor_label'], 'mmsi': v.get('mmsi'),
            'revision': v.get('revision', 1), 'last_check': {k: lc.get(k) for k in ('at', 'position_source', 'recommendation', 'eta', 'eta_drift_h',
                                                                                   'progress', 'reason', 'lat', 'lon', 'fuel_t', 'status', 'fuel_source')} if lc else None,
            'open_alerts': [a for a in v.get('alerts', []) if not a.get('ack')], 'sanctions': (v.get('sanctions') or {}).get('status'),
            'approved_eta': v['approved_plan']['summary']['eta'], 'path': p['path'],
            'path_t0': p['checkpoints'][0]['eta'],   # time the ship is at the start of the committed path
            'replan_path': (lc.get('plan') or {}).get('path') if lc.get('recommendation') in ('change_course', 'replan') else None,
            'replan_corridor': (lc.get('plan') or {}).get('corridor_label'), 'fuel_source': lc.get('fuel_source'),
            'events': [{'id': e.get('id'), 'description': e.get('description') or e.get('type')} for e in v['request'].get('events') or []],
            'jit': v.get('jit'), 'clock_offset_h': v.get('clock_offset_h', 0.0)}


def list_voyages():
    with _lock:
        vs = list(_load().values())
    vs.sort(key=lambda v: v['created_at'], reverse=True)
    return {'voyages': [_brief(v) for v in vs], 'monitor_minutes': MONITOR_MINUTES,
            'monitor_running': bool(_thread and _thread.is_alive()), 'audit': verify_audit()}


def get_voyage(vid):
    with _lock:
        v = _load().get(vid)
    if not v:
        raise KeyError(vid)
    return v


# ------------------------------------------------------------------ where is the ship

def _elapsed_h(v):
    return (_now() - _parse(v['departure'])).total_seconds() / 3600 + v.get('clock_offset_h', 0.0)


def _schedule_position(v, elapsed):
    """Position, leg and fuel if the ship is exactly on the approved schedule."""
    c = v['committed']
    plan, route, off = c['plan'], c['route'], c['first_offset']
    dep = _parse(v['departure'])
    cps = plan['checkpoints']
    hrs = lambda iso: (_parse(iso) - dep).total_seconds() / 3600
    for i, leg in enumerate(plan['legs']):
        arrive = hrs(leg['arrive_eta'])
        start = arrive - leg['hours'] - (leg.get('canal_wait_h') or 0)
        f_start = cps[i].get('fuel_after_bunker_t', cps[i]['fuel_t'])
        if elapsed < start:
            # in port at checkpoint i (bunkering) or not yet departed
            return {'leg_index': i, 'fraction': off if i == 0 else 0.0, 'fuel_t': cps[i]['fuel_t'] if i else f_start,
                    'state': 'not departed' if i == 0 else f'alongside {cps[i]["name"]}'}
        if elapsed < arrive:
            part = min(0.999, (elapsed - start) / max(leg['hours'], 0.1))
            frac = off + (1 - off) * part if i == 0 else part
            fuel = f_start + (cps[i + 1]['fuel_t'] - f_start) * part
            return {'leg_index': i, 'fraction': frac, 'fuel_t': fuel, 'state': 'at sea'}
    return None   # arrived


def _point_on(route, i, frac):
    from optimizer import split_points
    e = GRAPH.edge(route[i], route[i + 1])
    pos, _, _ = split_points(e['points'], max(0.0, min(1.0, frac)))
    return pos


def _snap_to_route(route, lat, lon):
    best = None
    for i, (a, b) in enumerate(zip(route[:-1], route[1:])):
        pts = GRAPH.edge(a, b)['points']
        seg = [haversine_nm(p, q) for p, q in zip(pts[:-1], pts[1:])]
        total = sum(seg) or 1
        run = 0.0
        for j, (p, q) in enumerate(zip(pts[:-1], pts[1:])):
            kx = math.cos(math.radians((p[0] + q[0]) / 2))
            ax, ay, bx, by, px, py = p[1] * kx, p[0], q[1] * kx, q[0], lon * kx, lat
            dx, dy = bx - ax, by - ay
            t = 0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            d = haversine_nm((lat, lon), [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t])
            if best is None or d < best[2]:
                best = (i, (run + seg[j] * t) / total, d)
            run += seg[j]
    return best


def _fuel_at(v, leg_index, frac):
    c = v['committed']
    cps = c['plan']['checkpoints']
    off = c['first_offset']
    part = (frac - off) / (1 - off) if leg_index == 0 and off else frac
    f0 = cps[leg_index].get('fuel_after_bunker_t', cps[leg_index]['fuel_t'])
    return f0 + (cps[leg_index + 1]['fuel_t'] - f0) * max(0.0, min(1.0, part))


def locate(v):
    """Best available position: AIS > noon report > schedule."""
    elapsed = _elapsed_h(v)
    route = v['committed']['route']
    sched = _schedule_position(v, elapsed)
    out = {'elapsed_h': elapsed, 'schedule': sched}
    if v.get('mmsi'):
        snap = next((s for s in ais.TRACKER.snapshot()['ships'] if s['mmsi'] == v['mmsi']), None)
        if snap and snap.get('lat') is not None and snap.get('age_s', 1e9) <= AIS_MAX_AGE_H * 3600:
            i, frac, d = _snap_to_route(route, snap['lat'], snap['lon'])
            fuel = _fuel_at(v, i, frac)
            noon = _noon_fuel(v)
            out.update(source='AIS', lat=snap['lat'], lon=snap['lon'], sog=snap.get('sog'), off_route_nm=round(d, 1),
                       leg_index=i, fraction=frac, fuel_t=noon if noon is not None else fuel,
                       fuel_source='noon report' if noon is not None else 'estimated from plan')
            return out
    noon = v.get('noon')
    if noon and noon.get('lat') is not None and (_now() - _parse(noon['at'])).total_seconds() < NOON_MAX_AGE_H * 3600:
        i, frac, d = _snap_to_route(route, noon['lat'], noon['lon'])
        out.update(source='noon report', lat=noon['lat'], lon=noon['lon'], off_route_nm=round(d, 1), leg_index=i,
                   fraction=frac, fuel_t=_noon_fuel(v), fuel_source='noon report')
        return out
    if sched is None:
        out.update(source='schedule', arrived=True)
        return out
    pos = _point_on(route, sched['leg_index'], sched['fraction'])
    noon_fuel = _noon_fuel(v)
    out.update(source='schedule', lat=pos[0], lon=pos[1], off_route_nm=0.0, leg_index=sched['leg_index'],
               fraction=sched['fraction'], fuel_t=noon_fuel if noon_fuel is not None else sched['fuel_t'],
               fuel_source='noon report' if noon_fuel is not None else 'estimated from plan', state=sched['state'])
    return out


def _noon_fuel(v):
    noon = v.get('noon')
    if not noon or noon.get('fuel_t') is None:
        return None
    age_h = (_now() - _parse(noon['at'])).total_seconds() / 3600
    if age_h > NOON_MAX_AGE_H:
        return None
    p = v['committed']['plan']
    rate = p['summary']['fuel_burned_t'] / max(p['summary']['days'] * 24, 1)
    return max(0.0, noon['fuel_t'] - rate * age_h)


# ------------------------------------------------------------------ re-check

def _recheck_request(v, loc):
    req = copy.deepcopy(v['request'])
    req['departure'] = v['departure']
    req['live_weather'] = True
    ship = FLEET[v['ship_id']]
    fuel = max(0.0, min(ship['fuel_capacity_t'], loc['fuel_t']))
    if loc.get('off_route_nm', 0) > OFF_ROUTE_NM:
        req['start_at'] = {'lat': loc['lat'], 'lon': loc['lon'], 'label': f"{v['ship_name']} (AIS)"}
        req['fuel_onboard_t'] = fuel
        req.pop('berth_ready_days', None) if loc['elapsed_h'] > 0 else None
        return req
    route = v['committed']['route']
    i, frac = loc['leg_index'], loc['fraction']
    if v['committed']['first_offset'] == 0 and v.get('revision', 1) == 1 and loc['elapsed_h'] < 1:
        req['fuel_onboard_t'] = fuel   # not departed yet: plan again from the origin
        return req
    req['resume'] = {'route': route, 'leg_index': i, 'fraction': round(frac, 4), 'elapsed_h': max(0.0, loc['elapsed_h']),
                     'fuel_onboard_t': fuel}
    return req


def _alert(v, kind, level, text, key):
    for a in v['alerts'][-20:]:
        if a['key'] == key and not a.get('ack'):
            return None
    for old in v['alerts']:
        if old['kind'] == kind and not old.get('ack'):
            old['ack'] = 'superseded'   # a newer reading of the same thing replaces it
    a = {'id': uuid.uuid4().hex[:8], 'at': _iso(_now()), 'kind': kind, 'level': level, 'text': text, 'key': key, 'ack': False}
    v['alerts'].append(a)
    v['alerts'] = v['alerts'][-50:]
    return a


def check(vid, actor='monitor'):
    with _lock:
        voyages = _load()
        v = voyages.get(vid)
        if not v:
            raise KeyError(vid)
        if v['status'] not in ACTIVE:
            return v
    loc = locate(v)
    approved = v['committed']['plan']
    lc = {'at': _iso(_now()), 'position_source': loc.get('source'), 'elapsed_h': round(loc['elapsed_h'], 1),
          'lat': loc.get('lat'), 'lon': loc.get('lon'), 'fuel_t': round(loc['fuel_t'], 1) if loc.get('fuel_t') is not None else None,
          'fuel_source': loc.get('fuel_source'), 'off_route_nm': loc.get('off_route_nm')}
    new_alerts = []
    if loc.get('arrived'):
        lc.update(status='arrived', recommendation='arrived', reason='The approved schedule shows the ship has arrived.')
        v['status'] = 'arrived'
        audit('arrived', vid, actor, {'eta': approved['summary']['eta']})
    else:
        if loc['elapsed_h'] >= 1:   # departure is the start of the hour the plan was made in
            v['status'] = 'underway'
        total = approved['summary']['days'] * 24
        lc['progress'] = round(max(0.0, min(1.0, loc['elapsed_h'] / total)), 3) if total else None
        try:
            req = _recheck_request(v, loc)
            res = plan_voyage(req)
        except ValueError as exc:
            res, req = {'status': 'error', 'error': str(exc)}, None
        rec = res.get('recommended')
        if loc.get('off_route_nm', 0) > OFF_ROUTE_NM:
            new_alerts.append(_alert(v, 'off_route', 'warn', f"AIS puts {v['ship_name']} {loc['off_route_nm']:,.0f} nm off the approved route. "
                                     'Re-planned from its reported position.', f"off-{round(loc['off_route_nm'] / 100)}"))
        if res.get('status') == 'error':
            lc.update(status='error', recommendation='unknown', reason=res['error'])
        elif not rec:
            msg = (res.get('infeasible') or {}).get('message') or 'No feasible plan from the current position.'
            lc.update(status='infeasible', recommendation='no_plan', reason=msg)
            new_alerts.append(_alert(v, 'infeasible', 'critical', f'No feasible plan from the current position: {msg}', 'infeasible'))
        else:
            rs = res.get('resume') or {}
            drift = (_parse(rec['summary']['eta']) - _parse(approved['summary']['eta'])).total_seconds() / 3600
            lc.update(status='ok', recommendation=rs.get('recommendation') or ('replan' if req.get('start_at') else 'stay'),
                      reason=rs.get('reason') or ('Planned from the reported position.' if req.get('start_at') else 'Not departed: plan re-checked from the origin.'),
                      eta=rec['summary']['eta'], eta_drift_h=round(drift, 1), corridor=rec['corridor_label'],
                      fuel_on_arrival_t=rec['summary']['fuel_on_arrival_t'], saving_usd=rs.get('saving_score_usd'),
                      plan=rec, events=res.get('events'), weather=(res.get('weather') or {}).get('status'),
                      resume_request=req.get('resume'), start_at=req.get('start_at'))
            if rs.get('recommendation') == 'change_course':
                new_alerts.append(_alert(v, 'change_course', 'action', f"Change course recommended: {rs['reason']}", f"cc-{rec['corridor']}"))
            elif not rs and loc['elapsed_h'] < 1 and [c['node'] for c in rec['checkpoints']] != v['committed']['route']:
                new_alerts.append(_alert(v, 'replan', 'action', f"Before departure, a different plan is now better ({rec['corridor_label']}, "
                                         f"ETA {rec['summary']['eta'][:16].replace('T', ' ')}).", f"pre-{rec['corridor']}"))
            if abs(drift) >= ETA_ALERT_H:
                new_alerts.append(_alert(v, 'eta', 'warn', f"Arrival now {'later' if drift > 0 else 'earlier'} than approved by {abs(drift):.0f} h "
                                         f"(ETA {rec['summary']['eta'][:16].replace('T', ' ')} UTC).", f"eta-{round(drift / ETA_ALERT_H)}"))
            planned_left = approved['summary']['fuel_on_arrival_t']
            if planned_left > 0 and rec['summary']['fuel_on_arrival_t'] < planned_left * (1 - FUEL_ALERT_FRAC):
                new_alerts.append(_alert(v, 'fuel', 'warn', f"Fuel on arrival now {rec['summary']['fuel_on_arrival_t']:,.0f} t versus "
                                         f"{planned_left:,.0f} t approved.", f"fuel-{round(rec['summary']['fuel_on_arrival_t'] / 100)}"))
    v['last_check'] = lc
    new_alerts = [a for a in new_alerts if a]
    with _lock:
        voyages = _load()
        if vid in voyages:
            cur = voyages[vid]
            cur.update(status=v['status'], last_check=lc, alerts=v['alerts'])
            _save(voyages)
            v = cur
    for a in new_alerts:
        audit('alert', vid, actor, {'kind': a['kind'], 'level': a['level'], 'text': a['text']})
    return v


def check_all():
    with _lock:
        ids = [k for k, v in _load().items() if v['status'] in ACTIVE]
    for vid in ids:
        try:
            check(vid)
        except Exception as exc:   # keep monitoring the other voyages
            print(f'[monitor] {vid}: {type(exc).__name__}: {exc}', flush=True)


def start_monitor():
    global _thread
    if _thread or MONITOR_MINUTES <= 0:
        return

    def loop():
        while True:
            time.sleep(MONITOR_MINUTES * 60)
            check_all()

    _thread = threading.Thread(target=loop, name='tideline-monitor', daemon=True)
    _thread.start()
    for vid in [k for k, v in _load().items() if v['status'] in ACTIVE]:
        if _load()[vid].get('mmsi'):
            _track(_load()[vid]['mmsi'])


# ------------------------------------------------------------------ operator actions

def act(vid, payload):
    if not isinstance(payload, dict):
        raise ValueError('Body must be a JSON object')
    action = payload.get('action')
    actor = str(payload.get('actor') or 'operator')[:80]
    if action == 'check':
        return check(vid, actor)
    with _lock:
        voyages = _load()
        v = voyages.get(vid)
        if not v:
            raise KeyError(vid)
        detail = {}
        if action == 'ack':
            for a in v['alerts']:
                if a['id'] == payload.get('alert_id') or payload.get('alert_id') == 'all':
                    a['ack'] = True
            detail = {'alert': payload.get('alert_id')}
        elif action in ('cancel', 'complete'):
            v['status'] = 'cancelled' if action == 'cancel' else 'completed'
            detail = {'reason': str(payload.get('text') or '')[:300]}
        elif action == 'note':
            detail = {'text': str(payload.get('text') or '')[:500]}
            if not detail['text']:
                raise ValueError('Note text is empty')
        elif action == 'link_ais':
            mmsi = str(payload.get('mmsi') or '').strip()
            if mmsi and not (mmsi.isdigit() and len(mmsi) == 9):
                raise ValueError('MMSI must be 9 digits')
            v['mmsi'] = mmsi or None
            if mmsi:
                _track(mmsi)
            detail = {'mmsi': v['mmsi']}
        elif action == 'noon_report':
            ship = FLEET[v['ship_id']]
            fuel = payload.get('fuel_t')
            try:
                fuel = float(fuel)
            except (TypeError, ValueError):
                raise ValueError('fuel_t must be a number')
            if not 0 <= fuel <= ship['fuel_capacity_t']:
                raise ValueError('fuel_t is outside the tank capacity')
            noon = {'at': _iso(_now()), 'fuel_t': fuel, 'lat': None, 'lon': None}
            if payload.get('lat') not in (None, '') and payload.get('lon') not in (None, ''):
                lat, lon = float(payload['lat']), float(payload['lon'])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError('Position out of range')
                noon.update(lat=lat, lon=lon)
            for k in ('speed_kn', 'fuel_used_24h_t'):
                if payload.get(k) not in (None, ''):
                    noon[k] = float(payload[k])
            v['noon'] = noon
            detail = noon
        elif action == 'simulate_hours':
            h = float(payload.get('hours') or 0)
            if not 0 < h <= 24 * 30:
                raise ValueError('hours must be between 0 and 720')
            v['clock_offset_h'] = v.get('clock_offset_h', 0.0) + h
            detail = {'hours': h, 'total_offset_h': v['clock_offset_h'], 'note': 'demo time simulation, not real time'}
        elif action == 'add_event':
            ev = payload.get('event')
            if not isinstance(ev, dict) or not ev.get('type'):
                raise ValueError('event must be an object with a type')
            ev = dict(ev, id=str(ev.get('id') or f"{ev['type']}-{len(v['request'].get('events') or []) + 1}")[:60],
                      source=str(ev.get('source') or actor)[:200])
            from optimizer import apply_events
            apply_events([ev])   # validate before storing
            v['request'].setdefault('events', []).append(ev)
            detail = {'event': ev}
            adv = float(payload.get('advance_hours') or 0)
            if 0 < adv <= 24 * 60:   # demo playback: move this voyage's clock to the shown position first
                v['clock_offset_h'] = v.get('clock_offset_h', 0.0) + adv
                detail['advance_hours'] = adv
        elif action == 'remove_event':
            before = len(v['request'].get('events') or [])
            v['request']['events'] = [e for e in v['request'].get('events') or [] if e.get('id') != payload.get('event_id')]
            if len(v['request']['events']) == before:
                raise ValueError('No such event on this voyage')
            detail = {'event_id': payload.get('event_id')}
        elif action == 'accept_replan':
            lc = v.get('last_check') or {}
            plan = lc.get('plan')
            if not plan or lc.get('status') != 'ok':
                raise ValueError('No re-planned route to accept. Run a check first.')
            route, off = _route_from_replan(v, lc)
            old = v['committed']['plan']
            v['history'] = (v.get('history') or []) + [{'revision': v.get('revision', 1), 'plan_eta': old['summary']['eta'],
                                                          'corridor': old['corridor_label'], 'replaced_at': _iso(_now())}]
            v['committed'] = _committed(plan, off, route)
            v['revision'] = v.get('revision', 1) + 1
            for a in v['alerts']:
                a['ack'] = True
            detail = {'revision': v['revision'], 'corridor': plan['corridor_label'], 'eta': plan['summary']['eta'],
                      'reason': lc.get('reason'), 'previous_eta': old['summary']['eta']}
        else:
            raise ValueError('action must be one of: check, ack, cancel, complete, note, link_ais, noon_report, simulate_hours, add_event, remove_event, accept_replan')
        _save(voyages)
    audit(action, vid, actor, detail)
    if action in ('simulate_hours', 'noon_report', 'link_ais', 'add_event', 'remove_event', 'accept_replan'):
        return check(vid, actor)
    return v


def _route_from_replan(v, lc):
    """Graph-node route (and how far along its first edge the ship already is) for a re-planned voyage."""
    plan = lc['plan']
    nodes = [c['node'] for c in plan['checkpoints']]
    if nodes[0] != SHIP_NODE:
        return nodes, 0.0
    res = lc.get('resume_request')
    if not res:
        raise ValueError('This re-plan started from an off-route AIS position; approve a new voyage from the next port instead.')
    old, i, f = res['route'], res['leg_index'], res['fraction']
    prev, nxt = old[i], old[i + 1]
    if nodes[1] == nxt:
        return [prev] + nodes[1:], f
    if nodes[1] == prev:   # turning back along the same segment
        return [nxt] + nodes[1:], 1 - f
    raise ValueError('Unexpected re-plan shape')


# ------------------------------------------------------------------ dispatch drafts

def _fmt_t(x):
    return f'{x:,.0f} t'


def orders(vid):
    v = get_voyage(vid)
    plan = v['committed']['plan']
    ship = FLEET[v['ship_id']]
    s, c = plan['summary'], plan['costs']
    when = lambda iso: iso[:16].replace('T', ' ') + ' UTC'
    rev = v.get('revision', 1)
    n = iter(range(1, 20))
    lines = [f"VOYAGE ORDERS — {v['id']} (revision {rev})",
             f"To: Master, {ship['name']}",
             f"From: Operations — approved by {v['approved_by']} on {when(v['created_at'])}",
             '',
             f"{next(n)}. VOYAGE: {GRAPH.name(v['origin'])} to {GRAPH.name(v['destination'])} via {plan['corridor_label'].replace('Via ', '')}.",
             (f"   Departed {when(v['departure'])}. Re-planned at sea (revision {rev}): from the ship's position, {s['distance_nm']:,} nm to go. ETA {when(s['eta'])}."
              if rev > 1 else f"   Depart {when(v['departure'])}. ETA {when(s['eta'])}. Distance {s['distance_nm']:,} nm."),
             f"   Objective: {v['objective']}. Planning conditions: {v.get('conditions_as_of') or 'normal-times baseline'}.",
             '',
             f'{next(n)}. ROUTE AND SPEED (adjust for safety of navigation at the Master\'s discretion):']
    for leg in plan['legs']:
        z = [GRAPH.zones[x]['label'] for x in leg['zones']]
        lines.append(f"   - {leg['from_name']} → {leg['to_name']}: {leg['speed_kn']} kn, {leg['distance_nm']:,} nm, ETA {when(leg['arrive_eta'])}"
                     + (f"; forecast max waves {leg['max_wave_m']:g} m" if leg['max_wave_m'] else '')
                     + (f"; transits {', '.join(z)}" if z else '') + (f"; canal wait {leg['canal_wait_h']:g} h" if leg.get('canal') else ''))
    lines += ['', f'{next(n)}. BUNKERS:']
    if plan['bunkering']:
        for b in plan['bunkering']:
            lines.append(f"   - {b['name']}: lift about {_fmt_t(b['buy_t'])} VLSFO (planning price ${b['price_usd_t']:,.0f}/t), "
                         f"ETA {when(b['eta'])}, about {b['stop_hours']:g} h alongside. Confirm stem before arrival.")
    else:
        lines.append('   - No bunkering planned.')
    lines.append(f"   - Keep at least {_fmt_t(ship['min_reserve_t'])} on board at all times. Planned fuel on arrival {_fmt_t(s['fuel_on_arrival_t'])}.")
    lines.append('   - Send a noon report daily (position, speed, fuel remaining, fuel used in the last 24 h).')
    j = v.get('jit')
    if j and j.get('fuel_saved_t', 0) >= 1:
        lines += ['', f"{next(n)}. ARRIVAL: berth expected ready {when(j['berth_ready_eta'])}. Adjust speed to arrive just in time "
                      f"(saves about {_fmt_t(j['fuel_saved_t'])} fuel versus arriving early and anchoring)."]
    risk = [x for x in plan.get('compliance', []) if x['kind'] in ('warn', 'cost')]
    if risk:
        lines += ['', f'{next(n)}. RISK AND COMPLIANCE NOTES:']
        lines += [f"   - {x['rule']}: {x['detail']}" for x in risk]
    hz = [GRAPH.zones[z]['label'] for z in plan['zones'] if z in compliance.REG['jwc_listed']['zones']]
    if hz:
        lines.append(f"   - Follow current flag-state and industry security guidance for: {', '.join(hz)}. Report to the relevant reporting centre on entry and exit.")
    lines += ['', f"Budget: {c['cash_total_usd']:,} USD cash {'for the rest of this voyage' if rev > 1 else 'for this voyage'} (fuel, tolls, port fees, insurance, operating cost).",
              'Draft generated by Tideline from the approved plan. Review before sending; Tideline does not send messages.']
    enquiries = []
    for b in plan['bunkering']:
        enquiries.append({'port': b['port'], 'name': b['name'], 'subject': f"Bunker enquiry — {ship['name']} at {b['name']}, ETA {b['eta'][:10]}",
                          'body': '\n'.join([
                              f"Dear supplier,",
                              '',
                              f"Please quote for the following delivery:",
                              f"Vessel: {ship['name']} ({ship['type']})" + (f", IMO {ship['imo']}" if ship.get('imo') else ''),
                              f"Port: {b['name']}",
                              f"ETA: {when(b['eta'])}, about {b['stop_hours']:g} h available",
                              f"Grade: VLSFO 0.5% S (ISO 8217:2017 RMG 380)",
                              f"Quantity: {_fmt_t(b['buy_t'])} (min/max ±5%)",
                              f"Delivery: barge or pipeline, please advise",
                              '',
                              f"Please confirm price, availability, delivery window and terms. Our planning price is ${b['price_usd_t']:,.0f}/t.",
                              '',
                              f"Regards, Operations ({v['approved_by']})"])})
    return {'voyage': v['id'], 'revision': v.get('revision', 1), 'orders_text': '\n'.join(lines), 'bunker_enquiries': enquiries,
            'note': 'Drafts only. Tideline does not send messages or book fuel or berths.'}
