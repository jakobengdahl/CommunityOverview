/**
 * Manual layer ordering for annotations.
 *
 * An annotation's layer is its envelope `z` field, carried on the ReactFlow
 * node as `zIndex` (overlayToFlowNode/flowNodeToOverlay in ./annotations.js)
 * and persisted by the host's session translators. This module owns only the
 * arithmetic: which `z` a "bring to front" / "send to back" click produces,
 * given what else is currently on the canvas.
 *
 * Any layer it returns is an integer strictly past every other annotation's,
 * and inside the range CSS `z-index` accepts — including when the
 * annotations already on the canvas carry fractional `z` values (an agent may
 * set any float over MCP — `z` is `Optional[float]` in
 * backend/core/session_annotations.py). When no such value exists it returns
 * null rather than an approximate one — including in a few cases where an
 * in-range layer does exist but the step that would reach it cannot be
 * computed safely. ReactFlow
 * writes a node's `zIndex` straight into the element's inline style, and CSS
 * `z-index` accepts only `auto | <integer>`: a browser rejects
 * `z-index: 0.5` outright and the element silently keeps whatever it had.
 * A fractional layer would therefore publish an op and move nothing on
 * screen — worse than refusing, because the other clients would apply it.
 *
 * Why front/back rather than a one-step forward/back: a true one-step swap
 * needs distinct integer levels to step between, and every annotation is
 * created at `z = 0`, so the common case is a pile of ties. Breaking those
 * ties one step at a time means renumbering the annotations around the one
 * that moved, and an `annotation_updated` op carries the *whole* annotation
 * (sessionSyncClient.js) — for an embedded image that is its entire data
 * URI. A renumber touching a few images would exceed the session op batch's
 * byte cap and be rejected atomically. Front/back always writes exactly one
 * annotation, so it cannot hit that cap at all.
 */

import { ANNOTATION_TYPES } from './annotations';

export const LAYER_FRONT = 'front';
export const LAYER_BACK = 'back';

// CSS clamps `z-index` to a signed 32-bit integer, so a layer past that
// bound is not the layer the browser actually applies. Reachable because an
// agent may set any `z` over MCP, and `Date.now()` is a common way to write
// a bring-to-front.
const Z_MAX = 2147483647;
const Z_MIN = -2147483648;

// Both bounds are checked in both directions, not just the one each step
// moves toward. A neighbour already past the range in the *opposite*
// direction is the dangerous case: with an annotation at `Date.now()` —
// the bring-to-front idiom named above — send-to-back computes a layer just
// under it, still far above Z_MAX, which the browser clamps back down to
// Z_MAX and paints at the very front. That is not a no-op, it is the
// opposite of what was asked for.
function inCssRange(z) {
  return z >= Z_MIN && z <= Z_MAX;
}

function layerOf(node) {
  const z = node?.zIndex;
  return Number.isFinite(z) ? z : 0;
}

/**
 * The `z` that puts `id` in front of (or behind) every other annotation on
 * the canvas, or `null` when it is already there and the click is a no-op.
 *
 * Only other *annotations* are consulted: the layer model orders annotations
 * against each other, and no graph node is ever read to decide the result.
 * That is not the same as staying inside the graph's band, and must not be
 * read as such — graph nodes carry no `zIndex` at all (nothing outside
 * ./annotations.js ever sets one), so they sit at 0 alongside a freshly
 * created annotation. Send-to-back writes one below the backmost annotation,
 * so while that annotation is at or below 0 — the default, since every
 * annotation is created there — the result is negative and does place the
 * annotation behind the graph's own nodes and edges. That is intended and
 * useful, and it is how a `frame` gets behind the nodes it frames. It is not
 * a guarantee: once every annotation has been pushed above 0 the result
 * lands at 0 or higher, level with or in front of the graph.
 *
 * An annotation tied with the current front is *not* already in front — the
 * tie is what the click is there to break — so it moves.
 */
export function resolveLayerZ(nodes, id, direction) {
  // An unrecognised direction must not fall through to one of the two real
  // ones: a stale constant left by a rename would then move the annotation
  // the wrong way rather than doing nothing.
  if (direction !== LAYER_FRONT && direction !== LAYER_BACK) return null;
  const self = nodes.find((n) => n.id === id);
  if (!self) return null;
  const current = layerOf(self);
  const others = nodes.filter((n) => n.id !== id && ANNOTATION_TYPES.has(n.type)).map(layerOf);
  if (others.length === 0) return null;
  if (direction === LAYER_FRONT) {
    const max = Math.max(...others);
    if (current > max) return null;
    // Floor before stepping so a fractional neighbour still yields an
    // integer strictly above it. Then refuse rather than hand back a value
    // that is not strictly above after all: clamping Z_MAX + 1 to Z_MAX
    // would *tie* with the neighbour it is meant to pass — the exact
    // ambiguity this module exists to remove — and past 2^53 the step is
    // swallowed by float precision so `z > max` is false too. Either way the
    // annotation has nowhere further to go, so the click is a no-op like any
    // other already-at-the-front case.
    const z = Math.floor(max) + 1;
    return z > max && inCssRange(z) ? z : null;
  }
  const min = Math.min(...others);
  if (current < min) return null;
  // Mirror of the front case above, including both refusals.
  const z = Math.ceil(min) - 1;
  return z < min && inCssRange(z) ? z : null;
}
