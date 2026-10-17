"""Fleet assignment: which ship should take which cargo job?

For every compatible ship-job pair, Tideline plans the positioning leg (ballast from where the ship
is now to the loading port, when they differ) and the loaded voyage with the full optimizer, then
picks the assignment with the lowest total decision score (cash plus the chosen risk, carbon and
time weights). With at most 6 ships and 4 jobs every combination is checked exactly.
It compares the result with a simple rule-of-thumb dispatch (each job, in order, takes the first
free compatible ship) so the value of optimizing is visible.
"""
from __future__ import annotations

import itertools

from optimizer import CARGO_SEGMENTS, FLEET, GRAPH, plan_voyage

MAX_JOBS, MAX_SHIPS = 4, 6
SHARED_KEYS = ('priority', 'weights', 'conditions', 'events', 'live_weather', 'affiliations')


def compatible(ship, cargo):
    t = ship['type'].lower()
    if cargo == 'crude':
        return 'tanker' in t, 'crude needs a tanker'
    if cargo in ('retail', 'food'):
        return 'container' in t or 'feeder' in t, f"{CARGO_SEGMENTS[cargo]['label'].lower()} needs a container ship"
    if cargo == 'bulk':
        return False, 'no bulk carrier in this fleet'
    return True, ''


def _job(j, i):
    if not isinstance(j, dict):
        raise ValueError('Each job must be an object')
    o, d = j.get('origin'), j.get('destination')
    if o not in GRAPH.ports or d not in GRAPH.ports or o == d:
        raise ValueError(f'Job {i + 1}: choose two different known ports')
    cargo = j.get('cargo') or 'retail'
    if cargo not in CARGO_SEGMENTS:
        raise ValueError(f"Job {i + 1}: cargo must be one of {', '.join(CARGO_SEGMENTS)}")
    dl = j.get('deadline_days')
    dl = None if dl in (None, '') else float(dl)
    return {'id': str(j.get('id') or f'J{i + 1}')[:20], 'origin': o, 'destination': d, 'cargo': cargo,
            'deadline_days': dl, 'deadline_type': j.get('deadline_type') or 'soft'}


