// Pure transforms between the server-side annotation model and the
// canvas-facing shapes GraphCanvas emits/consumes. Shared by the shared-session
// lifecycle (useSharedSession) and by App's incremental-op and snapshot paths.
import { createAnnotation, normalizeAnnotationDocument } from '@community-graph/ui-graph-canvas';

// Group boxes persist inside the generic server-side annotation list as
// `kind: "group"` (design 3.1). These two helpers translate between that
// server shape and the {groups, parentIds} shape the canvas round-trips.
// An annotation this version cannot read is dropped, not fatal. It used to
// take the whole call down, which in the app means the session never opens —
// the user losing everything because one stored decoration had a kind that no
// longer exists. Reported to the console so the discard is findable, since a
// silent drop looks to the user like their annotation was deleted.
function skippedAnnotation(annotation, error) {
  console.warn('Skipping an annotation this version cannot read:', annotation, error?.message);
}

function skippedOverlay(overlay, error) {
  console.warn('Skipping an annotation overlay that cannot be saved:', overlay, error?.message);
}

function documentAnnotations(annotations) {
  if (annotations == null) return [];
  if (annotations?.schema_version === 1 && Array.isArray(annotations.annotations)) {
    return normalizeAnnotationDocument(annotations, { onSkipped: skippedAnnotation }).annotations;
  }
  if (Array.isArray(annotations)) {
    return normalizeAnnotationDocument(annotations, { onSkipped: skippedAnnotation }).annotations;
  }
  // Still fatal, deliberately: a payload whose annotation slot is not a list at
  // all is a malformed session, not an annotation this version cannot read.
  throw new Error('Malformed session payload: state.annotations is not an array');
}

export function annotationDocumentToLegacyMetadata(documentInput) {
  const document = normalizeAnnotationDocument(documentInput || [], {
    onSkipped: skippedAnnotation,
  });
  return {
    annotation_schema_version: document.schema_version,
    annotations: annotationsToOverlays(document),
    groups: annotationsToGroups(document).groups,
  };
}

export function savedViewMetadataToCanvasMetadata(metadata = {}) {
  const document = legacyMetadataToAnnotationDocument(metadata);
  const { groups, parentIds } = annotationsToGroups(document);
  return {
    groups,
    parentIds,
    annotations: annotationsToOverlays(document),
  };
}

export function legacyMetadataToAnnotationDocument(metadata = {}) {
  const skip = { onSkipped: skippedAnnotation };
  if (metadata.annotation_document)
    return normalizeAnnotationDocument(metadata.annotation_document, skip);
  if (metadata.annotations != null && !Array.isArray(metadata.annotations)) {
    throw new Error('Malformed session payload: state.annotations is not an array');
  }
  if (metadata.annotation_schema_version === 1 && Array.isArray(metadata.annotations)) {
    return normalizeAnnotationDocument(metadata.annotations, skip);
  }
  // Groups and overlays now both apply the same "skip the unreadable entry,
  // keep the rest" rule before handing the composed document to the normalizer.
  return normalizeAnnotationDocument(
    [
      ...groupsToAnnotations(metadata.groups || [], metadata.parentIds || {}),
      ...overlaysToAnnotations(metadata.annotations || []),
    ],
    skip
  );
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
      // The same envelope fields every overlay kind's translator already
      // carries. A group could always be locked over MCP, but the flag stopped
      // here, so the canvas never saw it and the next save diffed it back to
      // its default. `z` is carried for the same reason and is now also read:
      // reorderNodesForParentChild sorts the groups bucket by `data.z`
      // ascending, so group paint order is z among groups, array order among
      // everything else.
      z: a.z ?? 0,
      locked: Boolean(a.locked),
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
  // A group is an annotation kind, so it answers to the same rule as the rest:
  // skip what cannot be read, report it, keep the others. Two distinct ways to
  // be unreadable are handled below, because they fail differently — a
  // primitive slips through silently, while a bad payload throws.
  const annotations = [];
  for (const g of viewGroups || []) {
    // A primitive entry must be refused explicitly. It does NOT throw on
    // `g.id` — a string or a number just yields undefined — so without this it
    // silently becomes a group annotation with a generated id, an empty label
    // and no members: a phantom box on the canvas, which is worse than the
    // skip, because nothing anywhere reports it.
    if (!g || typeof g !== 'object') {
      skippedAnnotation(g, new Error('group entry is not an object'));
      continue;
    }
    try {
      annotations.push(
        createAnnotation({
          id: g.id,
          type: 'group',
          position: g.position || { x: 0, y: 0 },
          label: g.label || '',
          description: g.description || '',
          color: g.color,
          size: g.style ? { w: g.style.width, h: g.style.height } : undefined,
          member_node_ids: membersByGroup[g.id] || [],
          z: g.z ?? 0,
          locked: Boolean(g.locked),
        })
      );
    } catch (error) {
      skippedAnnotation(g, error);
    }
  }
  return annotations;
}

