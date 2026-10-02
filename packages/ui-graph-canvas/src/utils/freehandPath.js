/**
 * Deterministic helpers that turn a raw array of sampled freehand points into
 * a reduced point list and an SVG path `d` string. Used by both
 * FreehandAnnotationNode (rendering) and freehandStroke (capture) so drawing
 * and rendering agree on the same shape.
 *
 * Two separate concerns used to be conflated behind one `smoothing` (0-1)
 * knob: point *decimation* (how many of the sampled points survive) and curve
 * *smoothness* (how gently the rendered line flows through the points that
 * remain). `smoothing` fed straight into Ramer-Douglas-Peucker simplification,
 * so turning it up threw more points away — at 100% the stroke got visibly
 * coarser and more angular, the opposite of what "smoothing" should mean.
 *
 * They are now independent:
 *  - `reduceFreehandPoints` is a payload-size control only. It always dedupes
 *    consecutive near-identical samples. At smoothing=0 that's all it does —
 *    "no smoothing" still means every sampled point, at any length, exactly
 *    as before. Above 0, it simplifies further, but ONLY once a stroke is
 *    long enough that its serialized size is a real concern, and by a fixed
 *    tolerance — the *degree* of simplification is never scaled by how high
 *    smoothing is set, only whether smoothing is requested at all.
 *  - `smoothing` now also drives curve *fitting*: at 0 the path is exactly
 *    the pre-existing quadratic-through-midpoints curve through the retained
 *    points (byte-identical `d` output to before this change). Above 0, a
 *    Catmull-Rom spline is fit through the same retained points and resampled
 *    into extra intermediate points before that same quadratic pass runs, so
 *    higher smoothing means a visibly softer flowing line through the SAME
 *    anchors — not fewer of them.
 *
 * Resampling alone cannot remove hand tremor, though: pointer samples arrive
 * dense, so a spline through them still passes through every wobble. The
 * curve stage therefore first relaxes the anchors toward their neighbours
 * (RELAX_PASSES_AT_MAX_SMOOTHING repeated 1-2-1 averaging passes at
 * smoothing=1, scaled linearly, none at 0; first and last point stay fixed),
 * which is what makes 100% clearly smoother than 0%.
 *
 * Net effect on already-saved strokes: a stroke saved at smoothing=0 renders
 * pixel-identically, at any length (every stage is a no-op beyond dedupe,
 * exactly as before). A stroke saved with smoothing>0 renders differently
 * from earlier builds — its wobble is now averaged out in proportion to the
 * stored value, so e.g. the default 0.3 is visibly smoother than before.
 *
 * Pure functions, no randomness or wall-clock reads, so the same input always
 * produces the same output.
 */

// Simplification only engages past this many (deduped) points — well above
// what a normal freehand gesture samples at typical pointermove rates, so
// ordinary strokes are never touched regardless of the smoothing setting.
// This is what keeps decimation a payload-size safety net (op-batch byte caps
// are real) rather than a knob the user's smoothing choice can trigger.
const DECIMATION_POINT_THRESHOLD = 400;

// Simplification tolerance (model-space px) applied once a stroke crosses
// DECIMATION_POINT_THRESHOLD. Small and fixed — deliberately independent of
// `smoothing` — so it trims redundant near-collinear samples without
// perceptibly changing the drawn shape.
const DECIMATION_EPSILON = 1.5;

// Extra Catmull-Rom-interpolated points inserted per original segment at
// smoothing=1 (scaled linearly for values in between; 0 inserts none, so the
// curve stage is an exact no-op at smoothing=0). Chosen high enough to read
// as a visibly soft, flowing line without ballooning point counts.
const MAX_CURVE_SUBDIVISIONS = 6;

// Number of 1-2-1 neighbour-averaging passes applied at smoothing=1 (scaled
// linearly, rounded up, so any smoothing above 0 does at least one pass and
// 0 does none). Each pass has a variance of 0.5 samples^2, so 24 passes blur
// tremor over a window of roughly +/-3.5 samples: dense pointer samples lose
// their jitter while the gesture's overall shape survives. Chosen by eye on
// hand-drawn strokes; raising it flattens tight corners further.
const RELAX_PASSES_AT_MAX_SMOOTHING = 24;

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
// unconditionally, since it never removes a distinct sample the user
// actually drew.
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
 * Reduce a raw sampled point array for payload size. Always dedupes;
 * simplifies further only once the stroke is long enough
 * (DECIMATION_POINT_THRESHOLD) that its serialized cost is worth trimming.
 * The *degree* of that simplification is a fixed epsilon, deliberately NOT
 * scaled by the smoothing setting (see module doc comment) — but smoothing=0
 * still means what it always has, "every sampled point, full stop", at any
 * stroke length, so the safety net only ever engages once some smoothing is
 * actually requested (smoothing > 0). Always returns at least one point when
 * given at least one (never produces an empty stroke from a non-empty one).
 */
