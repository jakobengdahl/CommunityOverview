/**
 * Annotation helpers shared by GraphCanvas.
 *
 * Overlays (note/label/arrow) are free-floating annotation nodes. Groups are a
 * separate, pre-existing annotation kind with their own containment/parenting
 * logic, so they are tracked apart from these overlays.
 */

export const OVERLAY_TYPES = new Set(['note', 'label', 'arrow']);
export const ANNOTATION_TYPES = new Set(['group', 'note', 'label', 'arrow']);

export function isManualNode(node) {
  return node.type === 'group' || node.id.startsWith('group-') || OVERLAY_TYPES.has(node.type);
}

// Build a ReactFlow node for a note/label/arrow overlay from the host's
// canvas-shape annotation ({id, kind, position, ...payload}).
export function overlayToFlowNode(overlay) {
  const base = { id: overlay.id, type: overlay.kind, position: overlay.position || { x: 0, y: 0 } };
  if (overlay.kind === 'note') {
    return {
      ...base,
      data: { text: overlay.text || '', color: overlay.color },
      style: overlay.size ? { width: overlay.size.w, height: overlay.size.h } : { width: 200, height: 140 },
    };
  }
  if (overlay.kind === 'label') {
    return { ...base, data: { text: overlay.text || '', color: overlay.color } };
  }
  // arrow
  return { ...base, data: { dx: overlay.dx ?? 160, dy: overlay.dy ?? 0, color: overlay.color } };
}

// Serialize a ReactFlow overlay node back to the host's canvas-shape annotation.
export function flowNodeToOverlay(node) {
  const base = { id: node.id, kind: node.type, position: node.position };
  if (node.type === 'note') {
    return {
      ...base,
      text: node.data?.text || '',
      color: node.data?.color,
      size: node.style ? { w: node.style.width, h: node.style.height } : undefined,
    };
  }
  if (node.type === 'label') {
    return { ...base, text: node.data?.text || '', color: node.data?.color };
  }
  return { ...base, dx: node.data?.dx ?? 160, dy: node.data?.dy ?? 0, color: node.data?.color };
}