// The rest of the v1 annotation model (docs/ANNOTATION_CONTRACT.md) beyond
// note/label/line: text, shape, icon, vote_dot, image. Color lives
// under style.color (like label), and geometry.w/h lives in `geometry` for
// all five of these types — createAnnotation has no dedicated `size` payload
// field the way it does for note. `geometry.rotation` and `geometry.w/h` are
// both carried on every overlay of these kinds (not only the kinds the
// canvas currently renders as resizable) for the same reason as z/locked: a
// translator that dropped them would make the browser's next autosave diff
// the annotation back to rotation 0 / createAnnotation's 160x96 default,
// silently undoing what an agent or a collaborator had just set.
// `text` and `shape` additionally carry `style.fontSize`/`style.font`/
// `style.textAlign` (task-annotation-text-alignment-and-font) — the same
// reasoning applies: leaving any one of them out of either direction below
// would make the next autosave diff it back to its default, silently
// discarding a typography choice an agent or collaborator had just set (the
// "unsized-geometry clobber" class of bug this task's own node warns about).
// `shape` additionally carries `style.fill`/`style.border`
// (task-annotation-merge-frame-into-shape-rectangle) instead of `style.color`
// — each independently a colour or `'transparent'`, the setting that
// subsumes what the retired `frame` kind was.
const GENERIC_OVERLAY_TYPES = new Set(['text', 'shape', 'icon', 'vote_dot', 'image']);

