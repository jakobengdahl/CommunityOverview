/**
 * Annotation helpers shared by GraphCanvas.
 *
 * Overlays (note/label/arrow) are free-floating annotation nodes. Groups are a
 * separate, pre-existing annotation kind with their own containment/parenting
 * logic, so they are tracked apart from these overlays.
 */

// text/frame/shape/icon/vote_dot/image/freehand are the rest of the v1
// annotation model (docs/ANNOTATION_CONTRACT.md) that isn't note/label/
// arrow/group. text/frame/shape/icon/vote_dot/image render through
// GenericAnnotationNode — a simple, non-interactive visual representation
// rather than dedicated per-type UX like NoteNode. freehand renders through
// its own FreehandAnnotationNode (an SVG path, like ArrowNode) but still
// shares this generic envelope-field handling.
export const GENERIC_OVERLAY_TYPES = new Set([
  'text',
  'frame',
  'shape',
  'icon',
  'vote_dot',
  'image',
  'freehand',
]);
export const OVERLAY_TYPES = new Set(['note', 'label', 'arrow', ...GENERIC_OVERLAY_TYPES]);
export const ANNOTATION_TYPES = new Set([
  'group',
  'note',
  'label',
  'arrow',
  ...GENERIC_OVERLAY_TYPES,
]);

// Default box size (px) for a generic overlay that carries explicit
// dimensions (frame/shape/image) but wasn't given a size.
const DEFAULT_GENERIC_SIZE = { w: 160, h: 96 };

// Per-kind payload fields carried on a generic overlay's `data`, beyond the
// shared id/type/position/style. Drives both overlayToFlowNode and its
// inverse so the two stay exact mirrors of each other.
const GENERIC_OVERLAY_FIELDS = {
  text: ['text', 'color', 'fontSize'],
  frame: ['color'],
  shape: ['shape', 'color'],
  icon: ['icon', 'color'],
  vote_dot: ['value', 'color'],
  image: ['image', 'alt', 'color'],
  // `points` are node-relative (relative to the node's own `position`, the
  // stroke's anchor/first sampled point) — the same convention arrow's
  // dx/dy uses, so a plain ReactFlow drag (which only updates `position`)
  // moves the whole stroke without this layer having to touch `points`.
  freehand: ['points', 'color', 'strokeWidth', 'smoothing', 'pointerType', 'pressureSource'],
};

// Generic overlay kinds that carry an explicit box size (frame/shape/image);
// icon/vote_dot/text render at a fixed intrinsic size instead.
const SIZED_GENERIC_KINDS = new Set(['frame', 'shape', 'image']);

// The kinds the contract accepts rotation for: text/headings, labels/callouts,
// sticky notes, images, icons/dots and basic shapes (process arrow included,
// as a shape variant). Frames, lines, groups and freehand strokes are not
// rotatable, so their geometry.rotation is carried but never drawn.
export const ROTATABLE_OVERLAY_KINDS = new Set([
  'note',
  'label',
  'text',
  'shape',
  'icon',
  'vote_dot',
  'image',
]);

// Inline style that draws an annotation's geometry.rotation, or an empty
// style when this kind is not rotatable or has no rotation. Applied to the
// rendered element rather than to the ReactFlow node wrapper, so drag and
// resize keep working against the unrotated bounding box.
export function rotationStyle(kind, rotation) {
  if (!ROTATABLE_OVERLAY_KINDS.has(kind)) return {};
  if (!Number.isFinite(rotation) || rotation === 0) return {};
  return { transform: `rotate(${rotation}deg)`, transformOrigin: 'center center' };
}

// Default text sizes (px) for note body and label text; overridable per node.
export const DEFAULT_NOTE_FONT_SIZE = 14;
export const DEFAULT_LABEL_FONT_SIZE = 16;

// Flow distance (px, unscaled) within which an arrow endpoint snaps onto a
// node/annotation centre. Kept generous so connecting is easy (design intent).
export const SNAP_RADIUS = 40;

