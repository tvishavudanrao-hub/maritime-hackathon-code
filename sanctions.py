"""Vessel sanctions screening against the US Treasury OFAC SDN list (free, official).

Downloads SDN.CSV once a day into runtime/sdn.csv (columns: ent_num, SDN_Name, SDN_Type, Program,
Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag, Vess_owner, Remarks; null = "-0-") and keeps
the entries whose SDN_Type is "vessel". IMO and MMSI numbers are read from Remarks.

Matching: an IMO or MMSI number match is a match; a vessel-name match alone is a possible match
(names are reused), to be resolved by a compliance officer. This is screening support only: it
covers one list (US OFAC SDN), not EU, UK or UN lists, and is not a legal determination.
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
URLS = [u for u in [os.getenv('OFAC_SDN_URL'),
                    'https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV',
                    'https://www.treasury.gov/ofac/downloads/sdn.csv'] if u]
SOURCE = 'US Treasury OFAC Specially Designated Nationals (SDN) list'
REFRESH_S = 24 * 3600
_lock = threading.Lock()
_state = {'vessels': [], 'by_imo': {}, 'by_mmsi': {}, 'by_name': {}, 'status': 'not loaded', 'loaded_at': None,
          'error': None, 'file_date': None, 'url': None}


def _cache():
    d = Path(os.getenv('TIDELINE_RUNTIME_DIR', ROOT / 'runtime'))
    d.mkdir(parents=True, exist_ok=True)
    return d / 'sdn.csv'


def norm_name(s):
    s = re.sub(r'[^A-Z0-9 ]', ' ', str(s or '').upper())
    s = re.sub(r'\b(MV|M V|MT|M T|SS)\b', ' ', s)
    return ' '.join(s.split())


def parse_sdn(text):
    vessels = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 12:
            continue
        clean = [('' if c.strip() == '-0-' else c.strip()) for c in row]
        if clean[2].lower() != 'vessel':
            continue
        remarks = clean[11]
        imo = re.search(r'IMO\s*(\d{7})', remarks)
        mmsi = re.search(r'MMSI\s*(\d{9})', remarks)
        vessels.append({'ent_num': clean[0], 'name': clean[1], 'program': clean[3], 'call_sign': clean[5],
                        'vessel_type': clean[6], 'flag': clean[9], 'owner': clean[10],
                        'imo': imo.group(1) if imo else None, 'mmsi': mmsi.group(1) if mmsi else None})
    return vessels


def load_text(text, source_url=None, file_date=None):
    vessels = parse_sdn(text)
    by_imo = {v['imo']: v for v in vessels if v['imo']}
    by_mmsi = {v['mmsi']: v for v in vessels if v['mmsi']}
    by_name = {}
    for v in vessels:
        by_name.setdefault(norm_name(v['name']), []).append(v)
    with _lock:
        _state.update(vessels=vessels, by_imo=by_imo, by_mmsi=by_mmsi, by_name=by_name, status='loaded' if vessels else 'empty',
                      loaded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), error=None,
                      url=source_url, file_date=file_date)
    return len(vessels)


def refresh():
    f = _cache()
    if f.exists() and time.time() - f.stat().st_mtime < REFRESH_S:
        return load_text(f.read_text(encoding='utf-8', errors='replace'), 'cache', datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).date().isoformat())
    last = None
    for url in URLS:
        try:
            with urlopen(Request(url, headers={'User-Agent': 'TidelineHackathonDemo/0.3'}), timeout=60) as r:
                text = r.read().decode('utf-8', errors='replace')
            if 'vessel' not in text.lower():
                raise ValueError('download did not look like the SDN list')
            f.write_text(text, encoding='utf-8')
            n = load_text(text, url, datetime.now(timezone.utc).date().isoformat())
            print(f'[sanctions] OFAC SDN list loaded: {n} vessels', flush=True)
            return n
        except Exception as exc:
            last = f'{type(exc).__name__}: {str(exc)[:160]}'
    if f.exists():   # stale copy is better than none, and is labeled as such
        n = load_text(f.read_text(encoding='utf-8', errors='replace'), 'cache (stale)', datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).date().isoformat())
        with _lock:
            _state['error'] = last
        return n
    with _lock:
        _state.update(status='unavailable', error=last)
    print(f'[sanctions] OFAC list unavailable: {last}', flush=True)
    return 0


def start_background_refresh():
    def loop():
        while True:
            refresh()
            time.sleep(REFRESH_S)
    threading.Thread(target=loop, name='tideline-sanctions', daemon=True).start()


def status():
    with _lock:
        return {k: _state[k] for k in ('status', 'loaded_at', 'error', 'file_date', 'url')} | {
            'vessels': len(_state['vessels']), 'source': SOURCE,
            'scope': 'US OFAC SDN vessels only; EU, UK and UN lists are not screened. Screening support, not a legal determination.'}


def screen_vessel(name=None, imo=None, mmsi=None):
    with _lock:
        loaded = _state['status'] == 'loaded'
        hits = []
        if imo and str(imo) in _state['by_imo']:
            hits.append(dict(_state['by_imo'][str(imo)], matched_on='IMO'))
        if mmsi and str(mmsi) in _state['by_mmsi']:
            v = _state['by_mmsi'][str(mmsi)]
            if v not in hits:
                hits.append(dict(v, matched_on='MMSI'))
        name_hits = [dict(v, matched_on='name') for v in _state['by_name'].get(norm_name(name), [])] if name else []
    if not loaded:
        st = 'not screened'
    elif hits:
        st = 'match'
    elif name_hits:
        st = 'possible match'
    else:
        st = 'clear'
    return {'status': st, 'matches': (hits or name_hits)[:5], 'checked': {'name': name, 'imo': imo, 'mmsi': mmsi},
            'source': SOURCE, 'list_date': _state.get('file_date')}