def assign(payload):
    if not isinstance(payload, dict):
        raise ValueError('Body must be a JSON object')
    jobs = payload.get('jobs') or []
    ships = payload.get('ships') or []
    if not 1 <= len(jobs) <= MAX_JOBS:
        raise ValueError(f'Give 1 to {MAX_JOBS} jobs')
    if not 1 <= len(ships) <= MAX_SHIPS:
        raise ValueError(f'Give 1 to {MAX_SHIPS} ships')
    jobs = [_job(j, i) for i, j in enumerate(jobs)]
    fleet = []
    for s in ships:
        s = {'ship_id': s} if isinstance(s, str) else dict(s)
        if s.get('ship_id') not in FLEET:
            raise ValueError(f"Unknown ship {s.get('ship_id')!r}")
        at = s.get('at') or FLEET[s['ship_id']].get('home_port') or 'SIN'
        if at not in GRAPH.ports:
            raise ValueError(f"Unknown port for {s['ship_id']}: {at!r}")
        fleet.append({'ship_id': s['ship_id'], 'at': at, 'fuel_onboard_t': s.get('fuel_onboard_t')})
    if len({s['ship_id'] for s in fleet}) != len(fleet):
        raise ValueError('Each ship can appear once')
    shared = {k: payload[k] for k in SHARED_KEYS if k in payload}
    shared['uncertainty'] = False
    cache = {}

    def run(req):
        key = repr(sorted(req.items(), key=lambda kv: kv[0]))
        if key not in cache:
            cache[key] = plan_voyage(req)
        return cache[key]

    cells = {}
    for s in fleet:
        ship = FLEET[s['ship_id']]
        for j in jobs:
            ok, why = compatible(ship, j['cargo'])
            cell = {'ship_id': s['ship_id'], 'job': j['id'], 'feasible': False, 'reason': why}
            cells[(s['ship_id'], j['id'])] = cell
            if not ok:
                continue
            fuel = s['fuel_onboard_t'] if s['fuel_onboard_t'] not in (None, '') else ship['default_fuel_onboard_t']
            pos_days, pos_cash, pos_score, fuel_at_load = 0.0, 0, 0, fuel
            if s['at'] != j['origin']:
                pr = run(dict(shared, ship_id=s['ship_id'], origin=s['at'], destination=j['origin'], fuel_onboard_t=fuel, cargo=j['cargo']))
                p = pr.get('recommended')
                if not p:
                    cell['reason'] = 'cannot reach the loading port: ' + (pr.get('infeasible') or {}).get('message', '')
                    continue
                pos_days, pos_cash, pos_score = p['summary']['days'], p['costs']['cash_total_usd'], p['decision']['score_usd']
                fuel_at_load = p['summary']['fuel_on_arrival_t']
            req = dict(shared, ship_id=s['ship_id'], origin=j['origin'], destination=j['destination'], cargo=j['cargo'],
                       fuel_onboard_t=fuel_at_load)
            if j['deadline_days']:
                left = j['deadline_days'] - pos_days
                if left < 0.5:
                    cell['reason'] = f"positioning alone takes {pos_days:.1f} days, past the {j['deadline_days']:g}-day deadline"
                    continue
                req.update(deadline_days=round(left, 2), deadline_type=j['deadline_type'])
            vr = run(req)
            v = vr.get('recommended')
            if not v:
                cell['reason'] = (vr.get('infeasible') or {}).get('message', 'no feasible plan')
                continue
            cell.update(feasible=True, reason='', positioning_days=round(pos_days, 2), positioning_cash_usd=pos_cash,
                        voyage_days=v['summary']['days'], total_days=round(pos_days + v['summary']['days'], 2),
                        cash_usd=pos_cash + v['costs']['cash_total_usd'], score_usd=pos_score + v['decision']['score_usd'],
                        co2_t=round(v['summary']['co2_t'], 1), corridor=v['corridor_label'], late_days=v['summary']['late_days'],
                        eta=v['summary']['eta'], from_port=s['at'])

    ids = [s['ship_id'] for s in fleet]
    best, best_score = None, None
    for perm in itertools.permutations(ids, len(jobs)) if len(jobs) <= len(ids) else []:
        pairs = list(zip(perm, [j['id'] for j in jobs]))
        if all(cells[p]['feasible'] for p in pairs):
            sc = sum(cells[p]['score_usd'] for p in pairs)
            if best_score is None or sc < best_score:
                best, best_score = pairs, sc
    # partial cover when not every job can be served: maximize jobs served, then minimize score
    if best is None:
        for k in range(min(len(jobs), len(ids)) - 1, 0, -1):
            for js in itertools.combinations([j['id'] for j in jobs], k):
                for perm in itertools.permutations(ids, k):
                    pairs = list(zip(perm, js))
                    if all(cells[p]['feasible'] for p in pairs):
                        sc = sum(cells[p]['score_usd'] for p in pairs)
                        if best_score is None or sc < best_score:
                            best, best_score = pairs, sc
            if best:
                break
    naive, used = [], set()
    for j in jobs:   # rule of thumb: first free compatible ship in fleet order
        for sid in ids:
            if sid not in used and cells[(sid, j['id'])]['feasible']:
                naive.append((sid, j['id']))
                used.add(sid)
                break
    tot = lambda pairs, k: round(sum(cells[p][k] for p in pairs)) if pairs else 0
    out_assign = [dict(cells[p], ship_name=FLEET[p[0]]['name']) for p in (best or [])]
    served = {p[1] for p in best or []}
    result = {
        'assignment': out_assign,
        'unserved': [j['id'] for j in jobs if j['id'] not in served],
        'totals': {'cash_usd': tot(best, 'cash_usd'), 'score_usd': tot(best, 'score_usd'), 'co2_t': round(sum(cells[p]['co2_t'] for p in best or []), 1),
                   'jobs_served': len(best or [])},
        'rule_of_thumb': {'assignment': [{'ship_id': s, 'ship_name': FLEET[s]['name'], 'job': j} for s, j in naive],
                          'cash_usd': tot(naive, 'cash_usd'), 'score_usd': tot(naive, 'score_usd'), 'jobs_served': len(naive),
                          'rule': 'each job, in order, takes the first free compatible ship'},
        'matrix': [cells[(s['ship_id'], j['id'])] for s in fleet for j in jobs],
        'jobs': jobs, 'ships': [dict(s, name=FLEET[s['ship_id']]['name']) for s in fleet],
        'plans_run': len(cache),
        'method': 'Every compatible ship-job pair is planned (positioning + loaded voyage) with the optimizer; every assignment is checked exactly.',
    }
    if naive and best and len(naive) == len(best):
        result['saving_vs_rule_of_thumb_usd'] = result['rule_of_thumb']['score_usd'] - result['totals']['score_usd']
    return result
