#!/usr/bin/env python3
"""Tideline local demo server. Python standard library only. Binds to localhost.

Data provenance:
- Marine-wave nowcasts and hourly forecasts: Open-Meteo Marine API.
- News headlines: GDELT DOC API, fallback Google News RSS.
- Routes/tolls/vessel properties: explicitly illustrative assumptions.

Planning API (optimizer.py, fuel_model.py, risk_model.py, conditions.py):
- GET  /api/fleet        sample vessel profiles and speed options
- GET  /api/network      waypoint network, ports, zones, presets, event types
- GET  /api/fuel-model   which fuel model is active and its learned coefficients
- POST /api/fleet-plan   plan 2-6 ships together with refueling coordination
- GET  /api/ais          live AIS ships (AISStream.io; set AISSTREAM_API_KEY); POST /api/ais/track {mmsi} or {clear:true}
- GET  /api/conditions   today's sourced snapshot, PortWatch traffic and weather status
- GET  /api/weather-status live Open-Meteo field used by the optimizer (refreshed hourly)
- GET  /api/path-weather wave nowcast + arrival-time forecast at plan checkpoints (pts=lat,lon,hours;...)
- POST /api/plan         optimize one voyage (JSON body; see optimizer.normalize_request)
- POST /api/chat         GPT (if OPENAI_API_KEY is set) or offline parser -> planner -> explanation

Operations API (ops.py, sanctions.py, assign.py, calibration.py):
- GET  /api/voyages              approved voyages being monitored; POST /api/voyages approves a plan
- GET  /api/voyages/<id>         one voyage with alerts and the latest re-check
- POST /api/voyages/<id>/action  check | ack | note | noon_report | link_ais | simulate_hours | accept_replan | cancel | complete
- GET  /api/voyages/<id>/orders  draft voyage orders and bunker enquiries (never sent automatically)
- GET  /api/audit                tamper-evident audit log (SHA-256 hash chain) and its verification
- GET  /api/advisories          official NGA broadcast warnings (security) on Tideline risk zones
- GET  /api/sanctions            OFAC SDN list status; ?name=&imo=&mmsi= screens one vessel
- POST /api/assign               which ship takes which cargo job (exact search over assignments)
- POST /api/calibrate            retrain a ship's fuel model on noon reports (CSV); GET /api/calibration status
- GET  /api/calibrate/sample     SYNTHETIC example noon-report file for demos
"""
from __future__ import annotations

import email.utils
import json
import math
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from optimizer import CARGO_SEGMENTS, FLEET_LIST, conditions_payload, fleet_payload, network_payload, plan_voyage
import news_rank
import portwatch
from fuel_model import get_fuel_model
from ai_layer import ai_status, chat
import live_weather
import ais
from fleet import fleet_plan
import ops
import advisories
import sanctions

ROOT = Path(__file__).resolve().parent
ROUTES = json.loads((ROOT / 'routes.json').read_text())
SCENARIOS = {s['id']: s for s in ROUTES['scenarios']}
CACHE = {}
CACHE_LOCK = Lock()
USER_AGENT = 'TidelineHackathonDemo/0.1 (+https://open-meteo.com)'
MAX_BODY_BYTES = 128 * 1024


def get_cached(key, ttl_seconds, supplier, force=False):
    with CACHE_LOCK:
        entry = CACHE.get(key)
        if not force and entry and (datetime.now(timezone.utc) - entry[0]).total_seconds() < ttl_seconds:
            return dict(entry[1], cached=True)
    answer = supplier()
    with CACHE_LOCK:
        CACHE[key] = (datetime.now(timezone.utc), answer)
    return answer


def fetch_json(url, timeout=9):
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urlopen(request, timeout=timeout) as result:
        return json.loads(result.read(1000000).decode('utf-8'))


def fetch_xml(url, timeout=7):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 TidelineDemo/0.1'})
    with urlopen(request, timeout=timeout) as result:
        return ElementTree.fromstring(result.read(700000))


