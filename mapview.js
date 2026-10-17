// Map pan/zoom maths for the SVG map: zoom about the cursor (so you can scroll into Asia), a deep zoom limit
// like the Leaflet map in the Streamlit Tideline, clamped panning, and marker scaling that keeps ship icons a
// constant size on screen. Pure functions on {x, y, w, h} view boxes in map units; app.js does the events.
(function (root) {
  const MAX_ZOOM = 14, MAX_OUT = 1.35, MIN_MARKER_SCALE = 0.04;
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

  // Keep at least 65% of the view over the map width/height on one side, so the map can't be lost.
  function clampView(v, W, H) {
    return {...v, x: clamp(v.x, -0.35 * v.w, W - 0.65 * v.w), y: clamp(v.y, -0.35 * v.h, H - 0.65 * v.h)};
  }
  // New view of width newW; the map point (ax, ay) stays under the same spot on screen. No anchor = view centre.
  function zoomView(v, newW, ax, ay, W, H) {
    const w = clamp(newW, W / MAX_ZOOM, W * MAX_OUT), h = w * H / W;
    const px = ax == null ? v.x + v.w / 2 : ax, py = ay == null ? v.y + v.h / 2 : ay;
    const fx = (px - v.x) / v.w, fy = (py - v.y) / v.h;
    return clampView({x: px - fx * w, y: py - fy * h, w, h}, W, H);
  }
  function panView(v, dx, dy, W, H) { return clampView({...v, x: v.x + dx, y: v.y + dy}, W, H); }
  // Scale for ship icons: 1 at the world view, smaller as you zoom in, so they stay the same size on screen.
  function markerScale(v, W) { return Math.max(MIN_MARKER_SCALE, v.w / W); }

  const api = {MAX_ZOOM, MAX_OUT, MIN_MARKER_SCALE, clampView, zoomView, panView, markerScale};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLMapView = api;
})(typeof window !== 'undefined' ? window : globalThis);
