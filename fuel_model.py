"""Tideline fuel model. Python standard library only.

Two interchangeable models behind one interface, `leg(...)`:

* PhysicsFuelModel   -- Admiralty cubic law with added resistance for waves and wind,
                        and speed over ground adjusted for currents.
* RegressionFuelModel -- ordinary least squares on log fuel-per-mile, trained per ship.
                        THE TRAINING DATA IS SIMULATED: it is generated from the physics
                        model plus multiplicative noise. Retrain on real noon reports to
                        replace it; nothing else in the system changes.

Physics (per day):   fuel = ref_daily * (D / D_ref)^(2/3) * (V / V_ref)^3 * weather_factor
Weather factor:      1 + A_WAVE * Hs^2 + B_WIND * max(0, headwind)^2
Per leg:             hours = distance / V_ground,  V_ground = V + current_along
Regression:          ln(fuel per nm) = b0 + b1 ln V + b2 ln D + b3 Hs^2 + b4 headwind^2 + b5 (current / V)
"""
from __future__ import annotations

import math
import os
import random

A_WAVE = 0.012     # +1.2% resistance per m^2 of significant wave height (illustrative)
B_WIND = 0.0012    # +0.12% per (m/s)^2 of head wind (illustrative)
NOISE_SIGMA = 0.07 # 7% multiplicative noise in simulated training data
CO2_PER_T_FUEL = 3.114  # IMO emission factor for heavy fuel oil (t CO2 per t fuel)


def displacement(ship, fuel_onboard_t):
    return ship['lightship_t'] + ship['cargo_t'] + max(0.0, fuel_onboard_t)


def ground_speed(speed_kn, current_along_kn):
    # A ship never makes less than 30% of its water speed in this model.
    return max(speed_kn * 0.3, speed_kn + current_along_kn)


class PhysicsFuelModel:
    name = 'physics'
    description = 'Admiralty cubic law with wave and wind added resistance (baseline formula)'

    def daily_fuel(self, ship, speed_kn, disp_t, hs_m=0.0, headwind_ms=0.0):
        base = ship['ref_daily_t'] * (disp_t / ship['ref_displacement_t']) ** (2 / 3) * (speed_kn / ship['ref_speed_kn']) ** 3
        return base * (1 + A_WAVE * hs_m ** 2 + B_WIND * max(0.0, headwind_ms) ** 2)

    def fuel_per_nm(self, ship, speed_kn, disp_t, hs_m=0.0, headwind_ms=0.0, current_along_kn=0.0):
        return self.daily_fuel(ship, speed_kn, disp_t, hs_m, headwind_ms) / (24 * ground_speed(speed_kn, current_along_kn))

    def leg(self, ship, distance_nm, speed_kn, fuel_onboard_t, hs_m=0.0, headwind_ms=0.0, current_along_kn=0.0):
        """Return (hours, tonnes) for one leg."""
        hours = distance_nm / ground_speed(speed_kn, current_along_kn)
        disp = displacement(ship, fuel_onboard_t)
        return hours, self.fuel_per_nm(ship, speed_kn, disp, hs_m, headwind_ms, current_along_kn) * distance_nm

    def info(self):
        return {'name': self.name, 'description': self.description,
                'constants': {'A_WAVE': A_WAVE, 'B_WIND': B_WIND}}


FEATURES = ['intercept', 'ln_speed', 'ln_displacement', 'hs_squared', 'headwind_squared', 'current_over_speed']


def _features(speed_kn, disp_t, hs_m, headwind_ms, current_along_kn):
    return [1.0, math.log(speed_kn), math.log(disp_t), hs_m ** 2, max(0.0, headwind_ms) ** 2, current_along_kn / speed_kn]


def _solve(a, b):
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError('Singular matrix in least squares')
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    for c in range(col, n + 1):
                        m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(rows, targets, ridge=1e-9):
    k = len(rows[0])
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for x, y in zip(rows, targets):
        for i in range(k):
            xty[i] += x[i] * y
            xi = x[i]
            for j in range(i, k):
                xtx[i][j] += xi * x[j]
    for i in range(k):
        xtx[i][i] += ridge
        for j in range(i):
            xtx[i][j] = xtx[j][i]
    return _solve(xtx, xty)


