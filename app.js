'use strict';

// Tideline dashboard. Every number shown comes from the backend planner (/api/plan).
// The browser only collects inputs, turns scenario toggles into events, and draws results.
const NS = 'http://www.w3.org/2000/svg';
const $ = id => document.getElementById(id);
const W = 1200, H = 650;
const LANES = [
  ['SIN', 'RTM', 'Singapore → Rotterdam'],
  ['SHA', 'RTM', 'Shanghai → Rotterdam'],
  ['JEA', 'RTM', 'Jebel Ali → Rotterdam'],
  ['SHA', 'LPA', 'Shanghai → Las Palmas'],
];
const TOGGLES = ['blockBab', 'threatBab', 'coverBab', 'blockSuez', 'blockHormuz', 'closeWestAfrica', 'stormCape', 'liveWeather', 'autoNews', 'allowUninsured'];
const state = {
  fleet: null, network: null, ops: {voyages: [], sel: null, meta: null, busy: null, error: null},
  sim: {playing: false, speed: 24, speedNow: 24, offsetH: 0, auto: true},   // speeds are simulated hours per real second
  ship: 'aurora', origin: 'SIN', destination: 'RTM', priority: 'balanced', conditions: 'today', today: null,
  cargo: 'retail', weights: null,   // weights = null → use the preset's weights
  fuel: 2500, deadline: '', deadlineType: 'hard', berth: '',
  toggles: {blockBab: false, threatBab: false, coverBab: false, allowUninsured: false, blockSuez: false, blockHormuz: false, closeWestAfrica: false, stormCape: false, liveWeather: true, autoNews: true},
  plan: null, planStatus: 'idle', planError: null, preview: null, reqVersion: 0,
  weather: {status: 'idle', samples: []}, weatherKey: null, weatherVersion: 0,
  news: {status: 'loading', articles: []},
  mapView: {x: 0, y: 0, w: W, h: H},
  extraEvents: [],            // events added from chat that have no toggle
  committed: null, progressDay: 0,   // mid-voyage: the plan the ship is following and how far along it is
  mode: 'single', fleetPlan: null, fleetSel: null, fleetShips: {},
  assign: {jobs: [{origin: 'SHA', destination: 'RTM', cargo: 'retail', deadline: ''}, {origin: 'SIN', destination: 'DUR', cargo: 'food', deadline: ''}, {origin: 'JEA', destination: 'SIN', cargo: 'crude', deadline: ''}], at: {}, result: null, busy: false, error: null},   // fleet planning (state.fleet = ship profiles)
  affiliations: [], startAt: null,                                // compliance links; plan from a live AIS position
  ais: {on: false, data: null, fetchedAt: 0, sel: null, timer: null, anim: null},
  chat: {history: [], busy: false},
};
const SAMPLE_REPORT = 'Reuters, 16 Oct: Missile attack on a tanker near Bab el-Mandeb overnight; insurers raise war-risk premiums for Red Sea transits. Separately, Durban port reports congestion with 72 hour waits for bunkering, and a severe storm is forecast off the Cape of Good Hope with 8 m seas. Officials say the Suez Canal remains open to traffic.';

// ------------------------------------------------------------------ helpers
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fUSD = n => (n < 0 ? '−$' : '$') + Math.abs(Math.round(n)).toLocaleString('en-US');
const fCompact = n => {
  if (!Number.isFinite(n)) return '—';
  const a = Math.abs(n), sign = n < 0 ? '−' : '';
  return sign + '$' + (a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : (a / 1e3).toFixed(0) + 'k');
};
const fSigned = n => (n >= 0 ? '+' : '−') + fCompact(Math.abs(n));
const fT = n => Math.round(n).toLocaleString('en-US') + ' t';
const svgEl = (tag, attrs = {}) => { const el = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v)); return el; };
const addTitle = (el, text) => { const t = svgEl('title'); t.textContent = text; el.appendChild(t); return el; };
// ---- map engine. With Leaflet loaded, the live map is the same dark Esri basemap the Streamlit Tideline uses, pannable and zoomable
// anywhere in the world; our SVG layers sit in a Leaflet pane on top and are redrawn when the view settles. Without Leaflet
// (offline, or the script blocked) the old regional SVG map is used, with its own pan and zoom.
const DARK_TILES = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}';
const DARK_ATTR = 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ';
let lm = null;   // {map, host, svg, frame, origin, lonShift, fitted} once the Leaflet map is running
const project = ([lat, lon]) => lm ? lmProject(lat, lon) : [((lon + 31) / 170) * W, ((69 - lat) / 122) * H];
function lmProject(lat, lon) {   // layer points do not change while panning, so ships and routes stay put as the pane moves
  const p = lm.map.latLngToLayerPoint([lat, lon + lm.lonShift]).subtract(lm.origin);
  return [p.x / TLMapLayer.UNIT, p.y / TLMapLayer.UNIT];
}
const pathString = points => TLMapLayer.unwrapLon(points).map((p, i) => `${i ? 'L' : 'M'}${project(p).map(x => x.toFixed(1)).join(',')}`).join(' ');
const markerK = () => lm ? 1 : TLMapView.markerScale(state.mapView, W);   // on the Leaflet map symbols keep one size on screen
const mapZoomLevel = () => lm ? TLMapLayer.zoomLevelOf(lm.map.getZoom()) : PB.zoomLevel(state.mapView.w, W);
function initLeafletMap() {
  if (lm || !window.L) return false;
  const box = $('mapContainer'), svg = $('worldMap'), el = document.createElement('div');
  el.id = 'leafletMap'; box.insertBefore(el, box.firstChild);
  const map = L.map(el, {zoomControl: false, minZoom: 1.5, maxZoom: 12, zoomSnap: 0.5, zoomDelta: 1, wheelPxPerZoomLevel: 100, worldCopyJump: true,
    zoomAnimation: false, fadeAnimation: false, markerZoomAnimation: false});
  L.tileLayer(DARK_TILES, {attribution: DARK_ATTR, maxZoom: 12}).addTo(map);
  L.control.scale({position: 'bottomright'}).addTo(map);
  const pane = map.createPane('tlPane'), host = document.createElement('div');
  pane.style.zIndex = 450; host.id = 'tlHost'; pane.appendChild(host); host.appendChild(svg);
  box.classList.add('lf');
  lm = {map, host, svg, frame: TLMapLayer.overlayFrame(1, 1), origin: L.point(0, 0), lonShift: 0, fitted: false};
  let queued = false;
  map.on('moveend zoomend resize', () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; syncOverlay(); }); } });
  map.setView([25, 20], 2, {animate: false});
  syncOverlay();
  return true;
}
function syncOverlay() {   // place the overlay over the visible map (plus a margin) and redraw everything in its units
  if (!lm) return;
  const map = lm.map, size = map.getSize();
  if (!lm.fitted && size.x > 50) { lm.fitted = true; map.fitBounds(TLMapLayer.HOME_BOUNDS, {animate: false}); return; }   // fires moveend, which syncs again
  const f = lm.frame = TLMapLayer.overlayFrame(size.x, size.y);
  lm.lonShift = TLMapLayer.lonShiftFor(map.getCenter().lng);   // draw in the world copy nearest the view
  lm.origin = map.containerPointToLayerPoint([-f.padX, -f.padY]);
  L.DomUtil.setPosition(lm.host, lm.origin);
  lm.svg.setAttribute('width', f.width); lm.svg.setAttribute('height', f.height);
  lm.svg.setAttribute('viewBox', f.viewBox); lm.svg.setAttribute('preserveAspectRatio', 'none');
  redrawMapLayers();
}
function redrawMapLayers() {
  if (!state.network) return;
  drawNetwork(); drawMap(); drawVessels();
  if (state.ais.on) drawAis();
  drawWarnMarkers();
  if (document.body.dataset.page === 'operations') drawOps();
}
const fitHome = () => lm && lm.map.fitBounds(TLMapLayer.HOME_BOUNDS, {animate: false});
const haversine = (a, b) => {
  const r = Math.PI / 180, [a1, o1] = a, [a2, o2] = b;
  const v = Math.sin((a2 - a1) * r / 2) ** 2 + Math.cos(a1 * r) * Math.cos(a2 * r) * Math.sin((o2 - o1) * r / 2) ** 2;
  return 3440.065 * 2 * Math.atan2(Math.sqrt(v), Math.sqrt(Math.max(0, 1 - v)));
};
function interpolate(points, fraction) {
  const seg = points.slice(1).map((p, i) => haversine(points[i], p));
  let travel = seg.reduce((a, b) => a + b, 0) * fraction;
  for (let i = 0; i < seg.length; i++) {
    if (travel <= seg[i]) { const t = seg[i] ? travel / seg[i] : 0; return [points[i][0] + t * (points[i + 1][0] - points[i][0]), points[i][1] + t * (points[i + 1][1] - points[i][1])]; }
    travel -= seg[i];
  }
  return points[points.length - 1];
}
const fmtEta = iso => { const d = new Date(iso); return Number.isNaN(d.valueOf()) ? '—' : d.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'}); };
function formatDate(date) { let s = String(date ?? ''); if (/^\d{8}T\d{6}Z?$/.test(s)) { s = s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8) + 'T' + s.slice(9, 11) + ':' + s.slice(11, 13) + ':' + s.slice(13, 15) + 'Z'; } const d = new Date(s); return Number.isNaN(d.valueOf()) ? 'date not provided' : d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}); }
const portName = code => state.network?.ports?.[code]?.name || code;
const zoneLabel = z => state.network?.zones?.[z]?.label || z;
const shipObj = () => state.fleet?.ships.find(s => s.id === state.ship);
const plansOf = p => p && p.recommended ? [p.recommended, ...p.alternatives] : [];
const activePlan = () => { const all = plansOf(state.plan); return all.find(x => x.corridor === state.preview) || all[0] || null; };

// ------------------------------------------------------------------ events from toggles and news
function buildEvents() {
  const t = state.toggles, ev = [], src = 'Tideline scenario control (simulated)', ts = new Date().toISOString();
  if (t.blockBab) ev.push({type: 'zone_closure', zone: 'bab', id: 'red-sea-closed', source: src, timestamp: ts, description: 'Red Sea / Bab el-Mandeb closed'});
  if (t.threatBab) ev.push({type: 'security', zone: 'bab', level: 0.3, id: 'red-sea-threat', source: src, timestamp: ts, description: 'Red Sea threat level raised'});
  if (t.coverBab) ev.push({type: 'cover_cancelled', zone: 'bab', id: 'red-sea-cover-cancelled', source: src, timestamp: ts, description: 'War-risk cover cancelled for the Red Sea'});
  if (t.blockSuez) ev.push({type: 'zone_closure', zone: 'suez', id: 'suez-closed', source: src, timestamp: ts, description: 'Suez Canal closed'});
  if (t.blockHormuz) ev.push({type: 'zone_closure', zone: 'hormuz', id: 'hormuz-closed', source: src, timestamp: ts, description: 'Strait of Hormuz closed'});
  if (t.closeWestAfrica) {
    ev.push({type: 'port_closure', port: 'DKR', id: 'dakar-closed', source: src, timestamp: ts, description: 'Port of Dakar closed'});
    ev.push({type: 'port_closure', port: 'ABJ', id: 'abidjan-closed', source: src, timestamp: ts, description: 'Port of Abidjan closed'});
  }
  if (t.stormCape) ev.push({type: 'storm', id: 'cape-storm', center: [-35, 22], radius_nm: 300, hs_m: 7.5, wind_ms: 25, start_h: 0, end_h: 2000, source: src, timestamp: ts, description: 'Severe storm off the Cape (7.5 m waves)'});
  for (const e of state.extraEvents) ev.push(e);
  if (t.autoNews && state.news.status === 'live') {
    for (const zone of ['hormuz', 'bab', 'suez']) {
      const matched = (state.news.articles || []).filter(a => a.potentialDisruption && a.topic === zone);
      const relevant = matched.filter(a => a.relevant).length;
      const hits = matched.length + relevant;   // headlines about your cargo segment count double
      if (hits) ev.push({type: 'news', zone, hits, id: 'news-' + zone, source: (state.news.source || 'Press feed') + ' headlines (unverified)', timestamp: state.news.retrievedAt,
        description: `${matched.length} unverified disruption headline${matched.length > 1 ? 's' : ''} about ${zoneLabel(zone)}${relevant ? ` (${relevant} about ${state.news.cargoLabel || 'your cargo'}, counted double)` : ''}`});
    }
  }
  return ev;
}

// ------------------------------------------------------------------ planning requests
let planTimer = null;
function schedulePlan(delay = 120) { clearTimeout(planTimer); planTimer = setTimeout(requestPlan, delay); }
function currentRequest() {
  const body = {ship_id: state.ship, origin: state.origin, destination: state.destination, priority: state.priority,
    fuel_onboard_t: state.fuel, live_weather: state.toggles.liveWeather, conditions: state.conditions, cargo: state.cargo, events: buildEvents()};
  if (state.weights) body.weights = {...state.weights};
  if (state.deadline !== '' && Number(state.deadline) > 0) { body.deadline_days = Number(state.deadline); body.deadline_type = state.deadlineType; }
  if (state.affiliations.length) body.affiliations = [...state.affiliations];
  if (state.toggles.allowUninsured) body.allow_uninsured = true;
  if (state.berth !== '' && Number(state.berth) > 0) body.berth_ready_days = Number(state.berth);
  const pos = shipState();
  if (pos) { body.resume = pos.resume; body.fuel_onboard_t = pos.resume.fuel_onboard_t; }
  else if (state.startAt) body.start_at = {...state.startAt};
  return body;
}
async function requestPlan() {
  if (state.mode === 'fleet') return requestFleet();
  const version = ++state.reqVersion;
  state.planStatus = 'loading'; state.planStartedAt = Date.now(); renderStatus();
  const body = currentRequest();
  renderExtraEvents();
  try {
    const res = await fetch('/api/plan', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), cache: 'no-store'});
    const json = await res.json();
    if (version !== state.reqVersion) return;
    if (!res.ok) throw Error(json.error || `HTTP ${res.status}`);
    state.plan = json; state.planStatus = 'ok'; state.planError = null; state.preview = null;
  } catch (e) {
    if (version !== state.reqVersion) return;
    state.planStatus = 'error'; state.planError = e.message;
  }
  render();
  pullWeather();
  refreshNewsForRoute();
}

// ------------------------------------------------------------------ rendering
function render() {
  if (state.mode === 'fleet' && state.fleetPlan) {
    const sel = state.fleetPlan.ships.find(s => s.ship_id === state.fleetSel) || state.fleetPlan.ships[0];
    state.plan = sel ? sel.result : null;
  }
  renderFleet();
  renderStatus();
  renderMetrics();
  renderRoutes();
  renderAlerts();
  renderPlanPanel();
  drawMap();
  renderWeatherSummary();
  renderSources();
  syncProgress();
  syncDemoBar();
  if (state.ops) renderOps();
  renderOverview();
  updateWarnings();
}

function renderStatus() {
  const el = $('planStatus');
  const map = {loading: ['◌ Optimizing…', '#b45309'], ok: ['● Plan ready', '#0f766e'], error: ['△ Planner error', '#dc2626'], idle: ['◌ Planning', '#64748b']};
  const [text, color] = map[state.planStatus] || map.idle;
  const loading = state.planStatus === 'loading', secs = loading && state.planStartedAt ? Math.round((Date.now() - state.planStartedAt) / 1000) : 0;
  el.textContent = loading && secs >= 6 ? `${text} ${secs}s` : text; el.style.color = color;
  if (loading) $('metricRouteSub').innerHTML = '<span class="spinner"></span>Optimizing route, speed and refueling…'
    + (secs >= 10 ? ` <small>${secs}s so far. A busy or slow server can take a minute; asking for the same thing again is instant.</small>` : '');
  // while a plan is running, refresh the elapsed time every couple of seconds
  if (loading && !state.planTicker) state.planTicker = setInterval(renderStatus, 2000);
  else if (!loading && state.planTicker) { clearInterval(state.planTicker); state.planTicker = null; }
}

function renderMetrics() {
  const p = state.plan, a = activePlan(), best = p?.recommended;
  const ship = shipObj();
  $('mapSubtitle').textContent = `${portName(state.origin)} → ${portName(state.destination)} · ${ship ? ship.name : ''} · ${(p?.preset?.label || state.priority)} objective`;
  if (state.planStatus === 'error') {
    $('metricRoute').textContent = 'Planner error'; $('metricRouteSub').textContent = state.planError || '';
    for (const id of ['metricEta', 'metricCost', 'metricSavings']) $(id).textContent = '—';
    return;
  }
  if (!p) return;
  if (!a) {
    $('metricRoute').textContent = 'No feasible plan';
    $('metricRouteSub').textContent = p.infeasible?.kind === 'closure' ? 'Every sea route is blocked' : 'See the explanation below';
    $('metricEta').textContent = p.infeasible?.fastest_days ? p.infeasible.fastest_days + ' days min' : 'Blocked';
    $('metricEtaSub').textContent = p.infeasible?.fastest_days ? 'Fastest feasible, beyond your deadline' : 'No sea-only voyage meets the constraints';
    $('metricCost').textContent = '—'; $('metricCostSub').textContent = 'No feasible voyage quotation';
    $('metricSavings').textContent = '—'; $('metricSavingsSub').textContent = 'No comparable plan';
    return;
  }
  const isPreview = best && a.corridor !== best.corridor;
  $('metricRoute').textContent = a.corridor_label.replace('Via ', '');
  $('metricRouteSub').textContent = isPreview ? 'Manual preview · optimizer prefers ' + best.corridor_label : (a.zones.map(zoneLabel).join(' · ') || 'Open-ocean route');
  $('metricEta').textContent = a.summary.days.toFixed(1) + ' days';
  const u = a === p.recommended ? p.uncertainty : null;
  $('metricEtaSub').textContent = u ? `ETA ${fmtEta(a.summary.eta)} UTC · 80% range ${u.days.p10.toFixed(1)}–${u.days.p90.toFixed(1)} days` : `ETA ${fmtEta(a.summary.eta)} UTC · ${a.summary.distance_nm.toLocaleString()} nm`;
  $('metricCost').textContent = fCompact(a.costs.cash_total_usd);
  $('metricCostSub').textContent = `${fT(a.summary.fuel_burned_t)} fuel burned · ${fCompact(a.costs.canal_tolls_usd)} canal`;
  const rs = p.resume;
  if (rs) {
    $('metricEtaSub').textContent = `ETA ${fmtEta(a.summary.eta)} UTC · ${a.summary.remaining_days.toFixed(1)} days to go from day ${rs.elapsed_days}`;
    $('metricCostSub').textContent = `Remaining voyage from the ship's position · ${fT(a.summary.fuel_burned_t)} fuel`;
    if (rs.recommendation === 'change_course') {
      $('metricSavings').textContent = rs.saving_score_usd == null ? 'Divert' : fCompact(rs.saving_score_usd);
      $('metricSavingsSub').textContent = rs.saving_score_usd == null ? 'Committed route blocked: change course now' : 'better than staying on course (decision score)';
      $('metricSavings').style.color = '#d97706';
    } else {
      $('metricSavings').textContent = 'Stay';
      $('metricSavingsSub').textContent = 'on the committed route' + (rs.saving_score_usd > 0 ? ` (other route only ${fCompact(rs.saving_score_usd)} better)` : '');
      $('metricSavings').style.color = '#0d9488';
    }
    return;
  }
  // Decision advantage: versus the original plan when an event changed it, else versus the next corridor.
  const ch = p.change;
  if (ch && ch.route_changed && !isPreview) {
    $('metricSavings').textContent = fSigned(ch.extra_cash_usd);
    $('metricSavingsSub').textContent = `${ch.extra_days >= 0 ? '+' : '−'}${Math.abs(ch.extra_days).toFixed(1)} days vs the original ${p.baseline.corridor_label.replace('Via ', '')} plan`;
    $('metricSavings').style.color = ch.extra_cash_usd > 0 ? '#dc2626' : '';
    return;
  }
  $('metricSavings').style.color = '';
  const others = plansOf(p).filter(x => x.corridor !== a.corridor);
  if (!others.length) { $('metricSavings').textContent = '—'; $('metricSavingsSub').textContent = 'No other feasible corridor'; return; }
  const o = others[0], diff = o.costs.cash_total_usd - a.costs.cash_total_usd;
  if (diff >= 0) {
    $('metricSavings').textContent = fCompact(diff);
    $('metricSavingsSub').textContent = `cheaper than ${o.corridor_label.replace('Via ', '')}, ${Math.abs(o.summary.days - a.summary.days).toFixed(1)} days ${o.summary.days > a.summary.days ? 'faster' : 'slower'}`;
  } else {
    $('metricSavings').textContent = fSigned(-diff);
    $('metricSavingsSub').textContent = `costs more than ${o.corridor_label.replace('Via ', '')}, but wins on ${a.summary.route_risk < o.summary.route_risk ? 'risk' : 'time and risk'}`;
  }
}

function routeCard(r, i, {chosen, best, original = false, reason = '', committed = false}) {
  const speeds = r.summary.speeds_kn.length ? (r.summary.speeds_kn.length > 1 ? `${r.summary.speeds_kn[0]}–${r.summary.speeds_kn.at(-1)} kn` : r.summary.speeds_kn[0] + ' kn') : '';
  const refuel = r.bunkering.length ? 'Refuel: ' + r.bunkering.map(b => b.name).join(', ') : 'No refuel stop';
  const risk = Math.round(r.summary.route_risk * 100);
  return `<div class="route-choice ${chosen ? 'chosen' : ''} ${original ? 'original' : ''} ${committed ? 'committed-card' : ''}" ${original ? '' : `role="button" tabindex="0" data-corridor="${esc(r.corridor)}" aria-label="Preview ${esc(r.corridor_label)}"`}>
    <span class="route-number">0${i + 1}</span><div class="route-meta"><div class="route-top"><span class="route-name">${esc(r.corridor_label)}</span>${best ? '<span class="recommended">Optimized choice</span>' : ''}${original ? `<span class="tag">${committed ? 'Committed route' : 'Original plan'}</span>` : ''}</div>
    <div class="route-secondary">${esc(original ? reason : [speeds, refuel].join(' · '))}</div>
    <div class="route-stats"><span><small>Transit</small><b>${r.summary.days.toFixed(1)} days</b></span><span><small>Cash cost</small><b>${fCompact(r.costs.cash_total_usd)}</b></span><span><small>Risk score</small><b style="color:${risk >= 60 ? '#dc2626' : '#0f1f33'}">${risk}/100</b></span><span><small>Distance</small><b>${r.summary.distance_nm.toLocaleString()} nm</b></span><span><small>CO₂</small><b>${fT(r.summary.co2_t)}</b></span></div></div>
    <div class="route-status">${committed ? '◌ STAY' : original ? '✕ REPLACED' : chosen ? '● VIEWING' : '↗'}</div></div>`;
}

