// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
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

// ---- zoom-aware speed: effective = base x 2^(k x (zRef - zoom)), clamped to 0.25x..8x ---------------------
test('zoom level is 0 at the world view and rises by 1 for every halving of the view', () => {
  assert.strictEqual(P.zoomLevel(1200, 1200), 0);
  assert.strictEqual(P.zoomLevel(600, 1200), 1);
  assert.strictEqual(P.zoomLevel(150, 1200), 3);
  assert.ok(P.zoomLevel(1200 * 1.35, 1200) < 0);   // zoomed out past the world view
});

test('the multiplier is exactly 1 at the reference zoom', () => {
  assert.strictEqual(P.zoomMultiplier(P.ZOOM_REF), 1);
});

test('the multiplier never rises as you zoom in, and zooming out speeds the ship up', () => {
  let prev = Infinity;
  for (let z = -1; z <= 5; z += 0.25) {
    const m = P.zoomMultiplier(z);
    assert.ok(m <= prev + 1e-12, `rose at zoom ${z}`);
    prev = m;
  }
  assert.ok(P.zoomMultiplier(0) > 1, 'world view should be faster than the reference');
  assert.ok(P.zoomMultiplier(3.8) < 1, 'deepest zoom should be slower than the reference');
});

test('the multiplier is clamped to 0.25x..8x', () => {
  assert.strictEqual(P.zoomMultiplier(-50), 8);
  assert.strictEqual(P.zoomMultiplier(50), 0.25);
  assert.ok(P.zoomMultiplier(0) < 8 && P.zoomMultiplier(3.8) > 0.25, 'the real zoom range should not hit the clamps');
});

test('effective speed is base x multiplier when auto-scale is on and the base when it is off', () => {
  assert.strictEqual(P.effectiveSpeed(10, P.ZOOM_REF, true), 10);
  assert.ok(Math.abs(P.effectiveSpeed(10, 0, true) - 10 * P.zoomMultiplier(0)) < 1e-12);
  assert.strictEqual(P.effectiveSpeed(10, 0, false), 10);
});

test('advance accepts an effective speed above the slider maximum (auto-scale at 200 h/s zoomed out)', () => {
  const eff = P.effectiveSpeed(200, 0, true);
  assert.ok(eff > 200);
  const r = P.advance(0, 0.1, eff, Infinity);
  assert.ok(Math.abs(r.offsetH - 0.1 * eff) < 1e-9);
  assert.ok(P.advance(0, 0.1, 1e9, Infinity).offsetH <= 0.1 * 200 * 8 + 1e-9);   // still bounded
});

test('simulated time stays continuous while the zoom (and so the speed) changes every frame', () => {
  // zoom in and out during playback; the clock must equal the sum of dt x eased speed, with no jump at any frame
  let t = 0, speedNow = P.effectiveSpeed(10, 0, true);
  for (let i = 0; i < 120; i++) {
    const zoom = 1.9 * (1 + Math.sin(i / 10));                 // 0 .. 3.8
    const target = P.effectiveSpeed(10, zoom, true);
    speedNow += (target - speedNow) * (1 - Math.exp(-0.016 / 0.1));
    const next = P.advance(t, 0.016, speedNow, Infinity).offsetH;
    assert.ok(next - t <= 0.016 * 10 * 8 + 1e-9 && next >= t, `jump at frame ${i}`);
    t = next;
  }
  assert.ok(t > 0);
});

test('the rate caption shows base, multiplier and result, and says which way you are zoomed', () => {
  assert.strictEqual(P.rateCaption(10, 3.2, true), '10 h/s × 3.2 (zoomed out) = 32 h/s');
  assert.strictEqual(P.rateCaption(10, 0.5, true), '10 h/s × 0.5 (zoomed in) = 5 h/s');
  assert.strictEqual(P.rateCaption(10, 1, true), '10 h/s');
  assert.strictEqual(P.rateCaption(10, 3.2, false), '10 h/s');
  assert.strictEqual(P.rateCaption(P.MIN_SPEED, 1, true), 'Real time');
});
