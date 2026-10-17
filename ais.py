"""Live ship positions from AISStream.io (free API key: https://aisstream.io).

Python standard library only: a small RFC 6455 websocket client over TLS. Runs in a background
thread when AISSTREAM_API_KEY is set. It watches a few boxes around chokepoints and ports on our
network, plus any ships the operator chooses to track by MMSI, and keeps each ship's latest
position and a short trail. The dashboard polls /api/ais.

AIS is broadcast by ships themselves: positions can be delayed, missing or occasionally wrong,
and coverage far offshore depends on satellite and terrestrial receivers.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import socket
import ssl
import struct
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import sanctions

URL = os.getenv('AISSTREAM_WS_URL', 'wss://stream.aisstream.io/v0/stream')
# [[lat_min, lon_min], [lat_max, lon_max]] — watched areas on the Tideline network
WATCH_BOXES = {
    'Bab el-Mandeb': [[11.5, 42.5], [13.5, 44.5]],
    'Suez Canal': [[29.8, 32.2], [31.4, 32.7]],
    'Strait of Hormuz': [[25.8, 55.8], [27.0, 57.2]],
    'Singapore Strait': [[0.9, 103.4], [1.5, 104.4]],
    'Rotterdam approaches': [[51.8, 3.4], [52.2, 4.4]],
    'Cape of Good Hope': [[-35.5, 17.5], [-33.5, 20.5]],
}
MAX_SHIPS = 800
TRAIL_POINTS = 40
STALE_SECONDS = 3600


class WebSocket:
    """Minimal client: text frames, ping/pong, close. Enough for a JSON stream."""

    def __init__(self, url, timeout=20):
        u = urlparse(url)
        host, port = u.hostname, u.port or (443 if u.scheme == 'wss' else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == 'wss' else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or '/') + (('?' + u.query) if u.query else '')
        req = (f'GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: TidelineHackathonDemo/0.2\r\n\r\n')
        self.sock.sendall(req.encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError('Handshake closed')
            head += chunk
        status = head.split(b'\r\n', 1)[0]
        if b' 101 ' not in status:
            raise ConnectionError(f'Handshake failed: {status.decode(errors="replace")}')
        self.buf = head.split(b'\r\n\r\n', 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('Connection closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            head += bytes([0x80 | 127]) + struct.pack('>Q', n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text):
        self._send(0x1, text.encode())

    def recv(self):
        """Next complete text or binary message as str."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError('Server closed the stream' + (f': {data[2:].decode(errors="replace")}' if len(data) > 2 else ''))
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b''.join(parts).decode('utf-8', 'replace')

    def close(self):
        try:
            self._send(0x8, b'')
            self.sock.close()
        except OSError:
            pass