function renderRoutes() {
  const p = state.plan, list = $('routesList');
  if (!p) { list.innerHTML = '<div class="empty-feed">Waiting for the first plan…</div>'; return; }
  const plans = plansOf(p), a = activePlan();
  let html = plans.map((r, i) => routeCard(r, i, {chosen: a && r.corridor === a.corridor, best: i === 0})).join('');
  if ((p.change?.route_changed || !p.recommended) && p.baseline && !plans.some(x => x.corridor === p.baseline.corridor)) {
    const causes = (p.events || []).filter(e => e.impact === 'affects this voyage').map(e => e.description || e.type).join('; ');
    html += routeCard(p.baseline, plans.length, {original: true, reason: 'Ruled out or outranked after: ' + (causes || 'new events')});
  }
  const cont = p.resume?.continue;
  if (p.resume && p.resume.recommendation === 'change_course') {
    html += cont ? routeCard(cont, plans.length, {original: true, reason: 'Stay on course: the committed route from the ship\'s position', committed: true})
                 : `<div class="route-choice original committed-card"><span class="route-number">—</span><div class="route-meta"><div class="route-top"><span class="route-name">Stay on course</span><span class="tag">Committed route</span></div><div class="route-secondary">${esc(p.resume.reason)}</div></div><div class="route-status">✕ BLOCKED</div></div>`;
  }
  if (!plans.length && !(p.change && p.baseline)) html = '<div class="empty-feed">No feasible plan under the current constraints.</div>' + html;
  list.innerHTML = html;
  $('routeCount').textContent = `${plans.length} feasible corridor${plans.length === 1 ? '' : 's'} · ${p.search.plans_evaluated} full plans evaluated`;
  renderTradeoff();
  list.querySelectorAll('.route-choice[data-corridor]').forEach(el => {
    const f = () => { state.preview = el.dataset.corridor; render(); pullWeather(); };
    el.addEventListener('click', f);
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); f(); } });
  });
}

function renderAlerts() {
  const p = state.plan, out = [];
  if (state.planStatus === 'error') out.push({critical: true, title: 'Planner error', message: state.planError || 'Unknown error'});
  if (p) {
    const best = p.recommended, ch = p.change;
    if (!best && p.infeasible) out.push({critical: true, title: 'No feasible maritime plan', message: p.infeasible.message});
    const rs = p.resume;
    if (rs && best) {
      const causes = (p.events || []).filter(e => e.impact === 'affects this voyage').map(e => `${e.description || e.type} (source: ${e.source})`).join('; ');
      if (rs.recommendation === 'change_course') {
        const c = rs.continue;
        out.push({critical: !c, title: `Change course now · day ${rs.elapsed_days}, ${fT(rs.fuel_onboard_t)} on board`,
          message: `${rs.reason} New plan: ${best.corridor_label}, arriving ${fmtEta(best.summary.eta)} UTC (${best.summary.remaining_days.toFixed(1)} more days, ${fCompact(best.costs.cash_total_usd)} remaining cost).` +
                   (c ? ` Staying on course: ${c.summary.remaining_days.toFixed(1)} more days, ${fCompact(c.costs.cash_total_usd)}.` : '') + (causes ? ` Triggered by: ${causes}.` : '')});
      } else {
        out.push({critical: false, good: true, title: `Stay on course · day ${rs.elapsed_days}, ${fT(rs.fuel_onboard_t)} on board`,
          message: `${rs.reason} Arrival ${fmtEta(best.summary.eta)} UTC, ${best.summary.remaining_days.toFixed(1)} more days, ${fCompact(best.costs.cash_total_usd)} remaining cost.` + (causes ? ` Considered: ${causes}.` : '')});
      }
    }
    if (ch && best) {
      const causes = (p.events || []).filter(e => e.impact === 'affects this voyage');
      const why = causes.length ? causes.map(e => `${e.description || e.type} (source: ${e.source})`).join('; ') : 'updated inputs';
      if (ch.route_changed) {
        out.push({critical: false, title: 'Plan changed', message: `Original plan ${p.baseline.corridor_label}: ${p.baseline.summary.days.toFixed(1)} days, ${fCompact(p.baseline.costs.cash_total_usd)}. Updated plan ${best.corridor_label}: ${ch.extra_days >= 0 ? '+' : '−'}${Math.abs(ch.extra_days).toFixed(1)} days, ${fSigned(ch.extra_cash_usd)}, ${ch.extra_fuel_t >= 0 ? '+' : '−'}${fT(Math.abs(ch.extra_fuel_t))} fuel. Caused by: ${why}.`});
      } else if (causes.length) {
        out.push({critical: false, title: 'Route holds', message: `The best route is unchanged (${best.corridor_label}). Cost change vs original: ${fSigned(ch.extra_cash_usd)}. Considered: ${why}.`});
      }
    }
    const altOnly = (p.events || []).filter(e => e.impact === 'affects an alternative route');
    if (altOnly.length) out.push({critical: false, title: 'Affects an alternative only', message: altOnly.map(e => e.description || e.type).join('; ') + '. These change or remove another corridor, not the recommended plan.'});
    const none = (p.events || []).filter(e => String(e.impact).startsWith('no impact'));
    if (none.length) out.push({critical: false, title: 'No impact on this voyage', message: none.map(e => e.description || e.type).join('; ') + '. Tideline checked these against every route considered and they do not touch this voyage.'});
    if ((p.events || []).some(e => e.type === 'news')) out.push({critical: false, title: 'Unverified risk signals', message: 'Headline keyword matches nudge the risk score (capped). They are never treated as closures.'});
    if (best && !p.events?.length && !p.resume) {
      const today = p.conditions?.mode === 'today';
      const tip = today
        ? (best.corridor === 'cape' ? 'Under today\'s conditions (Red Sea security, war-risk premiums, reduced traffic) the Cape ranks first, as most carriers currently route. Switch to Normal times to see the Suez baseline.' : 'Try raising the Red Sea threat or adding the Cape storm.')
        : 'Try closing the Red Sea, raising its threat level, or adding the Cape storm.';
      out.push({critical: false, title: 'Optimization complete', message: `${best.corridor_label} ranks first under ${p.preset.label.toLowerCase()} preferences${today ? ' and today\'s sourced conditions' : ''}. ${tip}`});
    }
  }
  $('alerts').innerHTML = out.map(a => `<div class="alert-box ${a.critical ? 'critical' : a.good ? 'good' : ''}"><b>${a.critical ? '⛔' : '◇'} ${esc(a.title)}</b>${esc(a.message)}</div>`).join('');
}

const WX_LABEL = {live: 'live', seasonal: 'seasonal', mixed: 'live → seasonal', event: 'storm event'};
function renderPlanPanel() {
  const a = activePlan(), p = state.plan;
  const chart = $('fuelChart'), table = $('cpTable'), costs = $('costList');
  chart.innerHTML = ''; table.innerHTML = ''; costs.innerHTML = '';
  if (!a) { $('planMeta').textContent = '—'; costs.innerHTML = '<div class="cost-note">No plan to show.</div>'; return; }
  const w = p.weather || {};
  const wx = w.used ? `live weather on ${a.summary.live_weather_legs} of ${a.summary.legs} segments` : (w.requested ? 'live weather unavailable · seasonal sample' : 'live weather off · seasonal sample');
  const fm = p.fuel_model.ship_model === 'calibrated' ? `fuel model calibrated on ${p.fuel_model.reports} noon reports` : `${p.fuel_model.name} fuel model (simulated training)`;
  $('planMeta').textContent = `${a.corridor_label} · ${fm} · ${wx}`;
  drawFuelChart(a, p);
  renderCompliance(a);
  renderJit(a === p.recommended ? p.jit : null);
  renderUncertainty(a === p.recommended ? p.uncertainty : null, a);
  const priceAt = Object.fromEntries(a.bunkering.map(b => [b.port, b]));
  const rows = a.checkpoints.map((c, i) => {
    const leg = i > 0 ? a.legs[i - 1] : null;
    const buy = c.bunker_t ? `<span class="fuel-buy">+${fT(c.bunker_t)}</span> @ $${priceAt[c.node]?.price_usd_t ?? '—'}/t` : '';
    const cls = c.kind === 'waypoint' ? 'wp' : 'port';
    const wx = leg ? `<span class="wx wx-${esc(leg.weather_source)}" title="${esc(leg.weather_sources.join(', '))}">${leg.max_wave_m.toFixed(1)} m${leg.max_wind_ms ? ' · ' + Math.round(leg.max_wind_ms) + ' m/s' : ''} · ${esc(WX_LABEL[leg.weather_source] || leg.weather_source)}</span>` : '—';
    return `<tr class="${cls}"><td>${esc(c.name)}</td><td>${esc(fmtEta(c.eta))}</td><td class="num">${leg ? leg.speed_kn + ' kn' : '—'}</td><td>${wx}</td><td class="num">${fT(c.fuel_t)}</td><td>${buy}</td></tr>`;
  }).join('');
  table.innerHTML = `<thead><tr><th>Checkpoint</th><th>ETA (UTC)</th><th class="num">Speed in</th><th>Sea state on the way</th><th class="num">Fuel on arrival</th><th>Refuel</th></tr></thead><tbody>${rows}</tbody>`;
  const c = a.costs, d = a.decision;
  const row = (label, v, cls = '') => `<div class="cost-row ${cls}"><span>${esc(label)}</span><b>${fUSD(v)}</b></div>`;
  costs.innerHTML = [
    row('Fuel on board at departure (market value)', c.fuel_on_board_at_departure_usd ?? 0),
    row('Fuel purchased', c.fuel_purchased_usd),
    row('Fuel left at arrival (credit)', c.fuel_left_credit_usd),
    row('Canal tolls', c.canal_tolls_usd),
    row('Port fees', c.port_fees_usd),
    row(`Operating cost (${a.summary.days.toFixed(1)} days${a.summary.anchorage_hours ? ` + ${a.summary.anchorage_hours.toFixed(0)} h at anchorage` : ''})`, c.operating_usd),
    row('Insurance: war-risk and held-covered premiums', c.war_risk_insurance_usd),
    row('Delay penalty', c.delay_penalty_usd),
    row('EU ETS carbon allowances', c.eu_ets_usd || 0),
    row('Cash total', c.cash_total_usd, 'total'),
    '<div class="cost-note">Decision weights for the chosen objective (not cash):</div>',
    row(`Risk adjustment (score ${Math.round(a.summary.route_risk * 100)}/100)`, d.risk_adjustment_usd, 'muted'),
    row(`Carbon shadow price (${fT(a.summary.co2_t)} CO₂)`, d.carbon_shadow_usd, 'muted'),
    row('Time preference', d.time_preference_usd, 'muted'),
    row('Decision score', d.score_usd, 'muted'),
  ].join('');
}

function renderUncertainty(u, a) {
  const box = $('uncBox');
  if (!u) { box.hidden = true; return; }
  box.hidden = false;
  const pct = x => (x * 100).toFixed(x < 0.01 && x > 0 ? 1 : 0) + '%';
  const flag = (x, warn) => x >= warn ? ' class="unc-warn"' : '';
  box.innerHTML = `<div class="sub-h" style="margin-top:0">How sure is this plan? <span class="small" title="${esc(u.method)}">(${u.runs} simulated voyages ⓘ)</span></div>
    <div class="unc-grid">
      <div><small>Arrival, 80% range</small><b>${esc(fmtEta(u.eta.p10))} – ${esc(fmtEta(u.eta.p90))}</b><span>spread ${u.eta_spread_h.toFixed(0)} h · median ${esc(fmtEta(u.eta.p50))}</span></div>
      <div><small>Fuel burned, 80% range</small><b>${fT(u.fuel_burned_t.p10)} – ${fT(u.fuel_burned_t.p90)}</b><span>plan ${fT(a.summary.fuel_burned_t)}</span></div>
      <div><small>Fuel left, worst 1 in 10</small><b${flag(u.prob_reserve_breach, 0.05)}>${fT(u.fuel_on_arrival_t.p10)}</b><span>reserve breach risk ${pct(u.prob_reserve_breach)}</span></div>
      <div><small>${u.prob_late == null ? 'Weather hold risk' : 'Chance of missing deadline'}</small><b${flag(u.prob_late ?? u.prob_weather_hold, 0.1)}>${pct(u.prob_late ?? u.prob_weather_hold)}</b><span>${u.prob_late == null ? 'sea state above ship limit' : 'weather hold ' + pct(u.prob_weather_hold)}</span></div>
    </div>`;
}
function renderJit(j) {
  const box = $('jitBox');
  if (!j) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  const good = j.fuel_saved_t >= 1;
  box.classList.toggle('flat', !good);
  const src = j.source === 'congestion' ? `${j.queue_h} h berth queue at the destination` : 'berth time set by the operator';
  const sp = x => x.speeds_kn.length > 1 ? `${x.speeds_kn[0]}–${x.speeds_kn.at(-1)} kn` : `${x.speeds_kn[0]} kn`;
  const col = (x) => `<b>${esc(fmtEta(x.arrive_eta))}</b>`;
  box.innerHTML = `<h4>⏱ Just-in-time arrival · berth ready ${esc(fmtEta(j.berth_ready_eta))}</h4><p>${esc(j.note)} <span class="small">(${esc(src)})</span></p>
    <div class="jit-grid"><span class="h"></span><span class="h">Sail as usual, then wait</span><span class="h">Just in time (recommended)</span>
    <span>Arrive</span>${col(j.sail_then_wait)}${col(j.just_in_time)}
    <span>Speeds</span><b>${sp(j.sail_then_wait)}</b><b>${sp(j.just_in_time)}</b>
    <span>At anchorage</span><b>${j.sail_then_wait.anchorage_h.toFixed(0)} h</b><b>${j.just_in_time.anchorage_h.toFixed(0)} h</b>
    <span>Fuel</span><b>${fT(j.sail_then_wait.fuel_t)}</b><b>${fT(j.just_in_time.fuel_t)}${good ? ` <span class="jit-save">−${fT(j.fuel_saved_t)}</span>` : ''}</b>
    <span>CO₂</span><b>${fT(j.sail_then_wait.co2_t)}</b><b>${fT(j.just_in_time.co2_t)}${good ? ` <span class="jit-save">−${fT(j.co2_saved_t)}</span>` : ''}</b>
    <span>Cash</span><b>${fUSD(j.sail_then_wait.cash_usd)}</b><b>${fUSD(j.just_in_time.cash_usd)}${j.cash_saved_usd > 0 ? ` <span class="jit-save">−${fUSD(j.cash_saved_usd)}</span>` : ''}</b></div>`;
}

function renderCompliance(a) {
  const box = $('complianceList'); if (!box) return;
  const items = a?.compliance || [];
  const warn = items.filter(f => f.kind === 'warn').length;
  $('complianceCount').textContent = warn ? `${warn}` : '';
  box.innerHTML = items.length ? items.map(f => `<div class="cf"><span class="cf-tag ${esc(f.kind)}">${esc(f.kind === 'warn' ? 'action' : f.kind)}</span><div><b>${esc(f.rule)}</b> · ${esc(f.detail)}${f.url ? ` <a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : f.source ? ` <span style="color:#64748b">(${esc(f.source)})</span>` : ''}${f.date ? ` <span style="color:#64748b">· ${esc(f.date)}</span>` : ''}</div></div>`).join('')
    : '<div class="cf">No findings.</div>';
}
function drawFuelChart(a, p) {
  const svg = $('fuelChart'), cap = p.ship.fuel_capacity_t, reserve = p.ship.min_reserve_t;
  const dep = Date.parse(p.request.departure), totalH = Math.max(1, a.summary.days * 24);
  const X = h => 56 + Math.min(1, Math.max(0, h / totalH)) * 560, Y = f => 16 + (1 - f / cap) * 132;
  for (const f of [0, cap / 2, cap]) {
    svg.appendChild(svgEl('line', {x1: 56, x2: 616, y1: Y(f), y2: Y(f), stroke: '#e3e9f0', 'stroke-width': 1}));
    const t = svgEl('text', {x: 50, y: Y(f) + 3, 'text-anchor': 'end', fill: '#64748b', 'font-size': 9}); t.textContent = fT(f); svg.appendChild(t);
  }
  svg.appendChild(svgEl('line', {x1: 56, x2: 616, y1: Y(reserve), y2: Y(reserve), stroke: '#dc2626', 'stroke-width': 1, 'stroke-dasharray': '4 4'}));
  const rt = svgEl('text', {x: 60, y: Y(reserve) + 12, 'text-anchor': 'start', fill: '#dc2626', 'font-size': 9}); rt.textContent = `Minimum reserve ${fT(reserve)}`; svg.appendChild(rt);
  const pts = [];
  for (const c of a.checkpoints) {
    const h = (Date.parse(c.eta) - dep) / 36e5;
    pts.push([h, c.fuel_t, c]);
    if (c.fuel_after_bunker_t) pts.push([h, c.fuel_after_bunker_t, c]);
  }
  svg.appendChild(svgEl('path', {d: pts.map((q, i) => `${i ? 'L' : 'M'}${X(q[0]).toFixed(1)},${Y(q[1]).toFixed(1)}`).join(' '), fill: 'none', stroke: '#0d9488', 'stroke-width': 2.2, 'stroke-linejoin': 'round'}));
  for (const [h, f, c] of pts) {
    const isBuy = c.fuel_after_bunker_t && f === c.fuel_after_bunker_t;
    const dot = svgEl('circle', {cx: X(h), cy: Y(f), r: isBuy ? 4.5 : 3, fill: isBuy ? '#d97706' : (c.kind === 'waypoint' ? '#94a3b8' : '#0d9488'), stroke: '#fff', 'stroke-width': 1.5});
    addTitle(dot, `${c.name} · ${fmtEta(c.eta)} UTC · ${fT(f)} on board${isBuy ? ' after refueling' : ''}`);
    svg.appendChild(dot);
    if (isBuy || c.kind === 'destination') {
      const lbl = svgEl('text', {x: X(h), y: Y(f) - 8, 'text-anchor': c.kind === 'destination' ? 'end' : 'middle', fill: isBuy ? '#b45309' : '#0f1f33', 'font-size': 9.5, 'font-weight': 700});
      lbl.textContent = isBuy ? `Refuel at ${c.name}` : `${c.name}: ${fT(f)}`; svg.appendChild(lbl);
    }
  }
  for (const frac of [0, 0.5, 1]) {
    const t = svgEl('text', {x: X(totalH * frac), y: 168, 'text-anchor': frac === 0 ? 'start' : frac === 1 ? 'end' : 'middle', fill: '#64748b', 'font-size': 9});
    t.textContent = frac === 0 ? 'Departure' : `Day ${(a.summary.days * frac).toFixed(frac === 1 ? 1 : 0)}`; svg.appendChild(t);
  }
}

// ------------------------------------------------------------------ map
function drawGrid() {
  const g = $('gridSvg'); g.innerHTML = '';
  for (let lon = -30; lon <= 135; lon += 15) { const [x] = project([0, lon]); g.appendChild(svgEl('path', {d: `M${x},0V650`, stroke: '#a9c7e3', 'stroke-width': .9, 'stroke-dasharray': '2 7'})); }
  for (let lat = -45; lat <= 60; lat += 15) { const [, y] = project([lat, 0]); g.appendChild(svgEl('path', {d: `M0,${y}H1200`, stroke: '#a9c7e3', 'stroke-width': .9, 'stroke-dasharray': '2 7'})); }
  const labels = [[36, -20, 'NORTH ATLANTIC'], [-15, -17, 'SOUTH ATLANTIC'], [-12, 78, 'INDIAN OCEAN'], [14, 65, 'ARABIAN SEA'], [36, 23, 'MEDITERRANEAN'], [15, 113, 'SOUTH CHINA SEA'], [-39, 83, 'SOUTHERN INDIAN OCEAN']];
  const names = $('mapNamesSvg'); names.innerHTML = '';
  labels.forEach(([lat, lon, label]) => { const [x, y] = project([lat, lon]); const el = svgEl('text', {x, y, class: 'map-note', 'text-anchor': 'middle'}); el.textContent = label; names.appendChild(el); });
}

function drawNetwork() {
  const g = $('networkSvg'); g.innerHTML = '';
  for (const e of state.network.edges) {
    g.appendChild(addTitle(svgEl('path', {d: pathString(e.points), fill: 'none', stroke: '#4f7aa3', 'stroke-width': .9, opacity: .45, 'stroke-dasharray': '3 4'}), `Network segment: ${portName(e.from)} – ${portName(e.to)}`));
  }
}

// ---- condition-change warnings: banner, pulsing map markers and a timeline (rules live in warnings.js)
const WC = window.TLWarnings, warnCenter = WC.createCenter();
const SEV_COLOR = {info: '#2563eb', warning: '#d97706', critical: '#dc2626'};
let riskId = '', riskBase = null;
function warningCauses() {
  const plan = activePlan(), net = state.network, limit = shipObj()?.max_hs_m ?? 7;
  const locOf = e => e.zone ? net.zones[e.zone]?.location : e.port ? net.ports[e.port]?.loc : e.center;
  const events = buildEvents().filter(e => e.type !== 'news');   // headlines are handled by newsCauses
  const weatherSource = state.plan?.weather?.used ? 'Open-Meteo marine forecast (live)' : 'Seasonal SAMPLE wave data (no live forecast)';
  // Only news fetched for this route's regions counts as "on your route" (the first fetch happens before any route exists).
  const newsForRoute = [...(state.news.regions || [])].sort().join(',') === routeRegions().join(',');
  const causes = [...WC.scenarioCauses(events, limit, locOf),
    ...WC.weatherCauses(plan.legs, limit, node => plan.checkpoints.find(c => c.node === node)?.loc, weatherSource),
    ...(newsForRoute ? WC.newsCauses(state.news.articles, zoneLabel) : []),
    ...(state.ais.on ? WC.aisCauses(state.ais.data?.anomalies) : [])];   // possible stops, queues and ships gone quiet (advisory only)
  // Risk is compared with the first plan for this ship, route and conditions, so it clears when the cause clears.
  const risk = Math.round(plan.summary.route_risk * 100), id = [state.ship, state.origin, state.destination, state.conditions, state.priority].join('|');
  if (id !== riskId) { riskId = id; riskBase = null; }
  if (riskBase == null) riskBase = risk;
  const rc = WC.riskCause(riskBase, risk, !!state.plan?.change?.route_changed);
  return rc ? [...causes, rc] : causes;
}
function updateWarnings() {
  if (state.planStatus !== 'ok' || !state.network || !activePlan()) return;
  let causes = warningCauses();
  if (state.news.status === 'loading') causes = causes.concat(WC.active(warnCenter).filter(c => c.kind === 'news'));   // hold news warnings while the feed reloads
  const r = WC.update(warnCenter, causes, new Date().toISOString());
  if (r.raised.length || r.changed.length || r.cleared.length) renderCondLog();
  renderBanner(); drawWarnMarkers();
}
function renderBanner() {
  const el = $('condBanner'), b = WC.banner(warnCenter);
  if (!b) { el.hidden = true; return; }
  const open = WC.active(warnCenter).filter(c => warnCenter.dismissed[c.key] !== c.fp), risk = warnCenter.active['risk:route'];
  const text = `Condition change: ${b.title}${b.detail ? ' · ' + b.detail : ''}${risk && b.key !== risk.key ? ' · ' + risk.detail : ''}`;
  el.hidden = false; el.className = 'cond-banner sev-' + b.severity; el.dataset.key = b.key;
  el.innerHTML = `<span class="cb-sev">${WC.SEVERITY[b.severity].icon} ${WC.SEVERITY[b.severity].label}</span><span class="cb-text">${esc(text)}</span>`
    + `${open.length > 1 ? `<span class="cb-more">+${open.length - 1} more</span>` : ''}<span class="cb-src">Source: ${esc(b.source)}</span>`
    + '<button type="button" class="cb-x" aria-label="Dismiss warning">✕</button>';
}
function drawWarnMarkers() {   // pulsing marker where the condition is; icon and colour both say how serious it is
  let g = $('warnSvg'); if (!g) { g = svgEl('g', {id: 'warnSvg'}); $('portsSvg').after(g); }
  g.innerHTML = '';
  const k = markerK();
  for (const c of WC.active(warnCenter)) {
    if (!c.loc) continue;
    const [x, y] = project(c.loc), s = WC.SEVERITY[c.severity], col = SEV_COLOR[c.severity];
    const m = svgEl('g', {transform: `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${k.toFixed(3)})`, class: 'warn-marker'});
    m.appendChild(svgEl('circle', {r: 15, fill: col, opacity: .28, class: 'warn-pulse'}));
    m.appendChild(svgEl('circle', {r: 10, fill: '#fff', stroke: col, 'stroke-width': 2}));
    const t = svgEl('text', {y: 4.5, 'text-anchor': 'middle', 'font-size': 12, 'font-weight': 800, fill: col}); t.textContent = s.icon; m.appendChild(t);
    g.appendChild(addTitle(m, `${s.label}: ${c.title}${c.detail ? ' · ' + c.detail : ''}\nSource: ${c.source}`));
  }
}
function renderCondLog() {
  const log = [...warnCenter.log].reverse(), words = {raised: 'Raised', changed: 'Changed', cleared: 'Cleared'};
  $('condCount').textContent = log.length ? `(${log.length})` : '';
  $('condList').innerHTML = log.length ? log.map(e => `<li class="sev-${e.severity} ev-${e.event}"><span class="cl-t">${esc(e.t.slice(11, 16))} UTC</span> <span class="cl-ev">${WC.SEVERITY[e.severity].icon} ${words[e.event]}</span> <b>${esc(e.title)}</b>${e.detail ? ' · ' + esc(e.detail) : ''}<small>Source: ${esc(e.source)}</small></li>`).join('')
    : '<li class="cl-empty">No condition changes yet. Switch on a scenario to see one here.</li>';
}
function attachWarnings() {
  $('condBanner').addEventListener('click', e => {
    if (e.target.closest('.cb-x')) { WC.dismiss(warnCenter, e.currentTarget.dataset.key); renderBanner(); return; }
    $('condLog').open = true; $('condLog').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  });
  renderCondLog();
}

// ---- how cost, carbon and risk weigh in: score shares, why the winner won, and a cost-vs-CO2 Pareto chart (maths in compare.js)
const TC = window.TLCompare;
const SHARE_COLOR = {cost: '#0f3b63', carbon: '#16a34a', risk: '#dc2626', time: '#d97706'};
const SHARE_LABEL = {cost: 'Cost', carbon: 'Carbon', risk: 'Risk', time: 'Time'};
function renderTradeoff() {
  const el = $('tradeoff'), plans = plansOf(state.plan);
  if (!plans.length) { el.hidden = true; return; }
  const rows = plans.map(r => ({id: r.corridor, label: r.corridor_label, days: r.summary.days, cost: r.costs.cash_total_usd, co2: r.summary.co2_t,
    risk: Math.round(r.summary.route_risk * 100)}));
  const front = TC.paretoFront(rows), shares = TC.scoreShares(plans[0].decision), why = TC.whyLine(rows[0], rows[1]);
  const bar = Object.keys(SHARE_LABEL).filter(k => shares[k] > 0).map(k =>
    `<span class="to-seg" style="flex:${shares[k]};background:${SHARE_COLOR[k]}" title="${SHARE_LABEL[k]}: ${shares[k]}% of the decision score">${shares[k] >= 9 ? SHARE_LABEL[k] + ' ' + shares[k] + '%' : ''}</span>`).join('');
  const legend = Object.keys(SHARE_LABEL).map(k => `<span><i style="background:${SHARE_COLOR[k]}"></i>${SHARE_LABEL[k]} ${shares[k]}%</span>`).join('');
  el.hidden = false;
  el.innerHTML = `<div class="to-head">How cost, carbon and risk weigh in</div>`
    + (why ? `<div class="to-why">${esc(why)}</div>` : '')
    + `<div class="to-bar" role="img" aria-label="Share of the decision score: ${Object.keys(SHARE_LABEL).map(k => SHARE_LABEL[k] + ' ' + shares[k] + ' percent').join(', ')}">${bar}</div>`
    + `<div class="to-legend">${legend}</div>`
    + `<div class="to-note">Shares of the optimized route's decision score (cash cost, carbon at your carbon price, risk loss, time premium). The sliders under Optimize set those prices, so moving them shifts these shares and can change the winner.</div>`
    + tradeoffChart(rows, front);
}
function tradeoffChart(rows, front) {
  const W2 = 340, H2 = 190, L = 48, R = 18, T = 14, B = 36;
  const span = (vals, pad) => { const lo = Math.min(...vals), hi = Math.max(...vals), d = (hi - lo) || Math.max(1, Math.abs(hi) * 0.1); return [lo - d * pad, hi + d * pad]; };
  const [x0, x1] = span(rows.map(r => r.cost), 0.35), [y0, y1] = span(rows.map(r => r.co2), 0.35);
  const X = v => L + (v - x0) / (x1 - x0) * (W2 - L - R), Y = v => H2 - B - (v - y0) / (y1 - y0) * (H2 - T - B);
  const dots = rows.map(r => {
    const eff = front.has(r.id), x = X(r.cost), y = Y(r.co2), rad = 5 + r.risk / 10, right = x < W2 * 0.6;
    const tip = `${r.label}: ${fUSD(r.cost)} cash, ${fT(r.co2)} CO₂, risk ${r.risk}/100, ${r.days.toFixed(1)} days · ${eff ? 'Pareto-efficient: no other route is better on cost, CO₂ and risk at once' : 'dominated: another route is at least as good on all three'}`;
    return `<g><title>${esc(tip)}</title><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${rad.toFixed(1)}" fill="${eff ? '#0d9488' : '#e2e8f0'}" stroke="${eff ? '#0f3b63' : '#94a3b8'}" stroke-width="1.6" ${eff ? '' : 'stroke-dasharray="3 2"'}/>`
      + `<text x="${(right ? x + rad + 4 : x - rad - 4).toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="${right ? 'start' : 'end'}" class="to-pt">${eff ? '★ ' : ''}${esc(r.label.replace('Via ', ''))} · risk ${r.risk}</text></g>`;
  }).join('');
  const label = `Cost against CO₂ for ${rows.length} route${rows.length === 1 ? '' : 's'}; ${rows.filter(r => front.has(r.id)).map(r => r.label).join(', ')} ${front.size === 1 ? 'is' : 'are'} Pareto-efficient`;
  return `<svg class="to-chart" viewBox="0 0 ${W2} ${H2}" role="img" aria-label="${esc(label)}">`
    + `<line x1="${L}" y1="${H2 - B}" x2="${W2 - R}" y2="${H2 - B}" stroke="#cbd5e1"/><line x1="${L}" y1="${T}" x2="${L}" y2="${H2 - B}" stroke="#cbd5e1"/>`
    + `<text x="${(L + W2 - R) / 2}" y="${H2 - 6}" text-anchor="middle" class="to-ax">Cash cost →  (${fUSD(Math.max(0, x0))} to ${fUSD(x1)})</text>`
    + `<text transform="translate(12 ${(T + H2 - B) / 2}) rotate(-90)" text-anchor="middle" class="to-ax">CO₂ →  (${fT(Math.max(0, y0))} to ${fT(y1)})</text>`
    + dots + `</svg><div class="to-note">★ and a solid teal dot: Pareto-efficient (no other route beats it on cost, CO₂ and risk together). Dashed grey dot: dominated. Bigger dot: higher risk.</div>`;
}

