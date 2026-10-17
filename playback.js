// Fleet playback maths: a continuous speed (real time .. 200 simulated hours per real second) and a
// clock that never jumps. Pure functions, no DOM; app.js owns the animation loop.
(function (root) {
  const MIN_SPEED = 1 / 3600, MAX_SPEED = 200;      // simulated hours per real second
  const MAX_FRAME_S = 1;                            // a longer gap (hidden tab) counts as one 1 s frame, so the clock never jumps
  const LOG_RANGE = Math.log(MAX_SPEED / MIN_SPEED);

  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  function clampSpeed(s) { return Number.isFinite(s) ? clamp(s, MIN_SPEED, MAX_SPEED) : MIN_SPEED; }
  function sliderToSpeed(p) { return MIN_SPEED * Math.exp(LOG_RANGE * clamp(Number.isFinite(p) ? p : 0, 0, 1)); }
  function speedToSlider(s) { return Math.log(clampSpeed(s) / MIN_SPEED) / LOG_RANGE; }
  const sig = n => String(Number(n.toPrecision(2)));
  function speedCaption(s) {
    s = clampSpeed(s);
    if (s <= MIN_SPEED * 1.05) return 'Real time';
    return `${sig(s)} h/s · 1 day ≈ ${sig(24 / s)} s`;
  }
  // Zoom-aware speed: the ship's on-screen pace stays roughly constant, so zoomed out it runs faster and zoomed in slower.
  // effective = base x 2^(ZOOM_K x (ZOOM_REF - zoom)), clamped to 0.25x..8x. zoom = log2(world width / view width): 0 at the world view.
  const ZOOM_K = 0.8, ZOOM_REF = 1.5, MULT_MIN = 0.25, MULT_MAX = 8, ABS_MAX = MAX_SPEED * MULT_MAX;
  const zoomLevel = (viewW, worldW) => Math.log2(worldW / viewW);
  const zoomMultiplier = zoom => clamp(2 ** (ZOOM_K * (ZOOM_REF - zoom)), MULT_MIN, MULT_MAX);
  const effectiveSpeed = (base, zoom, auto) => auto ? base * zoomMultiplier(zoom) : base;
  function rateCaption(base, mult, auto) {
    const b = clampSpeed(base);
    if (b <= MIN_SPEED * 1.05 && (!auto || Math.abs(mult - 1) < 0.05)) return 'Real time';
    if (!auto || Math.abs(mult - 1) < 0.05) return `${sig(b)} h/s`;
    return `${sig(b)} h/s × ${sig(mult)} (${mult > 1 ? 'zoomed out' : 'zoomed in'}) = ${sig(b * mult)} h/s`;
  }
  // Move the simulated clock by the real time since the last frame. Stops exactly at endH.
  // The speed passed in may be the zoom-scaled one, so it is bounded by the slider maximum x the largest multiplier.
  function advance(offsetH, dtSec, speed, endH) {
    const dt = clamp(Number.isFinite(dtSec) ? dtSec : 0, 0, MAX_FRAME_S);
    const next = offsetH + dt * (Number.isFinite(speed) ? clamp(speed, MIN_SPEED, ABS_MAX) : MIN_SPEED);
    return next >= endH ? {offsetH: Math.max(offsetH, endH), done: true} : {offsetH: next, done: false};
  }
  // Simulated hours of playback left until the last voyage reaches its ETA.
  function hoursUntilAllArrive(voyages, nowMs, offsetH) {
    let left = 0;
    for (const v of voyages) {
      const h = (Date.parse(v.eta) - nowMs) / 3.6e6 - (v.clock_offset_h || 0);
      if (Number.isFinite(h)) left = Math.max(left, h);
    }
    return Math.max(0, left - offsetH);
  }

  const api = {MIN_SPEED, MAX_SPEED, MAX_FRAME_S, ZOOM_REF, clampSpeed, sliderToSpeed, speedToSlider, speedCaption, zoomLevel, zoomMultiplier,
    effectiveSpeed, rateCaption, advance, hoursUntilAllArrive};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLPlayback = api;
})(typeof window !== 'undefined' ? window : globalThis);
