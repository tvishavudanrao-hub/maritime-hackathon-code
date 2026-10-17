"""Compliance and regulatory screening for a voyage plan.

Decision support, not legal advice. Every finding names its rule and source. Three kinds:
* cost  - a regulatory charge included in the plan's cash cost (EU ETS)
* warn  - something an operator must act on (JWC listed area: notify underwriters)
* info  - context (sanctions-driven exclusions, MARPOL for oil cargo, tariff exposure)
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REG = json.loads((ROOT / 'data' / 'regulatory.json').read_text())
ETS = REG['eu_ets']
ETS_USD_PER_T = ETS['eua_eur_per_t'] * ETS['eur_usd']
AFFILIATIONS = REG['affiliations']


def ets_share(origin, destination):
    """Share of voyage CO2 that must be covered by EU allowances (bunkering stops are not port calls)."""
    o, d = origin in ETS['eu_ports'], destination in ETS['eu_ports']
    if o and d:
        share = ETS['share_intra_eu_voyage']
    elif o or d:
        share = ETS['share_non_eu_voyage']
    else:
        share = 0.0
    return share * ETS['phase_in_2026']


def _src(url, date, text=None):
    return {'url': url, 'date': date, 'source': text}


def screen(plan, r, port_name, zone_label):
    out = []
    if not plan:
        return out
    jwc = REG['jwc_listed']
    for z in plan['zones']:
        if z in jwc['zones']:
            out.append({'kind': 'warn', 'rule': 'JWC Listed Area',
                        'detail': f"Transits {zone_label(z)} ({jwc['zones'][z]}). Notify war-risk underwriters before entry; "
                                  f"additional premium and conditions may apply.",
                        **_src(jwc['url'], '2026-09-16', f"Joint War Committee {jwc['circular']}")})
    calls = {plan['checkpoints'][0]['node'], plan['checkpoints'][-1]['node']} | {b['port'] for b in plan['bunkering']}
    for p in sorted(calls):
        if p in jwc['ports']:
            out.append({'kind': 'warn', 'rule': 'JWC Listed Area (port)',
                        'detail': f"Calls at {port_name(p)}: {jwc['ports'][p]}. Breach-of-warranty / additional premium terms apply.",
                        **_src(jwc['url'], '2026-09-16', f"Joint War Committee {jwc['circular']}")})
    ets_usd = plan['costs'].get('eu_ets_usd', 0)
    if ets_usd:
        share = ets_share(r['origin'], r['destination'])
        co2 = plan['summary']['co2_t']
        out.append({'kind': 'cost', 'rule': 'EU Emissions Trading System',
                    'detail': f"{share:.0%} of this voyage's {co2:,.0f} t CO2 needs EU allowances: {share * co2:,.0f} t x "
                              f"EUR {ETS['eua_eur_per_t']:g} (${ETS_USD_PER_T:,.0f}/t) = ${ets_usd:,.0f}, included in the cash cost. "
                              f"Bunkering stops are not ports of call.",
                    **_src(ETS['sources'][0]['url'], ETS['sources'][0]['date'], 'EU ETS maritime rules; EUA price Jul 2026 (stale), EUR/USD 16 Oct 2026')})
    for a in r.get('affiliations', []):
        rule = AFFILIATIONS[a]
        out.append({'kind': 'info', 'rule': rule['label'],
                    'detail': f"{', '.join(zone_label(z) for z in rule['close_zones'])} excluded from routing: {rule['rule']}.",
                    **_src(rule['url'], rule['date'])})
    if r.get('cargo') == 'crude':
        out.append({'kind': 'info', 'rule': 'Oil cargo (MARPOL Annex I)',
                    'detail': 'Oil pollution rules apply: oil record book, shipboard oil pollution emergency plan; confirm tanker-specific '
                              'port and canal requirements with agents.', 'url': None, 'date': None, 'source': 'IMO MARPOL Annex I'})
    stops = [port_name(b['port']) for b in plan['bunkering'] if b['port'] not in (r['origin'], r['destination'])]
    out.append({'kind': 'info', 'rule': 'Tariffs and customs',
                'detail': ('Bunkering-only stops (' + ', '.join(stops) + ') are not customs entries. ' if stops else '')
                          + 'Duties depend on the goods, their origin and the destination country, not the sea route. '
                            'This plan includes no transshipment, which could change origin or customs treatment.',
                'url': None, 'date': None, 'source': 'General customs principle (no tariff schedules modelled)'})
    return out
