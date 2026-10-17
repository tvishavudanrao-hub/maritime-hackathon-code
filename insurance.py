"""Insurance coverage check: is every part of the route covered by the ship's policy, and at what cost?

For each risk zone on a route, against the ship's (sample) policy in data/insurance.json:
* covered                 - normal cover, no extra cost
* additional premium      - a JWC listed area with war cover: notify underwriters, pay the war-risk
                            additional premium (already in the plan's insurance cost)
* held covered            - outside a trading warranty, but the insurer agrees to cover it for an extra
                            premium (added to the plan's insurance cost)
* not covered             - no war cover for a listed area, or underwriters have cancelled war cover
                            there (event `cover_cancelled`). By default Tideline will not route through
                            it; with allow_uninsured the route is allowed but flagged.
Decision support only: real terms come from the operator's policies and broker.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / 'data' / 'insurance.json').read_text())
POLICIES = DATA['policies']
LISTED = json.loads((ROOT / 'data' / 'regulatory.json').read_text())['jwc_listed']['zones']

COVERED, ADDITIONAL, HELD, NOT_COVERED = 'covered', 'additional premium', 'held covered', 'not covered'


def policy(ship_id):
    return POLICIES.get(ship_id) or {'insurer': 'No policy on file', 'war_cover': False, 'notice_days': None, 'warranties': {}}


def zone_status(ship_id, zone, cancelled=()):
    p = policy(ship_id)
    if zone in LISTED and zone in cancelled:
        return NOT_COVERED, 'underwriters have cancelled war cover for this area'
    if zone in LISTED and not p['war_cover']:
        return NOT_COVERED, 'the policy has no war-risk cover and this is a JWC listed area'
    if zone in p['warranties']:
        w = p['warranties'][zone]
        return HELD, f"{w['text']} ({w['rate']:.2%} of hull value)"
    if zone in LISTED:
        return ADDITIONAL, 'JWC listed area: notify underwriters; war-risk additional premium applies'
    return COVERED, ''


def uninsured_zones(ship_id, zones, cancelled=()):
    return {z for z in zones if zone_status(ship_id, z, cancelled)[0] == NOT_COVERED}


def held_rate(ship_id, zone):
    w = policy(ship_id)['warranties'].get(zone)
    return w['rate'] if w else 0.0


def findings(plan, r, zone_label, cancelled=()):
    """Compliance-tab findings for one plan."""
    sid = r['ship']['id']
    p = policy(sid)
    out = []
    src = {'url': None, 'date': None, 'source': f"{p['insurer']} (sample policy, data/insurance.json)"}
    for z in plan['zones']:
        st, why = zone_status(sid, z, cancelled)
        if st == NOT_COVERED:
            out.append({'kind': 'warn', 'rule': 'Insurance: NOT COVERED',
                        'detail': f"{zone_label(z)}: {why}. Buy separate cover before entry or re-route.", **src})
        elif st == HELD:
            out.append({'kind': 'cost', 'rule': 'Insurance: held covered (warranty breach)',
                        'detail': f"{zone_label(z)}: {why}. Included in the insurance cost; tell the insurer before entry.", **src})
    has_listed = any(z in LISTED for z in plan['zones'])
    summary = (f"{p['insurer']}. War-risk cover: {'yes' if p['war_cover'] else 'NO'}"
               + (f", cancellable on {p['notice_days']} days' notice" if p['notice_days'] else '') + '. ')
    if not any(f['rule'].startswith('Insurance') for f in out):
        summary += ('Every zone on this route is within cover' + (' (listed areas at the war-risk additional premium shown in costs).' if has_listed else '.'))
    out.append({'kind': 'info', 'rule': 'Insurance coverage', 'detail': summary, **src})
    if r.get('allow_uninsured') is False and r.get('_insurance_avoided'):
        out.append({'kind': 'info', 'rule': 'Insurance: route restriction',
                    'detail': 'Avoided because the policy does not cover them: ' + ', '.join(zone_label(z) for z in sorted(r['_insurance_avoided'])) + '.', **src})
    return out