// ---- incident search: type to find an incident, ranked by how much it matters to the current route (logic in incidents.js)
const TI = window.TLIncidents;
let incCatalog = null, incOptions = [], incActive = -1, incTimer = 0;
function incidentContext() {
  const plans = plansOf(state.plan), a = activePlan(), alts = plans.filter(r => r !== a);
  const zonesOf = r => (r?.legs || []).flatMap(l => l.zones || []), portsOf = r => (r?.checkpoints || []).map(c => c.node), pointsOf = r => r?.path || [];
  return {routeZones: zonesOf(a), routePorts: portsOf(a), routePoints: pointsOf(a),
    altZones: alts.flatMap(zonesOf), altPorts: alts.flatMap(portsOf), altPoints: alts.flatMap(pointsOf)};
}
function renderIncidentList() {
  const input = $('incInput'), list = $('incList'), q = input.value.trim();
  if (!state.network) return;
  incCatalog = incCatalog || TI.buildCatalog(state.network);
  const taken = new Set(state.extraEvents.map(e => e.id));
  const rows = TI.rank(incCatalog, q, incidentContext()).filter(r => !taken.has('inc-' + r.item.id)).slice(0, q ? 12 : 8);
  incOptions = rows.map(r => ({item: r.item, group: r.group}));
  if (q) incOptions.push({ask: q});
  let html = '', last = '';
  incOptions.forEach((o, i) => {
    if (o.ask) { html += `<li role="option" id="inc-opt-${i}" data-i="${i}" aria-selected="false" class="inc-opt inc-ask"><span class="inc-ic" aria-hidden="true">💬</span><span>Ask Tideline: “${esc(o.ask)}” <small>(the assistant proposes a scenario)</small></span></li>`; return; }
    if (o.group !== last) { html += `<li role="presentation" class="inc-group">${esc(o.group)}</li>`; last = o.group; }
    html += `<li role="option" id="inc-opt-${i}" data-i="${i}" aria-selected="false" class="inc-opt sev-${o.item.severity}"><span class="inc-ic" aria-hidden="true">${TI.severityIcon(o.item.severity)}</span><span>${esc(o.item.label)}</span></li>`;
  });
  list.innerHTML = html || '<li role="presentation" class="inc-group">No incidents left to add</li>';
  incActive = -1; input.removeAttribute('aria-activedescendant');
  list.hidden = false; input.setAttribute('aria-expanded', 'true');
}
function closeIncidentList() { $('incList').hidden = true; $('incInput').setAttribute('aria-expanded', 'false'); $('incInput').removeAttribute('aria-activedescendant'); incActive = -1; }
function setIncidentActive(i) {
  const opts = [...$('incList').querySelectorAll('[role=option]')]; if (!opts.length) return;
  incActive = (i + opts.length) % opts.length;
  opts.forEach((el, k) => el.setAttribute('aria-selected', String(k === incActive)));
  $('incInput').setAttribute('aria-activedescendant', opts[incActive].id); opts[incActive].scrollIntoView({block: 'nearest'});
}
function chooseIncident(i) {
  const o = incOptions[i]; if (!o) return;
  if (o.ask) { const q = o.ask; $('incInput').value = ''; closeIncidentList(); setSideTab('chat'); sendChat(q); return; }
  const ev = TI.eventFor(o.item, new Date().toISOString());
  if (!state.extraEvents.some(e => e.id === ev.id)) state.extraEvents.push(ev);
  $('incInput').value = ''; closeIncidentList(); renderExtraEvents(); schedulePlan();
}
function attachIncidents() {
  const input = $('incInput'), list = $('incList');
  input.addEventListener('input', () => { clearTimeout(incTimer); incTimer = setTimeout(renderIncidentList, 120); });   // debounced
  input.addEventListener('focus', renderIncidentList);
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) renderIncidentList(); setIncidentActive(incActive + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setIncidentActive(incActive - 1); }
    else if (e.key === 'Enter') { if (!list.hidden && incActive >= 0) { e.preventDefault(); chooseIncident(incActive); } else if (input.value.trim()) { e.preventDefault(); renderIncidentList(); chooseIncident(incOptions.length - 1); } }
    else if (e.key === 'Escape') { if (!list.hidden) { e.preventDefault(); closeIncidentList(); } else input.value = ''; }
  });
  list.addEventListener('mousedown', e => { const li = e.target.closest('[role=option]'); if (li) { e.preventDefault(); chooseIncident(Number(li.dataset.i)); } });
  input.addEventListener('blur', () => setTimeout(closeIncidentList, 120));
}

