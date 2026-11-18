// Client side of the node shape contract (docs/design/NODE_SHAPES.md),
// mirroring backend/config/node_shapes.py so the canvas never renders a
// definition the server would reject.
import { createNodeShapeGeometry } from './nodeShapeGeometry';

const TRIANGLE_PLACEMENTS = ['centre', 'bottom'];
const RECTANGLE_PLACEMENTS = ['top', 'middle', 'bottom'].flatMap((v) =>
  ['left', 'centre', 'right'].map((h) => `${v}-${h}`)
);
const AREAS = ['first', 'second', 'third'];
const SELECTION_REF = /^(config|saved):([a-z][a-z0-9-]{0,63})$/;

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validDefinition(definition) {
  if (!isPlainObject(definition) || definition.version !== 1) return null;
  const { base, scale, placement, areas } = definition;
  const triangle = base === 'triangle-up' || base === 'triangle-down';
  if (!triangle && base !== 'rectangle' && base !== 'rectangle-flat') return null;
  if (typeof scale !== 'number' || !Number.isFinite(scale) || scale < 0.1 || scale > 10) {
    return null;
  }
  if (!(triangle ? TRIANGLE_PLACEMENTS : RECTANGLE_PLACEMENTS).includes(placement)) return null;
  if (!isPlainObject(areas) || Object.keys(areas).length !== 3) return null;
  const bindingOk = (b) => b === null || (typeof b === 'string' && b.length >= 1 && b.length <= 64);
  if (!AREAS.every((key) => Object.hasOwn(areas, key) && bindingOk(areas[key]))) return null;
  return definition;
}

/**
 * Build `(node) => {definition, fields} | null` resolving one whole selection:
 * node override → session type default → config type default → built-in card.
 * A selection that cannot be resolved (saved shapes are not loaded here, or it
 * is invalid) yields the built-in card instead of a lower-priority shape.
 */
export function createNodeShapeResolver({
  nodeTypes = {},
  nodeShapes = {},
  shapeOverrides = {},
  typeShapes = {},
} = {}) {
  return (node) => {
    let selection;
    if (Object.hasOwn(shapeOverrides || {}, node.id)) selection = shapeOverrides[node.id];
    else if (Object.hasOwn(typeShapes || {}, node.type)) selection = typeShapes[node.type];
    else {
      const name = Object.hasOwn(nodeTypes || {}, node.type) ? nodeTypes[node.type]?.shape : null;
      if (typeof name !== 'string') return null;
      selection = { ref: `config:${name}` };
    }
    if (!isPlainObject(selection) || Object.keys(selection).length !== 1) return null;
    let definition = null;
    if (Object.hasOwn(selection, 'inline')) definition = selection.inline;
    else if (typeof selection.ref === 'string') {
      const match = SELECTION_REF.exec(selection.ref);
      if (match?.[1] === 'config' && Object.hasOwn(nodeShapes || {}, match[2])) {
        definition = nodeShapes[match[2]];
      }
    }
    definition = validDefinition(definition);
    if (!definition) return null;
    const fields = nodeTypes?.[node.type]?.fields;
    return { definition, fields: Array.isArray(fields) ? fields : [] };
  };
}

const BUILTIN_FIELDS = ['type', 'name', 'summary'];

/**
 * Area values from loaded node data: built-in or declared scalar fields only.
 * Declared fields are stored in metadata, with a top-level value as fallback.
 */
export function nodeShapeAreaValues(definition, node, fields = []) {
  const declared = new Set(fields.filter((f) => f !== 'metadata' && !BUILTIN_FIELDS.includes(f)));
  const metadata = isPlainObject(node.metadata) ? node.metadata : {};
  const values = {};
  for (const area of AREAS) {
    const binding = definition.areas[area];
    let value = null;
    if (BUILTIN_FIELDS.includes(binding)) value = node[binding];
    else if (declared.has(binding)) {
      value = (Object.hasOwn(metadata, binding) ? metadata[binding] : null) ?? node[binding];
    }
    values[area] = ['string', 'number', 'boolean'].includes(typeof value) ? String(value) : '';
  }
  return values;
}

/**
 * Type and name, then the mapped values in area order. A value bound to type
 * or name is already spoken first, so it is not repeated; None and empty
 * values contribute nothing. Badge texts follow, since the label replaces the
 * card content that would otherwise announce them.
 */
