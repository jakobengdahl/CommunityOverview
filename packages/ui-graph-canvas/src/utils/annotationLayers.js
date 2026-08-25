/**
 * Manual layer ordering for annotations.
 *
 * An annotation's layer is its envelope `z` field, carried on the ReactFlow
 * node as `zIndex` (overlayToFlowNode/flowNodeToOverlay in ./annotations.js)
 * and persisted by the host's session translators. This module owns only the
 * arithmetic: which `z` a "bring to front" / "send to back" click produces,
 * given what else is currently on the canvas.
 *
 * The result is always an integer within the CSS int32 range, even when the
 * annotations already on the canvas carry fractional `z` values (an agent may
 * set any float over MCP — `z` is `Optional[float]` in
 * backend/core/session_annotations.py). ReactFlow
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
 * created annotation. Sending an annotation to the back therefore writes a
 * negative `z` and does place it behind the graph's own nodes and edges.
 * That is the intended, useful behaviour — it is how a `frame` gets behind
 * the nodes it frames — not an accident of the arithmetic.
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
    // floor before stepping so a fractional neighbour still yields an
    // integer that is strictly above it.
    return clampLayer(Math.floor(max) + 1);
  }
  const min = Math.min(...others);
  if (current < min) return null;
  return clampLayer(Math.ceil(min) - 1);
}

// CSS clamps `z-index` to a signed 32-bit integer, so a layer past that
// bound is not the layer the browser actually applies: two annotations
// stepped beyond it would clamp to the same value and the tie this module
// exists to break would survive. Reachable because an agent may set any `z`
// over MCP, and `Date.now()` is a common way to write a bring-to-front.
// Clamping keeps the stored value and the painted value the same number.
const Z_MAX = 2147483647;
const Z_MIN = -2147483648;
function clampLayer(z) {
  return Math.min(Z_MAX, Math.max(Z_MIN, z));
}
