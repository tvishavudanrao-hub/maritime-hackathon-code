// Run with: node --test tests/*.test.js   (also run from tests/test_playback_js.py)
const test = require('node:test');
const assert = require('node:assert');
const M = require('../maplayer.js');

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('the overlay reaches half a screen beyond every edge, so dragging never shows a gap', () => {
  const f = M.overlayFrame(700, 380);
  assert.deepStrictEqual([f.padX, f.padY, f.width, f.height], [350, 190, 1400, 760]);
});

test('the overlay frame is in units of 0.6 px so symbols keep the sizes they were designed at', () => {
  const f = M.overlayFrame(700, 380);
  const [x0, y0, w, h] = f.viewBox.split(' ').map(Number);
  assert.deepStrictEqual([x0, y0], [0, 0]);
  near(w * M.UNIT, f.width); near(h * M.UNIT, f.height);
});

test('a container point maps into the overlay in units, and back', () => {
  const f = M.overlayFrame(700, 380);
  const [ux, uy] = M.toUnits(100, 50, f);
  near(ux * M.UNIT, 100 + f.padX); near(uy * M.UNIT, 50 + f.padY);
  const [px, py] = M.toPixels(ux, uy, f);
  near(px, 100); near(py, 50);
});

test('the frame stays sane for tiny or zero sizes', () => {
  for (const [w, h] of [[0, 0], [1, 1], [320, 200]]) {
    const f = M.overlayFrame(w, h);
    assert.ok(f.width >= w && f.height >= h && Number.isFinite(f.padX));
  }
});

test('longitudes are unwrapped so a route crossing the dateline does not jump across the map', () => {
  const east = M.unwrapLon([[0, 170], [0, -170], [0, -160]]).map(p => p[1]);
  assert.deepStrictEqual(east, [170, 190, 200]);
  const west = M.unwrapLon([[0, -170], [0, 170], [0, 160]]).map(p => p[1]);
  assert.deepStrictEqual(west, [-170, -190, -200]);
});

test('already continuous routes are left alone, latitudes are never touched, input is not changed', () => {
  const route = [[51, 4], [36, -6], [10, -60], [8.9, -79.5], [33.7, -118.3]];
  const out = M.unwrapLon(route);
  assert.deepStrictEqual(out, route);
  assert.notStrictEqual(out, route);
  const wrapped = [[10, 179], [20, -179]];
  M.unwrapLon(wrapped);
  assert.deepStrictEqual(wrapped, [[10, 179], [20, -179]]);
  assert.deepStrictEqual(M.unwrapLon([]), []);
  assert.deepStrictEqual(M.unwrapLon([[5, 5]]), [[5, 5]]);
});

test('the zoom level used for zoom-aware speed is 0 at the old regional view and rises one per Leaflet zoom step', () => {
  near(M.zoomLevelOf(M.REF_ZOOM), 0);
  near(M.zoomLevelOf(M.REF_ZOOM + 2) - M.zoomLevelOf(M.REF_ZOOM), 2);
  assert.ok(M.zoomLevelOf(2) < 0, 'the whole-world view counts as zoomed out');
});

test('layers are drawn in the world copy nearest the view, so panning across copies never strands them', () => {
  assert.strictEqual(M.lonShiftFor(0), 0);
  assert.strictEqual(M.lonShiftFor(100), 0);
  assert.strictEqual(M.lonShiftFor(-150), 0);
  assert.strictEqual(M.lonShiftFor(200), 360);
  assert.strictEqual(M.lonShiftFor(-190), -360);
  assert.strictEqual(M.lonShiftFor(-776.25), -720);
  assert.strictEqual(M.lonShiftFor(NaN), 0);
});

test('the shift is a whole number of worlds and puts the home copy within half a world of the centre', () => {
  for (const center of [-776.25, -400, -190, -10, 0, 90, 179, 190, 400, 1000]) {
    const shift = M.lonShiftFor(center);
    assert.ok(shift % 360 === 0, `${shift} is a whole number of worlds`);   // (-360 % 360 is -0, which === 0)
    assert.ok(Math.abs(shift - center) <= 180 + 1e-9, `centre ${center} -> shift ${shift}`);
  }
});

test('the home view fits the Americas, Europe, Africa and Asia in one screen', () => {
  const b = M.HOME_BOUNDS;
  assert.ok(b[0][1] <= -125 && b[1][1] >= 140, 'covers Los Angeles to Japan');
  assert.ok(b[0][0] <= -50 && b[1][0] >= 65, 'covers Cape Horn to Scandinavia');
});
