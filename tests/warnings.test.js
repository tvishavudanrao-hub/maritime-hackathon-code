// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const W = require('../warnings.js');

const cause = (key, severity = 'warning', detail = 'd', extra = {}) => ({key, severity, title: key + ' title', detail, source: 'test source', ...extra});
const T = n => `2026-10-17T12:0${n}:00Z`;

test('a new cause is raised once and logged with its source', () => {
  const c = W.createCenter();
  const r = W.update(c, [cause('a')], T(0));
  assert.deepStrictEqual(r.raised.map(x => x.key), ['a']);
  assert.strictEqual(W.active(c).length, 1);
  assert.strictEqual(c.log[0].event, 'raised');
  assert.strictEqual(c.log[0].source, 'test source');
});

test('the same cause does not re-alert while nothing changes (dedupe)', () => {
  const c = W.createCenter();
  W.update(c, [cause('a')], T(0));
  const again = W.update(c, [cause('a')], T(1));
  assert.deepStrictEqual([again.raised.length, again.changed.length, again.cleared.length], [0, 0, 0]);
  assert.strictEqual(c.log.length, 1);
});

test('a changed cause (new severity or detail) alerts again and keeps its start time', () => {
  const c = W.createCenter();
  W.update(c, [cause('a', 'warning', 'x')], T(0));
  const r = W.update(c, [cause('a', 'critical', 'x')], T(1));
  assert.deepStrictEqual(r.changed.map(x => x.key), ['a']);
  assert.strictEqual(c.active.a.since, T(0));
  assert.strictEqual(c.log.at(-1).event, 'changed');
  const r2 = W.update(c, [cause('a', 'critical', 'y')], T(2));
  assert.strictEqual(r2.changed.length, 1);
});

test('a cause that goes away is cleared and logged', () => {
  const c = W.createCenter();
  W.update(c, [cause('a'), cause('b')], T(0));
  const r = W.update(c, [cause('b')], T(1));
  assert.deepStrictEqual(r.cleared.map(x => x.key), ['a']);
  assert.deepStrictEqual(W.active(c).map(x => x.key), ['b']);
  assert.strictEqual(c.log.at(-1).event, 'cleared');
});

test('after it clears, the same cause is a new alert', () => {
  const c = W.createCenter();
  W.update(c, [cause('a')], T(0));
  W.update(c, [], T(1));
  assert.strictEqual(W.update(c, [cause('a')], T(2)).raised.length, 1);
});

test('severity levels each have their own icon, label and rank', () => {
  const {info, warning, critical} = W.SEVERITY;
  assert.ok(info.rank < warning.rank && warning.rank < critical.rank);
  assert.strictEqual(new Set([info.icon, warning.icon, critical.icon]).size, 3);
  assert.strictEqual(new Set([info.label, warning.label, critical.label]).size, 3);
});

test('active warnings are sorted most severe first', () => {
  const c = W.createCenter();
  W.update(c, [cause('i', 'info'), cause('c', 'critical'), cause('w', 'warning')], T(0));
  assert.deepStrictEqual(W.active(c).map(x => x.key), ['c', 'w', 'i']);
});

test('banner shows the most severe warning and can be dismissed', () => {
  const c = W.createCenter();
  W.update(c, [cause('w', 'warning'), cause('c', 'critical')], T(0));
  assert.strictEqual(W.banner(c).key, 'c');
  W.dismiss(c, 'c');
  assert.strictEqual(W.banner(c).key, 'w');
  W.dismiss(c, 'w');
  assert.strictEqual(W.banner(c), null);
});

test('a dismissed warning stays hidden until it changes, and stays in the timeline and on the map', () => {
  const c = W.createCenter();
  W.update(c, [cause('a', 'warning', 'x')], T(0));
  W.dismiss(c, 'a');
  W.update(c, [cause('a', 'warning', 'x')], T(1));
  assert.strictEqual(W.banner(c), null);
  assert.strictEqual(W.active(c).length, 1);
  W.update(c, [cause('a', 'critical', 'x')], T(2));
  assert.strictEqual(W.banner(c).key, 'a');
});

test('the timeline is capped', () => {
  const c = W.createCenter();
  for (let i = 0; i < 120; i++) W.update(c, [cause('k' + i)], T(0));
  assert.ok(c.log.length <= W.LOG_MAX);
});

test('scenario causes: closures are critical, storms warn (critical above the ship limit), all say SIMULATED', () => {
  const ev = [
    {id: 'bab', type: 'zone_closure', description: 'Red Sea closed'},
    {id: 's1', type: 'storm', hs_m: 5, description: 'Storm'},
    {id: 's2', type: 'storm', hs_m: 9, description: 'Big storm'},
    {id: 'sec', type: 'security', description: 'Threat raised'},
    {id: 'f', type: 'fuel_price', description: 'Fuel up'},
  ];
  const sev = Object.fromEntries(W.scenarioCauses(ev, 7).map(c => [c.key, c.severity]));
  assert.deepStrictEqual(sev, {'scenario:bab': 'critical', 'scenario:s1': 'warning', 'scenario:s2': 'critical', 'scenario:sec': 'warning', 'scenario:f': 'info'});
  assert.ok(W.scenarioCauses(ev, 7).every(c => /SIMULATED/.test(c.source)));
});