function genericAnnotationToOverlay(a) {
  const overlay = {
    id: a.id,
    kind: a.type,
    position: a.position || { x: 0, y: 0 },
    color: a.style?.color,
    z: a.z ?? 0,
    locked: Boolean(a.locked),
    rotation: a.geometry?.rotation ?? 0,
    size: { w: a.geometry?.w ?? 0, h: a.geometry?.h ?? 0 },
  };
  if (a.type === 'text') {
    overlay.text = a.text || '';
    overlay.fontSize = a.style?.fontSize;
    // `font`/`textAlign` (task-annotation-text-alignment-and-font) live
    // under `style` alongside `fontSize`, not `content` — the same
    // convention `fontSize` already established for this kind, and the
    // `style` argument's own documented home for typography
    // (backend/service/mcp_tools.py's create_annotation/update_annotation
    // docstrings).
    overlay.font = a.style?.font;
    overlay.textAlign = a.style?.textAlign;
    overlay.attachment = a.attachment;
  } else if (a.type === 'shape') {
    overlay.shape = a.shape || 'rectangle';
    // Optional caption (task-annotation-doubleclick-to-edit-text) — same
    // empty-string default as every other kind's `text` field.
    overlay.text = a.text || '';
    // Caption typography (task-annotation-text-alignment-and-font) — new on
    // `shape`, same `style`-nested convention as `text`'s own fontSize above.
    overlay.fontSize = a.style?.fontSize;
    overlay.font = a.style?.font;
    overlay.textAlign = a.style?.textAlign;
    // Independent fill/border (task-annotation-merge-frame-into-shape-
    // rectangle) — `shape` no longer reads the generic `overlay.color` this
    // function sets above (that field survives only because every other
    // generic kind still uses it); the canvas package's GENERIC_OVERLAY_FIELDS
    // for `shape` no longer lists `color`, so an agent-set `style.color` on a
    // shape is simply not projected onto the live node, matching `frame`'s own
    // retirement rather than silently resurrecting it under a new name.
    overlay.fill = a.style?.fill;
    overlay.border = a.style?.border;
  } else if (a.type === 'icon') {
    overlay.icon = a.icon || 'circle';
    overlay.attachment = a.attachment;
  } else if (a.type === 'image') {
    overlay.image = a.image || {};
    overlay.alt = a.alt || '';
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
  // `text` and `shape` (task-annotation-text-alignment-and-font) both carry
  // fontSize/font/textAlign under `style`, mirroring `text`'s pre-existing
  // fontSize convention rather than the plain `{color}` every other generic
  // kind gets. `shape` carries `fill`/`border` instead of `color`
  // (task-annotation-merge-frame-into-shape-rectangle) — see
  // genericAnnotationToOverlay's comment on the same fields.
  input.style =
    o.kind === 'shape'
      ? {
          fill: o.fill,
          border: o.border,
          fontSize: o.fontSize,
          font: o.font,
          textAlign: o.textAlign,
        }
      : o.kind === 'text'
        ? { color: o.color, fontSize: o.fontSize, font: o.font, textAlign: o.textAlign }
        : { color: o.color };
  if (o.kind === 'text') {
    input.text = o.text || '';
    input.attachment = o.attachment;
  } else if (o.kind === 'shape') {
    input.shape = o.shape || 'rectangle';
    input.text = o.text || '';
  } else if (o.kind === 'icon') {
    input.icon = o.icon || 'circle';
    input.attachment = o.attachment;
  } else if (o.kind === 'image') {
    input.image = o.image || {};
    input.alt = o.alt || '';
  }
  if (o.size) input.size = o.size;
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
    opacity: a.style?.opacity,
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
    style: { color: o.color, opacity: o.opacity },
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
      // `start`/`end` (docs/ANNOTATION_CONTRACT.md's line-endpoint attachment,
      // distinct from the GUI-only startAnchor/endAnchor snap above) always
      // come back from createAnnotation as at least `{point}` — never
      // undefined — so forwarding them unconditionally would put a `start`/
      // `end` field on every arrow overlay, including ones nobody ever
      // attached, and would make that stored point rather than
      // `from`/`to`/`dx`/`dy` (the fields the canvas actually drags) look
      // authoritative. The attachment itself is the agent-authored state this
      // translator must not lose, so it is carried only when present.
      if (a.start?.attachment) overlay.start = a.start;
      if (a.end?.attachment) overlay.end = a.end;
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
  const annotations = [];
  for (const o of overlays || []) {
    try {
      if (!o || typeof o !== 'object') {
        throw new Error('overlay entry is not an object');
      }
      if (o.kind === 'note') {
        annotations.push(
          createAnnotation({
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
          })
        );
        continue;
      }
      if (o.kind === 'label') {
        annotations.push(
          createAnnotation({
            id: o.id,
            type: 'label',
            position: o.position || { x: 0, y: 0 },
            text: o.text || '',
            style: { color: o.color, fontSize: o.fontSize },
            attachment: o.attachment,
            z: o.z ?? 0,
            locked: Boolean(o.locked),
            rotation: o.rotation ?? 0,
          })
        );
        continue;
      }
      if (o.kind === 'freehand') {
        annotations.push(freehandOverlayToAnnotation(o));
        continue;
      }
      if (GENERIC_OVERLAY_TYPES.has(o.kind)) {
        annotations.push(genericOverlayToAnnotation(o));
        continue;
      }
      if (o.kind !== 'arrow') {
        throw new Error(`Unsupported overlay kind: ${o.kind || '<missing>'}`);
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
      // Carry an attached endpoint's `start`/`end` back onto the annotation
      // (see the matching comment in annotationsToOverlays above) so the
      // attachment an agent set survives this leg too, instead of being
      // rebuilt as a bare point by annotationModel.js's normalizeEndpoint.
      if (o.start) ann.start = o.start;
      if (o.end) ann.end = o.end;
      annotations.push(createAnnotation(ann));
    } catch (error) {
      skippedOverlay(o, error);
    }
  }
  return annotations;
}
