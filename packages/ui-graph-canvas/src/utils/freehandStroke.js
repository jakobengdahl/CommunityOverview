/**
 * Freehand stroke capture — a small, framework-agnostic state machine that
 * accumulates pointer samples into a point array, mirroring the decoupled
 * `createLongPressDetector` pattern in `longPress.js` (fed events by the
 * caller, no DOM/React dependency) so it is unit-testable without a full
 * component/canvas render.
 *
 * Only the first ("primary") pointer of a stroke is tracked — a second
 * pointer going down mid-stroke is ignored rather than starting a second
 * stroke or interrupting the first (that is the start of a pinch/pan
 * gesture on touch, not a second freehand line).
 *
 * Each sample carries model-space x/y plus optional pressure and the
 * originating pointerType ('pen' | 'touch' | 'mouse' | ...), matching the
 * per-point shape `normalizeFreehandPoint` (annotationModel.js) accepts —
 * this module does not itself smooth/reduce the samples; that is
 * `freehandPath.js`'s job, run once on completion.
 */

export function createFreehandStrokeCapture({ onStrokeComplete, minPoints = 2 } = {}) {
  let primaryPointerId = null;
  let points = [];
  let pointerType = null;

  function isActive() {
    return primaryPointerId != null;
  }

  // `toModelPoint(event)` is supplied by the caller (screen-to-flow-position
  // conversion is host/viewport-specific); this module only knows about
  // pointer identity and sample accumulation.
  function onPointerDown(event, toModelPoint) {
    if (isActive()) return;
    primaryPointerId = event.pointerId;
    pointerType = event.pointerType || null;
    points = [samplePoint(event, toModelPoint)];
  }

  function onPointerMove(event, toModelPoint) {
    if (!isActive() || event.pointerId !== primaryPointerId) return;
    points.push(samplePoint(event, toModelPoint));
  }

  // Ends the stroke and fires `onStrokeComplete` when it has enough points to
  // be a real stroke (not a stray tap); otherwise discards it silently. Safe
  // to call for any pointer id — a mismatched id is a no-op, not an error, so
  // callers can wire this straight to a window-level pointerup listener.
  function onPointerUp(event) {
    if (!isActive() || event.pointerId !== primaryPointerId) return;
    const finished = points;
    const finishedPointerType = pointerType;
    reset();
    if (finished.length >= minPoints) {
      onStrokeComplete?.({ points: finished, pointerType: finishedPointerType });
    }
  }

  // Browser/OS cancelled the pointer (e.g. a system gesture took over): the
  // stroke is abandoned, same as `longPress.js`'s cancel — there is no
  // "complete on cancel".
  function onPointerCancel(event) {
    if (!isActive() || event.pointerId !== primaryPointerId) return;
    reset();
  }

  function reset() {
    primaryPointerId = null;
    points = [];
    pointerType = null;
  }

  return { isActive, onPointerDown, onPointerMove, onPointerUp, onPointerCancel, reset };
}

function samplePoint(event, toModelPoint) {
  const { x, y } = toModelPoint(event);
  const point = { x, y };
  if (Number.isFinite(event.pressure) && event.pressure > 0) point.pressure = event.pressure;
  return point;
}