// Hazard overlay. Every marker pairs colour with a shape and a symbol, and the legend explains each one.
const PORT_KIND = {
  closed:      {label: 'Port closed or avoided', glyph: '✕', fill: '#dc2626', shape: (x, y) => `M${x - 6},${y - 6}h12v12h-12z`, dy: 3.2},
  shallow:     {label: 'Too shallow for this ship', glyph: '↓', fill: '#7c3aed', shape: (x, y) => `M${x},${y - 8}l7,8l-7,8l-7,-8z`, dy: 3.2},
  unreachable: {label: 'Unreachable: every sea route passes a risk zone', glyph: '⊘', fill: '#7f1d1d', shape: (x, y) => `M${x - 7},${y}a7,7 0 1,0 14,0a7,7 0 1,0 -14,0`, dy: 3.2},
  in_zone:     {label: 'Inside a high-risk zone', glyph: '!', fill: '#ea580c', shape: (x, y) => `M${x},${y - 9}l8,15h-16z`, dy: 4},
  congested:   {label: 'Congested: long expected wait', glyph: '⏱', fill: '#d97706', shape: (x, y) => `M${x - 4},${y - 7}h8l4,7l-4,7h-8l-4,-7z`, dy: 3.2},
};
function zoneTitle(z) {
  const head = `${z.label} · ${z.level.toUpperCase()}${Number.isFinite(z.score) ? ` · security risk score ${z.score}/100` : ''}`;
  const parts = z.parts?.length ? z.parts.map(p => `• ${p.text} — ${p.source}`) : (z.reasons || []).map(r => `• ${r}`);
  return [head, ...(parts.length ? parts : ['• no active risk signals'])].join('\n');
}
function ensureMapDefs() {   // diagonal hatch for closed zones, so "closed" does not depend on colour alone
  const svg = $('worldMap'); if (svg.querySelector('#hazHatch')) return;
  const defs = svg.querySelector('defs') || svg.insertBefore(svgEl('defs'), svg.firstChild);
  const pat = svgEl('pattern', {id: 'hazHatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'});
  pat.appendChild(svgEl('rect', {width: 6, height: 6, fill: '#ef444422'}));
  pat.appendChild(svgEl('line', {x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#dc2626', 'stroke-width': 2}));
  defs.appendChild(pat);
}

function drawMap() {
  if (!state.network) return;
  const t = state.toggles, a = activePlan(), p = state.plan;
  // risk zones, colored by the level the planner computed (closed / high / elevated / normal)
  const zones = $('zonesSvg'); zones.innerHTML = '';
  const hz = p?.hazards?.zones || Object.entries(state.network.zones).map(([id, z]) => ({id, ...z, level: 'normal', reasons: []}));
  ensureMapDefs();
  for (const z of hz) {
    const [x, y] = project(z.location), r = z.id === 'gulf_guinea' ? 20 : z.level === 'normal' ? 10 : 14;
    const g = svgEl('g');
    g.appendChild(svgEl('circle', {cx: x, cy: y, r, class: 'haz-zone ' + z.level}));
    if (z.level !== 'normal') {   // symbol at the centre: ⚠ elevated / high, ✕ closed
      const mark = svgEl('text', {x, y: y + 4.5, 'text-anchor': 'middle', class: 'haz-mark ' + z.level});
      mark.textContent = z.level === 'closed' ? '✕' : '⚠';
      g.appendChild(mark);
    }
    if (z.level === 'high' || z.level === 'closed') {
      const lbl = svgEl('text', {x: x + r + 4, y: y - r + 2, class: 'haz-label ' + z.level});
      lbl.textContent = `▲ ${z.label.split(' / ')[0]} · ${z.level.toUpperCase()}`;
      g.appendChild(lbl);
    }
    zones.appendChild(addTitle(g, zoneTitle(z)));
  }
  // storm
  const storm = $('stormSvg'); storm.innerHTML = '';
  if (t.stormCape) {
    const [x, y] = project([-35, 22]);
    storm.appendChild(addTitle(svgEl('circle', {cx: x, cy: y, r: 36, fill: '#fe7d8126', stroke: '#fe7d81', 'stroke-width': 1.4, 'stroke-dasharray': '3 4'}), 'Simulated storm: 7.5 m waves, 300 nm radius'));
  }
  // routes: original plan (if replaced), alternatives, then the active plan on top
  const layer = $('routesSvg'); layer.innerHTML = '';
  if (p?.change?.route_changed && p.baseline) {
    layer.appendChild(addTitle(svgEl('path', {d: pathString(p.baseline.path), class: 'route-line blocked'}), `Original plan: ${p.baseline.corridor_label}, ${p.baseline.summary.days.toFixed(1)} days`));
  } else if (p && !p.recommended && p.baseline) {
    layer.appendChild(addTitle(svgEl('path', {d: pathString(p.baseline.path), class: 'route-line blocked'}), `Original plan, now infeasible: ${p.baseline.corridor_label}`));
  }
  for (const r of plansOf(p)) {
    if (a && r.corridor === a.corridor) continue;
    const line = addTitle(svgEl('path', {d: pathString(r.path), class: 'route-line secondary'}), `${r.corridor_label}: ${r.summary.days.toFixed(1)} days, ${fUSD(r.costs.cash_total_usd)} (click to preview)`);
    line.addEventListener('click', () => { state.preview = r.corridor; render(); pullWeather(); });
    layer.appendChild(line);
  }
  const rsm = p?.resume, st = shipState();
  if (rsm && st && state.committed) {
    const cp = state.committed.plan, route = st.resume.route, traveled = [];
    for (let i = 0; i <= st.resume.leg_index; i++) {
      const pts = edgePoints(route[i], route[i + 1]);
      if (!pts) continue;
      if (i < st.resume.leg_index) { traveled.push(...(traveled.length ? pts.slice(1) : pts)); continue; }
      // partial segment: points before the ship, then the ship itself
      const seg = pts.slice(1).map((q, k) => haversine(pts[k], q)), total = seg.reduce((x, y) => x + y, 0);
      let run = 0;
      if (!traveled.length) traveled.push(pts[0]);
      for (let k = 0; k < seg.length && (run + seg[k]) / total < st.resume.fraction; k++) { run += seg[k]; traveled.push(pts[k + 1]); }
    }
    if (rsm.position) traveled.push(rsm.position);
    layer.appendChild(addTitle(svgEl('path', {d: pathString(traveled), class: 'route-line traveled'}), `Sailed so far: ${state.progressDay} days on the committed ${cp.corridor_label} plan`));
    if (rsm.recommendation === 'change_course' && !rsm.continue) {
      const ahead = [rsm.position];
      for (let i = st.resume.leg_index; i < route.length - 1; i++) { const pts = edgePoints(route[i], route[i + 1]); if (pts) ahead.push(...(i === st.resume.leg_index ? pts.slice(1).filter((_, k, arr) => true) : pts.slice(1))); }
      layer.appendChild(addTitle(svgEl('path', {d: pathString(ahead), class: 'route-line blocked'}), 'Committed route ahead: blocked'));
    }
    if (rsm.recommendation === 'change_course' && rsm.continue) {
      layer.appendChild(addTitle(svgEl('path', {d: pathString(rsm.continue.path), class: 'route-line committed'}), `Stay on course: ${rsm.continue.summary.remaining_days.toFixed(1)} more days, ${fUSD(rsm.continue.costs.cash_total_usd)}`));
    }
  }
  drawFleetPaths();
  if (a) layer.appendChild(addTitle(svgEl('path', {d: pathString(a.path), class: 'route-line selected'}), `${a.corridor_label}: ${a.summary.days.toFixed(1)} days, ${fUSD(a.costs.cash_total_usd)}`));
  // ports
  const ports = $('portsSvg'); ports.innerHTML = '';
  const closedPorts = new Set(t.closeWestAfrica ? ['DKR', 'ABJ'] : []);
  for (const [code, port] of Object.entries(state.network.ports)) {
    if (code === state.origin || code === state.destination) continue;
    const [x, y] = project(port.loc);
    const g = svgEl('g');
    if (closedPorts.has(code)) {
      g.appendChild(svgEl('path', {d: `M${x - 4},${y - 4}L${x + 4},${y + 4}M${x + 4},${y - 4}L${x - 4},${y + 4}`, stroke: '#fe7d81', 'stroke-width': 2}));
    } else {
      g.appendChild(svgEl('circle', {cx: x, cy: y, r: 2.8, fill: '#64748b', opacity: .9}));
    }
    const fp = state.conditions === 'today' && state.today?.fuel_prices?.[code];
    ports.appendChild(addTitle(g, `${port.name} · fuel $${fp ? Math.round(fp.price) + ' (' + fp.basis + ', ' + fp.date + ')' : port.fuel_price + ' (sample)'}/t · max draft ${port.max_draft_m} m${closedPorts.has(code) ? ' · CLOSED (simulated)' : ''}`));
  }
  for (const hp of p?.hazards?.ports || []) {
    if ((hp.port === state.origin || hp.port === state.destination) && !hp.reasons.length) continue;
    const [x, y] = project(hp.loc), g = svgEl('g', {class: 'haz-port'}), k = PORT_KIND[hp.kind] || PORT_KIND.in_zone;
    g.appendChild(svgEl('path', {d: k.shape(x, y), fill: k.fill, stroke: '#ffffff', 'stroke-width': 1.2, opacity: hp.level === 'severe' ? 1 : .85}));
    const t = svgEl('text', {x, y: y + k.dy, 'text-anchor': 'middle', 'font-size': 8.5, 'font-weight': 800, fill: '#ffffff'}); t.textContent = k.glyph; g.appendChild(t);
    const also = (hp.kinds || []).filter(x => x !== hp.kind).map(x => PORT_KIND[x]?.label).filter(Boolean);
    ports.appendChild(addTitle(g, [`${hp.name} · ${k.label} (${hp.level === 'severe' ? 'HAZARD' : 'CAUTION'})`, ...hp.reasons.map(r => '• ' + r), ...(also.length ? ['Also: ' + also.join('; ')] : [])].join('\n')));
  }
  for (const b of a?.bunkering || []) {
    const [x, y] = project(state.network.ports[b.port].loc), g = svgEl('g');
    g.appendChild(svgEl('circle', {cx: x, cy: y, r: 6, fill: '#d97706', stroke: '#fff', 'stroke-width': 1.5}));
    if (b.port !== state.origin) {
      const tx = svgEl('text', {x: x + 9, y: y + 14, class: 'map-port-label', style: 'fill:#b45309'}); tx.textContent = `Refuel ${fT(b.buy_t)}`; g.appendChild(tx);
    }
    ports.appendChild(addTitle(g, `Refuel at ${b.name}: ${fT(b.buy_t)} at $${b.price_usd_t}/t (${fUSD(b.cost_usd)})`));
  }
  for (const [type, code] of [['origin', state.origin], ['destination', state.destination]]) {
    const port = state.network.ports[code]; if (!port) continue;
    const [x, y] = project(port.loc), c = svgEl('g');
    c.appendChild(svgEl('circle', {cx: x, cy: y, r: 16, fill: 'url(#portGlow)'}));
    c.appendChild(svgEl('circle', {cx: x, cy: y, r: 5, fill: '#0d9488', stroke: '#fff', 'stroke-width': 2}));
    const tx = svgEl('text', {x: x + (type === 'destination' ? -11 : 11), y: y - 12, class: 'map-port-label', 'text-anchor': type === 'destination' ? 'end' : 'start'});
    tx.textContent = port.name; c.appendChild(tx); ports.appendChild(c);
  }
  // ship: at its position mid-voyage, otherwise in the origin port
  const ship = $('shipSvg'); ship.innerHTML = '';
  if (a) {
    const here = p?.resume?.position || (state.startAt ? [state.startAt.lat, state.startAt.lon] : null) || state.network.ports[state.origin]?.loc || a.path[0];
    const [x, y] = project(here);
    ship.appendChild(svgEl('circle', {cx: x, cy: y, r: 14, fill: '#50e3cf', opacity: .13}));
    ship.appendChild(svgEl('circle', {cx: x, cy: y, r: 7, fill: '#66f0de', opacity: .28}));
    ship.appendChild(addTitle(svgEl('path', {d: `M${x - 8},${y + 4} L${x + 8},${y} L${x - 7},${y - 5} L${x - 3},${y} Z`, fill: '#0f3b63', stroke: '#fff', 'stroke-width': 1.1, class: 'ship-icon'}), p?.resume ? `Ship position: day ${p.resume.elapsed_days}, ${fT(p.resume.fuel_onboard_t)} on board (simulated progress along the committed plan; no live AIS)` : 'Ship in port at departure (no live AIS tracking)'));
  }
  drawWeatherPoints();
}

function drawWeatherPoints() {
  const layer = $('weatherSvg'); layer.innerHTML = '';
  if (state.weather.status !== 'live') return;
  for (const s of state.weather.samples) {
    if (!Number.isFinite(s.nowcastWaveM)) continue;
    const [x, y] = project([s.lat, s.lon]), wave = s.nowcastWaveM;
    const color = wave >= 5 ? '#fc7888' : wave >= 3.5 ? '#ffc075' : '#50d6ca';
    layer.appendChild(addTitle(svgEl('circle', {cx: x, cy: y, r: 4.7, fill: color, class: 'wave-dot'}),
      `${s.name || 'Checkpoint'} · now ${wave.toFixed(1)} m waves. ${Number.isFinite(s.forecastWaveM) ? 'Forecast on arrival: ' + s.forecastWaveM.toFixed(1) + ' m at ' + s.forecastTimeUtc : 'Arrival beyond 7-day forecast horizon.'}`));
  }
}

// ------------------------------------------------------------------ live feeds
async function pullWeather(force = false) {
  const a = activePlan();
  if (!a) { state.weather = {status: 'idle', samples: []}; state.weatherKey = null; renderWeatherSummary(); drawWeatherPoints(); return; }
  const dep = Date.parse(state.plan.request.departure);
  const cps = a.checkpoints.slice(1, 13);
  const pts = cps.map(c => `${c.loc[0]},${c.loc[1]},${Math.max(0, ((Date.parse(c.eta) - dep) / 36e5)).toFixed(1)}`).join(';');
  if (!force && pts === state.weatherKey && state.weather.status === 'live') { drawWeatherPoints(); renderWeatherSummary(); return; }
  state.weatherKey = pts;
  const version = ++state.weatherVersion;
  state.weather = {status: 'loading', samples: []}; renderWeatherSummary();
  try {
    const res = await fetch(`/api/path-weather?pts=${encodeURIComponent(pts)}${force ? '&refresh=1' : ''}`, {cache: 'no-store'});
    if (!res.ok) throw Error(`HTTP ${res.status}`);
    const json = await res.json();
    if (version !== state.weatherVersion) return;
    json.samples = (json.samples || []).map((s, i) => ({...s, name: cps[i]?.name}));
    state.weather = json;
  } catch (e) {
    if (version !== state.weatherVersion) return;
    state.weather = {status: 'unavailable', samples: [], detail: e.message};
  }
  drawWeatherPoints(); renderWeatherSummary();
}

// News follows the route: search the regions the plan passes through (zone ids), not a fixed list.
const routeRegions = () => [...new Set((activePlan()?.legs || []).flatMap(l => l.zones || []))].sort();
let newsRegionsKey = null, newsRegionsAt = 0;
function refreshNewsForRoute() {   // re-fetch when the route's regions change; later changes at most once a minute (a reroute can flip them)
  const key = routeRegions().join(',');
  if (!key || key === newsRegionsKey) return;
  if (newsRegionsKey && Date.now() - newsRegionsAt < 60000) return;   // '' = fetched before any route existed: correct it at once
  pullNews();
}
async function pullNews(force = false) {
  state.news = {status: 'loading', articles: []}; renderNews();
  const regions = routeRegions(); newsRegionsKey = regions.join(','); newsRegionsAt = Date.now();
  try {
    const res = await fetch(`/api/news?cargo=${encodeURIComponent(state.cargo)}${regions.length ? '&regions=' + regions.join(',') : ''}${force ? '&refresh=1' : ''}`, {cache: 'no-store'});
    if (!res.ok) throw Error(`HTTP ${res.status}`);
    state.news = await res.json();
  } catch (e) { state.news = {status: 'unavailable', articles: [], detail: e.message}; }
  renderNews();
  if (state.toggles.autoNews && state.news.status === 'live') schedulePlan();
}

function renderWeatherSummary() {
  const w = state.weather, samples = w.status === 'live' ? w.samples : [];
  const current = samples.map(s => s.nowcastWaveM).filter(Number.isFinite);
  const forecast = samples.filter(s => Number.isFinite(s.forecastWaveM));
  const max = current.length ? Math.max(...current) : null;
  $('waveNumber').innerHTML = max === null ? '— <span style="font-size:13px">m</span>' : max.toFixed(1) + ' <span style="font-size:13px">m</span>';
  $('waveCaption').textContent = w.status === 'live' ? 'Largest modeled nowcast at plan checkpoints' : w.status === 'loading' ? 'Fetching marine forecasts for this plan…' : w.status === 'idle' ? 'No plan to forecast yet' : 'Marine API unreachable · plan uses sample seasonal sea state';
  $('waveFlag').textContent = max === null ? '—' : max >= 5 ? 'SEVERE' : max >= 3.5 ? 'ELEVATED' : 'LOW–MODERATE';
  $('waveFlag').style.color = max >= 5 ? '#dc2626' : max >= 3.5 ? '#d97706' : '#0d9488';
  $('waveBar').style.width = max === null ? '0%' : Math.min(100, max / 8 * 100) + '%';
  $('forecastCount').textContent = w.status === 'live' ? `${forecast.length}/${samples.length}` : '—';
  $('weatherStatus').textContent = w.status === 'live' ? '● Marine forecast live' : w.status === 'loading' ? '◌ Loading weather' : w.status === 'idle' ? '◌ Weather idle' : '△ Weather offline';
  $('weatherStatus').style.color = w.status === 'live' ? '#0f766e' : '#b45309';
  const n = state.news;
  $('newsStatus').textContent = n.status === 'live' ? '● Headlines live' : n.status === 'loading' ? '◌ Loading headlines' : '△ News offline';
  $('newsStatus').style.color = n.status === 'live' ? '#0f766e' : '#b45309';
  $('feedBadge').textContent = (w.status === 'live' || n.status === 'live') ? 'LIVE INPUTS' : (w.status === 'loading' || n.status === 'loading') ? 'CONNECTING' : 'OFFLINE';
  $('feedBadge').className = 'live-chip' + (w.status === 'unavailable' && n.status === 'unavailable' ? ' offline' : '');
  $('geoCount').textContent = n.status === 'live' ? (n.articles || []).filter(a => a.potentialDisruption).length : '—';
}

// Tag each headline: icon + text (never colour alone). A fleet match that is also on the route shows both.
function newsTagHtml(a) {
  const fleet = `<span class="news-tag tag-fleet" title="Matches ${esc(state.news.cargoLabel || 'your fleet')}">🚢 Your fleet</span>`;
  const route = '<span class="news-tag tag-route" title="About a region your route passes through">📍 On your route</span>';
  const general = '<span class="news-tag tag-general" title="General shipping news">General</span>';
  return ' ' + (a.relevant && a.onRoute ? fleet + ' ' + route : a.relevant ? fleet : a.onRoute ? route : general);
}
function renderNews() {
  const items = $('newsItems');
  if (state.news.status !== 'live') {
    items.innerHTML = `<div class="empty-feed">${state.news.status === 'loading' ? 'Fetching recent shipping and chokepoint news…' : 'Live headlines unavailable. No events invented. Scenario events still work from the controls.'}</div>`;
    renderWeatherSummary(); return;
  }
  const articles = state.news.articles.slice(0, 10);
  items.innerHTML = articles.length ? articles.map(a => `<a class="news-item" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer"><strong>${esc(a.title)}</strong><div class="news-meta"><i class="news-bullet ${a.potentialDisruption ? 'warn' : ''}"></i>${esc(a.source || 'Press feed')} · ${esc(formatDate(a.published))}${a.potentialDisruption ? ' · Keyword signal' : ''}${newsTagHtml(a)}</div></a>`).join('') : '<div class="empty-feed">No matching articles in the latest search window.</div>';
  renderWeatherSummary();
}

// ------------------------------------------------------------------ mid-voyage
// Where the ship is on the committed plan after `progressDay` days: segment, fraction, fuel on board.
function shipState() {
  const c = state.committed;
  if (!c || state.progressDay <= 0) return null;
  const dep = Date.parse(c.departure), h = state.progressDay * 24;
  const cps = c.plan.checkpoints, legs = c.plan.legs;
  for (let i = 0; i < legs.length; i++) {
    const t0 = (Date.parse(cps[i].eta) - dep) / 36e5, t1 = (Date.parse(cps[i + 1].eta) - dep) / 36e5;
    if (h >= t1 && i < legs.length - 1) continue;
    const stop = (c.plan.bunkering.find(b => b.port === cps[i].node && b.eta === cps[i].eta)?.stop_hours) || 0;
    const sailStart = t0 + stop, frac = Math.max(0, Math.min(0.995, (h - sailStart) / Math.max(1, legs[i].hours)));
    const f0 = cps[i].fuel_after_bunker_t ?? cps[i].fuel_t, f1 = cps[i + 1].fuel_t;
    const fuel = Math.max(0, f0 + (f1 - f0) * frac);
    return {resume: {route: cps.map(x => x.node), leg_index: i, fraction: Number(frac.toFixed(4)), elapsed_h: h, fuel_onboard_t: Math.round(fuel * 10) / 10},
            leg: legs[i], fuel, frac};
  }
  return null;
}
function edgePoints(a, b) {
  for (const e of state.network?.edges || []) {
    if (e.from === a && e.to === b) return e.points;
    if (e.from === b && e.to === a) return [...e.points].reverse();
  }
  return null;
}
function syncProgress() {
  const c = state.committed, el = $('progress');
  const maxDay = c ? Math.max(1, Math.floor(c.plan.summary.days * 2) / 2 - 0.5) : 40;
  el.max = maxDay; el.value = state.progressDay;
  $('progressMax').textContent = c ? `Day ${maxDay}` : 'Arrival';
  const s = shipState();
  $('progressLabel').textContent = s ? `Day ${state.progressDay} · ${fT(s.fuel)} on board` : 'In port';
  $('progressHint').textContent = s
    ? `At sea between ${s.leg.from_name} and ${s.leg.to_name} on the committed ${c.plan.corridor_label} plan. Add an event: Tideline re-plans from here.`
    : 'Slide to put the ship at sea on the current plan, then add an event: Tideline re-plans from where the ship is.';
  $('progressReset').hidden = !s;
}
function resetVoyage() { state.committed = null; state.progressDay = 0; syncProgress(); }
function onProgress(day) {
  if (day > 0 && !state.committed) {
    const a = activePlan();
    if (!a || !state.plan || state.plan.resume) { $('progress').value = 0; return; }
    state.committed = {plan: JSON.parse(JSON.stringify(a)), departure: state.plan.request.departure};
    syncProgress();
  }
  state.progressDay = day;
  if (day <= 0) { resetVoyage(); }
  syncProgress();
  schedulePlan(400);
}

// ------------------------------------------------------------------ chat
const TOGGLE_FOR = e => {
  if (e.type === 'zone_closure' && e.zone === 'bab') return 'blockBab';
  if (e.type === 'security' && e.zone === 'bab') return 'threatBab';
  if (e.type === 'cover_cancelled' && e.zone === 'bab') return 'coverBab';
  if (e.type === 'zone_closure' && e.zone === 'suez') return 'blockSuez';
  if (e.type === 'zone_closure' && e.zone === 'hormuz') return 'blockHormuz';
  if (e.type === 'storm' && e.id === 'cape-storm' && Number(e.hs_m) === 7.5) return 'stormCape';
  return null;
};
function renderExtraEvents() {
  const box = $('extraEvents'); if (!box) return;
  box.innerHTML = state.extraEvents.map((e, i) => `<span class="extra-event" title="${esc('Source: ' + (e.source || 'chat'))}"><span aria-hidden="true">${TI.severityIcon(['zone_closure', 'port_closure'].includes(e.type) ? 'critical' : 'warning')}</span> ${esc(e.description || e.type)}<button type="button" data-i="${i}" aria-label="Remove event">✕</button></span>`).join('');
  box.querySelectorAll('button').forEach(b => b.addEventListener('click', () => { state.extraEvents.splice(Number(b.dataset.i), 1); renderExtraEvents(); schedulePlan(); }));
}
function applyRequest(req) {
  const shipChanged = req.ship_id && req.ship_id !== state.ship;
  if (shipChanged || (req.origin && req.origin !== state.origin) || (req.destination && req.destination !== state.destination) || !req.resume) resetVoyage();
  if (req.ship_id) state.ship = req.ship_id;
  if (req.origin) state.origin = req.origin;
  if (req.destination) state.destination = req.destination;
  if (req.priority) state.priority = req.priority;
  if (req.conditions) state.conditions = req.conditions;
  if (req.cargo && req.cargo !== state.cargo) { state.cargo = req.cargo; pullNews(); }
  state.affiliations = Array.isArray(req.affiliations) ? [...req.affiliations] : [];
  state.weights = req.weights ? {...req.weights} : null;
  if (!req.resume) state.fuel = Number.isFinite(Number(req.fuel_onboard_t)) && req.fuel_onboard_t !== undefined ? Number(req.fuel_onboard_t) : (shipChanged ? shipObj().default_fuel_onboard_t : state.fuel);
  state.deadline = req.deadline_days ? String(req.deadline_days) : '';
  state.deadlineType = req.deadline_type || 'hard';
  state.berth = req.berth_ready_days && req.berth_source !== 'congestion' ? String(req.berth_ready_days) : '';
  const newToggles = {blockBab: false, threatBab: false, coverBab: false, allowUninsured: !!req.allow_uninsured, blockSuez: false, blockHormuz: false, closeWestAfrica: false, stormCape: false, liveWeather: req.live_weather !== false, autoNews: state.toggles.autoNews};
  const extras = [];
  const events = (req.events || []).filter(e => e.type !== 'news');
  const closedPorts = new Set(events.filter(e => e.type === 'port_closure').map(e => e.port));
  const westAfrica = closedPorts.has('DKR') && closedPorts.has('ABJ');
  if (westAfrica) newToggles.closeWestAfrica = true;
  for (const e of events) {
    const tg = TOGGLE_FOR(e);
    if (tg) newToggles[tg] = true;
    else if (!(westAfrica && e.type === 'port_closure' && (e.port === 'DKR' || e.port === 'ABJ'))) extras.push(e);
  }
  state.toggles = newToggles; state.extraEvents = extras;
  syncControls();
}
function syncControls() {
  $('ship').value = state.ship; $('origin').value = state.origin; $('destination').value = state.destination;
  syncShip(); syncLane();
  $('deadline').value = state.deadline; $('deadlineType').value = state.deadlineType; $('berth').value = state.berth;
  for (const id of TOGGLES) $(id).checked = !!state.toggles[id];
  document.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === state.priority));
  syncConditions();
  $('cargo').value = state.cargo; syncWeights();
  $('affSaudi').checked = state.affiliations.includes('saudi'); $('affIran').checked = state.affiliations.includes('iran');
  renderExtraEvents();
}
function chatBubble(role, text, meta = []) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + (role === 'user' ? 'user' : 'bot');
  div.textContent = text;
  if (meta.length) {
    const m = document.createElement('div'); m.className = 'chat-meta';
    m.innerHTML = meta.map(([cls, t]) => `<span class="chat-chip ${cls}">${esc(t)}</span>`).join('');
    div.appendChild(m);
  }
  $('chatLog').appendChild(div); $('chatLog').scrollTop = $('chatLog').scrollHeight;
  return div;
}
async function sendChat(message) {
  message = message.trim(); if (!message || state.chat.busy) return;
  state.chat.busy = true; $('chatSend').disabled = true; $('chatInput').value = '';
  chatBubble('user', message.length > 400 ? message.slice(0, 400) + '…' : message);
  const thinking = chatBubble('bot', 'Working: reading the request and running the optimizer…');
  const rec = state.plan?.recommended;
  const brief = rec ? {corridor: rec.corridor_label, days: rec.summary.days, cash_cost_usd: rec.costs.cash_total_usd, risk_score_0_100: Math.round(rec.summary.route_risk * 100)} : null;
  try {
    const res = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, cache: 'no-store',
      body: JSON.stringify({message, current: currentRequest(), plan: brief, history: state.chat.history.slice(-8)})});
    const json = await res.json();
    if (!res.ok) throw Error(json.error || `HTTP ${res.status}`);
    thinking.remove();
    const meta = [[json.mode === 'gpt' ? 'ok' : '', json.mode === 'gpt' ? `GPT · ${json.model}` : 'Offline parser']];
    if (json.plan) meta.push(json.untraced_numbers?.length ? ['warn', '⚠ Untraced figures: ' + json.untraced_numbers.join(', ')] : ['ok', '✓ Every figure traced to the planner']);
    if (json.note) meta.push(['warn', json.note]);
    chatBubble('bot', json.reply || '(no reply)', meta);
    state.chat.history.push({role: 'user', content: message}, {role: 'assistant', content: json.reply || ''});
    if (json.request && state.mode === 'fleet') {
      applyRequest(json.request); schedulePlan(0);   // the fleet re-plans with the updated shared settings
    } else if (json.request && json.plan) {
      state.reqVersion++;              // cancel any plan request already in flight
      clearTimeout(planTimer);
      applyRequest(json.request);
      state.plan = json.plan; state.planStatus = 'ok'; state.planError = null; state.preview = null;
      render(); pullWeather();
    }
  } catch (e) {
    thinking.remove();
    chatBubble('bot', 'Sorry, that failed: ' + e.message, [['warn', 'error']]);
  } finally {
    state.chat.busy = false; $('chatSend').disabled = false; $('chatInput').focus();
    if (state.ops) pollOps();   // the copilot can report events to approved voyages
  }
}
function attachChat() {
  $('chatForm').addEventListener('submit', e => { e.preventDefault(); sendChat($('chatInput').value); });
  $('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat($('chatInput').value); } });
  $('chatSuggest').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.report) { $('chatInput').value = SAMPLE_REPORT; $('chatInput').focus(); return; }
    sendChat(b.textContent);
  }));
}
let lastWeatherStamp = null;
async function pollWeatherStatus() {
  try {
    const s = await (await fetch('/api/weather-status', {cache: 'no-store'})).json();
    state.liveField = s;
    const stamp = s.status === 'live' ? s.retrieved_at : null;
    renderLiveFieldBadge();
    if (stamp && stamp !== lastWeatherStamp) {
      const first = lastWeatherStamp === null;
      lastWeatherStamp = stamp;
      if (state.toggles.liveWeather && state.plan) {
        if (!first || !state.plan.weather?.used) {
          chatBubble('bot', `Live forecast ${first ? 'loaded' : 'updated'} (Open-Meteo, ${s.points_with_data}/${s.points} points). Re-planning with the new waves, wind and currents.`, [['ok', 'Monitor']]);
          schedulePlan(0);
        }
      }
    }
  } catch (e) { /* server restarting; try again next tick */ }
}
function renderLiveFieldBadge() {
  const s = state.liveField || {}, el = $('liveWeatherDesc');
  if (!el) return;
  el.textContent = s.status === 'live' ? `Open-Meteo waves, wind, currents · updated ${fmtEta(s.retrieved_at)} UTC`
    : s.status === 'loading' ? 'Fetching Open-Meteo forecast…' : 'Forecast unavailable · seasonal sample used';
}
async function loadAiStatus() {
  try {
    const h = await (await fetch('/api/health', {cache: 'no-store'})).json();
    const gpt = h.ai?.mode === 'gpt';
    $('aiBadge').textContent = gpt ? 'GPT · ' + h.ai.model : 'OFFLINE PARSER';
    $('aiBadge').className = 'live-chip' + (gpt ? '' : ' offline');
    $('aiBadge').title = gpt ? 'Requests are interpreted by OpenAI; numbers come from the planner.' : 'No OPENAI_API_KEY set: a built-in parser interprets requests.';
  } catch (e) { $('aiBadge').textContent = 'AI UNKNOWN'; }
}

