// Pure transforms between the server-side annotation model and the
// canvas-facing shapes GraphCanvas emits/consumes. Shared by the shared-session
// lifecycle (useSharedSession) and by App's incremental-op and snapshot paths.
import { createAnnotation, normalizeAnnotationDocument } from '@community-graph/ui-graph-canvas';

// Group boxes persist inside the generic server-side annotation list as
// `kind: "group"` (design 3.1). These two helpers translate between that
// server shape and the {groups, parentIds} shape the canvas round-trips.
function documentAnnotations(annotations) {
  if (annotations == null) return [];
  if (annotations?.schema_version === 1 && Array.isArray(annotations.annotations)) {
    return normalizeAnnotationDocument(annotations).annotations;
  }
  if (Array.isArray(annotations)) return normalizeAnnotationDocument(annotations).annotations;
  throw new Error('Malformed session payload: state.annotations is not an array');
}

export function annotationDocumentToLegacyMetadata(documentInput) {
  const document = normalizeAnnotationDocument(documentInput || []);
  return {
    annotation_schema_version: document.schema_version,
    annotations: annotationsToOverlays(document),
    groups: annotationsToGroups(document).groups,
  };
}

export function legacyMetadataToAnnotationDocument(metadata = {}) {
  if (metadata.annotation_document)
    return normalizeAnnotationDocument(metadata.annotation_document);
  if (metadata.annotations != null && !Array.isArray(metadata.annotations)) {
    throw new Error('Malformed session payload: state.annotations is not an array');
  }
  if (metadata.annotation_schema_version === 1 && Array.isArray(metadata.annotations)) {
    return normalizeAnnotationDocument(metadata.annotations);
  }
  return normalizeAnnotationDocument([
    ...groupsToAnnotations(metadata.groups || [], metadata.parentIds || {}),
    ...overlaysToAnnotations(metadata.annotations || []),
  ]);
}

export function annotationsToGroups(annotations) {
  const groups = [];
  const parentIds = {};
  const document = documentAnnotations(annotations);
  for (const a of document) {
    if (a?.type !== 'group') continue;
    groups.push({
      id: a.id,
      label: a.label || 'Group',
      description: a.description || '',
      position: a.position || { x: 0, y: 0 },
      style: a.size ? { width: a.size.w, height: a.size.h } : undefined,
      color: a.color ?? a.style?.color,
    });
    for (const m of a.member_node_ids || []) parentIds[m] = a.id;
  }
  return { groups, parentIds };
}

export function groupsToAnnotations(viewGroups, parentIds) {
  const membersByGroup = {};
  for (const [nodeId, groupId] of Object.entries(parentIds || {})) {
    (membersByGroup[groupId] = membersByGroup[groupId] || []).push(nodeId);
  }
  return (viewGroups || []).map((g) =>
    createAnnotation({
      id: g.id,
      type: 'group',
      position: g.position || { x: 0, y: 0 },
      label: g.label || '',
      description: g.description || '',
      color: g.color,
      size: g.style ? { w: g.style.width, h: g.style.height } : undefined,
      member_node_ids: membersByGroup[g.id] || [],
    })
  );
}

// The rest of the v1 annotation model (docs/ANNOTATION_CONTRACT.md) beyond
// note/label/line: text, frame, shape, icon, vote_dot, image. Color lives
// under style.color (like label), and frame/shape/image size lives in
// geometry — createAnnotation has no dedicated `size` payload field for
// these types the way it does for note. `geometry.rotation` is carried on
// every overlay (not only the kinds that draw it) for the same reason as
// z/locked: a translator that dropped it would make the browser's next
// autosave diff the annotation back to rotation 0, silently undoing what an
// agent or a collaborator had just set.
const SIZED_GENERIC_TYPES = new Set(['frame', 'shape', 'image']);
const GENERIC_OVERLAY_TYPES = new Set(['text', 'frame', 'shape', 'icon', 'vote_dot', 'image']);

