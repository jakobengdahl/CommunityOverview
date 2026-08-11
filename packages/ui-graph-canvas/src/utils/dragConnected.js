/**
 * Helpers for "drag a node together with its directly connected nodes".
 *
 * Holding Alt while dragging a graph node moves that node together with every
 * node directly joined to it by an edge. Alt is deliberately chosen because
 * Shift/Ctrl/Meta are all reserved for multi-select on the canvas
 * (multiSelectionKeyCode and the graph-suppress-selection gesture), so Alt is
 * the only standard modifier that does not collide with selection.
 *
 * The neighbour set is the same "directly connected" notion the "Select related
 * nodes" action uses: any node sharing an edge with a dragged node.
 */

import { ANNOTATION_TYPES } from './annotations';

// Directly-connected neighbour ids of a set of dragged nodes: every node joined
// by an edge to any dragged node, excluding the dragged nodes themselves (they
// are already moved by ReactFlow, so they must not be double-moved).
export function directNeighborIds(edges, draggedIds) {
  const neighbors = new Set();
  for (const e of edges || []) {
    if (draggedIds.has(e.source) && !draggedIds.has(e.target)) neighbors.add(e.target);
    else if (draggedIds.has(e.target) && !draggedIds.has(e.source)) neighbors.add(e.source);
  }
  return neighbors;
}

// Snapshot the starting positions of the graph nodes that should trail the
// drag: the given neighbour ids, minus annotation/group nodes (which have their
// own containment rules and never follow a connected node). Returns a Map of
// id -> {x, y} captured at drag start; the drag handler adds the anchor's delta
// to these to keep the whole neighbourhood rigid.
//
// `draggedIds` (optional) is the full set of node ids ReactFlow is already
// dragging (including any group nodes). A neighbour that is a child of one of
// those groups is moved in absolute space by its parent's drag, so adding the
// delta a second time would move it twice — such neighbours are skipped.
export function neighborStartPositions(nodes, neighborIds, draggedIds = null) {
  const startById = new Map();
  for (const n of nodes || []) {
    if (!neighborIds.has(n.id) || ANNOTATION_TYPES.has(n.type)) continue;
    if (draggedIds && n.parentId && draggedIds.has(n.parentId)) continue;
    startById.set(n.id, { x: n.position.x, y: n.position.y });
  }
  return startById;
}

// Given the drag anchor's start and current position, translate every recorded
// neighbour start by the same delta. Positions live in each node's own frame
// (parent-relative for grouped nodes); because no parent moves during the drag,
// a single flow-space delta translates every neighbour by the same absolute
// amount regardless of grouping. Returns a Map of id -> {x, y}.
export function neighborDragPositions(startById, anchorStart, anchorNow) {
  const dx = anchorNow.x - anchorStart.x;
  const dy = anchorNow.y - anchorStart.y;
  const out = new Map();
  for (const [id, start] of startById) {
    out.set(id, { x: start.x + dx, y: start.y + dy });
  }
  return out;
}
