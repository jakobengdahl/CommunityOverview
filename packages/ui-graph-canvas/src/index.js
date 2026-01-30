/**
 * ui-graph-canvas - React components for graph visualization
 *
 * Main component: GraphCanvas
 * Supporting components: CustomNode, GroupNode
 * Utilities: Layout algorithms, constants
 */

// Main component
export { default as GraphCanvas } from './components/GraphCanvas';

// Individual components (for customization)
export { default as CustomNode } from './components/CustomNode';
export { default as GroupNode } from './components/GroupNode';

// Layout utilities
export {
  getLayoutedElements,
  getCircularLayout,
  getGridLayout,
  chooseLayout,
  applyLayout,
} from './utils/graphLayout';

// Constants
export {
  NODE_COLORS,
  DEFAULT_EDGE_STYLE,
  LAZY_LOAD_THRESHOLD,
  INITIAL_LOAD_COUNT,
  NODE_WIDTH,
  NODE_HEIGHT,
  getNodeColor,
} from './utils/constants';