export function reduceFreehandPoints(points, smoothing = 0) {
  const deduped = dedupePoints(Array.isArray(points) ? points : []);
  if (clampSmoothing(smoothing) <= 0) return deduped;
  if (deduped.length <= DECIMATION_POINT_THRESHOLD) return deduped;
  return simplify(deduped, DECIMATION_EPSILON);
}

// Uniform Catmull-Rom interpolation of a single scalar (x or y) at parameter
// t in [0, 1] between v1 and v2, using v0/v3 as the neighbours that shape the
// tangents. Standard formulation (tension = 0.5 baked into the 0.5 factor).
function catmullRom(v0, v1, v2, v3, t) {
  const t2 = t * t;
  const t3 = t2 * t;
  return (
    0.5 *
    (2 * v1 +
      (-v0 + v2) * t +
      (2 * v0 - 5 * v1 + 4 * v2 - v3) * t2 +
      (-v0 + 3 * v1 - 3 * v2 + v3) * t3)
  );
}

// Interpolate one new sample between anchors p1 and p2 (using p0/p3 as the
// Catmull-Rom neighbours for x/y) at parameter t. Pressure is interpolated
// linearly between p1 and p2 directly — it is a device reading, not a
// position to curve-fit — and only when at least one of the two actually
// carries a sample, mirroring buildPressureSegments' own fallback rule below.
function interpolatePoint(p0, p1, p2, p3, t) {
  const point = {
    x: catmullRom(p0.x, p1.x, p2.x, p3.x, t),
    y: catmullRom(p0.y, p1.y, p2.y, p3.y, t),
  };
  const p1HasPressure = Number.isFinite(p1.pressure);
  const p2HasPressure = Number.isFinite(p2.pressure);
  if (p1HasPressure && p2HasPressure) {
    point.pressure = p1.pressure + (p2.pressure - p1.pressure) * t;
  } else if (p1HasPressure) {
    point.pressure = p1.pressure;
  } else if (p2HasPressure) {
    point.pressure = p2.pressure;
  }
  return point;
}

/**
 * Fit a Catmull-Rom spline through `points` and resample it, inserting
 * `smoothing`-scaled extra points between each original pair. At
 * smoothing=0 (or fewer than 3 points) this is an identity — the exact
 * anchors come back unchanged, which is what keeps a smoothing=0 stroke's
 * rendered path byte-identical to before this module changed. Never removes
 * or reorders an anchor (relaxation moves interior ones toward their
 * neighbours; the endpoints are fixed), so curve fitting never fights with
 * `reduceFreehandPoints`'s decimation.
 *
 * Exported (rather than kept internal to `buildFreehandPath`/
 * `buildPressureSegments`) so a caller building BOTH a `d` string and
 * pressure segments from the same points — as FreehandAnnotationNode does —
 * can run `reduceFreehandPoints` + `smoothAnchors` once and feed the result
 * to `pointsToPathData` and `segmentsFromCurvePoints` respectively, instead
 * of paying for the reduce-and-curve-fit twice.
 */
export function smoothAnchors(points, smoothing) {
  if (!Array.isArray(points)) return [];
  const level = clampSmoothing(smoothing);
  if (level <= 0 || points.length < 3) return points;
  const anchors = relaxAnchors(points, level);
  return curveThrough(anchors, level);
}

// Repeated 1-2-1 averaging of each interior point with its two neighbours.
// First and last points never move, so the stroke still starts and ends where
// the user put the pen. Pressure is a device reading and is carried over
// untouched. Pure: returns a new array, never mutates the input.
function relaxAnchors(points, level) {
  const passes = Math.ceil(level * RELAX_PASSES_AT_MAX_SMOOTHING);
  let current = points;
  const last = points.length - 1;
  for (let pass = 0; pass < passes; pass++) {
    const next = new Array(points.length);
    next[0] = current[0];
    next[last] = current[last];
    for (let i = 1; i < last; i++) {
      next[i] = {
        ...current[i],
        x: current[i].x * 0.5 + (current[i - 1].x + current[i + 1].x) * 0.25,
        y: current[i].y * 0.5 + (current[i - 1].y + current[i + 1].y) * 0.25,
      };
    }
    current = next;
  }
  return current;
}

