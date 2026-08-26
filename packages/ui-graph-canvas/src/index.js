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
export { default as AnnotationToolbox } from './components/AnnotationToolbox';

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

// Canvas-facing annotation type set (ReactFlow node `type` values, e.g.
// 'arrow') — distinct from ANNOTATION_TYPES above, which lists the
// server-side document-model kind names (e.g. 'line') and is not the set to
// check a ReactFlow node's `type` against. Exposed for the host app to
// extend selection claims and realtime publish timing to annotation nodes
// (task-annotation-shared-session-realtime).
export { ANNOTATION_TYPES as CANVAS_ANNOTATION_TYPES } from './utils/annotations';

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