// ------------------------------------------------------------------ controls
function fillSelect(sel, options, value) {
  sel.innerHTML = options.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join('');
  sel.value = value;
}
function syncLane() {
  const i = LANES.findIndex(([o, d]) => o === state.origin && d === state.destination);
  $('lane').value = i >= 0 ? String(i) : 'custom';
}
function syncShip() {
  const s = shipObj(); if (!s) return;
  $('shipHint').textContent = `${s.type} · draft ${s.draft_m} m · tank ${fT(s.fuel_capacity_t)} · ${s.min_speed_kn}–${s.max_speed_kn} kn · sample profile`;
  if (state.calibration !== undefined) renderCalibration();
  const fuel = $('fuel');
  fuel.max = s.fuel_capacity_t;
  state.fuel = Math.min(state.fuel, s.fuel_capacity_t);
  fuel.value = state.fuel;
  $('fuelLabel').textContent = fT(state.fuel);
  $('fuelMax').textContent = fT(s.fuel_capacity_t);
}
function syncConditions() {
  document.querySelectorAll('[data-cond]').forEach(b => {
    const on = b.dataset.cond === state.conditions;
    b.classList.toggle('active', on);
    b.textContent = (on ? '◉ ' : '○ ') + (b.dataset.cond === 'today' ? 'Today' + (state.today ? ' · ' + fmtDay(state.today.as_of).replace(/, \d{4}$/, '') : '') : 'Normal times');
  });
  $('condHint').textContent = state.conditions === 'today'
    ? 'Sourced snapshot of real conditions: fuel prices, war-risk premiums, security, live chokepoint traffic.'
    : 'Pre-crisis baseline: sample prices and normal-times risk. Good for showing classic disruption scenarios.';
}
const fmtDay = iso => { const d = new Date(iso + 'T00:00:00Z'); return Number.isNaN(d.valueOf()) ? iso : d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'}); };
function setConditions(mode) { state.conditions = mode; syncConditions(); schedulePlan(); }
function srcItem(title, tag, note, url, date) {
  return `<div class="src"><div class="src-top"><b>${esc(title)}</b><span class="src-tag ${esc(tag)}">${esc(tag)}</span></div><div class="src-note">${esc(note)}${date ? ' · ' + esc(date) : ''}${url ? ` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">source ↗</a>` : ''}</div></div>`;
}
function renderSources() {
  const box = $('sourcesList'); if (!box) return;
  const p = state.plan; if (!p) return;
  const c = p.conditions || {}, w = p.weather || {}, out = [];
  $('srcBadge').textContent = c.mode === 'today' ? 'TODAY · ' + (c.as_of || '') : 'NORMAL TIMES';
  $('srcBadge').className = 'live-chip' + (c.mode === 'today' ? '' : ' offline');
  out.push(srcItem('Ocean conditions', w.used ? 'live' : 'sample', w.used ? `Open-Meteo waves, wind, currents · first 7 days of the voyage · updated ${fmtEta(w.retrieved_at)} UTC; seasonal sample after` : 'Seasonal sample wave heights (live forecast off or unavailable)', 'https://open-meteo.com/en/docs/marine-weather-api'));
  if (c.mode === 'today') {
    const fp = Object.entries(state.today?.fuel_prices || {});
    const list = basis => fp.filter(([, v]) => basis.includes(v.basis)).map(([code, v]) => `${esc(portName(code))} $${Math.round(v.price).toLocaleString('en-US')}`).join(' · ');
    const nEst = fp.filter(([, v]) => v.basis === 'estimated').length;
    out.push(`<div class="src"><div class="src-top"><b>Fuel prices (VLSFO, $/t)</b><span class="src-tag published">${fp.filter(([, v]) => v.basis === 'published').length} published</span></div>
      <div class="src-note"><b>Published:</b> ${list(['published'])} · <a href="https://www.bunkerindex.com/" target="_blank" rel="noopener noreferrer">Bunker Index ↗</a> · 16 Oct 2026<br>
      <b>Derived / indicative:</b> ${list(['derived', 'indicative'])} · <a href="https://www.engine.online/news/europe-and-africa-market-update-16-oct-82d6" target="_blank" rel="noopener noreferrer">ENGINE ↗</a><br>
      <span class="src-tag estimated">estimated</span> ${nEst} other ports: nearest benchmark plus a sample price differential (hover a port on the map)</div></div>`);
    for (const i of (c.items || []).filter(i => i.kind !== 'fuel_prices')) {
      const tag = i.kind === 'traffic' ? (i.live ? 'live' : 'published') : i.kind === 'security' ? 'assessment' : i.kind === 'advisory' ? 'official' : (i.stale ? 'stale' : 'published');
      out.push(srcItem(i.description, tag, i.source, i.url, i.date));
    }
    const adv = c.advisories || {};
    if (!(c.items || []).some(i => i.kind === 'advisory')) out.push(srcItem('Official navigational warnings (NGA)', adv.status === 'live' ? 'official' : 'sample', adv.status === 'live' ? `No active security warnings on these risk zones · checked ${fmtEta(adv.retrieved_at)} UTC` : 'Feed unavailable right now' + (adv.error ? ' (' + adv.error + ')' : ''), 'https://msi.nga.mil/NavWarnings'));
    const pw = c.portwatch || {};
    if (pw.status !== 'live') out.push(srcItem('Chokepoint traffic (IMF PortWatch)', 'sample', pw.status === 'loading' ? 'Fetching live transit counts…' : 'Live feed unavailable; published estimate used where available' + (pw.error ? ' (' + pw.error + ')' : ''), 'https://portwatch.imf.org/'));
  } else {
    out.push(srcItem('Fuel prices, war-risk, security', 'sample', 'Normal-times baseline: Tideline sample values. Switch to Today for sourced data.'));
  }
  out.push(p.fuel_model?.ship_model === 'calibrated' ? srcItem('Fuel burn', 'published', `Regression calibrated on ${p.fuel_model.reports} noon reports for this ship (see Advanced)`) : srcItem('Fuel burn', 'simulated', 'Regression trained on simulated data from ship physics (Admiralty law); calibrate on noon reports in Advanced'));
  out.push(srcItem('Ships, port fees, waiting times, draft limits', 'sample', 'Illustrative profiles and port data; a customer would supply their own'));
  out.push(srcItem('Sea lanes and distances', 'published', 'Lanes follow the Eurostat SeaRoute MARNET maritime network (via searoute-py); Singapore→Rotterdam via Suez 8,494 nm vs about 8,440 nm published (Carra Globe, Sep 2026)', 'https://github.com/genthalili/searoute-py'));
  box.innerHTML = out.join('');
}
function currentWeights() {
  const pre = state.network?.presets?.[state.priority] || {};
  return state.weights || {risk_loss_frac: pre.risk_loss_frac, carbon_usd_per_t: pre.carbon_usd_per_t, time_value_mult: pre.time_value_mult};
}
const RISK_MAX = 0.03;   // slider 100 = 3% of cargo value per unit of route risk
function syncWeights() {
  const w = currentWeights(), s = shipObj();
  if (w.risk_loss_frac == null) return;
  $('wRisk').value = Math.round(w.risk_loss_frac / RISK_MAX * 100);
  $('wCarbon').value = w.carbon_usd_per_t;
  $('wTime').value = w.time_value_mult;
  const perPoint = s ? w.risk_loss_frac * s.cargo_value_usd / 100 : 0;
  $('wRiskVal').textContent = `${Math.round(w.risk_loss_frac / RISK_MAX * 100)} · ≈${fCompact(perPoint)} per risk point`;
  $('wCarbonVal').textContent = `$${Math.round(w.carbon_usd_per_t)}/t CO₂`;
  $('wTimeVal').textContent = `${Number(w.time_value_mult).toFixed(2)}× · ${s ? fCompact(w.time_value_mult * s.daily_cost_usd) + '/day' : ''}`;
  $('objectives').classList.toggle('custom', !!state.weights);
  $('weightsHint').textContent = state.weights ? 'Custom weights in use. Click a preset to go back.' : 'Presets set these. Move one to weigh cost, carbon and risk your own way.';
}
function onWeight() {
  state.weights = {risk_loss_frac: Number($('wRisk').value) / 100 * RISK_MAX, carbon_usd_per_t: Number($('wCarbon').value), time_value_mult: Number($('wTime').value)};
  syncWeights(); schedulePlan(350);
}
function setMode(mode) {
  state.priority = mode; state.weights = null;
  document.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  syncWeights();
  schedulePlan();
}
function attachControls() {
  const ports = Object.entries(state.network.ports).map(([code, p]) => [code, p.name]).sort((a, b) => a[1].localeCompare(b[1]));
  fillSelect($('lane'), [...LANES.map(([, , label], i) => [String(i), label]), ['custom', 'Custom route']], '0');
  fillSelect($('ship'), state.fleet.ships.map(s => [s.id, `${s.name} · ${s.type.split(' · ')[1] || s.type}`]), state.ship);
  fillSelect($('origin'), ports, state.origin);
  fillSelect($('destination'), ports, state.destination);
  syncShip(); syncLane();

  $('lane').addEventListener('change', e => {
    const lane = LANES[Number(e.target.value)]; if (!lane) return;
    [state.origin, state.destination] = lane; $('origin').value = state.origin; $('destination').value = state.destination; resetVoyage(); schedulePlan();
  });
  $('ship').addEventListener('change', e => {
    state.ship = e.target.value; state.fuel = shipObj().default_fuel_onboard_t; state.cargo = shipObj().default_cargo || state.cargo; $('cargo').value = state.cargo;
    syncShip(); syncWeights(); resetVoyage(); pullNews(); schedulePlan();
  });
  $('progress').addEventListener('input', e => onProgress(Number(e.target.value)));
  for (const id of ['wRisk', 'wCarbon', 'wTime']) $(id).addEventListener('input', onWeight);
  for (const [id, key] of [['affSaudi', 'saudi'], ['affIran', 'iran']]) $(id).addEventListener('change', e => {
    state.affiliations = state.affiliations.filter(x => x !== key).concat(e.target.checked ? [key] : []); schedulePlan();
  });
  attachFleet(); attachAis();
  fillSelect($('cargo'), Object.entries(state.network.cargo_segments), state.cargo);
  $('cargo').addEventListener('change', e => { state.cargo = e.target.value; pullNews(); schedulePlan(); });
  $('progressReset').addEventListener('click', () => { resetVoyage(); schedulePlan(); });
  for (const id of ['origin', 'destination']) {
    $(id).addEventListener('change', e => {
      state[id] = e.target.value;
      if (state.origin === state.destination) { $('metricRouteSub').textContent = 'Choose two different ports'; return; }
      syncLane(); resetVoyage(); schedulePlan();
    });
  }
  document.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
  document.querySelectorAll('[data-cond]').forEach(b => b.addEventListener('click', () => setConditions(b.dataset.cond)));
  $('fuel').addEventListener('input', e => { state.fuel = Number(e.target.value); $('fuelLabel').textContent = fT(state.fuel); resetVoyage(); schedulePlan(350); });
  $('deadline').addEventListener('input', e => { state.deadline = e.target.value.trim(); schedulePlan(500); });
  $('berth').addEventListener('input', e => { state.berth = e.target.value.trim(); schedulePlan(500); });
  $('deadlineType').addEventListener('change', e => { state.deadlineType = e.target.value; if (state.deadline) schedulePlan(); });
  for (const id of TOGGLES) $(id).addEventListener('change', e => { state.toggles[id] = e.target.checked; schedulePlan(); });
  $('refreshFeeds').addEventListener('click', () => { pullNews(true); pullWeather(true); });

  if (innerWidth >= 900 && $('mapLegend')) $('mapLegend').open = true;   // collapsible; open on wide screens
  $('zoomIn').addEventListener('click', () => zoom(.7)); $('zoomOut').addEventListener('click', () => zoom(1 / .7));
  $('zoomReset').addEventListener('click', () => { if (lm) { fitHome(); return; } state.mapView = {x: 0, y: 0, w: W, h: H}; updateView(); });
  const svg = $('worldMap'), ptrs = new Map(); let pan = null, pinch = null;
  const mapPoint = (cx, cy) => { const p = svg.createSVGPoint(); p.x = cx; p.y = cy; return p.matrixTransform(svg.getScreenCTM().inverse()); };
  // Zoom about the cursor (not the map centre), so scrolling over Asia zooms into Asia.
  svg.addEventListener('wheel', e => {
    if (lm) return;
    e.preventDefault();
    const p = mapPoint(e.clientX, e.clientY);
    zoomTo(state.mapView.w * Math.exp(Math.max(-80, Math.min(80, e.deltaY)) * 0.0022), p.x, p.y);
  }, {passive: false});
  svg.addEventListener('dblclick', e => { if (lm) return; const p = mapPoint(e.clientX, e.clientY); zoomTo(state.mapView.w * .5, p.x, p.y); });
  svg.addEventListener('pointerdown', e => {
    if (lm || e.button > 0) return;
    ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (ptrs.size === 1) pan = {x: e.clientX, y: e.clientY, v: {...state.mapView}, s: svg.getScreenCTM().a, on: false};
    else if (ptrs.size === 2) { pan = null; const [p, q] = [...ptrs.values()]; pinch = {d: Math.hypot(p.x - q.x, p.y - q.y) || 1, w: state.mapView.w}; }
  });
  svg.addEventListener('pointermove', e => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pinch && ptrs.size === 2) {
      const [p, q] = [...ptrs.values()], c = mapPoint((p.x + q.x) / 2, (p.y + q.y) / 2);
      zoomTo(pinch.w * pinch.d / (Math.hypot(p.x - q.x, p.y - q.y) || 1), c.x, c.y); return;
    }
    if (!pan) return;
    const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
    if (!pan.on) {   // a few pixels of movement before it counts as a drag, so clicks on ships and ports still work
      if (Math.hypot(dx, dy) < 4) return;
      pan.on = true; svg.classList.add('panning');
      try { svg.setPointerCapture(e.pointerId); } catch { /* pointer already gone: keep panning without capture */ }
    }
    state.mapView = TLMapView.panView({...pan.v, x: pan.v.x - dx / pan.s, y: pan.v.y - dy / pan.s}, 0, 0, W, H); updateView();
  });
  const endPtr = e => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null; if (!ptrs.size) { pan = null; svg.classList.remove('panning'); } };
  svg.addEventListener('pointerup', endPtr); svg.addEventListener('pointercancel', endPtr);
}
function zoomTo(w, ax, ay) { state.mapView = TLMapView.zoomView(state.mapView, w, ax, ay, W, H); updateView(); }
function zoom(factor) { if (lm) { if (factor < 1) lm.map.zoomIn(); else lm.map.zoomOut(); return; } zoomTo(state.mapView.w * factor); }
function updateView() {
  if (lm) return;   // the Leaflet map keeps its own view
  const v = state.mapView; $('worldMap').setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
  moveVessels(); if (state.ais.on) drawAis(); drawWarnMarkers(); syncSimUi();   // the speed label shows the zoom multiplier   // ship icons keep their on-screen size at every zoom
}
function timeTick() {
  const d = new Date();
  $('utcClock').textContent = d.toLocaleTimeString('en-GB', {timeZone: 'UTC', hour12: false, hour: '2-digit', minute: '2-digit'});
  $('ovDate').textContent = d.toLocaleDateString('en-GB', {timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'});
}

// ------------------------------------------------------------------ fleet planning
const FLEET_COLORS = ['#0d9488', '#d97706', '#2563eb', '#c026d3', '#65a30d', '#dc2626'];
function fleetColor(id) { const i = state.fleet.ships.findIndex(s => s.id === id); return FLEET_COLORS[Math.max(0, i) % FLEET_COLORS.length]; }
function attachFleet() {
  state.fleetShips = Object.fromEntries(state.fleet.ships.map((s, i) => [s.id, {on: i < 3, fuel: s.default_fuel_onboard_t}]));
  renderFleetPicker();
  document.querySelectorAll('[data-pmode]').forEach(b => b.addEventListener('click', () => setPlanMode(b.dataset.pmode)));
  $('fleetGo').addEventListener('click', () => schedulePlan(0));
  for (const sh of state.fleet.ships) state.assign.at[sh.id] = sh.home_port || 'SIN';
  $('assignAdd').addEventListener('click', () => { if (state.assign.jobs.length < 4) { state.assign.jobs.push({origin: 'SIN', destination: 'RTM', cargo: 'retail', deadline: ''}); renderAssignForm(); } });
  $('assignGo').addEventListener('click', requestAssign);
  renderAssignForm();
}
function renderFleetPicker() {
  $('fleetShips').innerHTML = state.fleet.ships.map((s, i) => `<label class="fleet-ship"><input type="checkbox" data-fs="${esc(s.id)}" ${state.fleetShips[s.id]?.on ? 'checked' : ''}><span class="sw" style="background:${FLEET_COLORS[i % FLEET_COLORS.length]}"></span>${esc(s.name)}<input type="number" min="0" max="${s.fuel_capacity_t}" step="50" value="${state.fleetShips[s.id]?.fuel}" data-ff="${esc(s.id)}" aria-label="Fuel on board for ${esc(s.name)} (t)"></label>`).join('');
  $('fleetShips').querySelectorAll('[data-fs]').forEach(el => el.addEventListener('change', e => { state.fleetShips[el.dataset.fs].on = e.target.checked; }));
  $('fleetShips').querySelectorAll('[data-ff]').forEach(el => el.addEventListener('change', e => { state.fleetShips[el.dataset.ff].fuel = Math.max(0, Number(e.target.value) || 0); }));
}
function setPlanMode(mode) {
  state.mode = mode;
  document.querySelectorAll('[data-pmode]').forEach(b => { const on = b.dataset.pmode === mode; b.classList.toggle('active', on); b.textContent = (on ? '◉ ' : '○ ') + (b.dataset.pmode === 'fleet' ? 'Fleet' : 'Single voyage'); });
  $('fleetPick').hidden = mode !== 'fleet';
  $('fleetPanel').hidden = mode !== 'fleet';
  $('assignPanel').hidden = mode !== 'fleet' || !state.assign.result;
  document.querySelectorAll('.progress-block').forEach(el => { el.hidden = mode === 'fleet'; });
  $('ship').closest('.field-block').hidden = mode === 'fleet';
  $('fuel').closest('.field-block').hidden = mode === 'fleet';
  resetVoyage(); state.startAt = null; renderStartPill();
  if (mode === 'single') { state.fleetPlan = null; state.fleetSel = null; }
  schedulePlan(0);
}
// ------------------------------------------------------------------ fuel model calibration (noon reports)
function attachCalibration() {
  $('calFile').addEventListener('change', e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 120000) { $('calInfo').textContent = 'File too large (limit 120 kB).'; return; }
    const rd = new FileReader();
    rd.onload = () => calibrate({csv: rd.result, file_name: f.name});
    rd.readAsText(f); e.target.value = '';
  });
  $('calSample').addEventListener('click', async () => {
    const csv = await (await fetch(`/api/calibrate/sample?ship_id=${encodeURIComponent(state.ship)}`)).text();
    calibrate({csv, file_name: 'SYNTHETIC sample reports'});
  });
  $('calReset').addEventListener('click', () => calibrate({reset: true}));
  pullCalibration();
}
async function pullCalibration() {
  try { state.calibration = await (await fetch('/api/calibration', {cache: 'no-store'})).json(); } catch (e) { state.calibration = null; }
  renderCalibration();
}
async function calibrate(body) {
  $('calInfo').textContent = 'Training on the reports…';
  try { await postJson('/api/calibrate', {ship_id: state.ship, ...body}); await pullCalibration(); schedulePlan(0); }
  catch (e) { $('calInfo').textContent = 'Not calibrated: ' + e.message; }
}
function renderCalibration() {
  const d = state.calibration?.[state.ship];
  $('calDownload').href = `/api/calibrate/sample?ship_id=${encodeURIComponent(state.ship)}`;
  if (!d) { $('calState').textContent = 'unavailable'; return; }
  const cal = d.source === 'calibrated';
  $('calState').textContent = cal ? `calibrated · ${d.reports} reports` : 'simulated training data';
  $('calReset').hidden = !cal;
  $('calInfo').innerHTML = cal ? `Trained on ${d.reports} noon reports${d.file_name ? ' (' + esc(d.file_name) + ')' : ''}. Error on the ${d.holdout_reports} held-out reports: <b>${(d.holdout_error_before * 100).toFixed(1)}%</b> with the simulated model → <b>${(d.holdout_error_after * 100).toFixed(1)}%</b> calibrated. This ship burns ${((d.fuel_vs_prior - 1) * 100).toFixed(0)}% ${d.fuel_vs_prior >= 1 ? 'more' : 'less'} than the textbook model predicted.${d.kept_prior.length ? ' Kept prior values for: ' + esc(d.kept_prior.join(', ')) + ' (too little variation in the reports).' : ''}`
    : 'Upload the ship\'s noon reports (CSV: speed_kn, distance_nm, fuel_t; optional displacement_t, hs_m, headwind_ms, current_kn) to retrain its fuel model on real data.';
}
function renderAssignForm() {
  const ports = Object.entries(state.network.ports).map(([c, p]) => [c, p.name]);
  const opt = (list, v) => list.map(([k, l]) => `<option value="${esc(k)}"${k === v ? ' selected' : ''}>${esc(l)}</option>`).join('');
  const cargos = Object.entries(state.network.cargo_segments || {}).map(([k, l]) => [k, l.split(' ')[0]]);
  $('assignJobs').innerHTML = state.assign.jobs.map((j, i) => `<div class="assign-row" data-j="${i}"><select data-k="origin" aria-label="Job ${i + 1} from">${opt(ports, j.origin)}</select><select data-k="destination" aria-label="Job ${i + 1} to">${opt(ports, j.destination)}</select><select data-k="cargo" aria-label="Job ${i + 1} cargo">${opt(cargos, j.cargo)}</select><input data-k="deadline" type="number" min="1" max="120" placeholder="days" value="${esc(j.deadline)}" aria-label="Job ${i + 1} deadline (days)"><button type="button" data-rm="${i}" aria-label="Remove job ${i + 1}">✕</button></div>`).join('');
  $('assignJobs').querySelectorAll('[data-k]').forEach(el => el.addEventListener('change', () => { state.assign.jobs[Number(el.closest('[data-j]').dataset.j)][el.dataset.k] = el.value; }));
  $('assignJobs').querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => { if (state.assign.jobs.length > 1) { state.assign.jobs.splice(Number(b.dataset.rm), 1); renderAssignForm(); } }));
  $('assignShips').innerHTML = state.fleet.ships.map(sh => `<label class="assign-ship"><span>${esc(sh.name.replace('MV ', ''))}</span><select data-at="${esc(sh.id)}" aria-label="${esc(sh.name)} is at">${opt(ports, state.assign.at[sh.id])}</select></label>`).join('');
  $('assignShips').querySelectorAll('[data-at]').forEach(el => el.addEventListener('change', () => { state.assign.at[el.dataset.at] = el.value; }));
}
async function requestAssign() {
  const ships = Object.entries(state.fleetShips).filter(([, v]) => v.on).map(([id, v]) => ({ship_id: id, fuel_onboard_t: v.fuel, at: state.assign.at[id]}));
  const jobs = state.assign.jobs.map((j, i) => ({id: `J${i + 1}`, origin: j.origin, destination: j.destination, cargo: j.cargo, deadline_days: j.deadline ? Number(j.deadline) : null, deadline_type: 'soft'}));
  $('assignPanel').hidden = false; state.assign.busy = true; state.assign.error = null; renderAssign();
  try {
    const body = {jobs, ships, priority: state.priority, conditions: state.conditions, live_weather: state.toggles.liveWeather, events: buildEvents()};
    if (state.weights) body.weights = state.weights;
    state.assign.result = await postJson('/api/assign', body);
  } catch (e) { state.assign.error = e.message; }
  state.assign.busy = false; renderAssign();
  $('assignPanel').scrollIntoView({behavior: 'smooth', block: 'start'});
}
function renderAssign() {
  const a = state.assign, box = $('assignBody');
  if (a.busy) { $('assignMeta').textContent = 'Planning every ship-job pair…'; box.innerHTML = '<div class="cost-note"><span class="spinner"></span>Running the optimizer for each compatible pair (about 1 s each)…</div>'; return; }
  if (a.error) { $('assignMeta').textContent = 'Error'; box.innerHTML = `<div class="ops-alert critical"><span>${esc(a.error)}</span></div>`; return; }
  const r = a.result; if (!r) return;
  const jobName = id => { const j = r.jobs.find(x => x.id === id); return `${id}: ${portName(j.origin)} → ${portName(j.destination)}`; };
  $('assignMeta').textContent = `${r.totals.jobs_served} of ${r.jobs.length} jobs served · ${r.plans_run} plans run`;
  const pick = new Set(r.assignment.map(x => x.ship_id + '|' + x.job));
  const head = `<tr><th>Ship (now at)</th>${r.jobs.map(j => `<th>${esc(jobName(j.id))}<br><span class="small">${esc(state.network.cargo_segments[j.cargo] || j.cargo)}${j.deadline_days ? ' · ' + j.deadline_days + ' d' : ''}</span></th>`).join('')}</tr>`;
  const rows = r.ships.map(sh => `<tr><td>${esc(sh.name)}<br><span class="small">${esc(portName(sh.at))}</span></td>${r.jobs.map(j => {
    const c = r.matrix.find(m => m.ship_id === sh.ship_id && m.job === j.id);
    if (!c.feasible) return `<td class="assign-cell"><span class="no">✕ ${esc(c.reason)}</span></td>`;
    return `<td class="assign-cell ${pick.has(sh.ship_id + '|' + j.id) ? 'pick' : ''}">${pick.has(sh.ship_id + '|' + j.id) ? '<b>✓ assigned</b><br>' : ''}${fUSD(c.cash_usd)} · ${c.total_days.toFixed(1)} d${c.positioning_days ? `<br><span class="small">incl. ${c.positioning_days.toFixed(1)} d positioning</span>` : ''}${c.late_days > 0 ? `<br><span class="cf-tag warn">${c.late_days.toFixed(1)} d late</span>` : ''}</td>`;
  }).join('')}</tr>`).join('');
  const save = r.saving_vs_rule_of_thumb_usd;
  box.innerHTML = `<div class="fleet-totals"><div><small>Total cash</small><b>${fUSD(r.totals.cash_usd)}</b></div><div><small>Decision score</small><b>${fUSD(r.totals.score_usd)}</b></div><div><small>CO₂</small><b>${fT(r.totals.co2_t)}</b></div><div><small>Jobs served</small><b>${r.totals.jobs_served} / ${r.jobs.length}</b></div><div><small>${save != null && save < 0 ? 'Costs more than rule of thumb' : 'Saved vs rule of thumb'}</small><b>${save != null ? fUSD(Math.abs(save)) : '—'}</b></div></div>
    <div class="table-wrap"><table class="cp-table">${head}${rows}</table></div>
    <div class="fleet-notes"><div>Assigned: ${r.assignment.map(x => `<b>${esc(x.ship_name)}</b> → ${esc(jobName(x.job))} (${esc(x.corridor)})`).join('; ') || 'none'}.${r.unserved.length ? ` Not served: ${r.unserved.map(esc).join(', ')}.` : ''}</div>
    <div>Rule of thumb (${esc(r.rule_of_thumb.rule)}): ${r.rule_of_thumb.assignment.map(x => `${esc(x.ship_name)} → ${esc(x.job)}`).join(', ') || 'none'} · score ${fUSD(r.rule_of_thumb.score_usd)}.</div><div>${esc(r.method)}</div></div>`;
}
async function requestFleet() {
  const ships = Object.entries(state.fleetShips).filter(([, v]) => v.on).map(([id, v]) => ({ship_id: id, fuel_onboard_t: v.fuel}));
  if (!ships.length) { state.planStatus = 'error'; state.planError = 'Pick at least one ship for the fleet'; render(); return; }
  const version = ++state.reqVersion;
  state.planStatus = 'loading'; renderStatus();
  $('fleetMeta').textContent = `Optimizing ${ships.length} ships and coordinating refueling…`;
  const base = currentRequest();
  for (const k of ['ship_id', 'fuel_onboard_t', 'resume', 'start_at', 'cargo']) delete base[k];
  try {
    const res = await fetch('/api/fleet-plan', {method: 'POST', headers: {'Content-Type': 'application/json'}, cache: 'no-store', body: JSON.stringify({...base, ships})});
    const json = await res.json();
    if (version !== state.reqVersion) return;
    if (!res.ok) throw Error(json.error || `HTTP ${res.status}`);
    state.fleetPlan = json; state.planStatus = 'ok'; state.planError = null; state.preview = null;
    if (!json.ships.some(s => s.ship_id === state.fleetSel)) state.fleetSel = (json.ships.find(s => s.result.recommended) || json.ships[0])?.ship_id;
  } catch (e) {
    if (version !== state.reqVersion) return;
    state.planStatus = 'error'; state.planError = e.message;
  }
  render(); pullWeather();
}
function renderFleet() {
  if (state.mode !== 'fleet') return;
  const f = state.fleetPlan, body = $('fleetBody');
  if (!f) { body.innerHTML = '<div class="empty-feed">Pick ships and press Plan fleet.</div>'; return; }
  const t = f.totals;
  $('fleetMeta').textContent = `${t.feasible}/${t.ships} ships planned · ${f.rounds} coordination round${f.rounds === 1 ? '' : 's'}`;
  const rows = f.ships.map(s => {
    const r = s.result.recommended, col = fleetColor(s.ship_id);
    if (!r) return `<tr data-ship="${esc(s.ship_id)}" class="${s.ship_id === state.fleetSel ? 'sel' : ''}"><td><span class="sw" style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${col}"></span> ${esc(s.name)}</td><td colspan="6" style="color:#dc2626">${esc(s.result.infeasible?.message || 'No feasible plan')}</td></tr>`;
    return `<tr data-ship="${esc(s.ship_id)}" class="${s.ship_id === state.fleetSel ? 'sel' : ''}"><td><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${col}"></span> ${esc(s.name)}</td><td>${esc(r.corridor_label.replace('Via ', ''))}</td><td class="num">${r.summary.days.toFixed(1)} d</td><td>${esc(fmtEta(r.summary.eta))}</td><td>${esc(r.bunkering.map(b => `${b.name} ${fT(b.buy_t)}`).join(', ') || '—')}</td><td class="num">${fCompact(r.costs.cash_total_usd)}</td><td class="num">${Math.round(r.summary.route_risk * 100)}</td></tr>`;
  }).join('');
  body.innerHTML = `<div class="fleet-totals"><div><small>Fleet cash cost</small><b>${fCompact(t.cash_usd)}</b></div><div><small>Fuel burned</small><b>${fT(t.fuel_t)}</b></div><div><small>CO₂</small><b>${fT(t.co2_t)}</b></div><div><small>EU ETS</small><b>${fCompact(t.ets_usd)}</b></div><div><small>Last arrival</small><b>${t.latest_eta ? esc(fmtEta(t.latest_eta)) : '—'}</b></div></div>
    <div class="table-wrap"><table class="cp-table fleet-table"><thead><tr><th>Ship</th><th>Route</th><th class="num">Transit</th><th>ETA (UTC)</th><th>Refuel</th><th class="num">Cash</th><th class="num">Risk</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="fleet-notes">${(f.coordination.length ? f.coordination : [{text: 'No refueling clashes: every ship keeps its individually optimal plan.'}]).map(n => `<div>◇ ${esc(n.text)}</div>`).join('')}<div style="color:#64748b">${esc(f.assumptions.join(' '))} Click a ship for its full plan.</div></div>`;
  body.querySelectorAll('tr[data-ship]').forEach(tr => tr.addEventListener('click', () => { state.fleetSel = tr.dataset.ship; state.preview = null; render(); pullWeather(); }));
}
function drawFleetPaths() {
  const g = $('fleetSvg'); if (!g) return; g.innerHTML = '';
  if (state.mode !== 'fleet' || !state.fleetPlan) return;
  for (const s of state.fleetPlan.ships) {
    const r = s.result.recommended; if (!r || s.ship_id === state.fleetSel) continue;
    g.appendChild(addTitle(svgEl('path', {d: pathString(r.path), fill: 'none', stroke: fleetColor(s.ship_id), 'stroke-width': 2, opacity: .75, 'stroke-linecap': 'round', 'stroke-dasharray': '1 0'}), `${s.name}: ${r.corridor_label}, ${r.summary.days.toFixed(1)} days, ${fUSD(r.costs.cash_total_usd)} (click the table row for details)`));
    for (const b of r.bunkering) {
      const [x, y] = project(state.network.ports[b.port].loc);
      g.appendChild(addTitle(svgEl('circle', {cx: x + 4, cy: y - 4, r: 3.5, fill: fleetColor(s.ship_id), stroke: '#ffffff', 'stroke-width': 1}), `${s.name} refuels at ${b.name}: ${fT(b.buy_t)}`));
    }
  }
}

