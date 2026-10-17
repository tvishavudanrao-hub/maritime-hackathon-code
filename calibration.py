"""Calibrate a ship's fuel model on its own noon reports.

POST /api/calibrate {ship_id, csv}        retrain that ship's regression on the reports
POST /api/calibrate {ship_id, reset: true} go back to the model trained on simulated data
GET  /api/calibrate/sample?ship_id=...   a SYNTHETIC example file (a hull burning ~12% above the
                                          textbook formula) to demonstrate the workflow

CSV: one row per report period (usually 24 h), header names are flexible:
  speed_kn (or speed, stw_kn), fuel_t (or fuel, foc_t, fuel_used_t), distance_nm (or distance;
  or hours/steaming_hours, then distance = speed x hours); optional displacement_t, hs_m (waves),
  headwind_ms (or wind_ms, taken as 70% head-on), current_kn (along the course).
Fitting: the same features as the simulated model, ln(fuel per nm) on ln speed, ln displacement,
waves, head wind and current. Terms the reports barely vary (e.g. displacement on one loading
condition) keep the prior coefficient instead of being guessed from noise. The last 20% of
reports (in file order, i.e. time) are held out to measure accuracy before and after.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fuel_model import FEATURES, NOISE_SIGMA, RegressionFuelModel, _features, displacement, get_fuel_model, ols
from optimizer import FLEET, FLEET_LIST

ROOT = Path(__file__).resolve().parent
MIN_REPORTS = 20
MAX_BYTES = 120_000
ALIASES = {
    'speed': ('speed_kn', 'speed', 'stw_kn', 'sog_kn', 'avg_speed_kn'),
    'fuel': ('fuel_t', 'fuel', 'foc_t', 'fuel_used_t', 'me_fuel_t', 'fuel_consumption_t'),
    'distance': ('distance_nm', 'distance', 'dist_nm', 'distance_run_nm'),
    'hours': ('hours', 'steaming_hours', 'hours_underway'),
    'disp': ('displacement_t', 'disp_t', 'displacement'),
    'hs': ('hs_m', 'wave_m', 'waves_m', 'sea_state_m'),
    'headwind': ('headwind_ms', 'head_wind_ms'),
    'wind': ('wind_ms', 'wind_speed_ms'),
    'current': ('current_kn', 'current_along_kn'),
}
# A feature whose spread in the reports is below this keeps the prior coefficient.
MIN_SPREAD = {2: 0.03, 3: 1.0, 4: 20.0, 5: 0.02}


def _dir():
    d = Path(os.getenv('TIDELINE_RUNTIME_DIR', ROOT / 'runtime')) / 'calibration'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _model():
    m = get_fuel_model(FLEET_LIST)
    if not isinstance(m, RegressionFuelModel):
        raise ValueError('Calibration needs the regression fuel model (TIDELINE_FUEL_MODEL=regression)')
    if not hasattr(m, 'prior'):
        m.prior = {sid: dict(v) for sid, v in m.ships.items()}
    return m


def parse_reports(text, ship):
    if len(text.encode('utf-8', 'replace')) > MAX_BYTES:
        raise ValueError('File too large (limit 120 kB, about 2,000 reports)')
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise ValueError('No header row found')
    cols = {k.strip().lower(): k for k in reader.fieldnames}
    pick = {key: next((cols[a] for a in names if a in cols), None) for key, names in ALIASES.items()}
    if not pick['speed'] or not pick['fuel'] or not (pick['distance'] or pick['hours']):
        raise ValueError('Need columns for speed (speed_kn), fuel used (fuel_t) and distance (distance_nm) or hours')
    rows, skipped = [], 0
    num = lambda r, k: float(r[pick[k]]) if pick[k] and str(r.get(pick[k], '')).strip() not in ('', '-') else None
    for r in reader:
        try:
            v, f = num(r, 'speed'), num(r, 'fuel')
            d = num(r, 'distance') if pick['distance'] else None
            if d is None and pick['hours']:
                d = v * num(r, 'hours')
            disp = num(r, 'disp') or displacement(ship, ship['default_fuel_onboard_t'])
            hs = num(r, 'hs') or 0.0
            hw = num(r, 'headwind')
            if hw is None:
                hw = 0.7 * (num(r, 'wind') or 0.0)
            cur = num(r, 'current') or 0.0
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (v and f and d) or v < 3 or v > 30 or f <= 0 or d <= 0 or f / d > 2 or abs(cur) > 5:
            skipped += 1
            continue
        rows.append({'x': _features(v, disp, hs, hw, cur), 'y': math.log(f / d), 'fuel': f, 'nm': d})
    if len(rows) < MIN_REPORTS:
        raise ValueError(f'Need at least {MIN_REPORTS} usable reports; found {len(rows)} ({skipped} skipped)')
    return rows, skipped


def _predict(beta, sigma2, x):
    return math.exp(sum(b * xi for b, xi in zip(beta, x)) + sigma2 / 2)


def _mape(beta, sigma2, rows):
    return sum(abs(_predict(beta, sigma2, r['x']) * r['nm'] - r['fuel']) / r['fuel'] for r in rows) / len(rows)


def _fit(rows, prior_beta):
    k = len(prior_beta)
    fixed = []
    for i, thr in MIN_SPREAD.items():
        vals = [r['x'][i] for r in rows]
        m = sum(vals) / len(vals)
        if math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) < thr:
            fixed.append(i)
    free = [i for i in range(k) if i not in fixed]
    xs = [[r['x'][i] for i in free] for r in rows]
    ys = [r['y'] - sum(prior_beta[i] * r['x'][i] for i in fixed) for r in rows]
    b_free = ols(xs, ys, ridge=1e-6)
    beta = list(prior_beta)
    for i, b in zip(free, b_free):
        beta[i] = b
    resid = [r['y'] - sum(b * x for b, x in zip(beta, r['x'])) for r in rows]
    sigma2 = sum(e * e for e in resid) / max(1, len(resid) - len(free))
    return beta, sigma2, [FEATURES[i] for i in fixed]


def calibrate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Body must be a JSON object')
    sid = payload.get('ship_id')
    if sid not in FLEET:
        raise ValueError('Unknown ship_id')
    m = _model()
    f = _dir() / f'{sid}.json'
    if payload.get('reset'):
        m.ships[sid] = dict(m.prior[sid])
        if f.exists():
            f.unlink()
        return {'ship_id': sid, 'status': 'reset', 'model': describe(sid)}
    rows, skipped = parse_reports(str(payload.get('csv') or ''), FLEET[sid])
    split = max(int(len(rows) * 0.8), MIN_REPORTS - 5)
    train, hold = rows[:split], rows[split:] or rows[-5:]
    prior = m.prior[sid]
    beta, sigma2, kept = _fit(train, prior['beta'])
    before = _mape(prior['beta'], prior['sigma2'], hold)
    after = _mape(beta, sigma2, hold)
    # Final model uses every report.
    beta_all, sigma2_all, kept_all = _fit(rows, prior['beta'])
    ratio = sum(r['fuel'] for r in rows) / sum(_predict(prior['beta'], prior['sigma2'], r['x']) * r['nm'] for r in rows)
    entry = {'beta': beta_all, 'sigma2': sigma2_all, 'r2': None, 'holdout_mape': after, 'n_train': len(rows), 'n_holdout': len(hold),
             'source': 'calibrated', 'calibrated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
             'reports': len(rows), 'skipped': skipped, 'holdout_mape_before': before, 'kept_prior': kept_all,
             'fuel_vs_prior': ratio, 'file_name': str(payload.get('file_name') or '')[:80]}
    m.ships[sid] = entry
    f.write_text(json.dumps(entry))
    return {'ship_id': sid, 'status': 'calibrated', 'model': describe(sid)}


def describe(sid):
    m = _model()
    e = m.ships[sid]
    out = {'ship_id': sid, 'source': e.get('source', 'simulated'), 'coefficients': {k: round(b, 4) for k, b in zip(FEATURES, e['beta'])},
           'residual_sigma': round(math.sqrt(e['sigma2']), 4)}
    if e.get('source') == 'calibrated':
        out.update(reports=e['reports'], skipped=e['skipped'], calibrated_at=e['calibrated_at'], holdout_reports=e['n_holdout'],
                   holdout_error_before=round(e['holdout_mape_before'], 4), holdout_error_after=round(e['holdout_mape'], 4),
                   kept_prior=e['kept_prior'], fuel_vs_prior=round(e['fuel_vs_prior'], 3), file_name=e.get('file_name'))
    return out


def status():
    m = _model()
    return {sid: describe(sid) for sid in m.ships}


def load_saved():
    try:
        m = _model()
    except ValueError:
        return 0
    n = 0
    for f in _dir().glob('*.json'):
        sid = f.stem
        if sid in m.ships:
            try:
                m.ships[sid] = json.loads(f.read_text())
                n += 1
            except ValueError:
                pass
    return n


def sample_csv(sid, n=80, seed=11):
    """SYNTHETIC noon reports: the physics formula x 1.12 (a fouled hull) with 5% noise. For demos only."""
    if sid not in FLEET:
        raise ValueError('Unknown ship_id')
    ship = FLEET[sid]
    phys = _model().physics
    rng = random.Random(f'{seed}:{sid}')
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['date', 'speed_kn', 'distance_nm', 'fuel_t', 'displacement_t', 'hs_m', 'headwind_ms', 'current_kn', 'note'])
    for i in range(n):
        v = round(rng.uniform(ship['min_speed_kn'], ship['max_speed_kn'] - 2), 1)
        disp = round(ship['ref_displacement_t'] * rng.uniform(0.75, 1.0))
        hs = round(rng.uniform(0.5, 4.5), 1)
        hw = round(rng.uniform(-8, 14), 1)
        cur = round(rng.uniform(-1, 1), 2)
        nm = round(24 * max(v * 0.3, v + cur), 1)
        fuel = phys.fuel_per_nm(ship, v, disp, hs, hw, cur) * nm * 1.12 * math.exp(rng.gauss(0, 0.05))
        w.writerow([(datetime(2026, 6, 1) + timedelta(days=i)).date().isoformat(), v, nm, round(fuel, 2), disp, hs, hw, cur, 'SYNTHETIC sample' if i == 0 else ''])
    return out.getvalue()