class RegressionFuelModel:
    name = 'regression'
    description = 'Per-ship OLS on log fuel per nautical mile, trained on SIMULATED segments (physics + 7% noise)'

    def __init__(self, fleet, samples_per_ship=1500, seed=7):
        self.physics = PhysicsFuelModel()
        self.ships = {}
        for ship in fleet:
            self.ships[ship['id']] = self._train(ship, samples_per_ship, seed)

    def _simulate(self, ship, n, rng):
        rows, ys = [], []
        for _ in range(n):
            v = rng.uniform(ship['min_speed_kn'] - 1, ship['max_speed_kn'] + 1)
            disp = ship['ref_displacement_t'] * rng.uniform(0.65, 1.1)
            hs = rng.uniform(0, 6.5)
            hw = rng.uniform(-15, 18)
            cur = rng.uniform(-2.5, 2.5)
            true = self.physics.fuel_per_nm(ship, v, disp, hs, hw, cur)
            observed = true * math.exp(rng.gauss(0, NOISE_SIGMA))
            rows.append(_features(v, disp, hs, hw, cur))
            ys.append(math.log(observed))
        return rows, ys

    def _train(self, ship, n, seed):
        rng = random.Random(f"{seed}:{ship['id']}")
        rows, ys = self._simulate(ship, n, rng)
        split = int(n * 0.8)
        beta = ols(rows[:split], ys[:split])
        resid = [y - sum(b * x for b, x in zip(beta, r)) for r, y in zip(rows[:split], ys[:split])]
        sigma2 = sum(e * e for e in resid) / max(1, len(resid) - len(beta))
        mean_y = sum(ys[:split]) / split
        ss_tot = sum((y - mean_y) ** 2 for y in ys[:split])
        r2 = 1 - sum(e * e for e in resid) / ss_tot
        # Hold-out error against the noise-free physics value.
        errors = []
        for r, y in zip(rows[split:], ys[split:]):
            pred = math.exp(sum(b * x for b, x in zip(beta, r)) + sigma2 / 2)
            v, disp = math.exp(r[1]), math.exp(r[2])
            hs, hw2, cur = math.sqrt(r[3]), r[4], r[5] * math.exp(r[1])
            true = self.physics.fuel_per_nm(ship, v, disp, hs, math.sqrt(hw2), cur)
            errors.append(abs(pred - true) / true)
        return {'beta': beta, 'sigma2': sigma2, 'r2': r2, 'holdout_mape': sum(errors) / len(errors),
                'n_train': split, 'n_holdout': n - split}

    def fuel_per_nm(self, ship, speed_kn, disp_t, hs_m=0.0, headwind_ms=0.0, current_along_kn=0.0):
        model = self.ships.get(ship['id'])
        if model is None:  # unseen ship: fall back to the transparent formula
            return self.physics.fuel_per_nm(ship, speed_kn, disp_t, hs_m, headwind_ms, current_along_kn)
        x = _features(speed_kn, disp_t, hs_m, headwind_ms, current_along_kn)
        return math.exp(sum(b * xi for b, xi in zip(model['beta'], x)) + model['sigma2'] / 2)

    def leg(self, ship, distance_nm, speed_kn, fuel_onboard_t, hs_m=0.0, headwind_ms=0.0, current_along_kn=0.0):
        hours = distance_nm / ground_speed(speed_kn, current_along_kn)
        disp = displacement(ship, fuel_onboard_t)
        return hours, self.fuel_per_nm(ship, speed_kn, disp, hs_m, headwind_ms, current_along_kn) * distance_nm

    def info(self):
        ships = {}
        for sid, m in self.ships.items():
            ships[sid] = {
                'coefficients': {f: round(b, 5) for f, b in zip(FEATURES, m['beta'])},
                'r2': round(m['r2'], 4), 'holdout_mape': round(m['holdout_mape'], 4),
                'n_train': m['n_train'], 'n_holdout': m['n_holdout'],
                'physics_check': 'ln_speed should be near 2.0 and ln_displacement near 0.667 if the model learned the physics',
            }
        return {'name': self.name, 'description': self.description, 'training_data': 'simulated',
                'noise_sigma': NOISE_SIGMA, 'ships': ships}


_MODEL = None


def get_fuel_model(fleet):
    """Selected by TIDELINE_FUEL_MODEL=physics|regression (default regression)."""
    global _MODEL
    if _MODEL is None:
        choice = os.getenv('TIDELINE_FUEL_MODEL', 'regression').lower()
        _MODEL = PhysicsFuelModel() if choice == 'physics' else RegressionFuelModel(fleet)
    return _MODEL