// ------------------------------------------------------------------ live ships (AIS)
function attachAis() {
  $('aisToggle').addEventListener('click', () => setAis(!state.ais.on));
  $('aisTrackBtn').addEventListener('click', () => trackMmsi($('aisMmsi').value.trim()));
  $('aisMmsi').addEventListener('keydown', e => { if (e.key === 'Enter') trackMmsi($('aisMmsi').value.trim()); });
  $('aisClearBtn').addEventListener('click', async () => { await postJson('/api/ais/track', {clear: true}); pollAis(); });
  $('startPill').addEventListener('click', () => { state.startAt = null; renderStartPill(); schedulePlan(0); });
}
async function postJson(url, body) {
  const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const json = await res.json(); if (!res.ok) throw Error(json.error || `HTTP ${res.status}`); return json;
}
async function trackMmsi(mmsi) {
  try { await postJson('/api/ais/track', {mmsi}); $('aisMmsi').value = ''; state.ais.sel = mmsi; pollAis(); }
  catch (e) { $('aisStatusText').textContent = '⚠ ' + e.message; }
}
function setAis(on) {
  state.ais.on = on;
  $('aisToggle').setAttribute('aria-pressed', String(on));
  $('aisBar').hidden = !on;
  clearInterval(state.ais.timer); clearInterval(state.ais.anim);
  if (on) { pollAis(); state.ais.timer = setInterval(pollAis, 5000); state.ais.anim = setInterval(drawAis, 1000); }
  else { $('aisSvg').innerHTML = ''; $('aisCard').hidden = true; state.ais.sel = null; }
  updateWarnings();   // AIS alerts only count while the Live ships layer is on
}
async function pollAis() {
  try {
    const res = await fetch('/api/ais', {cache: 'no-store'});
    state.ais.data = await res.json(); state.ais.fetchedAt = Date.now();
  } catch (e) { state.ais.data = {status: 'unavailable', error: e.message, ships: []}; }
  const d = state.ais.data || {};
  $('aisStatusText').textContent = d.status === 'live' ? `● ${d.ships.length} live ships · ${d.mode || ''}${d.tracked?.length ? ' · tracking ' + d.tracked.join(', ') : ''}`
    : d.status === 'connecting' ? '◌ Connecting to AISStream…' : `△ ${d.error || 'Live ships unavailable'}`;
  drawAis();
  updateWarnings();   // new possible stops, queues or ships gone quiet
}
// Where the ship is now: last report, moved forward along its course at its reported speed (max 15 min).
function deadReckon(s) {
  const age = Math.min(900, (s.age_s || 0) + (Date.now() - state.ais.fetchedAt) / 1000);
  if (!Number.isFinite(s.sog) || !Number.isFinite(s.cog) || s.sog < 0.5 || s.sog > 40) return [s.lat, s.lon];
  const d = s.sog * age / 3600, c = s.cog * Math.PI / 180;
  return [s.lat + d * Math.cos(c) / 60, s.lon + d * Math.sin(c) / (60 * Math.max(0.2, Math.cos(s.lat * Math.PI / 180)))];
}
function drawAis() {
  const g = $('aisSvg'); if (!g) return; g.innerHTML = '';
  const mk = markerK();
  const d = state.ais.data; if (!state.ais.on || !d?.ships) return;
  for (const s of d.ships) {
    const here = deadReckon(s), [x, y] = project(here), tracked = s.tracked, sel = s.mmsi === state.ais.sel;
    const col = sel ? '#0f1f33' : tracked ? '#d97706' : '#2563eb';
    const trail = [...(s.trail || []), here];
    if (trail.length > 1) g.appendChild(svgEl('path', {d: pathString(trail), fill: 'none', stroke: col, 'stroke-width': tracked || sel ? 2 : 1.2, 'vector-effect': 'non-scaling-stroke', opacity: tracked || sel ? .8 : .45, 'stroke-dasharray': '2 2'}));
    const c = (Number.isFinite(s.heading) ? s.heading : Number.isFinite(s.cog) ? s.cog : 0) * Math.PI / 180;
    const ang = Math.atan2((W / 170) * Math.sin(c) / Math.max(0.2, Math.cos(s.lat * Math.PI / 180)), (H / 122) * Math.cos(c)) * 180 / Math.PI;
    const k = tracked || sel ? 1.5 : 1;
    const icon = svgEl('path', {d: `M0,${-6 * k} L${3.6 * k},${5 * k} L0,${2.6 * k} L${-3.6 * k},${5 * k} Z`, transform: `translate(${x.toFixed(1)},${y.toFixed(1)}) rotate(${ang.toFixed(0)}) scale(${mk.toFixed(3)})`, fill: col, stroke: '#ffffff', 'stroke-width': .8, class: 'ais-ship'});
    addTitle(icon, `${s.name || 'Unknown'} · MMSI ${s.mmsi} · ${Number.isFinite(s.sog) ? s.sog.toFixed(1) + ' kn' : '— kn'} · course ${Number.isFinite(s.cog) ? Math.round(s.cog) + '°' : '—'} · reported ${Math.round((s.age_s || 0) / 60)} min ago (live AIS)`);
    icon.addEventListener('click', ev => { ev.stopPropagation(); state.ais.sel = s.mmsi; renderAisCard(); drawAis(); });
    if (s.sanctions) g.appendChild(addTitle(svgEl('circle', {cx: x, cy: y, r: 9 * mk, fill: 'none', stroke: '#ff6b6b', 'stroke-width': 1.6, 'stroke-dasharray': '3 2'}), `OFAC SDN ${s.sanctions.status}: ${s.sanctions.listed_name}`));
    g.appendChild(icon);
  }
  renderAisCard();
}
function renderAisCard() {
  const card = $('aisCard'), s = state.ais.data?.ships?.find(x => x.mmsi === state.ais.sel);
  if (!s) { card.hidden = true; return; }
  const here = deadReckon(s);
  card.hidden = false;
  card.innerHTML = `<button class="x" aria-label="Close">✕</button><b>${esc(s.name || 'Unknown vessel')}</b><div class="row"><span>MMSI</span><span>${esc(s.mmsi)}</span></div>
    <div class="row"><span>Speed / course</span><span>${Number.isFinite(s.sog) ? s.sog.toFixed(1) : '—'} kn / ${Number.isFinite(s.cog) ? Math.round(s.cog) + '°' : '—'}</span></div>
    <div class="row"><span>Position</span><span>${here[0].toFixed(2)}, ${here[1].toFixed(2)}</span></div>
    <div class="row"><span>Last report</span><span>${Math.round((s.age_s || 0) / 60)} min ago</span></div>
    ${s.destination ? `<div class="row"><span>Reported destination</span><span>${esc(s.destination)}</span></div>` : ''}
    ${s.imo ? `<div class="row"><span>IMO</span><span>${esc(s.imo)}</span></div>` : ''}
    ${s.sanctions ? `<div class="ais-flag" style="margin-top:4px">⚠ OFAC SDN ${esc(s.sanctions.status)} (${esc(s.sanctions.matched_on)}): listed as ${esc(s.sanctions.listed_name)}${s.sanctions.program ? ', ' + esc(s.sanctions.program) : ''}. Verify before any dealings.</div>` : ''}
    <button data-a="track">${s.tracked ? 'Tracking ✓' : 'Track worldwide'}</button><button data-a="plan">Plan from here → ${esc(portName(state.destination))}</button>
    ${linkable().length ? `<div style="margin-top:6px"><select data-a="voy" style="max-width:150px;font-size:10.5px;background:#fff;color:#0f1f33;border:1px solid #e3e9f0;border-radius:5px;padding:3px">${linkable().map(v => `<option value="${esc(v.id)}">${esc(v.ship_name)} · ${esc(v.id)}</option>`).join('')}</select><button data-a="link">Link to voyage</button></div>` : ''}
    <div style="font-size:9.5px;color:#64748b;margin-top:6px">Planning uses your selected vessel profile and fuel (${fT(state.fuel)}).</div>`;
  const lk = card.querySelector('[data-a=link]');
  if (lk) lk.onclick = () => { const id = card.querySelector('[data-a=voy]').value; state.ops.sel = id; opsAct(id, {action: 'link_ais', mmsi: s.mmsi}, 'Link AIS'); lk.textContent = 'Linked ✓'; };
  card.querySelector('.x').onclick = () => { state.ais.sel = null; card.hidden = true; drawAis(); };
  card.querySelector('[data-a=track]').onclick = () => trackMmsi(s.mmsi);
  card.querySelector('[data-a=plan]').onclick = () => {
    if (state.mode === 'fleet') setPlanMode('single');
    resetVoyage();
    state.startAt = {lat: Number(here[0].toFixed(4)), lon: Number(here[1].toFixed(4)), label: `${s.name || 'Vessel'} (MMSI ${s.mmsi}) live position`};
    renderStartPill(); schedulePlan(0);
  };
}
function linkable() { return (state.ops?.voyages || []).filter(v => ['approved', 'underway'].includes(v.status)); }
function renderStartPill() {
  const p = $('startPill');
  p.hidden = !state.startAt;
  if (state.startAt) p.textContent = `◉ Planning from ${state.startAt.label} ✕`;
}


// ------------------------------------------------------------------ operations: approve, monitor, dispatch, audit
const OPS_EVENTS = {
  bab: {type: 'zone_closure', zone: 'bab', description: 'Red Sea (Bab el-Mandeb) closed'},
  suez: {type: 'zone_closure', zone: 'suez', description: 'Suez Canal closed'},
  hormuz: {type: 'zone_closure', zone: 'hormuz', description: 'Strait of Hormuz closed'},
  babthreat: {type: 'security', zone: 'bab', level: 0.3, description: 'Red Sea threat level raised'},
  babcover: {type: 'cover_cancelled', zone: 'bab', description: 'Insurers cancelled Red Sea war-risk cover'},
  cape: {type: 'storm', id: 'cape-storm', center: [-35, 22], radius_nm: 300, hs_m: 7.5, wind_ms: 25, start_h: 0, end_h: 5000, description: 'Severe storm off the Cape (7.5 m waves)'},
  queue: {type: 'port_congestion', wait_h: 72, description: '72 h berth queue at the destination'},
};
const OPS_HINT = 'Tideline re-runs the optimizer on the server, logs who approved what, then re-checks the voyage every few minutes from where the ship is.';
function attachOps() {
  try { $('opsName').value = localStorage.getItem('tideline.opsName') || ''; } catch (e) { /* storage unavailable */ }
  $('opsApprove').addEventListener('submit', e => { e.preventDefault(); approvePlan(); });
  $('opsAuditBtn').addEventListener('click', () => openAudit());
  $('modalClose').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('modal').hidden) closeModal(); });
  pollOps(); setInterval(pollOps, 15000);
}
async function pollOps() {
  try {
    const res = await fetch('/api/voyages', {cache: 'no-store'}); const j = await res.json();
    state.ops.voyages = j.voyages || []; state.ops.meta = j;
  } catch (e) { state.ops.error = e.message; }
  renderOps(); drawOps(); renderOverview();
}
function approvable() {
  const p = state.plan?.recommended;
  if (state.mode === 'fleet') return [false, 'Switch to a single voyage to approve it.'];
  if (state.progressDay > 0) return [false, 'Move the voyage slider back to departure to approve; at sea, monitoring takes over.'];
  if (state.startAt) return [false, 'Clear the live-position start to approve from the departure port.'];
  if (!p) return [false, 'No feasible plan to approve.'];
  return [true, `${p.corridor_label} · ${p.summary.days.toFixed(1)} days · ${fUSD(p.costs.cash_total_usd)}`];
}
async function approvePlan() {
  const [ok, why] = approvable(), msg = $('opsApproveMsg');
  if (!ok) { msg.textContent = why; return; }
  const name = $('opsName').value.trim();
  if (!name) { msg.textContent = 'Enter your name: a person approves every voyage.'; $('opsName').focus(); return; }
  try { localStorage.setItem('tideline.opsName', name); } catch (e) { /* ignore */ }
  syncUser();
  $('opsApproveBtn').disabled = true; msg.textContent = 'Approving… (the server re-runs the optimizer)';
  try {
    const v = await postJson('/api/voyages', {request: currentRequest(), approved_by: name, note: $('opsNote').value.trim(), mmsi: $('opsMmsi').value.trim() || null});
    msg.textContent = `Approved as ${v.id}. Sanctions screening: ${v.sanctions.status}. Monitoring started.`;
    $('opsNote').value = ''; state.ops.sel = v.id;
    await pollOps();
    $('opsPanel').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e) { msg.textContent = 'Not approved: ' + e.message; }
  $('opsApproveBtn').disabled = false;
}
async function opsAct(id, body, label) {
  state.ops.busy = id; renderOps();
  try { await postJson(`/api/voyages/${encodeURIComponent(id)}/action`, {actor: $('opsName').value.trim() || 'operator', ...body}); state.ops.error = null; }
  catch (e) { state.ops.error = `${label || body.action}: ${e.message}`; }
  state.ops.busy = null; await pollOps();
}
const STATUS_CHIP = s => `<span class="chip ${esc(s)}">${esc(s)}</span>`;
function renderOps() {
  const [ok, why] = approvable();
  $('opsApprovePlan').textContent = ok ? '· ' + why : '';
  $('opsApproveBtn').disabled = !ok;
  // Show why approval is blocked; once a plan is approvable again, drop that stale reason.
  const msgEl = $('opsApproveMsg');
  if (!ok) { msgEl.textContent = why; msgEl.dataset.blocked = '1'; }
  else if (msgEl.dataset.blocked) { msgEl.textContent = OPS_HINT; delete msgEl.dataset.blocked; }
  const vs = state.ops.voyages, m = state.ops.meta;
  const open = vs.reduce((n, v) => n + (['approved', 'underway'].includes(v.status) ? v.open_alerts.length : 0), 0);
  $('opsAlertCount').textContent = open ? `${open} alert${open > 1 ? 's' : ''}` : '';
  $('opsMeta').textContent = m ? `${vs.filter(v => ['approved', 'underway'].includes(v.status)).length} active · re-checked every ${m.monitor_minutes} min` : '—';
  $('opsAuditState').innerHTML = m?.audit ? (m.audit.ok ? `✓ ${m.audit.entries} entr${m.audit.entries === 1 ? 'y' : 'ies'}, hash chain intact` : `<span class="ais-flag">⚠ audit chain broken at entry ${m.audit.broken_at}</span>`) : '';
  const list = $('opsList');
  if (!vs.length) { list.innerHTML = `<div class="ops-empty">No approved voyages yet.${state.ops.error ? ' ' + esc(state.ops.error) : ''}</div>`; return; }
  list.innerHTML = (state.ops.error ? `<div class="ops-alert critical"><span>${esc(state.ops.error)}</span></div>` : '') + vs.map(v => {
    const lc = v.last_check || {}, sel = v.id === state.ops.sel, active = ['approved', 'underway'].includes(v.status);
    const drift = lc.eta_drift_h, dtxt = drift == null ? '' : Math.abs(drift) < 1 ? 'on time' : `${drift > 0 ? '+' : '−'}${Math.abs(drift).toFixed(0)} h vs approved`;
    const rec = {stay: 'Stay on course', change_course: 'Change course', replan: 'Re-plan', no_plan: 'No feasible plan', arrived: 'Arrived', unknown: '—'}[lc.recommendation] || '—';
    const alerts = active ? v.open_alerts.map(a => `<div class="ops-alert ${esc(a.level)}"><span>${esc(a.text)}</span>${['change_course', 'replan'].includes(a.kind) ? `<button class="go" data-op="accept" data-id="${esc(v.id)}">Accept re-plan</button>` : ''}<button data-op="ack" data-id="${esc(v.id)}" data-alert="${esc(a.id)}">Dismiss</button></div>`).join('') : '';
    const busy = state.ops.busy === v.id;
    const actions = sel && active ? `<div class="ops-actions">
        <button data-op="check" data-id="${esc(v.id)}">${busy ? 'Checking…' : '↻ Check now'}</button>
        <button data-op="sim" data-id="${esc(v.id)}" title="Demo only: moves this voyage's clock forward">⏩ +24 h (demo clock)</button>
        <button data-op="orders" data-id="${esc(v.id)}">✉ Orders &amp; bunker enquiry</button>
        <button data-op="audit" data-id="${esc(v.id)}">Audit trail</button>
        <button data-op="complete" data-id="${esc(v.id)}">Mark complete</button><button data-op="cancel" data-id="${esc(v.id)}">Cancel voyage</button></div>
      <div class="ops-mini"><select data-ev="${esc(v.id)}" aria-label="Report an event"><option value="">Report an event…</option>${Object.entries(OPS_EVENTS).map(([k, e]) => `<option value="${k}">${esc(e.description)}</option>`).join('')}</select><button data-op="event" data-id="${esc(v.id)}">Add</button>
        <input data-fuel="${esc(v.id)}" type="number" min="0" placeholder="Noon report: fuel on board (t)" style="width:200px"><button data-op="noon" data-id="${esc(v.id)}">Send</button>
        <input data-mmsi="${esc(v.id)}" inputmode="numeric" maxlength="9" placeholder="${v.mmsi ? 'MMSI ' + esc(v.mmsi) : 'Link AIS: MMSI'}" style="width:130px"><button data-op="link" data-id="${esc(v.id)}">Link</button></div>
      ${v.events.length ? `<div class="ops-why">Events on this voyage: ${v.events.map(e => `${esc(e.description)} <button class="ops-link" data-op="rmev" data-id="${esc(v.id)}" data-ev-id="${esc(e.id)}">remove</button>`).join(' · ')}</div>` : ''}` : '';
    return `<div class="ops-card ${sel ? 'sel' : ''}" data-card="${esc(v.id)}">
      <div class="ops-top"><b>${esc(v.ship_name)}</b><span>${esc(v.origin_name)} → ${esc(v.destination_name)}</span>${STATUS_CHIP(v.status)}${v.sanctions && v.sanctions !== 'clear' ? `<span class="chip ${v.sanctions === 'match' ? 'bad' : 'warn'}">OFAC: ${esc(v.sanctions)}</span>` : ''}<span class="small" style="margin-left:auto">${esc(v.id)} · rev ${v.revision} · approved by ${esc(v.approved_by)}</span></div>
      ${active && lc.progress != null ? `<div class="ops-progress"><i style="width:${(lc.progress * 100).toFixed(1)}%"></i></div>` : ''}
      <div class="ops-grid"><div><small>Plan</small><b>${esc(v.corridor)}</b></div><div><small>ETA</small><b>${esc(fmtEta(lc.eta || v.eta))}</b> ${dtxt ? `<span class="small">${esc(dtxt)}</span>` : ''}</div>
        <div><small>Position · fuel</small><b>${lc.lat != null ? `${lc.lat.toFixed(1)}, ${lc.lon.toFixed(1)}` : '—'}</b> <span class="small">${esc(lc.position_source || '')}${lc.fuel_t != null ? ' · ' + fT(lc.fuel_t) + ' (' + esc(lc.fuel_source || '') + ')' : ''}</span></div>
        <div><small>Last check</small><b>${esc(rec)}</b> <span class="small">${lc.at ? esc(fmtEta(lc.at)) + ' UTC' : 'pending'}${v.clock_offset_h ? ` · demo clock +${v.clock_offset_h} h` : ''}</span></div></div>
      ${lc.reason && sel ? `<div class="ops-why">${esc(lc.reason)}</div>` : ''}${alerts}${actions}</div>`;
  }).join('');
  list.querySelectorAll('[data-card]').forEach(c => c.addEventListener('click', e => {
    if (e.target.closest('button,input,select')) return;
    state.ops.sel = state.ops.sel === c.dataset.card ? null : c.dataset.card; renderOps(); drawOps();
  }));
  list.querySelectorAll('[data-op]').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); opsButton(b); }));
}
function opsButton(b) {
  const id = b.dataset.id, op = b.dataset.op, q = sel => document.querySelector(sel);
  if (op === 'check') return opsAct(id, {action: 'check'}, 'Check');
  if (op === 'sim') return opsAct(id, {action: 'simulate_hours', hours: 24}, 'Demo clock');
  if (op === 'ack') return opsAct(id, {action: 'ack', alert_id: b.dataset.alert}, 'Dismiss');
  if (op === 'accept') return opsAct(id, {action: 'accept_replan'}, 'Accept re-plan');
  if (op === 'complete' || op === 'cancel') return opsAct(id, {action: op}, op);
  if (op === 'rmev') return opsAct(id, {action: 'remove_event', event_id: b.dataset.evId}, 'Remove event');
  if (op === 'event') {
    const k = q(`[data-ev="${id}"]`).value; if (!k) return;
    const v = state.ops.voyages.find(x => x.id === id), ev = {...OPS_EVENTS[k], source: 'Operator report'};
    if (k === 'queue') { ev.port = v.destination; ev.description = `72 h berth queue at ${v.destination_name}`; }
    return opsAct(id, {action: 'add_event', event: ev}, 'Add event');
  }
  if (op === 'noon') return opsAct(id, {action: 'noon_report', fuel_t: q(`[data-fuel="${id}"]`).value}, 'Noon report');
  if (op === 'link') return opsAct(id, {action: 'link_ais', mmsi: q(`[data-mmsi="${id}"]`).value.trim()}, 'Link AIS');
  if (op === 'orders') return openOrders(id);
  if (op === 'audit') return openAudit(id);
}
function openModal(html) { $('modalBody').innerHTML = html; $('modal').hidden = false; }
function closeModal() { $('modal').hidden = true; $('modalBody').innerHTML = ''; }
async function openOrders(id) {
  openModal('<h3>Loading drafts…</h3>');
  try {
    const o = await (await fetch(`/api/voyages/${encodeURIComponent(id)}/orders`, {cache: 'no-store'})).json();
    const mail = (subj, body) => `mailto:?subject=${encodeURIComponent(subj)}&body=${encodeURIComponent(body)}`;
    openModal(`<h3>Voyage orders · ${esc(o.voyage)} (revision ${o.revision})</h3><div class="hint" style="margin:0 0 8px">${esc(o.note)}</div>
      <div class="row-btns"><button data-copy="orders">Copy orders</button><a href="${mail('Voyage orders ' + o.voyage, o.orders_text)}">Open in email</a></div><pre id="ordersText">${esc(o.orders_text)}</pre>
      ${o.bunker_enquiries.map((b, i) => `<h3>Bunker enquiry · ${esc(b.name)}</h3><div class="row-btns"><button data-copy="enq${i}">Copy</button><a href="${mail(b.subject, b.body)}">Open in email</a></div><pre id="enq${i}">${esc('Subject: ' + b.subject + '\n\n' + b.body)}</pre>`).join('') || '<div class="hint">No bunkering on this plan, so no enquiry.</div>'}`);
    $('modalBody').querySelectorAll('[data-copy]').forEach(btn => btn.addEventListener('click', async () => {
      const el = btn.dataset.copy === 'orders' ? $('ordersText') : $(btn.dataset.copy);
      try { await navigator.clipboard.writeText(el.textContent); btn.textContent = 'Copied ✓'; } catch (e) { btn.textContent = 'Select the text to copy'; }
    }));
  } catch (e) { openModal(`<h3>Could not load drafts</h3><p>${esc(e.message)}</p>`); }
}
async function openAudit(id) {
  openModal('<h3>Loading audit log…</h3>');
  try {
    const a = await (await fetch('/api/audit' + (id ? `?voyage=${encodeURIComponent(id)}` : ''), {cache: 'no-store'})).json();
    const ver = a.verification;
    openModal(`<h3>Audit log${id ? ' · ' + esc(id) : ''}</h3><div class="hint" style="margin:0 0 10px">${ver.ok ? `✓ Hash chain verified: ${ver.entries} entries, none altered or removed.` : `<span class="ais-flag">⚠ Hash chain broken at entry ${ver.broken_at}: the log was edited.</span>`} Every entry stores the SHA-256 of the previous one.</div>
      ${a.entries.map(e => `<div class="audit-row"><span>${esc(fmtEta(e.at))} UTC<br><code>${esc(e.hash.slice(0, 16))}…</code></span><span><b>${esc(e.action)}</b><br>${esc(e.actor)}${e.voyage && !id ? '<br>' + esc(e.voyage) : ''}</span><span>${esc(auditText(e))}</span></div>`).join('') || '<div class="hint">No entries yet.</div>'}`);
  } catch (e) { openModal(`<h3>Could not load the audit log</h3><p>${esc(e.message)}</p>`); }
}
function auditText(e) {
  const d = e.detail || {};
  if (e.action === 'approved') return `${d.route}, ETA ${fmtEta(d.eta)}, ${fUSD(d.cash_usd)}, objective ${d.objective}. Sanctions: ${d.sanctions}.${d.note ? ' Note: ' + d.note : ''}`;
  if (e.action === 'alert') return `[${d.level}] ${d.text}`;
  if (e.action === 'accept_replan') return `Revision ${d.revision}: ${d.corridor}, ETA ${fmtEta(d.eta)} (was ${fmtEta(d.previous_eta)}). ${d.reason || ''}`;
  if (e.action === 'add_event') return d.event?.description || d.event?.type || '';
  if (e.action === 'simulate_hours') return `Demo clock +${d.hours} h (not real time)`;
  if (e.action === 'noon_report') return `Fuel ${fT(d.fuel_t)}${d.lat != null ? ` at ${d.lat}, ${d.lon}` : ''}`;
  if (e.action === 'link_ais') return d.mmsi ? `Linked MMSI ${d.mmsi}` : 'AIS link removed';
  return Object.entries(d).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');
}
function drawOps() {
  const g = $('opsSvg'); if (!g) return; g.innerHTML = '';
  for (const v of state.ops.voyages) {
    if (!['approved', 'underway'].includes(v.status)) continue;
    const sel = v.id === state.ops.sel, lc = v.last_check || {};
    if (sel) {
      g.appendChild(svgEl('path', {d: pathString(v.path), fill: 'none', stroke: '#d97706', 'stroke-width': 2.2, opacity: .85}));
      if (v.replan_path) g.appendChild(addTitle(svgEl('path', {d: pathString(v.replan_path), fill: 'none', stroke: '#0d9488', 'stroke-width': 2.4, 'stroke-dasharray': '6 5'}), `Recommended re-plan: ${v.replan_corridor}`));
    }
    // ships themselves are drawn by the live fleet layer (drawVessels)
  }
}


