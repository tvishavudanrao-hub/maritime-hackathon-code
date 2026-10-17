"""Forecast uncertainty: how sure are the plan's arrival time and fuel?

Monte Carlo over the recommended plan as approved (same route, speeds and refuelling): each run
perturbs the inputs the plan depends on, re-sails it, and records arrival time and fuel.

Sources of uncertainty, and the assumed spreads (illustrative, documented in the result):
* Wave height and wind: lognormal error that grows with forecast lead time on live-forecast legs
  (12% at day 0, +3% per day, max 35%); 40% on legs that use seasonal sample values.
  One error per segment, partly shared along the voyage (weather systems are large).
* Fuel model error: the regression's own residual spread for this ship (or a calibrated model's,
  once trained on noon reports), applied once per voyage.
* Waiting times (canal queues, bunkering stops): lognormal, 50% spread.
* Involuntary speed loss in heavy seas (waves above 3 m), relative to the planned sea state.
* Sea state above the ship's limit: the ship holds for 12 h and continues.
Results are percentiles (P10 = 1 in 10 runs better, P90 = 1 in 10 worse) and probabilities.
"""
from __future__ import annotations

import hashlib
import math
import random

from risk_model import RiskModel  # noqa: F401  (kept for symmetry with the optimizer)

RUNS = 300
SIGMA_LIVE0, SIGMA_LIVE_PER_DAY, SIGMA_LIVE_MAX = 0.12, 0.03, 0.35
SIGMA_SEASONAL = 0.40
SIGMA_WAIT = 0.5
SHARED = 0.6          # share of weather error common to the whole voyage
HOLD_H = 12.0
METHOD = (f'{RUNS} simulated voyages on the recommended route and speeds. Wave/wind error grows with forecast lead time '
          f'({SIGMA_LIVE0:.0%} at day 0, +{SIGMA_LIVE_PER_DAY:.0%}/day, max {SIGMA_LIVE_MAX:.0%}; {SIGMA_SEASONAL:.0%} on seasonal legs); '
          f'fuel model error from its residual spread; waiting times ±{SIGMA_WAIT:.0%}; speed loss in waves above 3 m. Spreads are illustrative assumptions.')


def _pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def speed_loss(hs):
    """Fraction of speed lost in head-on heavy seas (illustrative): none below 3 m, about 9% at 5 m, max 30%."""
    return min(0.3, 0.032 * max(0.0, hs - 3.0) ** 1.5)


def _model_sigma(search):
    m = getattr(search.fuel_model, 'ships', {}).get(search.ship['id'])
    if m and m.get('sigma2') is not None:
        return math.sqrt(m['sigma2'])
    return 0.07


def simulate(search, label, seed_text=''):
    from optimizer import GRAPH   # late import: optimizer imports this module
    steps = []
    lab = label
    while lab.parent is not None:
        steps.append(lab.step)
        lab = lab.parent
    steps.reverse()
    ship, r = search.ship, search.r
    edges = []
    for st in steps:
        if st['kind'] == 'sail':
            e = next((e for e in (search.extra_adj.get(st['from']) or GRAPH.adj[st['from']]) if e['to'] == st['to']), None)
            edges.append(e)
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16))
    msig = _model_sigma(search)
    deadline = search.deadline_h
    berth = search.berth_h
    out_t, out_burn, out_left, holds, breach, late = [], [], [], 0, 0, 0
    for _ in range(RUNS):
        common = rng.gauss(0, 1)
        model_k = math.exp(rng.gauss(0, msig) - msig * msig / 2)
        t, fuel, burn, held = search.start_t, r['fuel_onboard_t'], 0.0, False
        ei = 0
        min_fuel = fuel
        for st in steps:
            if st['kind'] == 'bunker':
                w = st['hours'] * math.exp(rng.gauss(0, SIGMA_WAIT) - SIGMA_WAIT ** 2 / 2)
                idle = search.idle_h * w
                fuel = max(fuel - idle, 0) + st['buy_t']
                burn += idle
                t += w
                continue
            e = edges[ei]
            ei += 1
            z = SHARED * common + math.sqrt(1 - SHARED ** 2) * rng.gauss(0, 1)
            for leg in e['legs']:
                c = search.cond.at(leg['mid'][0], leg['mid'][1], t)
                if c['source'].startswith('live'):
                    lead_d = max(0.0, (t - search.start_t) / 24)
                    sig = min(SIGMA_LIVE_MAX, SIGMA_LIVE0 + SIGMA_LIVE_PER_DAY * lead_d)
                elif c['source'].startswith('event'):
                    sig = 0.15
                else:
                    sig = SIGMA_SEASONAL
                k = math.exp(sig * z - sig * sig / 2)
                hs, wind = c['hs'] * k, c['wind_ms'] * k
                if hs > ship['max_hs_m']:
                    t += HOLD_H
                    held = True
                    hs = ship['max_hs_m']
                headwind = 0.7 * wind if c['wind_from'] is None else wind * math.cos(math.radians(c['wind_from'] - leg['bearing']))
                current = c['current_kn'] * math.cos(math.radians(c['current_to'] - leg['bearing']))
                h, f = search.fuel_model.leg(ship, leg['nm'], st['speed_kn'], fuel, hs, headwind, current)
                f *= model_k
                h *= (1 - speed_loss(min(c['hs'], ship['max_hs_m']))) / (1 - speed_loss(hs))
                t += h
                fuel -= f
                burn += f
                min_fuel = min(min_fuel, fuel)
            if st.get('canal_hours'):
                w = st['canal_hours'] * math.exp(rng.gauss(0, SIGMA_WAIT) - SIGMA_WAIT ** 2 / 2)
                fuel -= search.idle_h * w
                burn += search.idle_h * w
                t += w
        if berth is not None and t < berth:
            fuel -= search.idle_h * (berth - t)
            burn += search.idle_h * (berth - t)
        out_t.append(t)
        out_burn.append(burn)
        out_left.append(fuel)
        holds += held
        breach += min_fuel < r['min_reserve_t']
        late += deadline is not None and t > deadline
    dep = r['departure']
    from datetime import timedelta
    iso = lambda h: (dep + timedelta(hours=round(h))).isoformat().replace('+00:00', 'Z')
    res = {
        'runs': RUNS,
        'eta': {'p10': iso(_pct(out_t, .1)), 'p50': iso(_pct(out_t, .5)), 'p90': iso(_pct(out_t, .9))},
        'days': {'p10': round(_pct(out_t, .1) / 24, 2), 'p50': round(_pct(out_t, .5) / 24, 2), 'p90': round(_pct(out_t, .9) / 24, 2)},
        'fuel_burned_t': {'p10': round(_pct(out_burn, .1), 1), 'p50': round(_pct(out_burn, .5), 1), 'p90': round(_pct(out_burn, .9), 1)},
        'fuel_on_arrival_t': {'p10': round(_pct(out_left, .1), 1), 'p50': round(_pct(out_left, .5), 1)},
        'prob_reserve_breach': round(breach / RUNS, 3),
        'prob_weather_hold': round(holds / RUNS, 3),
        'prob_late': round(late / RUNS, 3) if deadline is not None else None,
        'eta_spread_h': round(_pct(out_t, .9) - _pct(out_t, .1), 1),
        'fuel_model_sigma': round(msig, 3),
        'method': METHOD,
    }
    return res
