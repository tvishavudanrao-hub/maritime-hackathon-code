"""Tideline risk model: a transparent scoring model, not a trained one.

There is no labeled data of voyages that went badly, so the factor weights are
explicit assumptions. Each factor scores 0-1 per leg; factors combine with a
noisy-OR so one severe factor dominates; the route combines legs weighted by
time exposed.

  r_weather  = 1 / (1 + exp(-k (Hs - H_mid))),  k = 2, H_mid = 0.8 x the ship's safe limit
  r_security = zone base level + min(0.3, 0.05 x matching headlines) + operator bump
  r_traffic  = 0.35 x max(0, 1 - transits this week / normal transits)   (IMF PortWatch)
  r_leg      = 1 - (1 - r_weather)(1 - r_security)(1 - r_traffic)
  route risk = 1 - prod (1 - r_leg)^(hours / 24)
"""
from __future__ import annotations

import math

WEATHER_K = 2.0
WEATHER_MID_FRACTION = 0.8
NEWS_PER_HIT = 0.05
NEWS_CAP = 0.3
TRAFFIC_WEIGHT = 0.35  # how much a traffic drop counts: other operators avoiding a chokepoint is evidence, not proof


def weather_risk(hs_m, ship):
    mid = ship['max_hs_m'] * WEATHER_MID_FRACTION
    return 1 / (1 + math.exp(-WEATHER_K * (hs_m - mid)))


class RiskModel:
    def __init__(self, zones, security_bumps=None, news_hits=None, traffic=None):
        self.zones = zones
        self.security_bumps = security_bumps or {}   # zone -> extra level from events
        self.news_hits = news_hits or {}             # zone -> count of disruption headlines
        self.traffic = traffic or {}                 # zone -> this week / normal transits

    def security(self, zone_ids):
        best = 0.0
        for z in zone_ids:
            base = self.zones.get(z, {}).get('security_base', 0.0)
            news = min(NEWS_CAP, NEWS_PER_HIT * self.news_hits.get(z, 0))
            best = max(best, min(0.95, base + news + self.security_bumps.get(z, 0.0)))
        return best

    def traffic_shift(self, zone_ids):
        best = 0.0
        for z in zone_ids:
            ratio = self.traffic.get(z)
            if ratio is not None:
                best = max(best, TRAFFIC_WEIGHT * max(0.0, min(1.0, 1 - ratio)))
        return best

    def leg(self, hs_m, ship, zone_ids):
        rw = weather_risk(hs_m, ship)
        rs = self.security(zone_ids)
        rt = self.traffic_shift(zone_ids)
        r = 1 - (1 - rw) * (1 - rs) * (1 - rt)
        return min(0.999, r), {'weather': rw, 'security': rs, 'traffic': rt}

    @staticmethod
    def exposure(r_leg, hours):
        """Additive survival exposure; route risk = 1 - exp(-sum)."""
        return -math.log(1 - r_leg) * hours / 24

    @staticmethod
    def route_risk(total_exposure):
        return 1 - math.exp(-total_exposure)
