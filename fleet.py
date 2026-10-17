"""Fleet planning: plan several ships together.

Each ship is optimized with the single-voyage planner, then the fleet is coordinated:
* Refueling clashes: when two or more fleet ships would bunker at the same port within
  CLASH_WINDOW_H hours, later arrivals face a queue. They are re-planned with an added
  waiting time at that port (QUEUE_H per ship already there), which lets the optimizer move
  their stop to another port or change speed if that is cheaper. Repeats until stable.
* Chokepoint concentration: the share of the fleet transiting each risk zone is reported, so
  an operator can see when one closure would hit most of the fleet.
Sample assumptions: queue time per ship and clash window are illustrative.
"""
from __future__ import annotations

from datetime import datetime

from optimizer import FLEET, GRAPH, plan_voyage

CLASH_WINDOW_H = 24
QUEUE_H = 18
MAX_ROUNDS = 10   # one change per round; enough for a 6-ship fleet
MAX_SHIPS = 6


def _t(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() / 3600


def _bunker_windows(ship_id, plan):
    out = []
    for b in (plan or {}).get('bunkering', []):
        start = _t(b['eta'])
        out.append({'ship': ship_id, 'port': b['port'], 'start': start, 'end': start + b['stop_hours']})
    return out


def req_origin(requests, sid):
    return next(r for r in requests if r['ship_id'] == sid).get('origin')


def fleet_plan(payload):
    if not isinstance(payload, dict):
        raise ValueError('Body must be a JSON object')
    ships = payload.get('ships')
    if not isinstance(ships, list) or not 1 <= len(ships) <= MAX_SHIPS:
        raise ValueError(f'ships must list 1 to {MAX_SHIPS} vessels')
    shared = {k: v for k, v in payload.items() if k != 'ships'}
    requests = []
    seen = set()
    for s in ships:
        s = {'ship_id': s} if isinstance(s, str) else dict(s)
        if s.get('ship_id') not in FLEET:
            raise ValueError(f"Unknown ship_id {s.get('ship_id')!r}")
        if s['ship_id'] in seen:
            raise ValueError('Each ship can appear once')
        seen.add(s['ship_id'])
        req = dict(shared)
        req.update({k: v for k, v in s.items() if v not in (None, '')})
        req.setdefault('fuel_onboard_t', FLEET[s['ship_id']]['default_fuel_onboard_t'])
        req['events'] = list(shared.get('events') or [])
        requests.append(req)

    extra = {r['ship_id']: [] for r in requests}       # fleet-coordination events per ship
    results = {r['ship_id']: plan_voyage(r) for r in requests}
    notes, rounds = [], 0
    for rounds in range(1, MAX_ROUNDS + 1):
        windows = []
        for r in requests:
            windows += _bunker_windows(r['ship_id'], results[r['ship_id']].get('recommended'))
        windows.sort(key=lambda w: w['start'])
        changed = False
        for i, w in enumerate(windows):
            ahead = [o for o in windows[:i] if o['port'] == w['port'] and o['ship'] != w['ship']
                     and w['start'] - CLASH_WINDOW_H <= o['end']]
            if not ahead:
                continue
            sid, port = w['ship'], w['port']
            if any(e.get('port') == port for e in extra[sid]):
                continue
            base = 0 if port == req_origin(requests, sid) else GRAPH.ports[port]['wait_h']
            wait = base + QUEUE_H * len(ahead)
            names = ', '.join(FLEET[o['ship']]['name'] for o in ahead)
            extra[sid].append({'type': 'port_congestion', 'port': port, 'wait_h': wait, 'id': f'fleet-queue-{port.lower()}',
                               'source': 'Tideline fleet coordination', 'timestamp': None,
                               'description': f"Fleet queue at {GRAPH.name(port)}: {names} bunkering there first ({wait:g} h expected wait)"})
            before = results[sid]['recommended']
            req = next(r for r in requests if r['ship_id'] == sid)
            results[sid] = plan_voyage(dict(req, events=req['events'] + extra[sid]))
            after = results[sid]['recommended']
            moved = after and before and [b['port'] for b in after['bunkering']] != [b['port'] for b in before['bunkering']]
            notes.append({'ship': sid, 'port': port,
                          'text': f"{FLEET[sid]['name']}: {names} reach {GRAPH.name(port)} first. "
                                  + (f"Refueling moved to {', '.join(GRAPH.name(b['port']) for b in after['bunkering']) or 'no stop'} to avoid the queue."
                                     if moved else f"Still cheapest to refuel there despite a {wait:g} h wait." if after else 'No feasible plan with the queue.')})
            changed = True
            break   # re-evaluate windows after each change
        if not changed:
            break

    ships_out, totals = [], {'cash_usd': 0, 'fuel_t': 0.0, 'co2_t': 0.0, 'ets_usd': 0, 'feasible': 0}
    latest, zones = None, {}
    for r in requests:
        res = results[r['ship_id']]
        rec = res.get('recommended')
        ships_out.append({'ship_id': r['ship_id'], 'name': FLEET[r['ship_id']]['name'], 'type': FLEET[r['ship_id']]['type'],
                          'origin': r.get('origin'), 'destination': r.get('destination'),
                          'coordination_events': extra[r['ship_id']], 'result': res})
        if not rec:
            continue
        totals['feasible'] += 1
        totals['cash_usd'] += rec['costs']['cash_total_usd']
        totals['fuel_t'] += rec['summary']['fuel_burned_t']
        totals['co2_t'] += rec['summary']['co2_t']
        totals['ets_usd'] += rec['costs'].get('eu_ets_usd', 0)
        latest = rec['summary']['eta'] if latest is None or rec['summary']['eta'] > latest else latest
        for z in rec['zones']:
            zones.setdefault(z, []).append(r['ship_id'])
    n = len(requests)
    concentration = [{'zone': z, 'label': GRAPH.zones[z]['label'], 'ships': ids, 'share': round(len(ids) / n, 2)}
                     for z, ids in sorted(zones.items(), key=lambda kv: -len(kv[1]))]
    for c in concentration:
        if c['share'] >= 0.6 and n >= 2 and c['zone'] in ('bab', 'hormuz', 'suez'):
            notes.append({'ship': None, 'port': None,
                          'text': f"{len(c['ships'])} of {n} ships transit {c['label']}: one closure there would hit most of the fleet."})
    totals.update(cash_usd=round(totals['cash_usd']), fuel_t=round(totals['fuel_t'], 1), co2_t=round(totals['co2_t'], 1),
                  ets_usd=round(totals['ets_usd']), latest_eta=latest, ships=n)
    return {'status': 'ok' if totals['feasible'] else 'infeasible', 'ships': ships_out, 'totals': totals,
            'coordination': notes, 'rounds': rounds, 'concentration': concentration,
            'assumptions': [f'Fleet queue model: +{QUEUE_H} h wait per fleet ship bunkering at the same port within {CLASH_WINDOW_H} h (sample assumption).']}
