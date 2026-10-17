// Searchable incidents: a catalog built from the network's own zones and ports (plus a few named storm areas), ranked by how
// much each one matters to the current route, and turned into the events the optimizer already understands.
// Every incident is a SIMULATED scenario; nothing here says that anything real has happened. Pure functions, no DOM.
(function (root) {
  const SOURCE = 'Incident search (SIMULATED scenario, not a real event)';
  const GROUPS = ['On your route', 'Affects alternatives', 'Everything else'];
  const SEVERITY_RANK = {critical: 3, warning: 2, info: 1};
  const ICON = {info: 'ℹ', warning: '⚠', critical: '⛔'};
  const severityIcon = s => ICON[s] || ICON.info;

  // Words people use for each zone, so "houthi" or "piracy" finds the right place.
  const ZONE_ALIASES = {
    suez: 'suez canal egypt', bab: 'red sea bab el-mandeb houthi yemen aden', hormuz: 'persian gulf iran strait',
    malacca: 'singapore strait indonesia malaysia', cape: 'cape of good hope south africa agulhas', gulf_guinea: 'west africa nigeria piracy',
  };
  // Named areas for storm scenarios (centre, radius in nautical miles). Scenario parameters, not forecasts.
  const STORMS = [
    ['storm-biscay', 'Bay of Biscay', [45, -8], 300], ['storm-cape', 'Cape of Good Hope', [-35, 22], 300], ['storm-arabian-sea', 'Arabian Sea', [16, 64], 350],
    ['storm-south-china-sea', 'South China Sea', [14, 114], 300], ['storm-east-china-sea', 'East China Sea', [28, 126], 300], ['storm-sicily', 'central Mediterranean', [36, 15], 250],
    ['storm-north-sea', 'North Sea', [56, 3], 250], ['storm-indian-ocean', 'Indian Ocean', [-14, 75], 300], ['storm-southern-ocean', 'Southern Ocean', [-45, 50], 400],
  ];

  const item = (id, kind, severity, label, extra) => ({id, kind, severity, label, haystack: `${label} ${(extra && extra.words) || ''}`.toLowerCase(), ...extra});

  function buildCatalog(network) {
    const out = [];
    for (const [z, info] of Object.entries((network && network.zones) || {})) {
      const aliases = ZONE_ALIASES[z] || '';
      out.push(item(`close-${z}`, 'zone_closure', 'critical', `${info.label} closed`, {zone: z, words: `${aliases} closure closed blockade shut`}));
      out.push(item(`threat-${z}`, 'security', 'warning', `${info.label} threat level raised`, {zone: z, words: `${aliases} threat attack security risk`}));
    }
    for (const [p, info] of Object.entries((network && network.ports) || {})) {
      out.push(item(`portclose-${p}`, 'port_closure', 'critical', `${info.name} port closed`, {port: p, words: 'closure closed shut'}));
      out.push(item(`congest-${p}`, 'port_congestion', 'warning', `${info.name} congestion (48 h wait)`, {port: p, words: 'queue wait delay'}));
    }
    for (const [id, area, center, radius] of STORMS) {
      out.push(item(id, 'storm', 'warning', `Severe storm in the ${area}`, {center, radius_nm: radius, words: 'weather waves gale cyclone typhoon hurricane'}));
    }
    return out;
  }

  function distanceNm(a, b) {
    const r = Math.PI / 180, dLat = (b[0] - a[0]) * r, dLon = (b[1] - a[1]) * r;
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLon / 2) ** 2;
    return 3440.065 * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
  }

  // 0 = on your route, 1 = affects the alternatives, 2 = everything else.
  function groupOf(it, ctx) {
    const has = (list, v) => (list || []).includes(v), near = (points, c, r) => (points || []).some(p => distanceNm(p, c) <= r + 150);
    if (it.zone) return has(ctx.routeZones, it.zone) ? 0 : has(ctx.altZones, it.zone) ? 1 : 2;
    if (it.port) return has(ctx.routePorts, it.port) ? 0 : has(ctx.altPorts, it.port) ? 1 : 2;
    if (it.center) return near(ctx.routePoints, it.center, it.radius_nm) ? 0 : near(ctx.altPoints, it.center, it.radius_nm) ? 1 : 2;
    return 2;
  }

  // A word starting the label scores most, then any match in the label, then a match in the aliases.
  function textScore(it, tokens) {
    const label = it.label.toLowerCase(), words = label.split(/[^a-z0-9]+/);
    let total = 0;
    for (const t of tokens) {
      const s = words.some(w => w.startsWith(t)) ? 3 : label.includes(t) ? 2 : it.haystack.includes(t) ? 1 : 0;
      if (!s) return 0;
      total += s;
    }
    return total;
  }

  // Matching incidents, most relevant first: your route, then the alternatives, then the rest; then text match; then severity.
  function rank(catalog, query, ctx) {
    const tokens = String(query || '').toLowerCase().split(/\s+/).filter(Boolean), c = ctx || {};
    const rows = [];
    for (const it of catalog) {
      const score = tokens.length ? textScore(it, tokens) : 0;
      if (tokens.length && !score) continue;
      const g = groupOf(it, c);
      rows.push({item: it, group: GROUPS[g], g, score});
    }
    rows.sort((a, b) => a.g - b.g || b.score - a.score || SEVERITY_RANK[b.item.severity] - SEVERITY_RANK[a.item.severity] || (a.item.label < b.item.label ? -1 : 1));
    return rows.map(({item: it, group, score}) => ({item: it, group, score}));
  }

  // The optimizer event for an incident (same shapes the scenario toggles use).
  function eventFor(it, nowIso) {
    const base = {id: `inc-${it.id}`, source: SOURCE, timestamp: nowIso, description: it.label};
    switch (it.kind) {
      case 'zone_closure': return {...base, type: 'zone_closure', zone: it.zone};
      case 'security': return {...base, type: 'security', zone: it.zone, level: 0.3};
      case 'port_closure': return {...base, type: 'port_closure', port: it.port};
      case 'port_congestion': return {...base, type: 'port_congestion', port: it.port, wait_h: 48};
      default: return {...base, type: 'storm', center: it.center, radius_nm: it.radius_nm, hs_m: 7.5, wind_ms: 25, start_h: 0, end_h: 2000};
    }
  }

  const api = {GROUPS, SOURCE, buildCatalog, rank, eventFor, severityIcon};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLIncidents = api;
})(typeof window !== 'undefined' ? window : globalThis);
