// Shared node type color mapping from the public metamodel.
//
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
  Dataset: '#06B6D4',
  Risk: '#DC2626',
  ActiveKnowledgeCollection: '#F59E0B',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
  VisualizationView: '#6B7280', // Legacy support
  Group: '#646cff',
});

export const DEFAULT_NODE_COLOR = '#9CA3AF';

// Get color for a node type (with fallback).
export function getNodeColor(nodeType) {
  return NODE_COLORS[nodeType] || DEFAULT_NODE_COLOR;
}