export function isManualNode(node) {
  return node.type === 'group' || node.id.startsWith('group-') || OVERLAY_TYPES.has(node.type);
}

// An arrow with either endpoint bound to a node/annotation should not be
// dragged as a whole — its anchored ends must stay on their targets. Only its
// endpoint handles move it. Free arrows drag normally.
export function isArrowAnchored(data) {
  return Boolean(data?.startAnchor || data?.endAnchor);
}

// Build a ReactFlow node for a note/label/arrow overlay from the host's
// canvas-shape annotation ({id, kind, position, ...payload}).
export function overlayToFlowNode(overlay) {
  const base = { id: overlay.id, type: overlay.kind, position: overlay.position || { x: 0, y: 0 } };
  // `z` (layer order) and `locked` (the canvas UI's own edit-lock convention,
  // set via the generic MCP annotation tools) are envelope fields on every v1
  // annotation type. They must round-trip through the ReactFlow node — a flow
  // node that silently dropped them would, on the next autosave, diff back out
  // as an `annotation_updated` that resets a collaborator's/agent's `z`/`locked`
  // to their defaults, overwriting the very change realtime sync just delivered.
  const locked = Boolean(overlay.locked);
  const zIndex = overlay.z ?? 0;
  // Rotation is an envelope field for the same reason as z/locked: a flow node
  // that dropped it would diff back out on the next autosave as a rotation
  // reset, silently overwriting whatever an agent or collaborator had set.
  const rotation = overlay.rotation ?? 0;
  if (overlay.kind === 'note') {
    return {
      ...base,
      data: {
        text: overlay.text || '',
        color: overlay.color,
        fontSize: overlay.fontSize,
        locked,
        rotation,
      },
      style: overlay.size
        ? { width: overlay.size.w, height: overlay.size.h }
        : { width: 200, height: 140 },
      draggable: !locked,
      zIndex,
    };
  }
  if (overlay.kind === 'label') {
    return {
      ...base,
      data: {
        text: overlay.text || '',
        color: overlay.color,
        fontSize: overlay.fontSize,
        locked,
        rotation,
      },
      draggable: !locked,
      zIndex,
    };
  }
  if (GENERIC_OVERLAY_TYPES.has(overlay.kind)) {
    const data = { locked, rotation };
    for (const field of GENERIC_OVERLAY_FIELDS[overlay.kind]) data[field] = overlay[field];
    const node = { ...base, data, draggable: !locked, zIndex };
    if (SIZED_GENERIC_KINDS.has(overlay.kind)) {
      node.style = overlay.size
        ? { width: overlay.size.w, height: overlay.size.h }
        : { width: DEFAULT_GENERIC_SIZE.w, height: DEFAULT_GENERIC_SIZE.h };
    }
    return node;
  }
  // arrow / line: endpoints carry independent head symbols and optional anchors.
  const data = {
    dx: overlay.dx ?? 160,
    dy: overlay.dy ?? 0,
    color: overlay.color,
    startArrow: overlay.startArrow ?? false,
    endArrow: overlay.endArrow ?? true,
    locked,
    rotation,
  };
  if (overlay.startAnchor) data.startAnchor = overlay.startAnchor;
  if (overlay.endAnchor) data.endAnchor = overlay.endAnchor;
  return { ...base, data, draggable: !locked && !isArrowAnchored(data), zIndex };
}