function genericAnnotationToOverlay(a) {
  const overlay = {
    id: a.id,
    kind: a.type,
    position: a.position || { x: 0, y: 0 },
    color: a.style?.color,
    z: a.z ?? 0,
    locked: Boolean(a.locked),
    rotation: a.geometry?.rotation ?? 0,
  };
  if (a.type === 'text') {
    overlay.text = a.text || '';
    overlay.fontSize = a.style?.fontSize;
    overlay.attachment = a.attachment;
  } else if (a.type === 'shape') {
    overlay.shape = a.shape || 'rectangle';
  } else if (a.type === 'icon') {
    overlay.icon = a.icon || 'circle';
    overlay.attachment = a.attachment;
  } else if (a.type === 'vote_dot') {
    overlay.value = a.value ?? null;
    overlay.attachment = a.attachment;
  } else if (a.type === 'image') {
    overlay.image = a.image || {};
    overlay.alt = a.alt || '';
  }
  if (SIZED_GENERIC_TYPES.has(a.type)) {
    overlay.size = { w: a.geometry?.w ?? 0, h: a.geometry?.h ?? 0 };
  }
  return overlay;
}

function genericOverlayToAnnotation(o) {
  const input = {
    id: o.id,
    type: o.kind,
    position: o.position || { x: 0, y: 0 },
    z: o.z ?? 0,
    locked: Boolean(o.locked),
    rotation: o.rotation ?? 0,
  };
  input.style = o.kind === 'text' ? { color: o.color, fontSize: o.fontSize } : { color: o.color };
  if (o.kind === 'text') {
    input.text = o.text || '';
    input.attachment = o.attachment;
  } else if (o.kind === 'shape') input.shape = o.shape || 'rectangle';
  else if (o.kind === 'icon') {
    input.icon = o.icon || 'circle';
    input.attachment = o.attachment;
  } else if (o.kind === 'vote_dot') {
    input.value = o.value ?? null;
    input.attachment = o.attachment;
  } else if (o.kind === 'image') {
    input.image = o.image || {};
    input.alt = o.alt || '';
  }
  if (SIZED_GENERIC_TYPES.has(o.kind) && o.size) input.size = o.size;
  return createAnnotation(input);
}

// freehand stores its sampled points as absolute model-space coordinates
// (design: same envelope as `line`'s from/to), but the canvas overlay/flow
// node shape needs `position` + points *relative* to it, the same anchor
// convention `line`'s dx/dy uses, so a plain ReactFlow drag (which only
// moves `position`) slides the whole stroke without this layer rewriting
// every point on every render.
function freehandAnnotationToOverlay(a) {
  const rawPoints = Array.isArray(a.points) && a.points.length ? a.points : [{ x: 0, y: 0 }];
  const anchor = rawPoints[0];
  return {
    id: a.id,
    kind: 'freehand',
    position: { x: anchor.x, y: anchor.y },
    points: rawPoints.map((p) => {
      const point = { x: p.x - anchor.x, y: p.y - anchor.y };
      if (p.pressure != null) point.pressure = p.pressure;
      return point;
    }),
    color: a.style?.color,
    strokeWidth: a.strokeWidth,
    smoothing: a.smoothing ?? 0,
    pointerType: a.pointerType,
    pressureSource: a.pressureSource,
    z: a.z ?? 0,
    locked: Boolean(a.locked),
    rotation: a.geometry?.rotation ?? 0,
  };
}

function freehandOverlayToAnnotation(o) {
  const anchor = o.position || { x: 0, y: 0 };
  const rawPoints = Array.isArray(o.points) && o.points.length ? o.points : [{ x: 0, y: 0 }];
  const points = rawPoints.map((p) => {
    const point = { x: anchor.x + (p.x ?? 0), y: anchor.y + (p.y ?? 0) };
    if (p.pressure != null) point.pressure = p.pressure;
    return point;
  });
  return createAnnotation({
    id: o.id,
    type: 'freehand',
    position: { x: anchor.x, y: anchor.y },
    points,
    style: { color: o.color },
    strokeWidth: o.strokeWidth,
    smoothing: o.smoothing ?? 0,
    pointerType: o.pointerType,
    pressureSource: o.pressureSource,
    z: o.z ?? 0,
    locked: Boolean(o.locked),
    rotation: o.rotation ?? 0,
  });
}

