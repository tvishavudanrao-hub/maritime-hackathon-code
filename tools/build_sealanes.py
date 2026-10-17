"""Rebuild data/network.json edge geometry on real shipping lanes (development tool, run once).

Usage:  python3 tools/build_sealanes.py path/to/marnet_searoute.geojson

Source network: Eurostat SeaRoute "MARNET" maritime network, as packaged in searoute-py
(https://github.com/genthalili/searoute-py, Apache-2.0). For every Tideline edge it snaps both
ends to the nearest MARNET node, finds the shortest MARNET path (straits and canals the edge is
not meant to use are blocked: an edge tagged 'suez' may use the Suez Canal, others may not),
simplifies it (Douglas-Peucker, ~3 nm), and writes it back with the original zones. Edges whose
new length differs from the old by more than 35% are reported for review rather than replaced.
The runtime stays standard-library only: this script is not needed to run Tideline.
"""
import heapq
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NET = ROOT / 'data' / 'network.json'
PASSAGE_FOR_ZONE = {'suez': 'suez', 'bab': 'babalmandab', 'hormuz': 'ormuz', 'malacca': 'malacca'}
ALWAYS_BLOCKED = {'panama', 'bering', 'northwest', 'chili', 'bosporus', 'dardanelles', 'sunda'}


def hav(a, b):   # (lat, lon)
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1 - x)))


def load(path):
    feats = json.loads(Path(path).read_text())['features']
    nodes, adj = {}, {}
    key = lambda lon, lat: (round(lat, 4), round(((lon + 180) % 360) - 180, 4))
    for f in feats:
        passage = f['properties'].get('passage')
        g = f['geometry']
        parts = g['coordinates'] if g['type'] == 'MultiLineString' else [g['coordinates']]
        for coords in parts:
            coords = [c[:2] for c in coords]
            for (lo1, la1), (lo2, la2) in zip(coords[:-1], coords[1:]):
                a, b = key(lo1, la1), key(lo2, la2)
                if a == b or abs(a[1] - b[1]) > 180:
                    continue
                d = hav(a, b)
                for u, v in ((a, b), (b, a)):
                    adj.setdefault(u, []).append((v, d, passage))
                nodes[a] = nodes[b] = True
    return list(nodes), adj


def nearest(nodes, p, adj, blocked):
    best = min((n for n in nodes if any(pp not in blocked for _, _, pp in adj[n])), key=lambda n: hav(n, p))
    return best, hav(best, p)


def shortest(adj, s, t, blocked):
    dist, prev, pq = {s: 0.0}, {}, [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w, passage in adj.get(u, []):
            if passage in blocked:
                continue
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if t not in dist:
        return None
    path, u = [t], t
    while u != s:
        u = prev[u]
        path.append(u)
    return path[::-1]


def dp(points, tol_nm=3.0):
    if len(points) < 3:
        return points
    a, b = points[0], points[-1]
    def dist(p):   # distance from p to chord a-b, flat approximation in nm
        kx = math.cos(math.radians((a[0] + b[0]) / 2))
        ax, ay, bx, by, px, py = a[1] * kx, a[0], b[1] * kx, b[0], p[1] * kx, p[0]
        dx, dy = bx - ax, by - ay
        t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) * 60
    i, dmax = max(((i, dist(p)) for i, p in enumerate(points[1:-1], 1)), key=lambda x: x[1])
    if dmax <= tol_nm:
        return [a, b]
    return dp(points[:i + 1], tol_nm)[:-1] + dp(points[i:], tol_nm)


def main(path):
    net = json.loads(NET.read_text())
    nodes, adj = load(path)
    report = []
    # Open-ocean waypoints are abstract: move each onto the nearest MARNET node so lanes meet cleanly.
    for k, w in net['waypoints'].items():
        n, d = nearest(nodes, w['loc'], adj, set())
        if d <= 150:
            report.append(f"waypoint {k}: moved {d:.0f} nm onto the lane network")
            w['loc'] = [round(n[0], 3), round(n[1], 3)]
    loc = {**{k: v['loc'] for k, v in net['ports'].items()}, **{k: v['loc'] for k, v in net['waypoints'].items()}}
    for e in net['edges']:
        allowed = {PASSAGE_FOR_ZONE[z] for z in e.get('zones', []) if z in PASSAGE_FOR_ZONE}
        if 'GIB' in (e['from'], e['to']):
            allowed.add('gibraltar')
        if {e['from'], e['to']} & {'agulhas', 'CPT', 'DUR'}:
            allowed.add('south_africa')
        if 'suez_south' in (e['from'], e['to']):
            allowed.add('suez')
        blocked = (ALWAYS_BLOCKED | set(PASSAGE_FOR_ZONE.values()) | {'gibraltar', 'south_africa'}) - allowed
        a, b = loc[e['from']], loc[e['to']]
        e['points'][0], e['points'][-1] = list(a), list(b)   # keep hand-drawn fallback consistent with moved waypoints
        s, ds = nearest(nodes, a, adj, blocked)
        t, dt = nearest(nodes, b, adj, blocked)
        path = shortest(adj, s, t, blocked)
        old = sum(hav(p, q) for p, q in zip(e['points'][:-1], e['points'][1:]))
        if not path:
            report.append(f"{e['from']}-{e['to']}: no MARNET path, kept hand-drawn geometry")
            continue
        pts = [list(a)] + [[p[0], p[1]] for p in path] + [list(b)]
        pts = dp(pts)
        pts = [[round(p[0], 3), round(p[1], 3)] for p in pts]
        new = sum(hav(p, q) for p, q in zip(pts[:-1], pts[1:]))
        change = new / old - 1
        flag = abs(change) > (0.6 if e.get('canal') else 0.35)
        report.append(f"{e['from']:>12}-{e['to']:<12} {old:7.0f} -> {new:7.0f} nm ({change:+.0%}) snap {ds:.0f}/{dt:.0f} nm, {len(pts)} pts{'  REVIEW' if flag else ''}")
        if not flag:
            e['points'] = pts
            e['geometry'] = 'marnet'
    net['_geometry'] = ('Edge geometry follows the Eurostat SeaRoute MARNET maritime network (via searoute-py, Apache-2.0), '
                        'snapped to Tideline ports and waypoints; edges marked REVIEW keep hand-drawn geometry.')
    NET.write_text(json.dumps(net, indent=1) + '\n')
    print('\n'.join(report))


if __name__ == '__main__':
    main(sys.argv[1])
