/**
 * Manual layer ordering for GROUP BACKGROUNDS RELATIVE TO EACH OTHER
 * (dec-annotation-group-background-layering, smallfix-group-annotation-has-
 * no-layer-control).
 *
 * "A group background always stays behind all graph nodes and all other
 * annotation kinds" is not something this module enforces — it does not need
 * to, because it is already structural. `reorderNodesForParentChild`
 * (GraphCanvas.jsx) buckets every group node ahead of every non-group node in
 * the ReactFlow array, unconditionally; on top of that, GroupNode.css pins
 * `.react-flow__node-group` to CSS `z-index: -1 !important` while
 * `.react-flow__node-custom` sits at `z-index: 1 !important` — so even a
 * group whose own z-space value below happened to collide with a graph
 * node's stacking level could not paint over it. Nothing this module
 * computes is ever applied as CSS `z-index` at all (see the module docstring
 * on `resolveGroupOrderZ` below), so it has no path to weaken that
 * guarantee.
 *
 * What *is* left open by the decision, and what this module resolves: which
 * group renders closest to the front of that always-behind-everything bucket
 * when two or more groups overlap or share visual space with each other.
 * Group-to-group order is decided by the bucket's own array order — the tied
 * `z-index: -1` above means, among groups, whichever comes later in the DOM
 * paints on top of the others — and `reorderNodesForParentChild` derives
 * that array order by sorting the groups it collects by each group's own
 * `data.z` (ascending, stable on ties). This module owns only the
 * arithmetic behind the two-button "bring forward / send backward, among
 * groups" control: which `data.z` a click should write, given the other
 * groups already on the canvas.
 *
 * Deliberately a separate mechanism from ./annotationLayers.js's bring-to-
 * front/send-to-back, not a reuse of it, per this task's own history
 * (docs/ANNOTATION_CONTRACT.md's Layer order / Semantic default layers
 * sections): annotationLayers.js reads and writes a ReactFlow node's own
 * `zIndex` — which ReactFlow applies straight into inline CSS `z-index` — and
 * compares it against every OTHER *annotation* kind sharing that same
 * CSS-facing z-space (ANNOTATION_TYPES). A group's `z` lives on `data.z`
 * instead, is never copied onto `zIndex`, and is never drawn as CSS at all.
 * Comparing a group's `data.z` against a shape's `zIndex` would be a
 * meaningless cross-namespace comparison, so this module only ever compares
 * one group's `data.z` against other groups' `data.z` — never against any
 * other kind, and never in a way that could move a group out of the
 * always-behind-everything bucket it structurally belongs to.
 */

export const GROUP_LAYER_FRONT = 'front';
export const GROUP_LAYER_BACK = 'back';

// Kept in the same safe integer range annotationLayers.js clamps to, for the
// same reason stated there (an agent may set any float `z` over MCP — `z` is
// `Optional[float]` in backend/core/session_annotations.py) even though a
// group's `z` is never handed to a browser as CSS `z-index` and so never hits
// CSS's own `int32` ceiling the way an overlay's `zIndex` can. Reusing the
// bound avoids a second, differently-reasoned set of edge cases: past 2^53
// the `Math.floor`/`Math.ceil` step below is swallowed by float precision
// regardless of what the value is eventually used for.
const Z_MAX = 2147483647;
const Z_MIN = -2147483648;

function inRange(z) {
  return z >= Z_MIN && z <= Z_MAX;
}

function groupZOf(node) {
  const z = node?.data?.z;
  return Number.isFinite(z) ? z : 0;
}

/**
 * The `data.z` that puts group `id` in front of (or behind) every OTHER
 * group currently on the canvas, or `null` when it is already there and the
 * click is a no-op — including when `id` is not a group node at all, or
 * when it is the only group on the canvas (nothing to order it against).
 *
 * Mirrors `resolveLayerZ` (./annotationLayers.js)'s arithmetic and no-op/
 * refusal rules exactly — same "integer strictly past every other value"
 * guarantee, same "a tie with the current front is not already in front"
 * rule, same both-bounds-in-both-directions CSS-range guard — but over the
 * groups-only `data.z` space described in the module docstring above,
 * never mixed with any other kind's `zIndex`.
 */
export function resolveGroupOrderZ(nodes, id, direction) {
  if (direction !== GROUP_LAYER_FRONT && direction !== GROUP_LAYER_BACK) return null;
  const self = (nodes || []).find((n) => n.id === id && n?.type === 'group');
  if (!self) return null;
  const current = groupZOf(self);
  const others = (nodes || []).filter((n) => n.id !== id && n?.type === 'group').map(groupZOf);
  if (others.length === 0) return null;
  if (direction === GROUP_LAYER_FRONT) {
    const max = Math.max(...others);
    if (current > max) return null;
    const z = Math.floor(max) + 1;
    return z > max && inRange(z) ? z : null;
  }
  const min = Math.min(...others);
  if (current < min) return null;
  const z = Math.ceil(min) - 1;
  return z < min && inRange(z) ? z : null;
}