// Serialize a ReactFlow overlay node back to the host's canvas-shape annotation.
export function flowNodeToOverlay(node) {
  const base = { id: node.id, kind: node.type, position: node.position };
  // Mirrors overlayToFlowNode's envelope fields; see its comment for why these
  // must survive the round trip. `node.zIndex`/`node.data.locked` are undefined
  // on a freshly created node (never synced yet), hence the defaults below.
  const z = node.zIndex ?? 0;
  const locked = Boolean(node.data?.locked);
  const rotation = node.data?.rotation ?? 0;
  if (node.type === 'note') {
    return {
      ...base,
      text: node.data?.text || '',
      color: node.data?.color,
      fontSize: node.data?.fontSize,
      size: node.style ? { w: node.style.width, h: node.style.height } : undefined,
      z,
      locked,
      rotation,
    };
  }
  if (node.type === 'label') {
    return {
      ...base,
      text: node.data?.text || '',
      color: node.data?.color,
      fontSize: node.data?.fontSize,
      z,
      locked,
      rotation,
    };
  }
  if (GENERIC_OVERLAY_TYPES.has(node.type)) {
    const out = { ...base, z, locked, rotation };
    for (const field of GENERIC_OVERLAY_FIELDS[node.type]) out[field] = node.data?.[field];
    if (SIZED_GENERIC_KINDS.has(node.type) && node.style) {
      out.size = { w: node.style.width, h: node.style.height };
    }
    return out;
  }
  const out = {
    ...base,
    dx: node.data?.dx ?? 160,
    dy: node.data?.dy ?? 0,
    color: node.data?.color,
    startArrow: node.data?.startArrow ?? false,
    endArrow: node.data?.endArrow ?? true,
    z,
    locked,
    rotation,
  };
  if (node.data?.startAnchor) out.startAnchor = node.data.startAnchor;
  if (node.data?.endAnchor) out.endAnchor = node.data.endAnchor;
  return out;
}

// Centre point (flow coords) of a node, using its measured size when available.
// Returns null when the node has no usable position.
export function nodeCenter(node) {
  const pos = node.positionAbsolute || node.position;
  if (!pos) return null;
  const w = node.width || node.style?.width || 0;
  const h = node.height || node.style?.height || 0;
  return { x: pos.x + w / 2, y: pos.y + h / 2 };
}

// Find the nearest snappable node/annotation centre to `point` within `radius`.
// Arrows never snap to other arrows, nor to themselves. Returns the target's
// id or null when nothing is close enough.
export function findSnapTarget(point, nodes, { excludeId, radius = SNAP_RADIUS } = {}) {
  let best = null;
  let bestDist = radius;
  for (const n of nodes) {
    if (n.id === excludeId || n.type === 'arrow') continue;
    const c = nodeCenter(n);
    if (!c) continue;
    const d = Math.hypot(c.x - point.x, c.y - point.y);
    if (d <= bestDist) {
      bestDist = d;
      best = n.id;
    }
  }
  return best;
}

// Whether an arrow endpoint is *currently* held to a present target. An anchor
// id is preserved even while its target is absent from the view (filtered,
// collapsed, or not yet loaded) so it re-glues when the target returns — but
// while absent the arrow is not held and stays freely draggable. `existingIds`
// is the set of node ids currently rendered.
export function isArrowHeld(data, existingIds) {
  const { startAnchor, endAnchor } = data || {};
  return Boolean(
    (startAnchor && existingIds.has(startAnchor)) || (endAnchor && existingIds.has(endAnchor))
  );
}

// Recompute an arrow's geometry so its anchored endpoints sit on the current
// centres of their target nodes. `centers` maps nodeId -> {x, y}. Returns a new
// {position, dx, dy} when it differs from the arrow's current geometry, else
// null (nothing to update). Endpoints without a live anchor keep their place.
export function resolveAnchoredArrow(arrow, centers) {
  const { startAnchor, endAnchor } = arrow.data || {};
  if (!startAnchor && !endAnchor) return null;
  const pos = arrow.position;
  const dx = arrow.data?.dx ?? 160;
  const dy = arrow.data?.dy ?? 0;
  const start = (startAnchor && centers.get(startAnchor)) || { x: pos.x, y: pos.y };
  const end = (endAnchor && centers.get(endAnchor)) || { x: pos.x + dx, y: pos.y + dy };
  const newDx = end.x - start.x;
  const newDy = end.y - start.y;
  if (start.x === pos.x && start.y === pos.y && newDx === dx && newDy === dy) return null;
  return { position: { x: start.x, y: start.y }, dx: newDx, dy: newDy };
}
