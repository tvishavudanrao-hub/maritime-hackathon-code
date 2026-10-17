"""PortWatch parsing tests with a fake ArcGIS response (no network)."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portwatch  # noqa: E402


def ms(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def fake_fetch(today, recent_bab=25, base_bab=75):
    def fetch(url):
        where = parse_qs(urlparse(url).query)['where'][0]
        feats = []
        if str(portwatch.BASELINE_YEAR) in where:
            end = today.replace(year=portwatch.BASELINE_YEAR)
            for i in range(14):
                d = end - timedelta(days=i)
                feats += [{'attributes': {'portname': 'Bab el-Mandeb Strait', 'date': ms(d), 'n_total': base_bab}},
                          {'attributes': {'portname': 'Suez Canal', 'date': ms(d), 'n_total': 70}}]
        else:
            for i in range(21):
                d = today - timedelta(days=i)
                feats += [{'attributes': {'portname': 'Bab el-Mandeb Strait', 'date': ms(d), 'n_total': recent_bab}},
                          {'attributes': {'portname': 'Suez Canal', 'date': ms(d), 'n_total': 35}},
                          {'attributes': {'portname': 'Panama Canal', 'date': ms(d), 'n_total': 30}}]
        return {'features': feats}
    return fetch


class PortWatchTests(unittest.TestCase):
    def test_ratios_vs_2023(self):
        today = date(2026, 10, 15)
        snap = portwatch.fetch_snapshot(today, fetch=fake_fetch(today))
        self.assertAlmostEqual(snap['zones']['bab']['ratio'], 25 / 75, places=3)
        self.assertAlmostEqual(snap['zones']['suez']['ratio'], 0.5, places=3)
        self.assertNotIn('hormuz', snap['zones'])
        self.assertIn('IMF PortWatch', snap['citation'])

    def test_error_payload_raises(self):
        with self.assertRaises(ValueError):
            portwatch.fetch_snapshot(date(2026, 10, 15), fetch=lambda url: {'error': {'message': 'bad query'}})


if __name__ == '__main__':
    unittest.main()
