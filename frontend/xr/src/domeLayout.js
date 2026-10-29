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
  minDensity: 1,
  maxDensity: 8,
  hFovRad: (Math.PI * 2) / 3, // 120° horizontal wrap
  vFovRad: Math.PI / 2, // 90° vertical wrap
};

function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

function denormalize(value, min, max) {
  if (max <= min) return Number.isFinite(min) ? min : 0;
  return min + clamp(value, 0, 1) * (max - min);
}

function midpoint(min, max) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return 0;
  return (min + max) / 2;
}

function hasPositiveSpan(min, max) {
  return Number.isFinite(min) && Number.isFinite(max) && max > min;
}

function positiveSpan(min, max) {
  return hasPositiveSpan(min, max) ? max - min : 1;
}

function finiteNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function wrap(value, min, width) {
  if (!(width > 0)) return Number.isFinite(min) ? min : 0;
  const wrapped = ((((value - min) % width) + width) % width) + min;
  return Object.is(wrapped, -0) ? 0 : wrapped;
}

function wrapLayoutX(value, bounds) {
  const width = positiveSpan(bounds.minX, bounds.maxX);
  const epsilon = 1e-9;
  if (value >= bounds.minX - epsilon && value <= bounds.maxX + epsilon) {
    return clamp(value, bounds.minX, bounds.maxX);
  }
  return wrap(value, bounds.minX, width);
}

function shortestWrappedDelta(value, center, min, width) {
  if (!(width > 0)) return 0;
  const half = width / 2;
  const raw = value - center;
  const delta = ((((raw + half) % width) + width) % width) - half;
  return Math.abs(delta + half) < 1e-9 && raw > 0 ? half : delta;
}

// Map a zoom scalar (1 = neutral, >1 = zoomed in) to a dome radius. Zooming in
// pulls the shell closer; the result is always within [minRadius, maxRadius],
// including on the non-positive-zoom fallback path.
export function zoomToRadius(zoom, opts = {}) {
  const { baseRadius, minRadius, maxRadius } = { ...DEFAULT_DOME, ...opts };
  if (!(zoom > 0)) return clamp(baseRadius, minRadius, maxRadius);
  return clamp(baseRadius / zoom, minRadius, maxRadius);
}

// Density is the layout-space zoom: at density 1 the whole layout span fills
// the configured FOV; at density 2 the visible window is half as wide/tall, so
// the graph has to be panned to inspect the rest.
export function zoomToDensity(zoom, opts = {}) {
  const { minDensity, maxDensity } = { ...DEFAULT_DOME, ...opts };
  if (!(zoom > 0)) return clamp(1, minDensity, maxDensity);
  return clamp(zoom, minDensity, maxDensity);
}

// Resolve a navigable view over the flat layout. Horizontal panning wraps over
// the real layout width. Vertical panning clamps to the top/bottom of the real
// layout and reports edge flags so renderers can show comfort indicators.
export function domeView(bounds, opts = {}) {
  const density = zoomToDensity(opts.density ?? opts.zoom ?? 1, opts);
  const hasHorizontalSpan = hasPositiveSpan(bounds.minX, bounds.maxX);
  const hasVerticalSpan = hasPositiveSpan(bounds.minY, bounds.maxY);
  const width = hasHorizontalSpan ? bounds.maxX - bounds.minX : 1;
  const height = hasVerticalSpan ? bounds.maxY - bounds.minY : 1;
  const visibleWidth = width / density;
  const visibleHeight = height / density;
  const defaultCenterX = midpoint(bounds.minX, bounds.maxX);
  const defaultCenterY = midpoint(bounds.minY, bounds.maxY);
  const requestedCenterX = Number.isFinite(opts.centerX) ? opts.centerX : defaultCenterX;
  const requestedCenterY = Number.isFinite(opts.centerY) ? opts.centerY : defaultCenterY;
  const centerX = hasHorizontalSpan ? wrap(requestedCenterX, bounds.minX, width) : defaultCenterX;

  let centerY = defaultCenterY;
  let atTop = false;
  let atBottom = false;
  if (hasVerticalSpan && height > visibleHeight) {
    const minCenterY = bounds.minY + visibleHeight / 2;
    const maxCenterY = bounds.maxY - visibleHeight / 2;
    centerY = clamp(requestedCenterY, minCenterY, maxCenterY);
    atTop = centerY <= minCenterY && requestedCenterY <= minCenterY;
    atBottom = centerY >= maxCenterY && requestedCenterY >= maxCenterY;
  }

  return {
    density,
    centerX,
    centerY,
    visibleWidth,
    visibleHeight,
    virtualWidth: width,
    virtualHeight: height,
    atTop,
    atBottom,
  };
}

