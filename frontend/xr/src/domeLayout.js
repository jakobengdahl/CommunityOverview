// Pure geometry for the curved "dome" spatial model (ADR 0003).
//
// Maps the graph's existing 2D {x, y} layout onto the inside of a curved dome
// that wraps partway around a viewer looking down -Z. No third layout dimension
// is introduced: depth comes only from the dome radius, which zoom controls
// (zooming in shrinks the radius so the shell comes closer and subtends a
// larger visual angle). This keeps positions fully compatible with the 2D
// shared-session protocol.
//
// Positions are returned relative to the dome centre, i.e. as if the viewer sat
// at the origin. Callers place the centre at eye height by offsetting Y — see
// EYE_HEIGHT in App.jsx.
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

// Degenerate bounds (min === max, from a single-node graph) and non-finite
// inputs both resolve to the dome centre. Without this the caller would feed
// NaN straight into the three.js matrices, which poisons them silently.
function normalize(value, min, max) {
  if (max <= min) return 0.5;
  const n = (value - min) / (max - min);
  return Number.isFinite(n) ? n : 0.5;
}

// Map a zoom scalar (1 = neutral, >1 = zoomed in) to a dome radius. Zooming in
// pulls the shell closer; the result is always within [minRadius, maxRadius],
// including on the non-positive-zoom fallback path.
export function zoomToRadius(zoom, opts = {}) {
  const { baseRadius, minRadius, maxRadius } = { ...DEFAULT_DOME, ...opts };
  if (!(zoom > 0)) return clamp(baseRadius, minRadius, maxRadius);
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
//
// The two axes are normalized independently, so the layout is stretched to fill
// the whole wrap rather than keeping its aspect ratio: a wide, short graph still
// spans the full ±vFov/2. That is deliberate for the spike — it guarantees the
// dome is always filled and nothing sits outside the comfortable viewing cone —
// but it means angular separation is not proportional to 2D distance. An
// aspect-preserving mode is a separate decision, not a scaffold concern.
export function domePosition(x, y, bounds, opts = {}) {
  const { hFovRad, vFovRad, baseRadius, radius } = { ...DEFAULT_DOME, ...opts };
  const shellRadius = radius ?? baseRadius;
  const nx = normalize(x, bounds.minX, bounds.maxX);
  const ny = normalize(y, bounds.minY, bounds.maxY);
  const azimuth = (nx - 0.5) * hFovRad;
  // Screen y grows downward; invert so a smaller y sits higher on the dome.
  const elevation = (0.5 - ny) * vFovRad;
  return sphericalToCartesian(shellRadius, azimuth, elevation);
}

// Extent of a list of {x, y} layout positions. An empty layout — or one whose
// positions are all non-finite — falls back to a unit box; a single point
// yields degenerate bounds (min === max), which `normalize` above resolves to
// the dome centre rather than dividing by zero.
export function layoutBounds(positions) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const p of positions ?? []) {
    // Non-finite coordinates would propagate NaN through every node's
    // transform, not just their own, so they never enter the extent.
    if (!Number.isFinite(p?.x) || !Number.isFinite(p?.y)) continue;
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (minX > maxX) {
    return { minX: -1, maxX: 1, minY: -1, maxY: 1 };
  }
  return { minX, maxX, minY, maxY };
}