export function nodeShapeAccessibleName(definition, node, values, extras = []) {
  const parts = [node.type, node.name];
  for (const area of AREAS) {
    const binding = definition.areas[area];
    if (binding !== 'type' && binding !== 'name') parts.push(values[area]);
  }
  return [...parts, ...extras].filter((part) => typeof part === 'string' && part.trim()).join(', ');
}

const geometryCache = new Map();

/** Geometry is immutable, so one descriptor serves every node of a base/scale. */
export function nodeShapeGeometry(base, scale) {
  const key = `${base}:${scale}`;
  if (!geometryCache.has(key)) geometryCache.set(key, createNodeShapeGeometry(base, scale));
  return geometryCache.get(key);
}

function luminance(hex) {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex || '');
  if (!match) return null;
  const digits = match[1].length === 3 ? [...match[1]].map((c) => c + c) : match[1].match(/../g);
  const [r, g, b] = digits.map((d) => {
    const c = parseInt(d, 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export const BAND_TEXT_LIGHT = '#ffffff';
export const BAND_TEXT_DARK = '#000000';

/**
 * Band text colour with at least 4.5:1 contrast against the band. A colour
 * that cannot be parsed gets an opaque backing in the card tokens instead.
 */
export function bandTextStyle(color) {
  const band = luminance(color);
  if (band === null) {
    return {
      color: 'var(--graph-card-text-primary)',
      background: 'var(--graph-card-surface)',
    };
  }
  const contrast = (text) => (Math.max(band, text) + 0.05) / (Math.min(band, text) + 0.05);
  if (contrast(1) >= 4.5) return { color: BAND_TEXT_LIGHT };
  if (contrast(luminance(BAND_TEXT_DARK)) >= 4.5) return { color: BAND_TEXT_DARK };
  return { color: 'var(--graph-card-text-primary)', background: 'var(--graph-card-surface)' };
}

// Triangle text stacks as fractions of the body, chosen so every corner stays
// inside the polygon; a downward triangle's bottom stops well above its tip.
const TRIANGLE_BOXES = {
  'triangle-up': { centre: [0.3, 0.45, 0.4, 0.35], bottom: [0.22, 0.62, 0.56, 0.3] },
  'triangle-down': { centre: [0.28, 0.2, 0.44, 0.3], bottom: [0.34, 0.42, 0.32, 0.22] },
};

/** The body-local box that holds the second and third areas. */
export function nodeShapeTextBox(geometry, placement) {
  const { width: w, height: h, bandHeight: band, padding: pad } = geometry;
  const fractions = TRIANGLE_BOXES[geometry.base];
  if (fractions) {
    const [x, y, width, height] = fractions[placement];
    return { x: x * w, y: y * h, width: width * w, height: height * h };
  }
  return { x: pad, y: band + pad, width: w - 2 * pad, height: h - band - 2 * pad };
}

/**
 * Action buttons stacked in a column at the trailing edge of the text box,
 * and the inset that keeps the text clear of that column. The text box lies
 * inside the convex body, so every button does too, at any base and scale.
 */
export function nodeShapeActionLayout(geometry, placement, count) {
  if (!count) return { buttons: [], inset: 0 };
  const box = nodeShapeTextBox(geometry, placement);
  const size = Math.min(24, box.height / (count + (count - 1) / 6), box.width / 3);
  const gap = size / 6;
  const top = box.y + (box.height - count * size - (count - 1) * gap) / 2;
  const x = box.x + box.width - size;
  return {
    buttons: Array.from({ length: count }, (_, i) => ({
      x,
      y: top + i * (size + gap),
      size,
    })),
    inset: size + gap,
  };
}

/** The coloured band:the silhouette's intersection with its top band height. */
export function nodeShapeBandPath(geometry) {
  const { width: w, height: h, bandHeight: band, radius: r, base } = geometry;
  if (base === 'triangle-up') {
    const half = ((w / 2) * band) / h;
    return `M ${w / 2} 0 L ${w / 2 + half} ${band} L ${w / 2 - half} ${band} Z`;
  }
  if (base === 'triangle-down') {
    const inset = ((w / 2) * band) / h;
    return `M 0 0 L ${w} 0 L ${w - inset} ${band} L ${inset} ${band} Z`;
  }
  return `M 0 ${band} V ${r} A ${r} ${r} 0 0 1 ${r} 0 H ${w - r} A ${r} ${r} 0 0 1 ${w} ${r} V ${band} Z`;
}
