// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const C = require('../compare.js');

const sum = a => a.reduce((x, y) => x + y, 0);

test('score shares add up to exactly 100', () => {
  for (const d of [
    {cash_total_usd: 3127922, risk_adjustment_usd: 31279, time_preference_usd: 0, carbon_shadow_usd: 61000},
    {cash_total_usd: 1000, risk_adjustment_usd: 1000, time_preference_usd: 1000, carbon_shadow_usd: 1000},
    {cash_total_usd: 1, risk_adjustment_usd: 1, time_preference_usd: 1, carbon_shadow_usd: 0},
    {cash_total_usd: 7, risk_adjustment_usd: 3, time_preference_usd: 3, carbon_shadow_usd: 3},
  ]) {
    const s = C.scoreShares(d);
    assert.strictEqual(sum([s.cost, s.carbon, s.risk, s.time]), 100, JSON.stringify(s));
  }
});

test('shares follow the size of each part of the score', () => {
  const s = C.scoreShares({cash_total_usd: 800, risk_adjustment_usd: 100, time_preference_usd: 0, carbon_shadow_usd: 100});
  assert.deepStrictEqual(s, {cost: 80, carbon: 10, risk: 10, time: 0});
});

test('an all-zero or missing score gives zero shares, never NaN', () => {
  assert.deepStrictEqual(C.scoreShares({cash_total_usd: 0, risk_adjustment_usd: 0, time_preference_usd: 0, carbon_shadow_usd: 0}), {cost: 0, carbon: 0, risk: 0, time: 0});
  assert.deepStrictEqual(C.scoreShares(null), {cost: 0, carbon: 0, risk: 0, time: 0});
});

test('negative parts are treated as zero so shares stay between 0 and 100', () => {
  const s = C.scoreShares({cash_total_usd: 100, risk_adjustment_usd: -50, time_preference_usd: 0, carbon_shadow_usd: 0});
  assert.ok(Object.values(s).every(v => v >= 0 && v <= 100));
  assert.strictEqual(sum(Object.values(s)), 100);
});

const P = (id, cost, co2, risk) => ({id, cost, co2, risk});

test('a route that is worse on everything is not Pareto-efficient', () => {
  const front = C.paretoFront([P('a', 1, 1, 10), P('b', 2, 2, 20)]);
  assert.deepStrictEqual([...front], ['a']);
});

test('routes that trade off against each other are all efficient', () => {
  const front = C.paretoFront([P('suez', 2.7, 12000, 60), P('cape', 2.9, 14000, 30), P('cheap', 2.5, 15000, 70)]);
  assert.deepStrictEqual([...front].sort(), ['cape', 'cheap', 'suez']);
});

test('a dominated route is never highlighted, even among many', () => {
  const pts = [P('a', 1, 5, 5), P('b', 2, 4, 5), P('c', 3, 3, 5), P('dominated', 3, 5, 6)];
  const front = C.paretoFront(pts);
  assert.ok(!front.has('dominated'));
  assert.ok(front.has('a') && front.has('b') && front.has('c'));
});

test('a single route is efficient, identical routes both are, and nothing breaks on empty input', () => {
  assert.deepStrictEqual([...C.paretoFront([P('only', 1, 1, 1)])], ['only']);
  assert.deepStrictEqual([...C.paretoFront([P('x', 1, 1, 1), P('y', 1, 1, 1)])].sort(), ['x', 'y']);
  assert.strictEqual(C.paretoFront([]).size, 0);
});

const route = (label, days, cost, co2, risk) => ({label, days, cost, co2, risk});

test('why-it-won names what the winner is better at and what it costs', () => {
  const line = C.whyLine(route('Cape of Good Hope', 43, 3_127_922, 14000, 41), route('Suez Canal', 27, 2_718_164, 12000, 63));
  assert.strictEqual(line, 'Cape of Good Hope wins on risk (41 vs 63); it costs $410k more, takes 16 days longer and emits 2,000 t more CO₂ than Suez Canal.');
});

test('why-it-won when the winner is better on everything', () => {
  const line = C.whyLine(route('Suez Canal', 27, 2_700_000, 12000, 40), route('Cape of Good Hope', 43, 3_100_000, 14000, 60));
  assert.strictEqual(line, 'Suez Canal wins on cost, time, CO₂ and risk against Cape of Good Hope.');
});

test('why-it-won when the winner is cheaper but riskier', () => {
  const line = C.whyLine(route('Suez Canal', 27, 2_700_000, 12000, 63), route('Cape of Good Hope', 43, 3_100_000, 14000, 41));
  assert.match(line, /^Suez Canal wins on cost, time and CO₂ /);
  assert.match(line, /risk is higher \(63 vs 41\)/);
});

test('why-it-won handles a tie and a missing runner-up', () => {
  assert.strictEqual(C.whyLine(route('A', 10, 1_000_000, 100, 20), null), '');
  assert.match(C.whyLine(route('A', 10, 1_000_000, 100, 20), route('B', 10, 1_000_000, 100, 20)), /^A and B are level/);
});
