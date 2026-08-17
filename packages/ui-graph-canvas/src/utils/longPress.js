/**
 * Long-press detector for touch interaction.
 *
 * Touch has no right-click, so a long-press on the canvas is how touch users
 * reach the same context menus a desktop right-click opens (edit, hide,
 * expand, delete, ...). This is a small, framework-agnostic state machine —
 * fed pointer events by the caller — so it can be unit tested directly with
 * fake timers instead of through a full component render.
 *
 * Only the first ("primary") pointer of a press is tracked. A second pointer
 * going down before the timer fires cancels the press outright (that is the
 * start of a pinch, not a hold), and movement of the primary pointer past
 * `tolerance` also cancels it (that is a drag/pan, not a hold).
 */

export const LONG_PRESS_DELAY_MS = 500;
export const LONG_PRESS_TOLERANCE_PX = 10;

export function createLongPressDetector({
  delay = LONG_PRESS_DELAY_MS,
  tolerance = LONG_PRESS_TOLERANCE_PX,
  onLongPress,
} = {}) {
  let timerId = null;
  let primaryPointerId = null;
  let originX = 0;
  let originY = 0;
  let payload = null;
  const activePointerIds = new Set();

  // Cancel any pending press without firing. Does not forget which pointers
  // are still down (see `reset` for that) — a cancelled press can be
  // followed by a fresh one from the same still-down pointer.
  function clearPending() {
    if (timerId != null) {
      clearTimeout(timerId);
      timerId = null;
    }
    primaryPointerId = null;
    payload = null;
  }

  function onPointerDown(pointerId, x, y, pressPayload) {
    activePointerIds.add(pointerId);
    clearPending();
    // More than one finger down: never start a long-press for this touch.
    if (activePointerIds.size > 1) return;
    primaryPointerId = pointerId;
    originX = x;
    originY = y;
    payload = pressPayload;
    timerId = setTimeout(() => {
      timerId = null;
      const firedPayload = payload;
      clearPending();
      onLongPress?.(firedPayload);
    }, delay);
  }

  function onPointerMove(pointerId, x, y) {
    if (timerId == null || pointerId !== primaryPointerId) return;
    const dx = x - originX;
    const dy = y - originY;
    if (Math.sqrt(dx * dx + dy * dy) > tolerance) {
      clearPending();
    }
  }

  // Pointer lifted or the browser/OS cancelled it (e.g. a system gesture
  // took over). Either way, a press that has not fired yet is abandoned —
  // there is no "long press on release".
  function onPointerUp(pointerId) {
    activePointerIds.delete(pointerId);
    if (pointerId === primaryPointerId) {
      clearPending();
    }
  }

  const onPointerCancel = onPointerUp;

  // Full reset: forget every tracked pointer, not just the pending press.
  // Used when touch mode is torn down (e.g. the host switches touchMode
  // away from 'on'/'auto'), so a stray pointer left "down" from before the
  // teardown cannot arm a press after it resumes.
  function reset() {
    activePointerIds.clear();
    clearPending();
  }

  return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, reset };
}
