// Run with: node --test tests/   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const P = require('../playback.js');

test('slider ends map to the min and max playback speed', () => {
  assert.ok(Math.abs(P.sliderToSpeed(0) - P.MIN_SPEED) < 1e-12);
  assert.ok(Math.abs(P.sliderToSpeed(1) - P.MAX_SPEED) < 1e-9);
  assert.strictEqual(P.MAX_SPEED, 200);
});

test('slider is monotonic, continuous and round-trips', () => {
  let prev = 0;
  for (let i = 0; i <= 100; i++) {
    const s = P.sliderToSpeed(i / 100);
    assert.ok(s > prev, `not increasing at ${i}`);
    prev = s;
    assert.ok(Math.abs(P.speedToSlider(s) - i / 100) < 1e-9);
  }
});

test('out-of-range values are clamped', () => {
  assert.strictEqual(P.clampSpeed(1e6), 200);
  assert.strictEqual(P.clampSpeed(0), P.MIN_SPEED);
  assert.strictEqual(P.clampSpeed(NaN), P.MIN_SPEED);
  assert.strictEqual(P.sliderToSpeed(-3), P.MIN_SPEED);
  assert.strictEqual(P.sliderToSpeed(7), 200);
});

test('caption says real time at the bottom and h/s with day length above', () => {
  assert.strictEqual(P.speedCaption(P.MIN_SPEED), 'Real time');
  assert.strictEqual(P.speedCaption(10), '10 h/s · 1 day ≈ 2.4 s');
  assert.strictEqual(P.speedCaption(200), '200 h/s · 1 day ≈ 0.12 s');
  assert.strictEqual(P.speedCaption(0.25), '0.25 h/s · 1 day ≈ 96 s');
});

test('advance moves the clock by dt x speed', () => {
  const r = P.advance(0, 0.1, 10, Infinity);
  assert.ok(Math.abs(r.offsetH - 1) < 1e-9);
  assert.strictEqual(r.done, false);
});

test('a long frame gap (hidden tab) is capped, so the clock never jumps', () => {
  const r = P.advance(0, 30, 100, Infinity);
  assert.ok(r.offsetH <= P.MAX_FRAME_S * 100 + 1e-9);
});

test('the clock stops exactly at the end, with no overshoot', () => {
  const r = P.advance(99.5, 0.2, 200, 100);
  assert.strictEqual(r.offsetH, 100);
  assert.strictEqual(r.done, true);
});

test('advance is continuous when the speed changes between frames', () => {
  let t = 0;
  for (const s of [1, 1, 50, 50, 200, 5]) t = P.advance(t, 0.016, s, Infinity).offsetH;
  const expect = 0.016 * (1 + 1 + 50 + 50 + 200 + 5);
  assert.ok(Math.abs(t - expect) < 1e-9);
});

test('playback is bounded by the latest arrival, counting each voyage clock offset', () => {
  const now = Date.parse('2026-10-17T00:00:00Z');
  const voyages = [
    {eta: '2026-10-20T00:00:00Z', clock_offset_h: 0},   // 72 h away
    {eta: '2026-10-19T00:00:00Z', clock_offset_h: 12},  // 48 h - 12 h = 36 h away
  ];
  assert.strictEqual(P.hoursUntilAllArrive(voyages, now, 0), 72);
  assert.strictEqual(P.hoursUntilAllArrive([], now, 0), 0);
  assert.strictEqual(P.hoursUntilAllArrive(voyages, now, 80), 0);
});
