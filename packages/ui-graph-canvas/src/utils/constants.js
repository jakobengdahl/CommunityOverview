/**
 * Graph canvas constants
 */

import { MarkerType } from 'reactflow';

// Node type color mapping from metamodel
// Null prototype: node type names come from profile config, and a plain object
// literal would resolve "toString" or "constructor" to an inherited member and
// return it as a color.
export const NODE_COLORS = Object.assign(Object.create(null), {
  Actor: '#3B82F6',
  Initiative: '#10B981',
  Capability: '#F97316',
  Resource: '#FBBF24',
  Legislation: '#EF4444',
  Theme: '#14B8A6',
  Goal: '#6366F1',
  Event: '#D946EF',
  Data: '#06B6D4',
  Risk: '#DC2626',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
  VisualizationView: '#6B7280', // Legacy support
});

// Default edge styling
export const DEFAULT_EDGE_STYLE = {
  stroke: '#666',
  strokeWidth: 2,
};

// Clamp bounds for the per-edge `thickness` attribute so a stray value can't
// render an invisible or canvas-swallowing line.
export const EDGE_MIN_THICKNESS = 1;
export const EDGE_MAX_THICKNESS = 12;

// Directions in which an arrowhead is drawn. 'forward' points at the target,
// 'backward' at the source, 'both' at each end, 'none' draws a plain line.
const FORWARD_DIRECTIONS = new Set(['forward', 'both']);
const BACKWARD_DIRECTIONS = new Set(['backward', 'both']);

/**
 * Translate an edge's free-form `metadata` into React Flow visual props.
 *
 * All attributes are optional; when absent the returned props reproduce the
 * historical default look exactly (grey #666, width 2, no arrowheads, static).
 * This keeps existing graphs visually unchanged while letting edges that do
 * carry visual metadata opt into direction, colour, arrow style, thickness and
 * a pulse/static animation state.
 *
 * Recognised metadata keys:
 *   - color:     CSS colour string for the stroke (and arrowheads)
 *   - thickness: stroke width in px, clamped to [EDGE_MIN, EDGE_MAX]
 *   - direction: 'forward' | 'backward' | 'both' | 'none'
 *   - arrow:     'closed' (filled) | 'open' (line) arrowhead style
 *   - animated / pulse: truthy => animated ("pulse") edge, else static
 */
export function resolveEdgeVisuals(metadata) {
  const meta = metadata && typeof metadata === 'object' ? metadata : {};

  const stroke =
    typeof meta.color === 'string' && meta.color.trim()
      ? meta.color.trim()
      : DEFAULT_EDGE_STYLE.stroke;

  let strokeWidth = DEFAULT_EDGE_STYLE.strokeWidth;
  const thickness = Number(meta.thickness);
  if (Number.isFinite(thickness) && thickness > 0) {
    strokeWidth = Math.min(EDGE_MAX_THICKNESS, Math.max(EDGE_MIN_THICKNESS, thickness));
  }

  const direction =
    typeof meta.direction === 'string' ? meta.direction.trim().toLowerCase() : 'none';
  const marker = {
    type: meta.arrow === 'open' ? MarkerType.Arrow : MarkerType.ArrowClosed,
    color: stroke,
  };

  const animated = meta.animated === true || meta.pulse === true;

  return {
    style: { stroke, strokeWidth },
    markerStart: BACKWARD_DIRECTIONS.has(direction) ? marker : undefined,
    markerEnd: FORWARD_DIRECTIONS.has(direction) ? marker : undefined,
    animated,
    // Kept representable for the sibling external pulse-trigger task; the class
    // adds a subtle stroke pulse on top of React Flow's native dash animation.
    className: animated ? 'rf-edge-pulse' : undefined,
  };
}

// Lazy loading thresholds
export const LAZY_LOAD_THRESHOLD = 200;
export const INITIAL_LOAD_COUNT = 100;

// Node dimensions
export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 100;

// Get color for a node type (with fallback)
export function getNodeColor(nodeType) {
  return NODE_COLORS[nodeType] || '#9CA3AF';
}
