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
export { default as NoteNode } from './components/NoteNode';
export { default as LabelNode } from './components/LabelNode';
export { default as ArrowNode } from './components/ArrowNode';
export { default as GenericAnnotationNode } from './components/GenericAnnotationNode';
export { default as FreehandAnnotationNode } from './components/FreehandAnnotationNode';

// Layout utilities
export {
  getLayoutedElements,
  getCircularLayout,
  getGridLayout,
  chooseLayout,
  applyLayout,
  positionNewNodes,
} from './utils/graphLayout';

// Annotation model utilities
export {
  ANNOTATION_SCHEMA_VERSION,
  ANNOTATION_TYPES,
  applyAnnotationOperation,
  createAnnotation,
  createAnnotationDocument,
  migrateLegacyAnnotations,
  normalizeAnnotationDocument,
} from './utils/annotationModel';

// Freehand annotation utilities
export { reduceFreehandPoints, pointsToPathData, buildFreehandPath } from './utils/freehandPath';
export { createFreehandStrokeCapture } from './utils/freehandStroke';

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
