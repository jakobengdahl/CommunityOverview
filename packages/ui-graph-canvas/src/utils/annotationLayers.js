/**
 * Manual layer ordering for annotations.
 *
 * An annotation's layer is its envelope `z` field, carried on the ReactFlow
 * node as `zIndex` (overlayToFlowNode/flowNodeToOverlay in ./annotations.js)
 * and persisted by the host's session translators. This module owns only the
 * arithmetic: which `z` a "bring to front" / "send to back" click produces,
 * given what else is currently on the canvas.
 *
 * The result is always an integer, even when the annotations already on the
 * canvas carry fractional `z` values (an agent may set any float over MCP —
 * `z` is `Optional[float]` in backend/core/session_annotations.py). ReactFlow
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
 * Only other *annotations* are considered. Graph nodes and edges share the
 * same ReactFlow z-space but are not part of the annotation layer model, and
 * ordering an annotation against one would be a silent, unrequested change to
 * how the graph itself paints.
 *
 * An annotation tied with the current front is *not* already in front — the
 * tie is what the click is there to break — so it moves.
 */
export function resolveLayerZ(nodes, id, direction) {
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
    return Math.floor(max) + 1;
  }
  const min = Math.min(...others);
  if (current < min) return null;
  return Math.ceil(min) - 1;
}
