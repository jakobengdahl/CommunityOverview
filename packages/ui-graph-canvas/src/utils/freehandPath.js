/**
 * Deterministic helpers that turn a raw array of sampled freehand points into
 * a reduced point list and an SVG path `d` string. Used by both
 * FreehandAnnotationNode (rendering) and freehandStroke (capture) so drawing
 * and rendering agree on the same shape.
 *
 * `smoothing` is a 0-1 knob: 0 keeps every distinct sampled point (a raw
 * polyline through them), higher values simplify the point list more
 * aggressively (Ramer-Douglas-Peucker) and round the corners between what's
 * left (quadratic-bezier-through-midpoints). Pure functions, no randomness or
 * wall-clock reads, so the same input always produces the same output.
 */

// Upper bound (model-space px) for the RDP simplification tolerance at
// smoothing=1. Chosen so smoothing=1 visibly thins a jittery hand-drawn
// stroke without collapsing a deliberately large gesture to a straight line.
const MAX_SIMPLIFY_EPSILON = 24;

// Matches FreehandAnnotationNode's own DEFAULT_STROKE_WIDTH; kept as a
// separate constant here (rather than imported) so this module stays free of
// a component-layer dependency, the same reason it doesn't import anything
// from FreehandAnnotationNode.jsx elsewhere in the file.
const DEFAULT_STROKE_WIDTH_FALLBACK = 2;

function clampSmoothing(smoothing) {
  const n = Number.isFinite(smoothing) ? smoothing : 0;
  return Math.min(1, Math.max(0, n));
}

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

// Drop consecutive duplicate/near-duplicate points (e.g. a pointer that
// paused mid-stroke firing several move events at the same spot). Applied
// unconditionally, even at smoothing=0, since it never removes a distinct
// sample the user actually drew.
function dedupePoints(points) {
  const out = [];
  for (const point of points) {
    const prev = out[out.length - 1];
    if (!prev || distance(prev, point) > 0.01) out.push(point);
  }
  return out;
}

function perpendicularDistance(point, lineStart, lineEnd) {
  const dx = lineEnd.x - lineStart.x;
  const dy = lineEnd.y - lineStart.y;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return distance(point, lineStart);
  const t = ((point.x - lineStart.x) * dx + (point.y - lineStart.y) * dy) / lengthSq;
  const closest = { x: lineStart.x + t * dx, y: lineStart.y + t * dy };
  return distance(point, closest);
}

// Ramer-Douglas-Peucker polyline simplification: keeps only the points that
// deviate from the straight line between their neighbours by more than
// `epsilon`. Deterministic and pure.
function simplify(points, epsilon) {
  if (points.length < 3 || epsilon <= 0) return points.slice();
  let maxDist = 0;
  let splitIndex = 0;
  const lastIndex = points.length - 1;
  for (let i = 1; i < lastIndex; i++) {
    const dist = perpendicularDistance(points[i], points[0], points[lastIndex]);
    if (dist > maxDist) {
      maxDist = dist;
      splitIndex = i;
    }
  }
  if (maxDist <= epsilon) return [points[0], points[lastIndex]];
  const left = simplify(points.slice(0, splitIndex + 1), epsilon);
  const right = simplify(points.slice(splitIndex), epsilon);
  return [...left.slice(0, -1), ...right];
}

/**
 * Reduce a raw sampled point array according to `smoothing` (0-1). Always
 * dedupes; simplifies further as smoothing rises toward 1. Always returns at
 * least one point when given at least one (never produces an empty stroke
 * from a non-empty one).
 */
export function reduceFreehandPoints(points, smoothing = 0) {
  const deduped = dedupePoints(Array.isArray(points) ? points : []);
  if (deduped.length === 0) return [];
  const level = clampSmoothing(smoothing);
  if (level === 0) return deduped;
  return simplify(deduped, level * MAX_SIMPLIFY_EPSILON);
}

/**
 * Build an SVG path `d` string through `points`. With fewer than 3 points
 * this is a straight `M`/`L` segment (or a single-point dot). With 3+ points
 * it draws quadratic-bezier segments through successive midpoints — a
 * standard hand-drawn-smoothing technique that softens corners without
 * needing per-point tangent estimation, and is stable under point removal
 * (simplifying first, then curving, never introduces overshoot).
 */