def miles(a,b):
    lat1,lon1,lat2,lon2 = map(math.radians, [a[0],a[1],b[0],b[1]])
    da,do=lat2-lat1,lon2-lon1
    x=math.sin(da/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(do/2)**2
    return 3440.065 * 2*math.atan2(math.sqrt(x),math.sqrt(max(0,1-x)))


def total_miles(points):
    return sum(miles(a,b) for a,b in zip(points[:-1],points[1:]))


def point_along(points, fraction):
    parts = [miles(a,b) for a,b in zip(points[:-1],points[1:])]
    remaining = sum(parts) * fraction
    for i, distance in enumerate(parts):
        if distance >= remaining:
            t = remaining/distance if distance else 0
            return [round(points[i][0] + (points[i+1][0]-points[i][0])*t,4), round(points[i][1] + (points[i+1][1]-points[i][1])*t,4)]
        remaining -= distance
    return points[-1]


def get_weather(scenario_id, speed):
    scenario = SCENARIOS[scenario_id]
    samples = []
    for route in scenario['routes']:
        dist = total_miles(route['waypoints'])
        for fraction in [0.08,0.18,0.30,0.45,0.62,0.78,0.90]:
            lat, lon = point_along(route['waypoints'],fraction)
            samples.append(dict(routeId=route['id'],fraction=fraction,lat=lat,lon=lon,
                arrivalHours=round((dist*fraction)/speed,2)))
    params = urlencode({
        'latitude': ','.join(str(p['lat']) for p in samples),
        'longitude': ','.join(str(p['lon']) for p in samples),
        'current': 'wave_height',
        'hourly': 'wave_height',
        'forecast_days': '7',
        'timezone': 'UTC',
        'cell_selection': 'sea'
    })
    uri = 'https://marine-api.open-meteo.com/v1/marine?' + params
    try:
        payload = fetch_json(uri,timeout=12)
        payload = payload if isinstance(payload,list) else [payload]
        if len(payload) != len(samples):
            raise ValueError('Marine API returned an unexpected number of coordinates')
        for sample, item in zip(samples,payload):
            nowcast = (item.get('current') or {}).get('wave_height')
            sample['nowcastWaveM'] = nowcast if isinstance(nowcast,(int,float)) else None
            hourly = item.get('hourly') or {}
            times, values = hourly.get('time',[]),hourly.get('wave_height',[])
            wanted = datetime.now(timezone.utc) + timedelta(hours=sample['arrivalHours'])
            target = wanted.strftime('%Y-%m-%dT%H:00')
            if times and wanted <= datetime.fromisoformat(times[-1]).replace(tzinfo=timezone.utc):
                index = min(range(len(times)),key=lambda k: abs((datetime.fromisoformat(times[k]).replace(tzinfo=timezone.utc)-wanted).total_seconds()))
                sample['forecastWaveM'] = values[index] if isinstance(values[index],(int,float)) else None
                sample['forecastTimeUtc'] = times[index]+'Z'
            else:
                sample['forecastWaveM'] = None
                sample['forecastTimeUtc'] = None
            del sample['arrivalHours']
        return dict(status='live',source='Open-Meteo marine forecast',retrievedAt=datetime.now(timezone.utc).isoformat(),
                    horizonDays=7, samples=samples, note='Wave heights are modeled estimates, not vessel observations. Beyond 7 days, no weather correction is applied.')
    except Exception as exc:
        return dict(status='unavailable',source='Open-Meteo marine forecast',retrievedAt=datetime.now(timezone.utc).isoformat(),
                    horizonDays=7,samples=[],note='Live marine forecasts are unavailable. Using baseline route estimates.',
                    detail=f'{type(exc).__name__}: {str(exc)[:150]}')


def parse_path_points(raw):
    """pts=lat,lon,hours;lat,lon,hours  (at most 12 points)"""
    samples=[]
    for chunk in (raw or '').split(';')[:12]:
        parts=chunk.split(',')
        if len(parts)!=3:
            raise ValueError('Each point must be lat,lon,hours')
        lat,lon,h=(float(x) for x in parts)
        if not (-90<=lat<=90 and -180<=lon<=180 and 0<=h<=5000) or not all(math.isfinite(v) for v in (lat,lon,h)):
            raise ValueError('Point out of range')
        samples.append(dict(lat=round(lat,3),lon=round(lon,3),arrivalHours=round(h,1)))
    if not samples:
        raise ValueError('No points supplied')
    return samples


def get_path_weather(samples):
    """Open-Meteo wave nowcast at each point, plus the forecast for the hour the ship is due there (7-day horizon)."""
    samples=[dict(s) for s in samples]
    params = urlencode({
        'latitude': ','.join(str(p['lat']) for p in samples),
        'longitude': ','.join(str(p['lon']) for p in samples),
        'current': 'wave_height', 'hourly': 'wave_height', 'forecast_days': '7',
        'timezone': 'UTC', 'cell_selection': 'sea'})
    try:
        payload = fetch_json('https://marine-api.open-meteo.com/v1/marine?' + params, timeout=12)
        payload = payload if isinstance(payload,list) else [payload]
        if len(payload) != len(samples):
            raise ValueError('Marine API returned an unexpected number of coordinates')
        now = datetime.now(timezone.utc)
        for sample, item in zip(samples,payload):
            nowcast = (item.get('current') or {}).get('wave_height')
            sample['nowcastWaveM'] = nowcast if isinstance(nowcast,(int,float)) else None
            hourly = item.get('hourly') or {}
            times, values = hourly.get('time',[]), hourly.get('wave_height',[])
            wanted = now + timedelta(hours=sample['arrivalHours'])
            sample['forecastWaveM'] = None
            sample['forecastTimeUtc'] = None
            if times and wanted <= datetime.fromisoformat(times[-1]).replace(tzinfo=timezone.utc):
                index = min(range(len(times)),key=lambda k: abs((datetime.fromisoformat(times[k]).replace(tzinfo=timezone.utc)-wanted).total_seconds()))
                if isinstance(values[index],(int,float)):
                    sample['forecastWaveM'] = values[index]
                    sample['forecastTimeUtc'] = times[index]+'Z'
        return dict(status='live',source='Open-Meteo marine forecast',retrievedAt=now.isoformat(),horizonDays=7,samples=samples,
                    note='Modeled wave heights at plan checkpoints, not vessel observations. Arrivals beyond 7 days have no forecast.')
    except Exception as exc:
        return dict(status='unavailable',source='Open-Meteo marine forecast',retrievedAt=datetime.now(timezone.utc).isoformat(),
                    horizonDays=7,samples=[],note='Live marine forecasts are unavailable.',detail=f'{type(exc).__name__}: {str(exc)[:150]}')


def news_gdelt(query):
    url = 'https://api.gdeltproject.org/api/v2/doc/doc?' + urlencode({
        'query':query,'mode':'ArtList','format':'json','maxrecords':15,'timespan':'3d','sort':'DateDesc'})
    data=fetch_json(url, timeout=8)
    articles=[]
    for x in data.get('articles',[]):
        if not x.get('title') or not str(x.get('url','')).startswith('https://'):continue
        articles.append(dict(title=str(x['title'])[:190],url=str(x['url']),source=str(x.get('domain',''))[:80],
                             published=str(x.get('seendate',''))[:22]))
    if not articles:raise ValueError('GDELT returned no articles')
    return articles


def news_google(query):
    query=query + ' when:3d'
    url = 'https://news.google.com/rss/search?' + urlencode({'q':query,'hl':'en-US','gl':'US','ceid':'US:en'})
    feed = fetch_xml(url,timeout=8)
    articles=[]
    for item in feed.findall('.//item')[:15]:
        def val(k):return (item.findtext(k) or '').strip()
        date=val('pubDate')
        try: date=email.utils.parsedate_to_datetime(date).astimezone(timezone.utc).isoformat()
        except Exception: pass
        if val('link').startswith('https://'):
            articles.append(dict(title=val('title')[:190],url=val('link'),source='Google News RSS',published=date[:30]))
    if not articles:raise ValueError('RSS returned no articles')
    return articles


def _fetch_articles(query):
    try: return news_gdelt(query),'GDELT DOC 2.0'
    except Exception: return news_google(query),'Google News RSS'


def get_news(cargo=None,regions=None):
    """Headlines for the regions the route passes: general shipping news plus the operator's fleet terms,
    tagged (Your fleet / On your route / General) and ranked by relevance."""
    seg=CARGO_SEGMENTS.get(cargo)
    regions=news_rank.normalise_regions(regions)
    try:
        articles,source=_fetch_articles(news_rank.build_query(regions,'shipping'))
        if seg:
            try:
                extra,_=_fetch_articles(news_rank.build_query(regions,seg['news_terms']))
                seen={a['url'] for a in articles}
                articles=[a for a in extra if a['url'] not in seen]+articles
            except Exception:
                pass  # general headlines still work
        articles=news_rank.tag_and_rank(articles,seg,regions)
        return dict(status='live',source=source,retrievedAt=datetime.now(timezone.utc).isoformat(),articles=articles[:25],
                    cargo=cargo,cargoLabel=seg['label'] if seg else None,regions=list(regions),
                    note='Unverified press signals only. Never treat news keywords as official closure alerts.')
    except Exception as exc:
        return dict(status='unavailable',source='GDELT / Google News RSS',retrievedAt=datetime.now(timezone.utc).isoformat(),
                    articles=[],cargo=cargo,note='Unable to fetch current headlines. No geopolitical facts are simulated.',
                    detail=f'{type(exc).__name__}: {str(exc)[:150]}')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control','no-cache')   # always revalidate the page and app.js after an update
        super().end_headers()
    def json_response(self, answer, code=200):
        data=json.dumps(answer,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        parsed=urlparse(self.path)
        if parsed.path=='/api/status':
            a=ais.TRACKER.snapshot()
            return self.json_response({'ai':ai_status(),'weather':live_weather.current_status(),'portwatch':portwatch.status(),
                'sanctions':sanctions.status(),'advisories':{k:advisories.current()[k] for k in ('status','retrieved_at','error')}|{'warnings':len(advisories.current()['warnings'])},
                'ais':{k:a.get(k) for k in ('status','error','messages','mode')}|{'ships':len(a['ships'])},
                'monitor':{'minutes':ops.MONITOR_MINUTES,'running':bool(ops._thread and ops._thread.is_alive())},'audit':ops.verify_audit()})
        if parsed.path=='/api/health':
            return self.json_response({'status':'ok','host':'localhost','version':'0.2','ai':ai_status()})
        if parsed.path=='/api/weather':
            q=parse_qs(parsed.query)
            scenario=q.get('scenario',['singapore'])[0]
            if scenario not in SCENARIOS:return self.json_response({'error':'Unknown scenario'},400)
            try:speed=min(24,max(12,float(q.get('speed',['18'])[0])))
            except (TypeError,ValueError):return self.json_response({'error':'Invalid speed'},400)
            speed=round(speed,1)
            # Cache is per scenario and speed so arrival-matched forecasts remain consistent.
            return self.json_response(get_cached('weather/'+scenario+'/'+str(speed),900,lambda:get_weather(scenario,speed),force=q.get('refresh',['0'])[0]=='1'))
        if parsed.path=='/api/news':
            q=parse_qs(parsed.query)
            cargo=q.get('cargo',[''])[0]
            if cargo and cargo not in CARGO_SEGMENTS:return self.json_response({'error':'Unknown cargo'},400)
            regions=news_rank.normalise_regions([r for r in q.get('regions',[''])[0].split(',') if r])
            return self.json_response(get_cached(news_rank.cache_key(cargo,regions),900,lambda:get_news(cargo or None,regions),force=q.get('refresh',['0'])[0]=='1'))
        if parsed.path=='/api/path-weather':
            q=parse_qs(parsed.query)
            try: samples=parse_path_points(q.get('pts',[''])[0])
            except ValueError as exc: return self.json_response({'error':str(exc)},400)
            key='path/'+';'.join(f"{p['lat']},{p['lon']},{round(p['arrivalHours']/3)}" for p in samples)
            return self.json_response(get_cached(key,900,lambda:get_path_weather(samples),force=q.get('refresh',['0'])[0]=='1'))
        if parsed.path=='/api/voyages':
            return self.json_response(ops.list_voyages())
        if parsed.path.startswith('/api/voyages/'):
            parts=parsed.path.split('/')[3:]
            try:
                if len(parts)==1: return self.json_response(ops.get_voyage(parts[0]))
                if len(parts)==2 and parts[1]=='orders': return self.json_response(ops.orders(parts[0]))
            except KeyError:
                return self.json_response({'error':'Unknown voyage'},404)
            return self.json_response({'error':'Not found'},404)
        if parsed.path=='/api/advisories':
            return self.json_response(advisories.current())
        if parsed.path=='/api/audit':
            q=parse_qs(parsed.query)
            return self.json_response(ops.audit_log(voyage_id=q.get('voyage',[None])[0]))
        if parsed.path=='/api/sanctions':
            q=parse_qs(parsed.query)
            one=lambda k:(q.get(k,[''])[0].strip() or None)
            if any(one(k) for k in ('name','imo','mmsi')):
                return self.json_response(sanctions.screen_vessel(one('name'),one('imo'),one('mmsi')))
            return self.json_response(sanctions.status())
        if parsed.path=='/api/calibration':
            import calibration
            return self.json_response(calibration.status())
        if parsed.path=='/api/calibrate/sample':
            import calibration
            q=parse_qs(parsed.query)
            try: body=calibration.sample_csv(q.get('ship_id',['aurora'])[0]).encode()
            except ValueError as exc: return self.json_response({'error':str(exc)},400)
            self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8')
            self.send_header('Content-Disposition','attachment; filename="sample-noon-reports-SYNTHETIC.csv"')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if parsed.path=='/api/ais':
            return self.json_response(ais.TRACKER.snapshot())
        if parsed.path=='/api/conditions':
            return self.json_response(conditions_payload())
        if parsed.path=='/api/weather-status':
            return self.json_response(live_weather.current_status())
        if parsed.path=='/api/fleet':
            return self.json_response(fleet_payload())
        if parsed.path=='/api/network':
            return self.json_response(network_payload())
        if parsed.path=='/api/fuel-model':
            return self.json_response(get_fuel_model(FLEET_LIST).info())
        if parsed.path=='/':self.path='/index.html'
        return super().do_GET()
    def do_POST(self):
        parsed=urlparse(self.path)
        action=parsed.path.split('/')[3] if parsed.path.startswith('/api/voyages/') and parsed.path.endswith('/action') and parsed.path.count('/')==4 else None
        if parsed.path not in ('/api/plan','/api/chat','/api/fleet-plan','/api/ais/track','/api/voyages','/api/assign','/api/calibrate') and not action:
            return self.json_response({'error':'Not found'},404)
        try:
            length=int(self.headers.get('Content-Length','0'))
        except ValueError:
            return self.json_response({'error':'Invalid Content-Length'},400)
        if length<=0 or length>MAX_BODY_BYTES:
            return self.json_response({'error':f'Body must be 1 to {MAX_BODY_BYTES} bytes of JSON'},400)
        try:
            request=json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError,json.JSONDecodeError):
            return self.json_response({'error':'Body is not valid JSON'},400)
        try:
            if parsed.path=='/api/ais/track':
                if not isinstance(request,dict):raise ValueError('Body must be a JSON object')
                if request.get('clear'):ais.TRACKER.untrack_all()
                else:ais.TRACKER.track(request.get('mmsi',''))
                return self.json_response({'tracked':sorted(ais.TRACKER.tracked),'status':ais.TRACKER.status})
            if parsed.path=='/api/voyages':
                return self.json_response(ops.approve(request))
            if action:
                try: return self.json_response(ops.act(action,request))
                except KeyError: return self.json_response({'error':'Unknown voyage'},404)
            if parsed.path=='/api/assign':
                import assign
                return self.json_response(assign.assign(request))
            if parsed.path=='/api/calibrate':
                import calibration
                return self.json_response(calibration.calibrate(request))
            if parsed.path=='/api/fleet-plan':
                return self.json_response(fleet_plan(request))
            return self.json_response(chat(request) if parsed.path=='/api/chat' else plan_voyage(request))
        except ValueError as exc:
            return self.json_response({'error':str(exc)},400)
        except Exception as exc:
            return self.json_response({'error':'Planner failed','detail':f'{type(exc).__name__}: {str(exc)[:200]}'},500)


if __name__ == '__main__':
    port=int(os.getenv('PORT','3001'))
    host=os.getenv('HOST','0.0.0.0')
    get_fuel_model(FLEET_LIST)  # train the fuel model once at startup (simulated data, about 0.1 s)
    import calibration
    n_cal=calibration.load_saved()   # ships calibrated on noon reports earlier keep their models
    if os.getenv('TIDELINE_LIVE_WEATHER','1')!='0':
        live_weather.start_background_refresh()  # Open-Meteo waves, wind, currents for the optimizer
    if os.getenv('TIDELINE_PORTWATCH','1')!='0':
        portwatch.start_background_refresh()     # IMF PortWatch chokepoint transits for the risk model
    httpd=ThreadingHTTPServer((host,port),Handler)
    ais.start()   # live ships, only if AISSTREAM_API_KEY is set
    if os.getenv('TIDELINE_SANCTIONS','1')!='0':
        sanctions.start_background_refresh()   # OFAC SDN vessel list, daily
    if os.getenv('TIDELINE_ADVISORIES','1')!='0':
        advisories.start_background_refresh()   # official NGA navigational warnings, every 3 h
    ops.start_monitor()   # re-checks approved voyages every TIDELINE_MONITOR_MIN minutes (default 15)
    ai=ai_status()
    print(f'Tideline local demo: http://{host}:{port}  (Ctrl+C to stop)',flush=True)
    print(f"Live AIS: {'AISStream.io' if os.getenv('AISSTREAM_API_KEY') else 'off (set AISSTREAM_API_KEY for live ships)'}",flush=True)
    print(f"Fuel model: {n_cal} ship(s) calibrated on noon reports" if n_cal else "Fuel model: trained on simulated data (calibrate with noon reports in Advanced)",flush=True)
    print(f"Voyage monitoring: every {ops.MONITOR_MINUTES:g} min · records in {ops._dir()}",flush=True)
    print(f"AI chat: {'GPT via OpenAI, model ' + ai['model'] if ai['mode']=='gpt' else 'offline parser (set OPENAI_API_KEY to use GPT)'}",flush=True)
    httpd.serve_forever()
