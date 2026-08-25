/**
 * Manual forward/back layer ordering for annotations
 * (docs/ANNOTATION_CONTRACT.md: "Use semantic default layers plus manual
 * forward/back").
 *
 * An annotation's layer is its envelope `z` field, carried on the ReactFlow
 * node as `zIndex` (overlayToFlowNode/flowNodeToOverlay in ./annotations.js)
 * and persisted by the host's session translators. This module owns only the
 * arithmetic: which `z` a "bring forward" / "send backward" click should
 * produce, given what else is currently on the canvas.
 */

import { ANNOTATION_TYPES } from './annotations';

export const LAYER_FORWARD = 'forward';
export const LAYER_BACKWARD = 'backward';

function layerOf(node) {
  const z = node?.zIndex;
  return Number.isFinite(z) ? z : 0;
}

/**
 * The `z` a forward/backward step should move `id` to, or `null` when the
 * annotation is already at the front (forward) or back (backward) and the
 * click is a no-op.
 *
 * One step moves past exactly one neighbouring layer rather than straight to
 * the front — that is what "forward/back" means, as against "to front/to
 * back" — and it lands *strictly* past it. Landing on a neighbour's own `z`
 * would leave the two annotations' paint order decided by ReactFlow's DOM
 * order, so a user who clicked "forward" could see nothing move; the step is
 * therefore the midpoint between the neighbour being passed and the next
 * layer beyond it, or neighbour ± 1 when there is no layer beyond. `z` is a
 * float in the annotation model (backend/core/session_annotations.py), so a
 * midpoint always exists and no other annotation ever has to be renumbered
 * to make room.
 *
 * Only other *annotations* are considered. Graph nodes and edges share the
 * same ReactFlow z-space but are not part of the annotation layer model, and
 * stepping an annotation past one would be a silent, unrequested change to
 * how the graph itself paints.
 */
export function resolveLayerZ(nodes, id, direction) {
  const self = nodes.find((n) => n.id === id);
  if (!self) return null;
  const current = layerOf(self);
  const forward = direction === LAYER_FORWARD;
  // Distinct layers only: several annotations sharing one `z` are one
  // neighbouring layer to step past, not several.
  const levels = [
    ...new Set(
      nodes
        .filter((n) => n.id !== id && ANNOTATION_TYPES.has(n.type))
        .map(layerOf)
        .filter((z) => (forward ? z >= current : z <= current))
    ),
  ].sort((a, b) => (forward ? a - b : b - a));
  if (levels.length === 0) return null;
  const [passed, beyond] = levels;
  if (beyond === undefined) return forward ? passed + 1 : passed - 1;
  return (passed + beyond) / 2;
}