export function pointsToPathData(points) {
  if (!Array.isArray(points) || points.length === 0) return '';
  if (points.length === 1) {
    const p = points[0];
    return `M ${p.x} ${p.y} L ${p.x} ${p.y}`;
  }
  if (points.length === 2) {
    return `M ${points[0].x} ${points[0].y} L ${points[1].x} ${points[1].y}`;
  }
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const mid = { x: (points[i].x + points[i + 1].x) / 2, y: (points[i].y + points[i + 1].y) / 2 };
    d += ` Q ${points[i].x} ${points[i].y} ${mid.x} ${mid.y}`;
  }
  const last = points[points.length - 1];
  d += ` L ${last.x} ${last.y}`;
  return d;
}

/** Convenience: reduce then build path data in one call. */
export function buildFreehandPath(points, smoothing = 0) {
  const reduced = reduceFreehandPoints(points, smoothing);
  return { points: reduced, d: pointsToPathData(reduced) };
}

// Stroke-width scaling range applied to a point's pressure (0-1): a light
// touch draws thinner than baseWidth, a hard press draws thicker, but neither
// end collapses to zero or balloons unreadably wide.
const MIN_PRESSURE_WIDTH_FACTOR = 0.4;
const MAX_PRESSURE_WIDTH_FACTOR = 1.6;

/**
 * Whether any point in the array carries a real pressure sample. Used to
 * choose between the uniform-width path (`buildFreehandPath`, the constant-
 * width fallback for mouse/touch/pressure-less pens) and the per-segment
 * variable-width rendering below — never both, so a stroke with no pressure
 * data at all keeps the exact pre-existing uniform look.
 */
export function hasPressureData(points) {
  return Array.isArray(points) && points.some((p) => Number.isFinite(p?.pressure));
}

function widthForPressure(pressure, baseWidth) {
  if (!Number.isFinite(pressure)) return baseWidth;
  const clamped = Math.min(1, Math.max(0, pressure));
  const factor =
    MIN_PRESSURE_WIDTH_FACTOR + clamped * (MAX_PRESSURE_WIDTH_FACTOR - MIN_PRESSURE_WIDTH_FACTOR);
  return Math.max(0.5, baseWidth * factor);
}

/**
 * Reduce `points` the same way `buildFreehandPath` does, then split the
 * result into one two-point path segment per adjacent pair, each carrying its
 * own stroke width derived from that pair's average pressure (falling back to
 * whichever single point has a sample when only one of the two does). This is
 * the "granular pressure" rendering — a wide/thin pen stroke — deliberately
 * built as several short straight segments with round caps/joins rather than
 * one continuous curve, so each segment can carry an independent width; the
 * single-path quadratic-through-midpoint curve `pointsToPathData` builds stays
 * the renderer for the no-pressure-data case, unchanged.
 *
 * Always returns at least one segment for a non-empty `points` (a single
 * point becomes a zero-length dot segment, matching `pointsToPathData`'s own
 * single-point behaviour).
 */
export function buildPressureSegments(
  points,
  smoothing = 0,
  baseWidth = DEFAULT_STROKE_WIDTH_FALLBACK
) {
  const reduced = reduceFreehandPoints(points, smoothing);
  if (reduced.length === 0) return [];
  if (reduced.length === 1) {
    const p = reduced[0];
    return [
      { d: `M ${p.x} ${p.y} L ${p.x} ${p.y}`, width: widthForPressure(p.pressure, baseWidth) },
    ];
  }
  const segments = [];
  for (let i = 0; i < reduced.length - 1; i++) {
    const a = reduced[i];
    const b = reduced[i + 1];
    let pressure;
    if (Number.isFinite(a.pressure) && Number.isFinite(b.pressure)) {
      pressure = (a.pressure + b.pressure) / 2;
    } else if (Number.isFinite(a.pressure)) {
      pressure = a.pressure;
    } else {
      pressure = b.pressure;
    }
    segments.push({
      d: `M ${a.x} ${a.y} L ${b.x} ${b.y}`,
      width: widthForPressure(pressure, baseWidth),
    });
  }
  return segments;
}