function curveThrough(points, level) {
  // Ceiling, not round: the module contract is "smoothing=0 is the only
  // identity case, anything above 0 fits a curve" — rounding down to 0
  // subdivisions for a small-but-nonzero level (below ~0.083) would silently
  // break that promise for a value the caller explicitly chose over 0.
  const subdivisions = Math.ceil(level * MAX_CURVE_SUBDIVISIONS);
  const lastIndex = points.length - 1;
  const out = [points[0]];
  for (let i = 0; i < lastIndex; i++) {
    const p0 = points[i - 1] ?? points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] ?? points[i + 1];
    for (let s = 1; s <= subdivisions; s++) {
      out.push(interpolatePoint(p0, p1, p2, p3, s / (subdivisions + 1)));
    }
    out.push(p2);
  }
  return out;
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

/**
 * Convenience: reduce (payload-size decimation — gated on whether smoothing
 * is requested at all, never scaled by how high it is), fit a
 * smoothing-scaled curve through the retained anchors, then build path data
 * in one call. `points` in the return is the reduced anchor list (before
 * curve-fit resampling) — the same "how many real samples survived" value
 * this function has always returned; `d` is built from the (possibly denser,
 * smoothing>0) curve-fit points.
 */
export function buildFreehandPath(points, smoothing = 0) {
  const reduced = reduceFreehandPoints(points, smoothing);
  const curved = smoothAnchors(reduced, smoothing);
  return { points: reduced, d: pointsToPathData(curved) };
}

// Stroke-width scaling applied to a point's pressure (0-1), as a multiple of
// the stroke's base width. Piecewise linear through three anchors:
//   pressure 0   -> MIN_PRESSURE_WIDTH_FACTOR (feather-light, still visible)
//   pressure 0.5 -> 1 (exactly the base width)
//   pressure 1   -> MAX_PRESSURE_WIDTH_FACTOR (hard press)
// Pinning 0.5 to the base width matters for the fallback devices: a mouse
// reports a constant pressure of 0.5 while its button is down, and a stroke
// from one must keep drawing at the chosen width rather than being inflated.
//
// The 0.15-3.5 span (about 23x thinnest to thickest, up from 0.25-2.6, ~10x)
// was widened again after stylus testing found the difference too subtle: a
// pen rarely spans the full 0-1 scale in one stroke, so the usable range is a
// fraction of it. The floor keeps a light stroke a visible line (widthFor-
// Pressure additionally never goes below 0.5 px).
const MIN_PRESSURE_WIDTH_FACTOR = 0.15;
const MAX_PRESSURE_WIDTH_FACTOR = 3.5;

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
    clamped <= 0.5
      ? MIN_PRESSURE_WIDTH_FACTOR + (1 - MIN_PRESSURE_WIDTH_FACTOR) * (clamped / 0.5)
      : 1 + (MAX_PRESSURE_WIDTH_FACTOR - 1) * ((clamped - 0.5) / 0.5);
  return Math.max(0.5, baseWidth * factor);
}

/**
 * Reduce `points` the same way `buildFreehandPath` does, fit the same
 * smoothing-scaled Catmull-Rom curve through the result, then split THAT into
 * one two-point path segment per adjacent pair, each carrying its own stroke
 * width derived from that pair's average pressure (falling back to whichever
 * single point has a sample when only one of the two does). This is the
 * "granular pressure" rendering — a wide/thin pen stroke — deliberately built
 * as several short straight segments with round caps/joins rather than one
 * continuous curve, so each segment can carry an independent width; curve
 * fitting here means smoothing produces more, shorter segments that trace a
 * softer line, not one continuous curve, keeping the per-segment width model
 * intact. The single-path quadratic-through-midpoint curve `pointsToPathData`
 * builds stays the renderer for the no-pressure-data case, unchanged.
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
  const curved = smoothAnchors(reduced, smoothing);
  return segmentsFromCurvePoints(curved, baseWidth);
}

/**
 * Split an already reduced-and-curve-fit point list into one two-point path
 * segment per adjacent pair, each carrying its own pressure-derived width.
 * Factored out of `buildPressureSegments` so a caller that already computed
 * `reduceFreehandPoints` + `smoothAnchors` once (to also build a `d` string
 * via `pointsToPathData`) can reuse that same result here instead of running
 * decimation and curve-fitting a second time — see `smoothAnchors`'s doc
 * comment. `buildPressureSegments` itself still does both steps for callers
 * (including this file's own tests) that only need the segments.
 */
export function segmentsFromCurvePoints(points, baseWidth = DEFAULT_STROKE_WIDTH_FALLBACK) {
  if (!Array.isArray(points) || points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    return [
      { d: `M ${p.x} ${p.y} L ${p.x} ${p.y}`, width: widthForPressure(p.pressure, baseWidth) },
    ];
  }
  const segments = [];
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
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
