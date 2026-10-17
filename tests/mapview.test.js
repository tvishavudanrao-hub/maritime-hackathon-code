// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const M = require('../mapview.js');

const W = 1200, H = 650, WORLD = {x: 0, y: 0, w: W, h: H};
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('zooming keeps the point under the cursor fixed', () => {
  const [ax, ay] = [850, 250];   // somewhere over Asia
  const v = M.zoomView(WORLD, 300, ax, ay, W, H);
  near((ax - v.x) / v.w, (ax - WORLD.x) / WORLD.w);
  near((ay - v.y) / v.h, (ay - WORLD.y) / WORLD.h);
});

test('zooming into Asia ends with Asia in view, not the map centre', () => {
  const [ax, ay] = [850, 250];
  const v = M.zoomView(WORLD, W / 8, ax, ay, W, H);
  assert.ok(ax > v.x && ax < v.x + v.w && ay > v.y && ay < v.y + v.h);
  assert.ok(v.x > W / 2 - 1, 'view should have moved east of the world centre');
});

test('aspect ratio is preserved at every zoom', () => {
  for (const w of [W * 2, W, 600, 200, 10]) {
    const v = M.zoomView(WORLD, w, 400, 300, W, H);
    near(v.h / v.w, H / W);
  }
});

test('zoom is limited to MAX_ZOOM in and 1.35x out', () => {
  near(M.zoomView(WORLD, 1, 600, 325, W, H).w, W / M.MAX_ZOOM);
  near(M.zoomView(WORLD, 1e9, 600, 325, W, H).w, W * 1.35);
  assert.ok(M.MAX_ZOOM >= 12, 'must be able to zoom in far enough to see a port region');
});

test('no anchor zooms about the centre of the view', () => {
  const v = M.zoomView(WORLD, 600, null, null, W, H);
  near(v.x + v.w / 2, W / 2);
  near(v.y + v.h / 2, H / 2);
});

test('zoom in then out at the same point returns to the start', () => {
  const a = M.zoomView(WORLD, 400, 900, 200, W, H);
  const b = M.zoomView(a, W, 900, 200, W, H);
  for (const k of ['x', 'y', 'w', 'h']) near(b[k], WORLD[k], 1e-6);
});

test('panning is clamped so the map can never be dragged out of view', () => {
  const zoomed = M.zoomView(WORLD, 300, 600, 325, W, H);
  const far = M.panView(zoomed, 1e6, -1e6, W, H);
  assert.ok(far.x <= W - 0.65 * far.w + 1e-9 && far.y >= -0.35 * far.h - 1e-9);
  const back = M.panView(zoomed, -1e6, 1e6, W, H);
  assert.ok(back.x >= -0.35 * back.w - 1e-9 && back.y <= H - 0.65 * back.h + 1e-9);
});

test('marker scale keeps icons a constant size on screen', () => {
  near(M.markerScale(WORLD, W), 1);
  near(M.markerScale({...WORLD, w: W / 4, h: H / 4}, W), 0.25);
  assert.ok(M.markerScale({...WORLD, w: 0.001}, W) >= M.MIN_MARKER_SCALE);
});
