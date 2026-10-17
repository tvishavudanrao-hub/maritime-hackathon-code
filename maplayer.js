// Geometry for drawing the app's SVG layers (routes, hazards, ports, ships, warnings) on top of a Leaflet map.
// The overlay lives in a Leaflet pane, so it pans with the map; it is redrawn when the view settles. Pure functions, no DOM.
(function (root) {
  const UNIT = 0.6;            // screen px per SVG unit: the size the symbols were designed at on the old 1200-unit map
  const PAD = 0.5;             // the overlay extends this fraction of the screen beyond each edge
  const REF_ZOOM = 2.56;       // the Leaflet zoom whose scale matches the old regional map (keeps zoom-aware speed meaningful)
  // Americas to Japan, Cape Horn to Scandinavia: home view and "reset" fit this box
  const HOME_BOUNDS = [[-56, -130], [70, 150]];

  function overlayFrame(w, h) {
    const padX = Math.round(w * PAD), padY = Math.round(h * PAD), width = w + 2 * padX, height = h + 2 * padY;
    return {padX, padY, width, height, viewBox: `0 0 ${width / UNIT} ${height / UNIT}`};
  }
  // A point in the map container (px) -> overlay SVG units, and back.
  const toUnits = (x, y, f) => [(x + f.padX) / UNIT, (y + f.padY) / UNIT];
  const toPixels = (ux, uy, f) => [ux * UNIT - f.padX, uy * UNIT - f.padY];

  // Make longitudes continuous along a route ([lat, lon] points), so a leg across the dateline is drawn the short way round.
  function unwrapLon(points) {
    const out = [];
    let offset = 0;
    points.forEach((p, i) => {
      if (i) {
        const prev = points[i - 1][1], step = p[1] - prev;
        if (step > 180) offset -= 360; else if (step < -180) offset += 360;
      }
      out.push([p[0], p[1] + offset]);
    });
    return out;
  }
  const zoomLevelOf = leafletZoom => leafletZoom - REF_ZOOM;
  // Leaflet repeats the world sideways. Draw the layers in the copy nearest the view centre: add this to every longitude.
  const lonShiftFor = centerLng => (Number.isFinite(centerLng) ? 360 * Math.round(centerLng / 360) || 0 : 0);

  const api = {UNIT, PAD, REF_ZOOM, HOME_BOUNDS, overlayFrame, toUnits, toPixels, unwrapLon, zoomLevelOf, lonShiftFor};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLMapLayer = api;
})(typeof window !== 'undefined' ? window : globalThis);
