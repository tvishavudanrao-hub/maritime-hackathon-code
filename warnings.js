// Condition-change warnings: what raises one, when it re-alerts, when it clears. Pure functions, no DOM; app.js
// draws the banner, the map markers and the timeline. Warnings are advisory: nothing here closes a corridor.
(function (root) {
  const SEVERITY = {
    info:     {rank: 1, icon: 'ℹ', label: 'Info'},
    warning:  {rank: 2, icon: '⚠', label: 'Warning'},
    critical: {rank: 3, icon: '⛔', label: 'Critical'},
  };
  const LOG_MAX = 60;
  const SIM = 'SIMULATED scenario (operator setting, not a real event)';

  const fingerprint = c => `${c.severity}|${c.detail || ''}`;
  const createCenter = () => ({active: {}, log: [], dismissed: {}});

  // Feed it every cause that is true right now. It returns what is new, what changed and what went away, and
  // keeps the timeline. A cause that is unchanged does not alert again.
  function update(center, causes, now) {
    const out = {raised: [], changed: [], cleared: []}, seen = new Set();
    const note = (event, c) => {
      center.log.push({t: now, key: c.key, event, severity: c.severity, title: c.title, detail: c.detail || '', source: c.source || ''});
      if (center.log.length > LOG_MAX) center.log.splice(0, center.log.length - LOG_MAX);
    };
    for (const c of causes) {
      seen.add(c.key);
      const prev = center.active[c.key], fp = fingerprint(c);
      if (!prev) { out.raised.push(c); note('raised', c); }
      else if (prev.fp !== fp) { out.changed.push(c); note('changed', c); delete center.dismissed[c.key]; }
      center.active[c.key] = {...c, fp, since: prev ? prev.since : now, updated: now};
    }
    for (const key of Object.keys(center.active)) {
      if (seen.has(key)) continue;
      out.cleared.push(center.active[key]); note('cleared', center.active[key]);
      delete center.active[key]; delete center.dismissed[key];
    }
    return out;
  }
  const active = center => Object.values(center.active).sort((a, b) =>
    SEVERITY[b.severity].rank - SEVERITY[a.severity].rank || (a.since < b.since ? -1 : a.since > b.since ? 1 : 0));
  const dismiss = (center, key) => { if (center.active[key]) center.dismissed[key] = center.active[key].fp; };
  // The banner shows the most severe warning that has not been dismissed (dismissal lasts until it changes).
  const banner = center => active(center).find(c => center.dismissed[c.key] !== c.fp) || null;
  const bannerText = c => `${SEVERITY[c.severity].icon} Condition change: ${c.title}${c.detail ? ' · ' + c.detail : ''}`;

  // ---- causes -----------------------------------------------------------------------------------------------
  // Scenarios the operator switched on: closures are critical, storms are critical above the ship's wave limit.
  function scenarioCauses(events, limitM, locOf) {
    return events.map(e => {
      const severity = e.type === 'zone_closure' || e.type === 'port_closure' ? 'critical'
        : e.type === 'storm' ? (e.hs_m >= limitM ? 'critical' : 'warning')
        : e.type === 'security' || e.type === 'cover_cancelled' ? 'warning' : 'info';
      return {key: 'scenario:' + e.id, kind: 'scenario', severity, title: e.description || e.type, detail: '', source: e.source || SIM,
        loc: locOf ? locOf(e) : undefined};
    });
  }
  // Waves on the route ahead against the ship's limit (legs come from the plan: max_wave_m per leg).
  function weatherCauses(legs, limitM, locOf, source) {
    const out = [];
    for (const l of legs || []) {
      const w = l.max_wave_m;
      if (!(w >= 0.8 * limitM)) continue;
      const over = w >= limitM;
      out.push({key: 'weather:' + l.to, kind: 'weather', severity: over ? 'critical' : 'warning',
        title: `Waves up to ${w} m on the leg ${l.from_name || l.from} → ${l.to_name || l.to}`,
        detail: `${over ? 'above' : 'close to'} the ship's ${limitM} m limit`, source, loc: locOf ? locOf(l.to) : undefined});
    }
    return out;
  }
  // Headlines about a region the route passes. Advisory only, never critical, never a closure.
  function newsCauses(articles, labelOf) {
    const byTopic = {};
    for (const a of articles || []) if (a.onRoute && a.potentialDisruption) byTopic[a.topic] = (byTopic[a.topic] || 0) + 1;
    return Object.entries(byTopic).map(([topic, n]) => ({key: 'news:' + topic, kind: 'news', severity: 'warning',
      title: `${n} unverified disruption headline${n > 1 ? 's' : ''} about ${labelOf ? labelOf(topic) : topic}`, detail: '',
      source: 'GDELT / Google News headlines (unverified press signal)'}));
  }
  // Route risk score (0-100) moved since the last plan.
  function riskCause(prev, next, rerouted) {
    if (prev == null || next == null || Math.abs(next - prev) < 10) return null;
    const severity = next < prev ? 'info' : next - prev >= 25 ? 'critical' : 'warning';
    return {key: 'risk:route', kind: 'risk', severity, title: next < prev ? 'Route risk fell' : 'Route risk rose',
      detail: `risk ${prev} → ${next} · ${rerouted ? 'rerouted' : 'staying on course'}`, source: 'Model estimate of the recommended route'};
  }

  const api = {SEVERITY, LOG_MAX, createCenter, update, active, dismiss, banner, bannerText, scenarioCauses, weatherCauses, newsCauses, riskCause};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLWarnings = api;
})(typeof window !== 'undefined' ? window : globalThis);
