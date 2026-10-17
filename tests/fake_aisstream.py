"""A tiny local stand-in for AISStream used by tests and offline demos.

Accepts one websocket client, reads the subscription, then streams PositionReports for a few
synthetic ships moving through Bab el-Mandeb and the Suez Canal (clearly fake names).
Run:  python3 tests/fake_aisstream.py 8765   then   AISSTREAM_WS_URL=ws://127.0.0.1:8765 AISSTREAM_API_KEY=test python3 server.py
"""
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
SHIPS = [
    {'mmsi': 999000001, 'name': 'TEST VESSEL ALPHA', 'lat': 12.4, 'lon': 43.6, 'sog': 14.0, 'cog': 330.0},
    {'mmsi': 999000002, 'name': 'TEST VESSEL BRAVO', 'lat': 12.9, 'lon': 43.1, 'sog': 12.0, 'cog': 150.0},
    {'mmsi': 999000003, 'name': 'TEST VESSEL CHARLIE', 'lat': 30.3, 'lon': 32.4, 'sog': 8.0, 'cog': 0.0},
]


def _frame(text):
    data = text.encode()
    n = len(data)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack('>H', n) if n < 65536 else bytes([127]) + struct.pack('>Q', n))
    return head + data


def _read_frame(conn):
    b1, b2 = conn.recv(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack('>H', conn.recv(2))[0]
    elif n == 127:
        n = struct.unpack('>Q', conn.recv(8))[0]
    mask = conn.recv(4)
    data = b''
    while len(data) < n:
        data += conn.recv(n - len(data))
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode()


def serve(port, steps=None, interval=0.5, received=None):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', port))
    srv.listen(1)
    conn, _ = srv.accept()
    req = conn.recv(4096).decode()
    key = [l.split(':', 1)[1].strip() for l in req.split('\r\n') if l.lower().startswith('sec-websocket-key')][0]
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n'.encode())
    sub = json.loads(_read_frame(conn))
    if received is not None:
        received.append(sub)
    i = 0
    try:
        while steps is None or i < steps:
            for s in SHIPS:
                d = s['sog'] * interval * 60 / 3600   # speeded-up motion so the demo visibly moves
                import math
                s['lat'] += d * math.cos(math.radians(s['cog'])) / 60
                s['lon'] += d * math.sin(math.radians(s['cog'])) / 60
                msg = {'MessageType': 'PositionReport',
                       'MetaData': {'MMSI': s['mmsi'], 'ShipName': s['name'], 'latitude': s['lat'], 'longitude': s['lon'],
                                    'time_utc': datetime.now(timezone.utc).isoformat()},
                       'Message': {'PositionReport': {'Cog': s['cog'], 'Sog': s['sog'], 'TrueHeading': int(s['cog']),
                                                      'Latitude': s['lat'], 'Longitude': s['lon'], 'UserID': s['mmsi']}}}
                conn.sendall(_frame(json.dumps(msg)))
            i += 1
            time.sleep(interval)
    except OSError:
        pass
    finally:
        conn.close()
        srv.close()


if __name__ == '__main__':
    while True:
        serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
