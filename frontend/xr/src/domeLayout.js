// Pure geometry for the curved "dome" spatial model (ADR 0003).
//
// Maps the graph's existing 2D {x, y} layout onto the inside of a curved dome
// that wraps partway around a viewer seated at the origin looking down -Z. No
// third layout dimension is introduced: depth comes only from the dome radius,
// which zoom controls (zooming in shrinks the radius so the shell comes closer
// and subtends a larger visual angle). This keeps positions fully compatible
// with the 2D shared-session protocol.
//
// Deliberately free of three.js and React so it stays unit-testable in plain
// Node — this is the piece the spike most needs to get right.

export const DEFAULT_DOME = {
  baseRadius: 6,
  minRadius: 2.5,
  maxRadius: 20,
  hFovRad: (Math.PI * 2) / 3, // 120° horizontal wrap
  vFovRad: Math.PI / 2, // 90° vertical wrap
};

function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

function normalize(value, min, max) {
  if (max <= min) return 0.5;
  return (value - min) / (max - min);
}

// Map a zoom scalar (1 = neutral, >1 = zoomed in) to a dome radius. Zooming in
// pulls the shell closer; the result is clamped to a comfortable range.
export function zoomToRadius(zoom, opts = {}) {
  const { baseRadius, minRadius, maxRadius } = { ...DEFAULT_DOME, ...opts };
  if (!(zoom > 0)) return baseRadius;
  return clamp(baseRadius / zoom, minRadius, maxRadius);
}

// Convert a radius + azimuth (left/right) + elevation (up/down) into a Cartesian
// point in three.js' right-handed space, with the viewer at the origin looking
// toward -Z. azimuth 0 / elevation 0 is straight ahead.
export function sphericalToCartesian(radius, azimuth, elevation) {
  const cosE = Math.cos(elevation);
  return {
    x: radius * cosE * Math.sin(azimuth),
    y: radius * Math.sin(elevation),
    z: -radius * cosE * Math.cos(azimuth),
  };
}

// Place a single 2D layout point on the dome. `bounds` is the {minX, maxX,
// minY, maxY} extent of the whole layout so the graph is centred in the FOV.
export function domePosition(x, y, bounds, opts = {}) {
  const { hFovRad, vFovRad, baseRadius } = { ...DEFAULT_DOME, ...opts };
  const radius = opts.radius ?? baseRadius;
  const nx = normalize(x, bounds.minX, bounds.maxX);
  const ny = normalize(y, bounds.minY, bounds.maxY);
  const azimuth = (nx - 0.5) * hFovRad;
  // Screen y grows downward; invert so a smaller y sits higher on the dome.
  const elevation = (0.5 - ny) * vFovRad;
  return sphericalToCartesian(radius, azimuth, elevation);
}

// Extent of a list of {x, y} layout positions. An empty layout falls back to a
// unit box; a single point yields degenerate bounds (min === max), which
// `normalize` above resolves to the dome centre rather than dividing by zero.
export function layoutBounds(positions) {
  if (!positions || positions.length === 0) {
    return { minX: -1, maxX: 1, minY: -1, maxY: 1 };
  }
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const p of positions) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, maxX, minY, maxY };
}