test('weather causes: waves over the ship limit are critical, near it are warnings, calm legs are ignored', () => {
  const legs = [{from: 'A', to: 'B', max_wave_m: 3}, {from: 'B', to: 'C', max_wave_m: 6}, {from: 'C', to: 'D', max_wave_m: 7.5}];
  const out = W.weatherCauses(legs, 7, n => [1, 2], 'Open-Meteo forecast');
  assert.deepStrictEqual(out.map(c => [c.key, c.severity]), [['weather:C', 'warning'], ['weather:D', 'critical']]);
  assert.deepStrictEqual(out[1].loc, [1, 2]);
  assert.match(out[1].title, /7\.5 m/);
  assert.match(out[1].detail, /above the ship's 7 m limit/);
  assert.match(out[0].detail, /close to the ship's 7 m limit/);
  assert.strictEqual(out[1].source, 'Open-Meteo forecast');
});

test('news causes: only on-route disruption headlines, grouped per region, never critical', () => {
  const arts = [
    {topic: 'bab', onRoute: true, potentialDisruption: true}, {topic: 'bab', onRoute: true, potentialDisruption: true},
    {topic: 'hormuz', onRoute: false, potentialDisruption: true}, {topic: 'suez', onRoute: true, potentialDisruption: false},
  ];
  const out = W.newsCauses(arts, z => z.toUpperCase());
  assert.deepStrictEqual(out.map(c => c.key), ['news:bab']);
  assert.strictEqual(out[0].severity, 'warning');
  assert.match(out[0].title, /2 unverified/);
  assert.match(out[0].source, /unverified/);
});

test('AIS anomalies become advisory warnings that keep their wording, source and place', () => {
  const anomalies = [
    {id: 'stopped_cluster:Suez Canal', kind: 'stopped_cluster', severity: 'warning', title: '4 vessels possibly stopped in Suez Canal', detail: 'A queue or blockage is possible; not confirmed.', loc: [30.5, 32.4], source: 'AISStream.io (live AIS, advisory)'},
    {id: 'stopped_vessel:111', kind: 'stopped_vessel', severity: 'info', title: 'X possibly stopped', detail: 'd', loc: [12, 43], source: 'AISStream.io (live AIS, advisory)'},
  ];
  const out = W.aisCauses(anomalies);
  assert.deepStrictEqual(out.map(c => [c.key, c.kind, c.severity]), [['ais:stopped_cluster:Suez Canal', 'ais', 'warning'], ['ais:stopped_vessel:111', 'ais', 'info']]);
  assert.deepStrictEqual(out[0].loc, [30.5, 32.4]);
  assert.match(out[0].title, /possibly/);
  assert.match(out[0].source, /AISStream/);
  assert.match(out[0].detail, /not confirmed/);
});

test('AIS causes never exceed warning (they are possibilities, not incidents) and tolerate missing data', () => {
  assert.strictEqual(W.aisCauses([{id: 'a', severity: 'critical', title: 't', loc: [1, 2], source: 's'}])[0].severity, 'warning');
  assert.strictEqual(W.aisCauses([{id: 'a', severity: 'bogus', title: 't', loc: [1, 2], source: 's'}])[0].severity, 'info');
  assert.deepStrictEqual(W.aisCauses(undefined), []);
  assert.deepStrictEqual(W.aisCauses(null), []);
});

test('risk cause: reports the move in points and what the plan did', () => {
  assert.strictEqual(W.riskCause(34, 38, false), null);
  const up = W.riskCause(34, 52, false);
  assert.strictEqual(up.severity, 'warning');
  assert.strictEqual(up.detail, 'risk 34 → 52 · staying on course');
  assert.strictEqual(W.riskCause(34, 70, true).severity, 'critical');
  assert.match(W.riskCause(34, 70, true).detail, /rerouted/);
  assert.strictEqual(W.riskCause(52, 34, false).severity, 'info');
  assert.strictEqual(W.riskCause(null, 50, false), null);
});

test('banner text names the condition, its source and the risk move', () => {
  const t = W.bannerText(cause('a', 'warning', 'risk 34 → 52 · rerouted', {title: 'Storm 7.5 m waves ahead'}));
  assert.match(t, /Condition change/);
  assert.match(t, /Storm 7\.5 m waves ahead/);
  assert.match(t, /risk 34 → 52 · rerouted/);
});
