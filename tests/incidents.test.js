// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const I = require('../incidents.js');

// A small network shaped like data/network.json
const NET = {
  zones: {
    suez: {label: 'Suez Canal', location: [30.5, 32.3]}, bab: {label: 'Bab el-Mandeb / Red Sea', location: [12.6, 43.4]},
    hormuz: {label: 'Strait of Hormuz', location: [26.6, 56.3]}, malacca: {label: 'Strait of Malacca', location: [3.0, 100.5]},
    cape: {label: 'Cape of Good Hope', location: [-35, 20]}, gulf_guinea: {label: 'Gulf of Guinea', location: [3, 3]},
  },
  ports: {HAM: {name: 'Hamburg', loc: [53.5, 9.9]}, SHA: {name: 'Shanghai', loc: [31.2, 121.5]}, PSD: {name: 'Port Said', loc: [31.3, 32.3]},
    JEA: {name: 'Jebel Ali', loc: [25, 55]}, DUR: {name: 'Durban', loc: [-29.9, 31]}},
};
const CATALOG = I.buildCatalog(NET);
const ctxHamSha = {routeZones: ['suez', 'bab', 'malacca'], routePorts: ['HAM', 'PSD', 'SHA'], altZones: ['cape'], altPorts: ['DUR'], routePoints: [[53.5, 9.9], [47, -6], [36, 15], [31, 32], [12, 44], [3, 100], [31, 121]], altPoints: [[-35, 20], [-29, 31]]};

test('the catalog is built from the network: closure and threat per zone, closure and congestion per port, plus storms', () => {
  const ids = CATALOG.map(c => c.id);
  assert.strictEqual(new Set(ids).size, ids.length, 'ids are unique');
  for (const z of Object.keys(NET.zones)) { assert.ok(ids.includes(`close-${z}`)); assert.ok(ids.includes(`threat-${z}`)); }
  for (const p of Object.keys(NET.ports)) { assert.ok(ids.includes(`portclose-${p}`)); assert.ok(ids.includes(`congest-${p}`)); }
  assert.ok(CATALOG.some(c => c.kind === 'storm'));
});

test('every item has a label, a kind, a severity and something to search on', () => {
  for (const c of CATALOG) {
    assert.ok(c.label && c.kind && ['info', 'warning', 'critical'].includes(c.severity), c.id);
    assert.ok(c.haystack.includes(c.label.toLowerCase()), c.id);
  }
});

test('with no query, incidents on the route come first, then alternatives, then the rest', () => {
  const groups = I.rank(CATALOG, '', ctxHamSha).map(r => r.group);
  const order = ['On your route', 'Affects alternatives', 'Everything else'];
  assert.deepStrictEqual([...groups].sort((a, b) => order.indexOf(a) - order.indexOf(b)), groups);
});

test('a Hamburg to Shanghai route surfaces Suez, Red Sea and Malacca, not Hormuz or the Gulf of Guinea', () => {
  const onRoute = I.rank(CATALOG, '', ctxHamSha).filter(r => r.group === 'On your route').map(r => r.item);
  const zones = new Set(onRoute.filter(i => i.zone).map(i => i.zone));
  assert.ok(['suez', 'bab', 'malacca'].every(z => zones.has(z)));
  assert.ok(!zones.has('hormuz') && !zones.has('gulf_guinea') && !zones.has('cape'));
  assert.ok(onRoute.some(i => i.port === 'PSD'), 'a port on the route counts too');
  assert.ok(!onRoute.some(i => i.port === 'JEA'));
});

test('the Cape and Durban are labelled as affecting alternatives', () => {
  const alt = I.rank(CATALOG, '', ctxHamSha).filter(r => r.group === 'Affects alternatives').map(r => r.item);
  assert.ok(alt.some(i => i.zone === 'cape') && alt.some(i => i.port === 'DUR'));
});

test('typing filters by every word, in any order, and ignores case', () => {
  const ids = q => I.rank(CATALOG, q, ctxHamSha).map(r => r.item.id);
  assert.deepStrictEqual(ids('red sea'), ['close-bab', 'threat-bab']);
  assert.deepStrictEqual(ids('SUEZ THREAT'), ['threat-suez']);
  assert.deepStrictEqual(ids('threat suez'), ['threat-suez']);
  assert.ok(ids('canal').includes('close-suez'));
});

test('aliases find incidents by the names people use', () => {
  const ids = q => I.rank(CATALOG, q, ctxHamSha).map(r => r.item.id);
  assert.ok(ids('houthi').includes('threat-bab'));
  assert.ok(ids('piracy').includes('threat-gulf_guinea'));
  assert.ok(ids('iran').includes('close-hormuz'));
});

test('a word at the start of the label ranks above a match in the middle', () => {
  const r = I.rank(CATALOG, 'port', ctxHamSha).map(x => x.item.id);
  assert.ok(r.indexOf('portclose-PSD') < r.indexOf('close-suez') || !r.includes('close-suez'));
});

test('a query that matches nothing returns an empty list', () => {
  assert.deepStrictEqual(I.rank(CATALOG, 'zzzz qqqq', ctxHamSha), []);
});

test('a storm near the route counts as on the route, a far one does not', () => {
  const ranked = I.rank(CATALOG, 'storm', ctxHamSha);
  const biscay = ranked.find(r => r.item.id === 'storm-biscay'), aus = ranked.find(r => r.item.id === 'storm-southern-ocean');
  assert.ok(biscay && biscay.group === 'On your route', JSON.stringify(biscay && biscay.group));
  assert.ok(aus && aus.group !== 'On your route');
});

test('events match the optimizer schema, are simulated, and have stable ids', () => {
  const ev = id => I.eventFor(CATALOG.find(c => c.id === id), '2026-10-17T12:00:00Z');
  const closure = ev('close-suez');
  assert.deepStrictEqual([closure.type, closure.zone, closure.id], ['zone_closure', 'suez', 'inc-close-suez']);
  assert.match(closure.source, /SIMULATED/i);
  assert.ok(closure.description.includes('Suez'));
  const threat = ev('threat-bab');
  assert.deepStrictEqual([threat.type, threat.zone], ['security', 'bab']);
  assert.ok(threat.level > 0 && threat.level <= 1);
  assert.deepStrictEqual([ev('portclose-PSD').type, ev('portclose-PSD').port], ['port_closure', 'PSD']);
  const cong = ev('congest-HAM');
  assert.deepStrictEqual([cong.type, cong.port, cong.wait_h], ['port_congestion', 'HAM', 48]);
  const storm = ev('storm-biscay');
  assert.strictEqual(storm.type, 'storm');
  assert.ok(Array.isArray(storm.center) && storm.center.length === 2 && storm.radius_nm > 0 && storm.hs_m > 0 && storm.end_h > storm.start_h);
  assert.strictEqual(ev('close-suez').id, closure.id);
});

test('closures are critical, threats and congestion are warnings', () => {
  const sev = id => CATALOG.find(c => c.id === id).severity;
  assert.deepStrictEqual([sev('close-suez'), sev('portclose-PSD'), sev('threat-bab'), sev('congest-HAM')], ['critical', 'critical', 'warning', 'warning']);
});

test('an empty or missing network gives a catalog with only the storms', () => {
  assert.ok(I.buildCatalog({}).every(c => c.kind === 'storm'));
  assert.ok(I.buildCatalog(null).every(c => c.kind === 'storm'));
});

test('severity icon helper gives each level its own symbol', () => {
  assert.strictEqual(new Set(['info', 'warning', 'critical'].map(I.severityIcon)).size, 3);
});
