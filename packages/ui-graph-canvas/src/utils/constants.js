/**
 * Graph canvas constants
 */

// Node type color mapping from metamodel
export const NODE_COLORS = {
  Actor: '#3B82F6',
  Initiative: '#10B981',
  Capability: '#F97316',
  Resource: '#FBBF24',
  Legislation: '#EF4444',
  Theme: '#14B8A6',
  Goal: '#6366F1',
  Event: '#D946EF',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
  VisualizationView: '#6B7280', // Legacy support
};

// Default edge styling
export const DEFAULT_EDGE_STYLE = {
  stroke: '#666',
  strokeWidth: 2,
};

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