// Note/label/arrow annotations round-trip between the server annotation model
// (design 3.1) and the canvas-shape overlay descriptors the GraphCanvas emits
// (via onSaveView) and consumes (via annotationsToRestore). Groups keep their
// own translation above; these cover the free-floating overlays from step 5.
export function annotationsToOverlays(annotations) {
  const out = [];
  const document = documentAnnotations(annotations);
  for (const a of document) {
    if (a?.type === 'note') {
      out.push({
        id: a.id,
        kind: 'note',
        position: a.position || { x: 0, y: 0 },
        text: a.text || '',
        color: a.color,
        fontSize: a.fontSize,
        size: a.size,
        z: a.z ?? 0,
        locked: Boolean(a.locked),
        rotation: a.geometry?.rotation ?? 0,
      });
    } else if (a?.type === 'label') {
      out.push({
        id: a.id,
        kind: 'label',
        position: a.position || { x: 0, y: 0 },
        text: a.text || '',
        color: a.style?.color,
        fontSize: a.style?.fontSize,
        attachment: a.attachment,
        z: a.z ?? 0,
        locked: Boolean(a.locked),
        rotation: a.geometry?.rotation ?? 0,
      });
    } else if (a?.type === 'line') {
      const from = a.from || a.position || { x: 0, y: 0 };
      const to = a.to || { x: from.x + 160, y: from.y };
      const overlay = {
        id: a.id,
        kind: 'arrow',
        position: { x: from.x, y: from.y },
        dx: to.x - from.x,
        dy: to.y - from.y,
        color: a.style?.color,
        startArrow: a.startArrow ?? false,
        endArrow: a.endArrow ?? true,
        z: a.z ?? 0,
        locked: Boolean(a.locked),
        rotation: a.geometry?.rotation ?? 0,
      };
      if (a.startAnchor) overlay.startAnchor = a.startAnchor;
      if (a.endAnchor) overlay.endAnchor = a.endAnchor;
      out.push(overlay);
    } else if (a?.type === 'freehand') {
      out.push(freehandAnnotationToOverlay(a));
    } else if (GENERIC_OVERLAY_TYPES.has(a?.type)) {
      out.push(genericAnnotationToOverlay(a));
    }
  }
  return out;
}

export function overlaysToAnnotations(overlays) {
  return (overlays || []).map((o) => {
    if (o.kind === 'note') {
      return createAnnotation({
        id: o.id,
        type: 'note',
        position: o.position || { x: 0, y: 0 },
        text: o.text || '',
        color: o.color,
        fontSize: o.fontSize,
        size: o.size,
        z: o.z ?? 0,
        locked: Boolean(o.locked),
        rotation: o.rotation ?? 0,
      });
    }
    if (o.kind === 'label') {
      return createAnnotation({
        id: o.id,
        type: 'label',
        position: o.position || { x: 0, y: 0 },
        text: o.text || '',
        style: { color: o.color, fontSize: o.fontSize },
        attachment: o.attachment,
        z: o.z ?? 0,
        locked: Boolean(o.locked),
        rotation: o.rotation ?? 0,
      });
    }
    if (o.kind === 'freehand') {
      return freehandOverlayToAnnotation(o);
    }
    if (GENERIC_OVERLAY_TYPES.has(o.kind)) {
      return genericOverlayToAnnotation(o);
    }
    // arrow: store both endpoints as absolute points (design 3.1)
    const from = o.position || { x: 0, y: 0 };
    const dx = o.dx ?? 160;
    const dy = o.dy ?? 0;
    const ann = {
      id: o.id,
      type: 'line',
      position: { x: from.x, y: from.y },
      from: { x: from.x, y: from.y },
      to: { x: from.x + dx, y: from.y + dy },
      style: { color: o.color },
      startArrow: o.startArrow ?? false,
      endArrow: o.endArrow ?? true,
      z: o.z ?? 0,
      locked: Boolean(o.locked),
      rotation: o.rotation ?? 0,
    };
    if (o.startAnchor) ann.startAnchor = o.startAnchor;
    if (o.endAnchor) ann.endAnchor = o.endAnchor;
    return createAnnotation(ann);
  });
}