export function panDomeView(view, angularDelta, bounds, opts = {}) {
  const { hFovRad, vFovRad } = { ...DEFAULT_DOME, ...opts };
  const current = domeView(bounds, view);
  const azimuthDelta = Number(angularDelta?.azimuth) || 0;
  const elevationDelta = Number(angularDelta?.elevation) || 0;
  return domeView(bounds, {
    ...current,
    centerX: current.centerX - (azimuthDelta / hFovRad) * current.visibleWidth,
    centerY: current.centerY + (elevationDelta / vFovRad) * current.visibleHeight,
  });
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
// minY, maxY} extent of the whole layout. At density 1 the bounds fill the
// configured FOV; higher densities keep layout coordinates unchanged but make
// the visible window smaller, so pan state decides which slice is centred.
export function domePosition(x, y, bounds, opts = {}) {
  const { hFovRad, vFovRad, baseRadius, radius } = { ...DEFAULT_DOME, ...opts };
  const shellRadius = radius ?? baseRadius;
  const view = domeView(bounds, opts);
  const fx = Number.isFinite(x) && hasPositiveSpan(bounds.minX, bounds.maxX) ? x : view.centerX;
  const fy = Number.isFinite(y) && hasPositiveSpan(bounds.minY, bounds.maxY) ? y : view.centerY;
  const dx = shortestWrappedDelta(fx, view.centerX, bounds.minX, view.virtualWidth);
  const azimuth = (dx / view.visibleWidth) * hFovRad;
  // Screen y grows downward; invert so a smaller y sits higher on the dome.
  const elevation = ((view.centerY - fy) / view.visibleHeight) * vFovRad;
  return sphericalToCartesian(shellRadius, azimuth, elevation);
}

// Inverse of `domePosition` for a point on, or near, the dome shell. The input
// point is relative to the dome centre, not world-space eye height.
export function layoutPositionFromDomePoint(point, bounds, opts = {}) {
  const { hFovRad, vFovRad } = { ...DEFAULT_DOME, ...opts };
  const view = domeView(bounds, opts);
  const x = finiteNumber(point?.x, 0);
  const y = finiteNumber(point?.y, 0);
  const z = finiteNumber(point?.z, -1);
  const radius = Math.sqrt(x * x + y * y + z * z);
  if (!(radius > 0)) {
    return {
      x: denormalize(0.5, bounds.minX, bounds.maxX),
      y: denormalize(0.5, bounds.minY, bounds.maxY),
    };
  }
  const azimuth = Math.atan2(x, -z);
  const elevation = Math.asin(clamp(y / radius, -1, 1));
  return {
    x: wrapLayoutX(view.centerX + (azimuth / hFovRad) * view.visibleWidth, bounds),
    y: denormalize(
      (view.centerY - (elevation / vFovRad) * view.visibleHeight - bounds.minY) /
        positiveSpan(bounds.minY, bounds.maxY),
      bounds.minY,
      bounds.maxY
    ),
  };
}

function domePointFromRay(origin, direction, opts = {}) {
  const { baseRadius, radius, eyeHeight = 0 } = { ...DEFAULT_DOME, ...opts };
  const shellRadius = radius ?? baseRadius;
  const ox = Number(origin?.x) || 0;
  const oy = (Number(origin?.y) || 0) - eyeHeight;
  const oz = Number(origin?.z) || 0;
  let dx = Number(direction?.x) || 0;
  let dy = Number(direction?.y) || 0;
  let dz = Number(direction?.z) || 0;
  const dLen = Math.sqrt(dx * dx + dy * dy + dz * dz);
  if (!(dLen > 0)) return null;
  dx /= dLen;
  dy /= dLen;
  dz /= dLen;

  const b = 2 * (ox * dx + oy * dy + oz * dz);
  const c = ox * ox + oy * oy + oz * oz - shellRadius * shellRadius;
  const disc = b * b - 4 * c;
  if (disc < 0) return null;
  const sqrtDisc = Math.sqrt(disc);
  const t0 = (-b - sqrtDisc) / 2;
  const t1 = (-b + sqrtDisc) / 2;
  const t = t0 > 0 ? t0 : t1 > 0 ? t1 : null;
  if (t === null) return null;

  return { x: ox + dx * t, y: oy + dy * t, z: oz + dz * t };
}

export function domeAnglesFromRay(origin, direction, opts = {}) {
  const point = domePointFromRay(origin, direction, opts);
  if (!point) return null;
  const radius = Math.sqrt(point.x * point.x + point.y * point.y + point.z * point.z);
  if (!(radius > 0)) return null;
  return {
    azimuth: Math.atan2(point.x, -point.z),
    elevation: Math.asin(clamp(point.y / radius, -1, 1)),
  };
}

// Project a controller/hand ray onto the dome shell and return the compatible
// 2D session coordinates for that point. `origin` and `direction` are world
// coordinates; the dome centre is `[0, eyeHeight, 0]`.
export function layoutPositionFromRay(origin, direction, bounds, opts = {}) {
  const point = domePointFromRay(origin, direction, opts);
  if (!point) return null;
  return layoutPositionFromDomePoint(point, bounds, opts);
}

// Extent of a list of {x, y} layout positions. An empty layout — or one whose
// positions are all non-finite — falls back to a unit box; a single point
// yields degenerate bounds (min === max), which the dome view resolves to a
// one-unit virtual span rather than dividing by zero.
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
