export const ANNOTATION_SCHEMA_VERSION = 1;

export const ANNOTATION_TYPES = Object.freeze([
  'note',
  'text',
  'label',
  'line',
  'frame',
  'group',
  'shape',
  'icon',
  'vote_dot',
  'image',
  'freehand',
]);

const TYPE_SET = new Set(ANNOTATION_TYPES);
const LEGACY_KIND_ALIASES = Object.freeze({ arrow: 'line' });
const DEFAULT_SIZE = Object.freeze({ w: 160, h: 96 });
const DEFAULT_LINE_DELTA = Object.freeze({ x: 160, y: 0 });
const DEFAULT_FREEHAND_STROKE_WIDTH = 2;

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function finiteNumber(value, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

function clone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function normalizePoint(point, fallback = { x: 0, y: 0 }) {
  const source = isPlainObject(point) ? point : fallback;
  return {
    x: finiteNumber(source.x, fallback.x || 0),
    y: finiteNumber(source.y, fallback.y || 0),
  };
}

function clamp01(value, fallback = 0) {
  return Math.min(1, Math.max(0, finiteNumber(value, fallback)));
}

// A single sampled freehand point: model-space x/y plus optional pressure
// (0-1, from the pointer device) — pressure is omitted rather than defaulted
// to 1 so a renderer/exporter can tell "no pressure data" apart from
// "full pressure".
function normalizeFreehandPoint(point) {
  const source = isPlainObject(point) ? point : {};
  const out = { x: finiteNumber(source.x, 0), y: finiteNumber(source.y, 0) };
  if (Number.isFinite(source.pressure)) out.pressure = clamp01(source.pressure);
  return out;
}

function normalizeSize(size, fallback = DEFAULT_SIZE) {
  const source = isPlainObject(size) ? size : {};
  const width = source.w ?? source.width;
  const height = source.h ?? source.height;
  return {
    w: Math.max(0, finiteNumber(width, fallback.w)),
    h: Math.max(0, finiteNumber(height, fallback.h)),
  };
}

function normalizeGeometry(annotation) {
  const position = normalizePoint(annotation.position || annotation.geometry);
  const size = normalizeSize(annotation.size || annotation.geometry, DEFAULT_SIZE);
  const geometry = isPlainObject(annotation.geometry) ? annotation.geometry : {};
  return {
    x: finiteNumber(geometry.x, position.x),
    y: finiteNumber(geometry.y, position.y),
    w: Math.max(0, finiteNumber(geometry.w ?? geometry.width, size.w)),
    h: Math.max(0, finiteNumber(geometry.h ?? geometry.height, size.h)),
    rotation: finiteNumber(geometry.rotation, finiteNumber(annotation.rotation, 0)),
  };
}

function normalizeEndpoint(endpoint, fallbackPoint) {
  if (!isPlainObject(endpoint)) return { point: normalizePoint(fallbackPoint) };
  const out = {
    point: normalizePoint(endpoint.point || endpoint.position || endpoint, fallbackPoint),
  };
  // `target_id` may be given directly on the endpoint as a shorthand for
  // `attachment: { target_id }`. Route both through normalizeAttachment so a
  // shorthand endpoint gets the same id coercion and anchor/offset support as
  // one written with an explicit `attachment` object.
  const attachment = normalizeAttachment(endpoint.attachment || endpoint);
  if (attachment) out.attachment = attachment;
  return out;
}

function normalizeAttachment(attachment) {
  if (!isPlainObject(attachment)) return undefined;
  const targetId =
    attachment.target_id || attachment.targetId || attachment.node_id || attachment.nodeId;
  if (!targetId) return undefined;
  return {
    target_id: String(targetId),
    target_type: attachment.target_type || attachment.targetType || 'node',
    anchor: attachment.anchor || attachment.side || undefined,
    offset: attachment.offset ? normalizePoint(attachment.offset) : undefined,
  };
}

function normalizeType(annotation) {
  const raw = annotation.type || annotation.kind;
  const type = LEGACY_KIND_ALIASES[raw] || raw;
  if (!TYPE_SET.has(type)) throw new Error(`Unsupported annotation type: ${raw || '<missing>'}`);
  return type;
}

function withTypePayload(annotation, type, geometry) {
  if (type === 'note') {
    return {
      text: annotation.text || '',
      color: annotation.color,
      fontSize: annotation.fontSize,
      size: normalizeSize(annotation.size || geometry),
    };
  }
  if (type === 'text' || type === 'label') {
    return {
      text: annotation.text || annotation.label || '',
      attachment: normalizeAttachment(annotation.attachment || annotation.anchor),
    };
  }
  if (type === 'line') {
    const from = normalizePoint(annotation.from || annotation.position || geometry);
    const to = normalizePoint(annotation.to, {
      x: from.x + finiteNumber(annotation.dx, DEFAULT_LINE_DELTA.x),
      y: from.y + finiteNumber(annotation.dy, DEFAULT_LINE_DELTA.y),
    });
    return {
      from,
      to,
      start: normalizeEndpoint(annotation.start || annotation.startEndpoint, from),
      end: normalizeEndpoint(annotation.end || annotation.endEndpoint, to),
      startArrow: annotation.startArrow ?? false,
      endArrow: annotation.endArrow ?? true,
      startAnchor: annotation.startAnchor,
      endAnchor: annotation.endAnchor,
    };
  }
  if (type === 'group') {
    return {
      label: annotation.label || '',
      description: annotation.description || '',
      color: annotation.color ?? annotation.style?.color,
      size: normalizeSize(annotation.size || geometry),
      member_node_ids: Array.isArray(annotation.member_node_ids)
        ? [...annotation.member_node_ids]
        : [],
    };
  }
  if (type === 'shape') {
    return { shape: annotation.shape || 'rectangle' };
  }
  if (type === 'icon') {
    return {
      icon: annotation.icon || annotation.icon_name || 'circle',
      attachment: normalizeAttachment(annotation.attachment),
    };
  }
  if (type === 'vote_dot') {
    return {
      value: annotation.value ?? null,
      attachment: normalizeAttachment(annotation.attachment),
    };
  }
  if (type === 'image') {
    return {
      image: clone(annotation.image || {}),
      alt: annotation.alt || '',
    };
  }
  if (type === 'freehand') {
    const rawPoints = Array.isArray(annotation.points) ? annotation.points : [];
    const points = rawPoints.map(normalizeFreehandPoint);
    // A stroke always needs at least one point to anchor its position; an
    // empty draw (e.g. a discarded/aborted stroke) falls back to the
    // annotation's own position/geometry instead of producing a pointless
    // annotation with no visible geometry.
    if (points.length === 0) points.push(normalizePoint(annotation.position || geometry));
    return {
      points,
      smoothing: clamp01(annotation.smoothing, 0),
      strokeWidth: Math.max(0.5, finiteNumber(annotation.strokeWidth, DEFAULT_FREEHAND_STROKE_WIDTH)),
      pointerType: annotation.pointerType || undefined,
      pressureSource: annotation.pressureSource || undefined,
    };
  }
  return {};
}

export function createAnnotation(input = {}) {
  const id =
    input.id || `annotation-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const type = normalizeType(input);
  const geometry = normalizeGeometry(input);
  const style = clone(input.style) || {};
  if (input.color !== undefined && style.color === undefined) style.color = input.color;
  const payload = withTypePayload(input, type, geometry);
  const annotation = {
    id: String(id),
    type,
    kind: type,
    geometry,
    position: { x: geometry.x, y: geometry.y },
    style,
    z: finiteNumber(input.z, 0),
    locked: Boolean(input.locked),
    ...payload,
  };
  for (const key of ['created_by', 'updated_by', 'created_at', 'updated_at']) {
    if (input[key] !== undefined) annotation[key] = input[key];
  }
  return annotation;
}

export function createAnnotationDocument(input) {
  if (Array.isArray(input)) return normalizeAnnotationDocument({ annotations: input });
  return normalizeAnnotationDocument(input || {});
}

export function normalizeAnnotationDocument(input = {}) {
  const rawAnnotations = Array.isArray(input) ? input : input.annotations || [];
  return {
    schema_version: ANNOTATION_SCHEMA_VERSION,
    annotations: rawAnnotations.map((annotation) => createAnnotation(annotation)),
  };
}

function replaceAnnotation(doc, nextAnnotation) {
  return {
    ...doc,
    annotations: doc.annotations.map((annotation) =>
      annotation.id === nextAnnotation.id ? nextAnnotation : annotation
    ),
  };
}

function removeAnnotation(doc, id) {
  return { ...doc, annotations: doc.annotations.filter((annotation) => annotation.id !== id) };
}

function requireAnnotation(doc, id) {
  const annotation = doc.annotations.find((item) => item.id === id);
  if (!annotation) throw new Error(`Annotation not found: ${id}`);
  return annotation;
}

function normalizePatch(annotation, patch = {}) {
  return createAnnotation({
    ...annotation,
    ...clone(patch),
    id: annotation.id,
    type: patch.type || annotation.type,
  });
}

export function applyAnnotationOperation(documentInput, operation) {
  const doc = normalizeAnnotationDocument(documentInput);
  if (!isPlainObject(operation) || !operation.type)
    throw new Error('Annotation operation type is required');
  if (operation.type === 'create') {
    const annotation = createAnnotation(operation.annotation || operation.value || {});
    if (doc.annotations.some((item) => item.id === annotation.id)) {
      throw new Error(`Annotation already exists: ${annotation.id}`);
    }
    return {
      document: { ...doc, annotations: [...doc.annotations, annotation] },
      inverse: { type: 'delete', id: annotation.id, previous: annotation },
    };
  }
  if (operation.type === 'update') {
    const previous = requireAnnotation(doc, operation.id);
    const next = normalizePatch(previous, operation.patch || {});
    return {
      document: replaceAnnotation(doc, next),
      inverse: { type: 'update', id: previous.id, patch: previous },
    };
  }
  if (operation.type === 'transform') {
    const previous = requireAnnotation(doc, operation.id);
    const geometry = { ...previous.geometry, ...clone(operation.geometry || {}) };
    const next = normalizePatch(previous, { geometry, position: { x: geometry.x, y: geometry.y } });
    return {
      document: replaceAnnotation(doc, next),
      inverse: { type: 'transform', id: previous.id, geometry: previous.geometry },
    };
  }
  if (operation.type === 'reorder') {
    const previous = requireAnnotation(doc, operation.id);
    const next = normalizePatch(previous, { z: finiteNumber(operation.z, previous.z) });
    return {
      document: replaceAnnotation(doc, next),
      inverse: { type: 'reorder', id: previous.id, z: previous.z },
    };
  }
  if (operation.type === 'lock') {
    const previous = requireAnnotation(doc, operation.id);
    const next = normalizePatch(previous, { locked: Boolean(operation.locked) });
    return {
      document: replaceAnnotation(doc, next),
      inverse: { type: 'lock', id: previous.id, locked: previous.locked },
    };
  }
  if (operation.type === 'duplicate') {
    const previous = requireAnnotation(doc, operation.id);
    const duplicate = createAnnotation({
      ...previous,
      id: operation.new_id || `${previous.id}-copy`,
    });
    return {
      document: { ...doc, annotations: [...doc.annotations, duplicate] },
      inverse: { type: 'delete', id: duplicate.id, previous: duplicate },
    };
  }
  if (operation.type === 'delete') {
    const previous = requireAnnotation(doc, operation.id);
    return {
      document: removeAnnotation(doc, operation.id),
      inverse: { type: 'create', annotation: previous },
    };
  }
  if (operation.type === 'clear') {
    return {
      document: { ...doc, annotations: [] },
      inverse: { type: 'restore', annotations: doc.annotations },
    };
  }
  if (operation.type === 'restore') {
    return {
      document: normalizeAnnotationDocument({ annotations: operation.annotations || [] }),
      inverse: { type: 'clear' },
    };
  }
  throw new Error(`Unsupported annotation operation: ${operation.type}`);
}

export function migrateLegacyAnnotations(input) {
  return normalizeAnnotationDocument(input).annotations;
}