// ------------------------------------------------------------------ Tideline shell: pages, overview, vessels, compliance, settings
const PAGES = ['overview', 'vessels', 'voyages', 'operations', 'compliance', 'reports', 'settings'];
const MAP_TITLES = {overview: 'Global fleet position', voyages: 'Route plan', operations: 'Monitored voyages'};
function showPage(page) {
  if (!PAGES.includes(page)) page = 'overview';
  document.body.dataset.page = page;
  document.body.classList.remove('nav-open');
  document.querySelectorAll('[data-nav]').forEach(b => b.classList.toggle('on', b.dataset.nav === page));
  const slot = $('slot-' + page), card = document.querySelector('.map-card');
  if (slot && card && card.parentElement !== slot) slot.appendChild(card);
  if (lm) setTimeout(() => { lm.map.invalidateSize(); syncOverlay(); }, 0);   // the card changed size or page
  if (MAP_TITLES[page]) $('mapTitle').textContent = MAP_TITLES[page];
  if (location.hash !== '#' + page) history.replaceState(null, '', '#' + page);
  if (page === 'vessels') renderVessels();
  if (page === 'compliance') renderCompliancePage();
  if (page === 'settings') renderSettings();
  if (page === 'overview') renderOverview();
  if (page === 'operations') drawVessels();
  window.scrollTo({top: 0});
}
function openAssistant(open) { $('assistDrawer').classList.toggle('open', open); if (open) setTimeout(() => $('chatInput').focus(), 150); }
function attachShell() {
  document.querySelectorAll('[data-nav]').forEach(b => b.addEventListener('click', () => showPage(b.dataset.nav)));
  document.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.goto === 'decisions') { showPage('overview'); $('decisionsCard').scrollIntoView({behavior: 'smooth', block: 'center'}); }
    else showPage(b.dataset.goto);
  }));
  $('bellBtn').addEventListener('click', () => { showPage('overview'); $('decisionsCard').scrollIntoView({behavior: 'smooth', block: 'center'}); });
  $('assistBtn').addEventListener('click', () => openAssistant(!$('assistDrawer').classList.contains('open')));
  $('assistClose').addEventListener('click', () => openAssistant(false));
  $('menuBtn').addEventListener('click', () => document.body.classList.toggle('nav-open'));
  $('helpBtn').addEventListener('click', openHelp);
  $('reportsAudit').addEventListener('click', () => openAudit());
  $('statusRefresh').addEventListener('click', renderSettings);
  $('vesselFilter').addEventListener('change', drawVessels);
  const search = $('globalSearch');
  search.addEventListener('change', () => runSearch(search.value));
  search.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(search.value); });
  window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
  syncUser(); fillSearch();
  showPage(location.hash.slice(1) || 'overview');
}
function syncUser() {
  let name = '';
  try { name = localStorage.getItem('tideline.opsName') || ''; } catch (e) { /* storage unavailable */ }
  $('userName').textContent = name || 'Fleet operator';
  $('userAvatar').textContent = (name || 'Fleet Operator').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
}
function fillSearch() {
  const opts = [...state.fleet.ships.map(s => `${s.name} — vessel`), ...Object.entries(state.network.ports).map(([c, p]) => `${p.name} (${c}) — port`),
    ...(state.ops?.voyages || []).map(v => `${v.id} — ${v.ship_name} ${v.origin_name} → ${v.destination_name}`)];
  $('searchList').innerHTML = opts.map(o => `<option value="${esc(o)}"></option>`).join('');
}
function runSearch(q) {
  q = (q || '').trim().toLowerCase(); if (!q) return;
  const ship = state.fleet.ships.find(s => q.startsWith(s.name.toLowerCase()) || q === s.id || q.includes(s.name.toLowerCase().replace('mv ', '')));
  const voy = (state.ops?.voyages || []).find(v => q.startsWith(v.id.toLowerCase()));
  const port = Object.entries(state.network.ports).find(([c, p]) => q.startsWith(p.name.toLowerCase()) || q === c.toLowerCase() || q.includes(`(${c.toLowerCase()})`));
  if (voy) { state.ops.sel = voy.id; showPage('operations'); renderOps(); drawOps(); }
  else if (ship) { if (state.mode === 'fleet') setPlanMode('single'); applyRequest({...currentRequest(), ship_id: ship.id, fuel_onboard_t: undefined}); schedulePlan(0); showPage('voyages'); }
  else if (port) { applyRequest({...currentRequest(), destination: port[0], ...(port[0] === state.origin ? {origin: state.destination} : {})}); schedulePlan(0); showPage('voyages'); }
  $('globalSearch').value = '';
}
// fleet status: each ship's latest active voyage decides its status
function vesselStatus() {
  const active = (state.ops?.voyages || []).filter(v => ['approved', 'underway'].includes(v.status));
  return state.fleet.ships.map(s => {
    const v = active.find(x => x.ship_id === s.id);
    const home = state.network.ports[s.home_port] || state.network.ports.SIN;
    if (!v) return {ship: s, status: 'idle', label: 'In port', where: `In port · ${home.name}`, loc: home.loc};
    const al = v.open_alerts || [];
    const status = al.some(a => ['action', 'critical'].includes(a.level)) ? 'decision' : al.length ? 'attention' : 'ontrack';
    const lc = v.last_check || {};
    return {ship: s, voyage: v, status, label: {decision: 'Decision needed', attention: 'Attention', ontrack: 'On track'}[status],
      where: `${v.origin_name} → ${v.destination_name}`, loc: lc.lat != null ? [lc.lat, lc.lon] : home.loc};
  });
}
const STATUS_COLOR = {ontrack: '#16a34a', attention: '#f59e0b', decision: '#ef4444', idle: '#94a3b8'};
const ALERT_TITLES = {change_course: 'Change course recommended', replan: 'Better plan before departure', eta: 'Arrival time drifting', fuel: 'Fuel margin shrinking', off_route: 'Ship off the approved route', infeasible: 'No feasible plan from position'};
function decisionItems() {
  const out = [];
  for (const v of (state.ops?.voyages || []).filter(x => ['approved', 'underway'].includes(x.status))) {
    for (const a of v.open_alerts || []) {
      const prio = ['action', 'critical'].includes(a.level) ? 'high' : 'medium';
      out.push({prio, title: ALERT_TITLES[a.kind] || 'Voyage alert', text: a.text, ships: [v.ship_name], voyage: v.id, kind: a.kind, action: true});
    }
  }
  out.sort((a, b) => (a.prio === 'high' ? 0 : 1) - (b.prio === 'high' ? 0 : 1));
  return out;
}
function watchItems() {
  const p = state.plan; if (!p?.hazards) return [];
  return p.hazards.zones.filter(z => z.level === 'high' || z.level === 'closed').map(z => ({prio: 'low', title: `Risk watch: ${z.label}`,
    text: `${z.level === 'closed' ? 'Closed' : 'High risk'}: ${z.reasons.join(', ')}. ${p.recommended ? 'The recommended ' + p.recommended.corridor_label.replace('Via ', 'route via ') + (p.recommended.zones.includes(z.id) ? ' passes through it.' : ' avoids it.') : ''}`,
    ships: [shipObj().name], plan: true}));
}
function renderOverview() {
  if (!state.fleet || !state.network) return;
  const vs = vesselStatus(), dec = decisionItems();
  const regions = new Set(state.fleet.ships.map(s => state.network.ports[s.home_port]?.region).filter(Boolean));
  $('kpiVessels').textContent = vs.length;
  $('kpiVesselsSub').textContent = `Across ${regions.size} regions · ${vs.filter(v => v.voyage).length} at sea or approved`;
  $('kpiDecisions').textContent = dec.length;
  $('kpiOnTrack').textContent = vs.filter(v => v.status === 'ontrack' || v.status === 'idle').length;
  $('decisionsBadge').textContent = dec.length;
  $('bellDot').hidden = !dec.length;
  $('navOpsCount').hidden = !dec.length; $('navOpsCount').textContent = dec.length;
  const items = [...dec, ...watchItems()].slice(0, 8);
  const icon = {high: '⚠', medium: '⚠', low: 'ⓘ'};
  $('decisionsList').innerHTML = items.length ? items.map((d, i) => `<div class="dcard ${d.prio}"><span class="ic">${icon[d.prio]}</span><div>
      <span class="prio">${{high: 'High priority', medium: 'Medium priority', low: 'Watch'}[d.prio]}</span>
      <h3>${esc(d.title)}</h3><p>${esc(d.text)}</p><div class="chips">${d.ships.map(n => `<span class="chip-s">${esc(n)}</span>`).join('')}</div>
      <button type="button" class="${d.prio === 'high' ? 'btn-primary' : 'btn-outline'}" data-dec="${i}">${d.plan ? 'Open planner →' : d.prio === 'high' ? 'Review route →' : 'Review options →'}</button></div></div>`).join('')
    : `<div class="empty"><b>No decisions needed</b>Approve a voyage in Voyages and Tideline will monitor it here.</div>`;
  $('decisionsList').querySelectorAll('[data-dec]').forEach(b => b.addEventListener('click', () => {
    const d = items[Number(b.dataset.dec)];
    if (d.plan) { showPage('voyages'); return; }
    state.ops.sel = d.voyage; showPage('operations'); renderOps(); drawOps();
  }));
  drawVessels();
  fillSearch();
}
// ---- live fleet: each approved ship sails along its committed route, in real time or at playback speed.
// The clock is state.sim.offsetH (simulated hours added to "now"); playback.js owns the maths. Ships are moved
// by a requestAnimationFrame loop that only sets attributes, so the map layer is never rebuilt per frame.
const PB = window.TLPlayback;
function simHoursNow() { return state.sim.offsetH; }
function activeVoyages() { return (state.ops.voyages || []).filter(v => ['approved', 'underway'].includes(v.status)); }
function pathCum(path) {
  const c = [0];
  for (let i = 1; i < path.length; i++) c.push(c[i - 1] + haversineNm(path[i - 1], path[i]));
  return c;
}
function haversineNm(a, b) {
  const r = Math.PI / 180, dLat = (b[0] - a[0]) * r, dLon = (b[1] - a[1]) * r;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLon / 2) ** 2;
  return 3440.065 * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
}
// Where the ship should be now on its committed route (constant speed between start and ETA).
function livePosition(v) {
  const path = v.path; if (!path || path.length < 2) return null;
  const t0 = Date.parse(v.path_t0 || v.departure), t1 = Date.parse(v.eta);
  const now = Date.now() + ((v.clock_offset_h || 0) + simHoursNow()) * 3.6e6;
  const frac = Math.max(0, Math.min(1, (now - t0) / Math.max(1, t1 - t0)));
  const cum = v._cum || (v._cum = pathCum(path)), target = cum.at(-1) * frac;
  let i = 1; while (i < cum.length - 1 && cum[i] < target) i++;
  const seg = Math.max(1e-9, cum[i] - cum[i - 1]), t = Math.max(0, Math.min(1, (target - cum[i - 1]) / seg));
  const a = path[i - 1], b = path[i];
  const pos = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  return {pos, frac, i, a, b, arrived: frac >= 1, departed: frac > 0};
}
// Route colours follow the Streamlit Tideline map: mint = distance travelled, blue = distance still to go.
const ROUTE_DONE = '#4de0c8', ROUTE_AHEAD = '#2f80ed', ROUTE_CASING = '#0f3b63';
let vesselNodes = [];   // SVG nodes built by drawVessels and moved every frame by placeVessel
function vesselTitle(n, live) {
  const v = n.v, pct = live ? ` · ${(live.frac * 100).toFixed(0)}% of route` : '';
  return `${v.ship.name} · ${v.label} · ${v.where}${pct}${n.voyage ? ` · ETA ${fmtEta((n.voyage.last_check || {}).eta || n.voyage.eta)} UTC` : ''}${state.sim.offsetH ? ' · simulated playback position' : ' · position from the approved schedule'}`;
}
function placeVessel(n) {
  let loc = n.v.loc, ang = -90, live = null;
  if (n.voyage?.path) {
    live = livePosition(n.voyage);
    if (live) {
      loc = live.pos;
      const [ax, ay] = project(live.a), [bx, by] = project(live.b);
      ang = Math.atan2(by - ay, bx - ax) * 180 / Math.PI;
    }
  }
  const [x, y] = project(loc);
  const k = markerK();
  const key = `${live ? Math.round(live.frac * 100) : ''}${live?.arrived}|${k.toFixed(3)}`;
  if (Math.abs(x - n.x) < 0.25 && Math.abs(y - n.y) < 0.25 && key === n.key) return;   // moved less than a quarter pixel
  n.x = x; n.y = y; n.key = key;
  n.grp.setAttribute('transform', `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${k.toFixed(3)})`);
  if (n.halo) { n.halo.setAttribute('cx', x.toFixed(1)); n.halo.setAttribute('cy', y.toFixed(1)); n.halo.setAttribute('r', (26 * k).toFixed(1)); }
  if (n.voyage) n.icon.setAttribute('transform', `rotate(${ang.toFixed(1)})`);
  if (live && n.done) {   // travelled part (solid mint) and the part still to go (dashed blue) are separate paths
    const sailed = pathString(n.voyage.path.slice(0, live.i).concat([live.pos]));
    n.done.setAttribute('d', sailed); n.casing.setAttribute('d', sailed);
    n.ahead.setAttribute('d', pathString([live.pos].concat(n.voyage.path.slice(live.i))));
  }
  const label = n.v.ship.name + (live?.arrived ? ' · arrived' : '');
  if (n.text.textContent !== label) { n.text.textContent = label; n.pill.setAttribute('width', label.length * 6.6 + 18); }
  n.title.textContent = vesselTitle(n, live);
}
function moveVessels() { for (const n of vesselNodes) placeVessel(n); }
function drawVessels() {
  const g = $('vesselSvg'); if (!g || !state.fleet) return; g.innerHTML = ''; vesselNodes = [];
  const f = $('vesselFilter').value, onOps = document.body.dataset.page === 'operations';
  for (const v of vesselStatus()) {
    if (f !== 'all' && v.status !== f) continue;
    if (onOps && !v.voyage) continue;
    const col = STATUS_COLOR[v.status], n = {v, voyage: v.voyage, x: NaN, y: NaN, key: ''};
    if (v.voyage?.path) {
      const line = (stroke, w, extra) => g.appendChild(svgEl('path', {fill: 'none', stroke, 'stroke-width': w, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', ...extra}));
      n.ahead = line(ROUTE_AHEAD, 2.5, {'stroke-dasharray': '7 6', opacity: .95});   // still to go: dashed blue
      n.casing = line(ROUTE_CASING, 7.5, {opacity: .9});                              // dark edge so the mint reads on any map colour
      n.done = line(ROUTE_DONE, 4.5);                                                 // travelled: solid mint
    }
    if (v.status === 'decision') n.halo = g.appendChild(svgEl('circle', {r: 26, fill: '#ef444433'}));
    const grp = n.grp = svgEl('g', {class: 'vessel-label', style: 'cursor:pointer'});
    n.icon = grp.appendChild(v.voyage   // ship icon pointing along the route, or a dot for a ship in port
      ? svgEl('path', {d: 'M11,0 L-7,-6.5 L-3.5,0 L-7,6.5 Z', fill: col, stroke: '#fff', 'stroke-width': 1.5, filter: 'url(#cardShadow)'})
      : svgEl('circle', {r: 5.5, fill: col, stroke: '#fff', 'stroke-width': 1.5}));
    n.pill = grp.appendChild(svgEl('rect', {x: 13, y: -11, width: v.ship.name.length * 6.6 + 18, height: 22, rx: 11, filter: 'url(#cardShadow)'}));
    grp.appendChild(svgEl('circle', {cx: 23, cy: 0, r: 4, fill: col}));
    n.text = grp.appendChild(svgEl('text', {x: 32, y: 4})); n.text.textContent = v.ship.name;
    addTitle(grp, ''); n.title = grp.querySelector('title');
    grp.addEventListener('click', () => { if (v.voyage) { state.ops.sel = v.voyage.id; showPage('operations'); renderOps(); drawOps(); } else { applyRequest({...currentRequest(), ship_id: v.ship.id, fuel_onboard_t: undefined}); schedulePlan(0); showPage('voyages'); } });
    g.appendChild(grp);
    vesselNodes.push(n); placeVessel(n);
  }
  syncSimUi();
}
const setSimSlider = () => { const el = $('simSpeed'); if (el) el.value = Math.round(PB.speedToSlider(state.sim.speed) * 1000); };
function syncSimUi() {
  const s = state.sim, el = $('simClock'); if (!el) return;
  $('simPlay').textContent = s.playing ? '❚❚ Pause' : '▶ Play';
  $('simPlay').setAttribute('aria-pressed', String(s.playing));
  const caption = PB.speedCaption(s.speed), mult = PB.zoomMultiplier(mapZoomLevel());
  const rate = PB.rateCaption(s.speed, mult, s.auto);   // e.g. '10 h/s × 2.3 (zoomed out) = 23 h/s'
  if ($('simSpeedLabel').textContent !== rate) $('simSpeedLabel').textContent = rate;
  $('simSpeedLabel').title = caption;
  const text = !activeVoyages().length ? 'No ships at sea · ▶ launches the fleet'
    : s.offsetH ? `Simulated +${(s.offsetH / 24).toFixed(1)} days` : (s.playing && caption === 'Real time' ? 'Live · real time' : 'Now');
  if (el.textContent !== text) el.textContent = text;
  el.classList.toggle('sim-on', !!s.offsetH);
}
// Frame loop: runs only while playing. dt is capped in playback.js, so a hidden tab (rAF pauses) never jumps the clock.
// Speed the ship should run at now: the slider speed, scaled by the map zoom when auto-scale is on (read here, in the frame loop).
const simTarget = () => PB.effectiveSpeed(state.sim.speed, mapZoomLevel(), state.sim.auto);
let simLast = 0, simRaf = 0, simUiAt = 0;
function simFrame(ts) {
  simRaf = 0;
  const s = state.sim, dt = simLast ? (ts - simLast) / 1000 : 0; simLast = ts;
  s.speedNow += (simTarget() - s.speedNow) * (1 - Math.exp(-dt / 0.1));   // ease speed and zoom changes over ~300 ms; the clock integrates this, so it never jumps
  const r = PB.advance(s.offsetH, dt, s.speedNow, PB.hoursUntilAllArrive(activeVoyages(), Date.now(), 0));
  s.offsetH = r.offsetH;
  if (r.done) s.playing = false;
  if (['overview', 'operations'].includes(document.body.dataset.page)) moveVessels();
  if (ts - simUiAt > 250 || !s.playing) { simUiAt = ts; syncSimUi(); }
  if (s.playing) simRaf = requestAnimationFrame(simFrame); else simLast = 0;
}
function ensureSimLoop() { if (!simRaf && state.sim.playing) simRaf = requestAnimationFrame(simFrame); }
// Play: needs approved voyages to move; with none, launch the demo fleet first.
function togglePlay(fast) {
  const active = activeVoyages(), s = state.sim;
  if (!active.length) { launchFleet(); return; }
  if (!s.playing) {
    if (fast && s.speed < 1) { s.speed = 24; setSimSlider(); }
    s.speedNow = simTarget();
    if (s.offsetH >= PB.hoursUntilAllArrive(active, Date.now(), 0) - 1e-6) s.offsetH = 0;   // all arrived: replay from now
  }
  s.playing = !s.playing; syncSimUi(); ensureSimLoop();
}
function attachSim() {
  setSimSlider();
  $('simPlay').addEventListener('click', () => togglePlay());
  $('simSpeed').addEventListener('input', e => {
    const s = state.sim; s.speed = PB.sliderToSpeed(Number(e.target.value) / 1000);
    if (!s.playing) s.speedNow = simTarget();
    syncSimUi();
  });
  $('simAuto').addEventListener('change', e => { const s = state.sim; s.auto = e.target.checked; if (!s.playing) s.speedNow = simTarget(); syncSimUi(); });
  $('simReset').addEventListener('click', () => { const s = state.sim; s.offsetH = 0; s.playing = false; s.speedNow = simTarget(); drawVessels(); });
  // Not playing: ships still drift at real time, so refresh their positions every few seconds.
  setInterval(() => { if (!state.sim.playing && ['overview', 'operations'].includes(document.body.dataset.page)) moveVessels(); }, 5000);
}
// Demo: a world event hits the ships at sea (at the positions shown by playback).
async function fleetEvent() {
  const act = (state.ops.voyages || []).filter(v => ['approved', 'underway'].includes(v.status));
  if (!act.length) { launchFleet(); return; }
  const suez = act.filter(v => /suez/i.test(v.corridor)).length, cape = act.filter(v => /cape/i.test(v.corridor)).length;
  const ev = suez >= cape && suez > 0
    ? {type: 'zone_closure', zone: 'bab', description: 'Red Sea (Bab el-Mandeb) closed', source: 'Demo world event'}
    : {type: 'storm', id: 'cape-storm', center: [-35, 22], radius_nm: 400, hs_m: 8, wind_ms: 26, start_h: 0, end_h: 5000, description: 'Severe storm off the Cape (8 m waves)', source: 'Demo world event'};
  const btn = document.querySelector('[data-demo=hit]'); if (btn) btn.textContent = '⚡ Re-checking fleet…';
  const adv = Math.max(0, state.sim.offsetH);
  await Promise.all(act.map(v => postJson(`/api/voyages/${encodeURIComponent(v.id)}/action`, {action: 'add_event', event: ev, advance_hours: adv, actor: $('opsName').value.trim() || 'Demo operator'}).catch(e => console.warn(e.message))));
  state.sim.offsetH = 0;   // the server clocks now include the playback time
  if (btn) btn.textContent = '⚡ Event hits fleet';
  await pollOps(); showPage('overview');
}
const DEMO_FLEET = [['aurora', 'SIN', 'RTM'], ['borealis', 'SHA', 'HAM'], ['meridian', 'JEA', 'SIN'], ['tern', 'CMB', 'MBA'], ['polaris', 'RTM', 'PIR']];
let launching = false;
async function launchFleet() {
  if (launching) return;   // a second click while the fleet is being planned would approve the same ships twice
  launching = true;
  try { await launchFleetNow(); } finally { launching = false; }
}
async function launchFleetNow() {
  if (!$('opsName').value.trim()) $('opsName').value = 'Demo operator';
  await pollOps();   // start from the server's list: clicking before it loaded used to approve ships already at sea
  const name = $('opsName').value.trim(), active = new Set((state.ops.voyages || []).filter(v => ['approved', 'underway'].includes(v.status)).map(v => v.ship_id));
  showPage('overview');
  const btn = document.querySelector('[data-demo=launch]'); if (btn) btn.textContent = '🚢 Launching…';
  for (const [ship_id, origin, destination] of DEMO_FLEET) {
    if (active.has(ship_id)) continue;
    try {
      await postJson('/api/voyages', {request: {ship_id, origin, destination, priority: state.priority, conditions: state.conditions, live_weather: state.toggles.liveWeather},
        approved_by: name, note: 'Demo fleet launch'});
    } catch (e) { console.warn('launch', ship_id, e.message); }
    await pollOps();
  }
  if (btn) btn.textContent = '🚢 Launch fleet';
  state.sim.speed = 24; state.sim.speedNow = simTarget(); setSimSlider(); state.sim.playing = true; syncSimUi(); ensureSimLoop();
}
function renderVessels() {
  const vs = vesselStatus(), pol = state.fleet.insurance || {}, cal = state.calibration || {};
  $('vesselsTable').innerHTML = `<thead><tr><th>Vessel</th><th>Status</th><th>Voyage / location</th><th>ETA</th><th class="num">Draft</th><th class="num">Tank</th><th>Speed</th><th>War cover</th><th>Fuel model</th><th></th></tr></thead><tbody>${vs.map(v => {
    const s = v.ship, p = pol[s.id] || {}, c = cal[s.id] || {};
    return `<tr><td><b>${esc(s.name)}</b><br><span class="hint-inline">${esc(s.type)}</span></td><td><span class="status"><i class="ldot" style="background:${STATUS_COLOR[v.status]}"></i>${esc(v.label)}</span></td>
      <td>${esc(v.where)}${v.voyage ? `<br><span class="hint-inline">${esc(v.voyage.id)} · ${esc(v.voyage.corridor)}</span>` : ''}</td><td>${v.voyage ? esc(fmtEta((v.voyage.last_check || {}).eta || v.voyage.eta)) : '—'}</td>
      <td class="num">${s.draft_m} m</td><td class="num">${fT(s.fuel_capacity_t)}</td><td>${s.min_speed_kn}–${s.max_speed_kn} kn</td>
      <td>${p.war_cover ? 'Yes' : '<b style="color:#b45309">No</b>'}${Object.keys(p.warranties || {}).length ? `<br><span class="hint-inline">warranty: ${esc(Object.keys(p.warranties).map(z => state.network.zones[z]?.label || z).join(', '))}</span>` : ''}</td>
      <td>${c.source === 'calibrated' ? `Calibrated · ${c.reports} reports` : 'Simulated training'}</td>
      <td><button type="button" class="act" data-plan="${esc(s.id)}">Plan voyage</button></td></tr>`;
  }).join('')}</tbody>`;
  $('vesselsTable').querySelectorAll('[data-plan]').forEach(b => b.addEventListener('click', () => {
    if (state.mode === 'fleet') setPlanMode('single');
    applyRequest({...currentRequest(), ship_id: b.dataset.plan, fuel_onboard_t: undefined}); schedulePlan(0); showPage('voyages');
  }));
}
async function renderCompliancePage() {
  const pol = state.fleet.insurance || {}, Z = z => state.network.zones[z]?.label || z;
  $('policyTable').innerHTML = `<thead><tr><th>Vessel</th><th>Policy</th><th>War-risk cover</th><th>Cancellation notice</th><th>Trading warranties (held covered)</th></tr></thead><tbody>${state.fleet.ships.map(s => {
    const p = pol[s.id] || {};
    return `<tr><td><b>${esc(s.name)}</b></td><td>${esc(p.insurer || 'No policy on file')}</td><td>${p.war_cover ? 'Yes — listed areas at additional premium' : '<b style="color:#b45309">No — listed areas not covered</b>'}</td>
      <td>${p.notice_days ? p.notice_days + ' days' : '—'}</td><td>${Object.entries(p.warranties || {}).map(([z, w]) => `${esc(Z(z))} (+${(w.rate * 100).toFixed(2)}% of hull)`).join('<br>') || '—'}</td></tr>`;
  }).join('')}</tbody>`;
  try {
    const [sx, ad] = await Promise.all([fetch('/api/sanctions').then(r => r.json()), fetch('/api/advisories').then(r => r.json())]);
    const fleetScreen = await Promise.all(state.fleet.ships.map(s => fetch(`/api/sanctions?name=${encodeURIComponent(s.name)}`).then(r => r.json())));
    $('sanctionsCard').innerHTML = `<h2>Sanctions screening</h2><div class="src"><div class="src-top"><b>${esc(sx.source)}</b><span class="src-tag ${sx.status === 'loaded' ? 'official' : 'stale'}">${esc(sx.status)}</span></div>
      <div class="src-note">${sx.vessels ? `${sx.vessels.toLocaleString()} listed vessels` : ''}${sx.file_date ? ` · list date ${esc(sx.file_date)}` : ''}${sx.error ? ' · ' + esc(sx.error) : ''}<br>${esc(sx.scope)}</div></div>
      ${state.fleet.ships.map((s, i) => `<div class="src"><div class="src-top"><b>${esc(s.name)}</b><span class="src-tag ${fleetScreen[i].status === 'clear' ? 'live' : fleetScreen[i].status === 'not screened' ? 'sample' : 'stale'}">${esc(fleetScreen[i].status)}</span></div>${fleetScreen[i].matches?.length ? `<div class="src-note">Listed: ${esc(fleetScreen[i].matches[0].name)} (${esc(fleetScreen[i].matches[0].program || '')}), matched on ${esc(fleetScreen[i].matches[0].matched_on)}. Sample ship names can collide with real listings; verify by IMO.</div>` : ''}</div>`).join('')}`;
    $('warningsCard').innerHTML = `<h2>Official navigational warnings</h2><div class="src-note" style="margin-bottom:6px">NGA broadcast warnings (HYDROLANT, HYDROPAC, NAVAREA IV/XII) about security on Tideline risk zones · status ${esc(ad.status)}${ad.retrieved_at ? ' · checked ' + esc(fmtEta(ad.retrieved_at)) + ' UTC' : ''}</div>
      ${ad.warnings.length ? ad.warnings.map(w => `<div class="src"><div class="src-top"><b>${esc(w.id)}</b><span class="src-tag official">${esc(w.zones.map(Z).join(', '))}</span></div><div class="src-note">${esc(w.issued || '')} · ${esc(w.text.slice(0, 260))}${w.text.length > 260 ? '…' : ''}</div></div>`).join('') : '<div class="empty-feed">No active security warnings on these zones right now.</div>'}`;
  } catch (e) { $('sanctionsCard').innerHTML = `<h2>Sanctions screening</h2><div class="hint">Unavailable: ${esc(e.message)}</div>`; }
}
async function renderSettings() {
  const g = $('statusGrid'); g.innerHTML = '<div class="hint">Checking connections…</div>';
  try {
    const s = await (await fetch('/api/status', {cache: 'no-store'})).json();
    const card = (title, ok, text) => `<div class="card status-card"><h3><i class="sdot ${ok}"></i>${esc(title)}</h3><p>${text}</p></div>`;
    const w = s.weather || {}, pw = s.portwatch || {}, sx = s.sanctions || {}, ad = s.advisories || {}, ai = s.ai || {}, a = s.ais || {}, m = s.monitor || {}, au = s.audit || {};
    g.innerHTML = [
      card('AI assistant', ai.mode === 'gpt' ? 'ok' : 'warn', ai.mode === 'gpt' ? `GPT via OpenAI · ${esc(ai.model || '')}` : 'Offline parser (set OPENAI_API_KEY for GPT)'),
      card('Marine weather (Open-Meteo)', w.status === 'live' ? 'ok' : 'warn', `${esc(w.status || '—')}${w.points ? ` · ${w.points_with_data ?? ''}${w.points_with_data != null ? '/' : ''}${w.points} points` : ''}${w.retrieved_at ? ' · ' + esc(fmtEta(w.retrieved_at)) + ' UTC' : ''}`),
      card('Chokepoint traffic (IMF PortWatch)', pw.status === 'live' ? 'ok' : 'warn', `${esc(pw.status || '—')}${pw.latest_date ? ' · data to ' + esc(pw.latest_date) : ''}`),
      card('Live ships (AISStream)', a.status === 'live' ? 'ok' : a.status === 'off' ? '' : 'warn', `${esc(a.status || '—')}${a.ships != null ? ` · ${a.ships} ships · ${a.messages || 0} messages` : ''}${a.error ? ' · ' + esc(a.error) : ''}`),
      card('AIS chokepoint traffic (advisory)', '', a.status !== 'live' ? 'Needs live AIS (set AISSTREAM_API_KEY).'
        : `${Object.entries(a.traffic || {}).map(([box, t]) => `${esc(box)}: ${esc(t.note)}`).join(' · ') || 'No ships heard in the watched areas yet.'} Source: AISStream.io. A sample of ships heard, not a full transit count; it does not change any plan or risk score.${a.anomalies ? ` ${a.anomalies} possible alert${a.anomalies === 1 ? '' : 's'} raised (not confirmed).` : ''}`),
      card('Sanctions list (US OFAC SDN)', sx.status === 'loaded' ? 'ok' : 'warn', `${esc(sx.status || '—')}${sx.vessels ? ` · ${sx.vessels.toLocaleString()} vessels` : ''}${sx.error ? ' · ' + esc(sx.error) : ''}`),
      card('Official warnings (NGA)', ad.status === 'live' ? 'ok' : 'warn', `${esc(ad.status || '—')} · ${ad.warnings ?? 0} security warnings on risk zones`),
      card('Voyage monitoring', m.running ? 'ok' : 'warn', `Re-checks approved voyages every ${m.minutes} min`),
      card('Audit log', au.ok ? 'ok' : 'bad', au.ok ? `${au.entries} entries · hash chain intact` : `Hash chain broken at entry ${au.broken_at}`),
      card('Fuel model', 'ok', 'Per-ship regression; calibrate on noon reports in Voyages → Advanced'),
    ].join('');
  } catch (e) { g.innerHTML = `<div class="hint">Could not read status: ${esc(e.message)}</div>`; }
}
function openHelp() {
  openModal(`<h3>Using Tideline</h3><div style="font-size:14px;line-height:1.6;color:#334155">
    <p><b>Overview</b> — every vessel on the map, coloured by status, and the decisions that need you.</p>
    <p><b>Voyages</b> — plan a route: pick ship, ports and objective; toggle what-ifs (closures, storms, cancelled insurance). Tideline optimizes route, speed and refuelling and explains the trade-offs.</p>
    <p><b>Operations</b> — approve a plan with your name; Tideline re-checks it every few minutes from where the ship is and raises alerts. Accept re-plans, draft voyage orders and bunker enquiries, and see the audit log.</p>
    <p><b>Vessels</b>, <b>Compliance</b>, <b>Reports</b>, <b>Settings</b> — fleet details, insurance and sanctions, data sources and news, and live connections.</p>
    <p><b>Ask Tideline</b> (bottom right) — type a change or paste a news report; the AI runs the planner and every number comes from it.</p>
    <p><b>Demo view</b> — one-click buttons for the pitch.</p><p class="hint">Decision support for a hackathon demo. Not for navigation.</p></div>`);
}

// ------------------------------------------------------------------ simplified layout: tabs and demo view
function attachLayout() {
  document.querySelectorAll('#planTabs [data-tab]').forEach(b => b.addEventListener('click', () => {
    document.querySelector('.plan-body').dataset.tab = b.dataset.tab;
    document.querySelectorAll('#planTabs [data-tab]').forEach(x => x.classList.toggle('on', x === b));
  }));
  document.querySelectorAll('#sideTabs [data-side]').forEach(b => b.addEventListener('click', () => setSideTab(b.dataset.side)));
  $('demoToggle').addEventListener('click', () => setDemo(!document.body.classList.contains('demo')));
  document.querySelectorAll('[data-demo]').forEach(b => b.addEventListener('click', () => demoAction(b.dataset.demo)));
}
function setSideTab(tab) { if (tab === 'chat') openAssistant(true); }
function setDemo(on) {
  document.body.classList.toggle('demo', on);
  $('demoBar').hidden = !on;
  $('demoToggle').setAttribute('aria-pressed', String(on));
  $('demoToggle').textContent = on ? '✕ Exit demo view' : '▶ Demo view';
  if (on) showPage('voyages');
  syncDemoBar();
}
function resetAll() {
  if (state.mode === 'fleet') { state.mode = 'single'; setPlanMode('single'); }
  for (const k of ['blockBab', 'threatBab', 'coverBab', 'blockSuez', 'blockHormuz', 'closeWestAfrica', 'stormCape', 'allowUninsured']) state.toggles[k] = false;
  state.toggles.liveWeather = true; state.toggles.autoNews = true;
  Object.assign(state, {conditions: 'today', priority: 'balanced', weights: null, extraEvents: [], startAt: null, affiliations: [], ship: 'aurora', origin: 'SIN', destination: 'RTM', deadline: '', berth: ''});
  state.fuel = shipObj().default_fuel_onboard_t; state.cargo = shipObj().default_cargo || 'retail';
  resetVoyage(); renderStartPill(); syncControls();
}
function demoAction(a) {
  if (a === 'today' || a === 'reset') { resetAll(); schedulePlan(0); }
  else if (a === 'normal') { if (state.mode === 'fleet') setPlanMode('single'); setConditions('normal'); }
  else if (a === 'redsea') { state.toggles.blockBab = !state.toggles.blockBab; $('blockBab').checked = state.toggles.blockBab; schedulePlan(0); }
  else if (a === 'atsea') {
    if (state.mode === 'fleet') { setPlanMode('single'); return; }
    if (state.progressDay > 0) { resetVoyage(); schedulePlan(0); }
    else if (state.plan?.recommended && !state.plan.resume) {
      const day = Math.min(9, Math.max(1, Math.floor(state.plan.recommended.summary.days / 3)));
      onProgress(day);
    }
  }
  else if (a === 'news') { setSideTab('chat'); sendChat(SAMPLE_REPORT); }
  else if (a === 'fleet') {
    if (state.mode === 'fleet') { setPlanMode('single'); }
    else {
      const fuels = {aurora: 900, borealis: 700, tern: 400, meridian: 500, polaris: 300};
      for (const s of state.fleet.ships) state.fleetShips[s.id] = {on: true, fuel: fuels[s.id] ?? s.default_fuel_onboard_t};
      state.origin = 'SHA'; state.destination = 'RTM'; syncControls(); renderFleetPicker(); setPlanMode('fleet');
    }
  }
  else if (a === 'live') setAis(!state.ais.on);
  else if (a === 'launch') launchFleet();
  else if (a === 'hit') fleetEvent();
  else if (a === 'play') { showPage('overview'); togglePlay(true); }
  else if (a === 'approve') {
    if (!$('opsName').value.trim()) $('opsName').value = 'Demo operator';
    if (state.progressDay > 0) { resetVoyage(); }
    if (state.mode === 'fleet') { setPlanMode('single'); return; }
    approvePlan().then(() => showPage('operations'));
  }
  syncDemoBar();
}
function syncDemoBar() {
  const on = (k, v) => document.querySelector(`[data-demo=${k}]`)?.classList.toggle('on', !!v);
  const anyEvent = ['blockBab', 'threatBab', 'coverBab', 'blockSuez', 'blockHormuz', 'closeWestAfrica', 'stormCape'].some(k => state.toggles[k]);
  on('today', state.conditions === 'today' && !anyEvent && state.mode === 'single' && !state.progressDay);
  on('normal', state.conditions === 'normal');
  on('redsea', state.toggles.blockBab);
  on('atsea', state.progressDay > 0);
  on('fleet', state.mode === 'fleet');
  on('live', state.ais.on);
  on('play', state.sim.playing);
}

async function init() {
  timeTick(); setInterval(timeTick, 1000);
  try {
    const [fleet, network, land, cond] = await Promise.all([fetch('/api/fleet'), fetch('/api/network'), fetch('/assets/land.svg'), fetch('/api/conditions')]);
    if (!fleet.ok || !network.ok) throw Error('Planning API not available');
    state.fleet = await fleet.json(); state.network = await network.json();
    if (cond.ok) state.today = (await cond.json()).today;
    state.fuel = shipObj().default_fuel_onboard_t;
    state.cargo = shipObj().default_cargo || 'retail';
    if (land.ok) $('landSvg').innerHTML = await land.text();
    drawGrid(); drawNetwork(); attachControls(); syncControls(); attachChat(); loadAiStatus(); renderStatus();
    pollWeatherStatus(); setInterval(pollWeatherStatus, 60000);
    attachLayout();
    attachOps();
    attachCalibration();
    attachShell();
    attachSim(); attachWarnings(); attachIncidents();
    initLeafletMap();   // after the page layout exists; falls back to the SVG map when Leaflet is not available
    syncProgress();
    schedulePlan(0);
    pullNews();
  } catch (error) {
    document.querySelector('.content').innerHTML = `<div class="panel" style="padding:24px">Unable to start the planner: ${esc(error.message)}. Start the app with <code>python3 server.py</code> rather than opening the HTML file directly.</div>`;
  }
}
init();