def parse_message(raw):
    """AISStream JSON -> (mmsi, record) or None."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if 'error' in msg:
        raise ValueError(str(msg['error'])[:200])
    kind = msg.get('MessageType')
    meta = msg.get('MetaData') or {}
    body = (msg.get('Message') or {}).get(kind) or {}
    mmsi = meta.get('MMSI') or body.get('UserID')
    if not mmsi:
        return None
    rec = {'mmsi': str(mmsi), 'name': (meta.get('ShipName') or '').strip() or None}
    if kind in ('PositionReport', 'StandardClassBPositionReport', 'ExtendedClassBPositionReport'):
        lat = meta.get('latitude', body.get('Latitude'))
        lon = meta.get('longitude', body.get('Longitude'))
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        hdg = body.get('TrueHeading')
        rec.update(lat=round(lat, 5), lon=round(lon, 5), sog=body.get('Sog'), cog=body.get('Cog'),
                   heading=hdg if isinstance(hdg, (int, float)) and hdg < 360 else None,
                   time=str(meta.get('time_utc') or datetime.now(timezone.utc).isoformat())[:26])
    elif kind == 'ShipStaticData':
        imo = body.get('ImoNumber')
        rec.update(destination=(body.get('Destination') or '').strip() or None, ship_type=body.get('Type'),
                   draught=body.get('MaximumStaticDraught'), imo=str(imo) if isinstance(imo, int) and imo > 999999 else None)
    else:
        return None
    return rec['mmsi'], rec


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.ships = {}
        self.tracked = set()
        self.status = {'status': 'off', 'error': None, 'connected_at': None, 'messages': 0}
        self._restart = threading.Event()
        self.thread = None

    def update(self, mmsi, rec):
        with self.lock:
            s = self.ships.get(mmsi)
            if s is None:
                if len(self.ships) >= MAX_SHIPS and mmsi not in self.tracked:
                    return
                s = self.ships[mmsi] = {'mmsi': mmsi, 'trail': collections.deque(maxlen=TRAIL_POINTS)}
            for k, v in rec.items():
                if v is not None:
                    s[k] = v
            if 'lat' in rec:
                s['trail'].append((rec['lat'], rec['lon']))
                s['seen'] = time.time()
            self.status['messages'] += 1

    def snapshot(self):
        now = time.time()
        with self.lock:
            for k in [k for k, s in self.ships.items() if now - s.get('seen', now) > STALE_SECONDS and k not in self.tracked]:
                del self.ships[k]
            ships = [dict(s, trail=list(s['trail']), age_s=round(now - s.get('seen', now)), tracked=s['mmsi'] in self.tracked)
                     for s in self.ships.values() if 'lat' in s]
        for s in ships:   # flag ships on the OFAC SDN list (IMO/MMSI match, or name-only possible match)
            sc = sanctions.screen_vessel(s.get('name'), s.get('imo'), s['mmsi'])
            if sc['status'] in ('match', 'possible match'):
                s['sanctions'] = {'status': sc['status'], 'program': sc['matches'][0].get('program'), 'listed_name': sc['matches'][0]['name'],
                                  'matched_on': sc['matches'][0]['matched_on']}
        with self.lock:
            return {**self.status, 'tracked': sorted(self.tracked), 'watch_boxes': WATCH_BOXES, 'ships': ships,
                    'note': 'Live AIS via AISStream.io. Ships report their own positions; data can be delayed or missing.'}

    def track(self, mmsi):
        mmsi = str(mmsi).strip()
        if not (mmsi.isdigit() and len(mmsi) == 9):
            raise ValueError('MMSI must be 9 digits')
        with self.lock:
            if mmsi in self.tracked:
                return
            if len(self.tracked) >= 20:
                raise ValueError('Tracking at most 20 ships')
            self.tracked.add(mmsi)
        self._restart.set()   # resubscribe with the new filter

    def subscription(self, key):
        """One connection per API key: either watch the chokepoint boxes, or follow chosen ships world-wide."""
        types = ['PositionReport', 'StandardClassBPositionReport', 'ShipStaticData']
        with self.lock:
            tracked = sorted(self.tracked)
        if tracked:
            return {'APIKey': key, 'BoundingBoxes': [[[-90, -180], [90, 180]]], 'FiltersShipMMSI': tracked, 'FilterMessageTypes': types}
        return {'APIKey': key, 'BoundingBoxes': list(WATCH_BOXES.values()), 'FilterMessageTypes': types}

    def untrack_all(self):
        with self.lock:
            self.tracked.clear()
        self._restart.set()

    def run(self, key, url=URL):
        while True:
            ws = None
            try:
                ws = WebSocket(url)
                sub = self.subscription(key)
                ws.send_text(json.dumps(sub))   # must be sent within a few seconds of connecting
                self._restart.clear()
                with self.lock:
                    self.status.update(status='live', error=None, connected_at=datetime.now(timezone.utc).isoformat(),
                                       mode='tracking' if 'FiltersShipMMSI' in sub else 'watching chokepoints')
                self._pump(ws)
            except Exception as exc:
                with self.lock:
                    self.status.update(status='unavailable', error=f'{type(exc).__name__}: {str(exc)[:160]}')
                print(f"[ais] {self.status['error']}", flush=True)
                self._restart.wait(20)
            finally:
                if ws:
                    ws.close()

    def _pump(self, ws):
        ws.sock.settimeout(5)
        last = time.time()
        while not self._restart.is_set():
            try:
                raw = ws.recv()
            except socket.timeout:
                if time.time() - last > 600:
                    raise ConnectionError('No AIS messages for 10 minutes')
                continue
            last = time.time()
            parsed = parse_message(raw)
            if parsed:
                self.update(*parsed)


TRACKER = Tracker()


def start(key=None):
    key = key or os.getenv('AISSTREAM_API_KEY')
    if not key or TRACKER.thread:
        if not key:
            TRACKER.status.update(status='off', error='Set AISSTREAM_API_KEY to show live ships')
        return
    TRACKER.status['status'] = 'connecting'
    TRACKER.thread = threading.Thread(target=TRACKER.run, args=(key,), name='tideline-ais', daemon=True)
    TRACKER.thread.start()
