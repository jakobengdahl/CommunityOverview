import { useCallback, useMemo, useEffect, useState, useRef } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  useReactFlow,
  ReactFlowProvider,
  useOnSelectionChange,
  SelectionMode,
} from 'reactflow';
import 'reactflow/dist/style.css';

import CustomNode from './CustomNode';
import GroupNode from './GroupNode';
import NoteNode from './NoteNode';
import LabelNode from './LabelNode';
import ArrowNode from './ArrowNode';
import GenericAnnotationNode from './GenericAnnotationNode';
import AnnotationToolbox from './AnnotationToolbox';
import FreehandAnnotationNode from './FreehandAnnotationNode';
import { AnnotationContext } from './AnnotationContext';
import SimpleFloatingEdge from './SimpleFloatingEdge';
import {
  NodeContextMenu,
  MultiNodeContextMenu,
  EdgeContextMenu,
  PaneContextMenu,
} from './ContextMenus';
import { useRemotePositions } from '../hooks/useRemotePositions';
import { useAnimatedLayout } from '../hooks/useAnimatedLayout';
import { useCanvasHistory } from '../hooks/useCanvasHistory';
import {
  applyLayout,
  getCircularLayout,
  reconcileSessionNodes,
  arrangeNodes,
} from '../utils/graphLayout';
import {
  getNodeColor,
  LAZY_LOAD_THRESHOLD,
  INITIAL_LOAD_COUNT,
  resolveEdgeVisuals,
} from '../utils/constants';
import {
  OVERLAY_TYPES,
  ANNOTATION_TYPES,
  ATTACHABLE_OVERLAY_KINDS,
  isManualNode,
  isArrowHeld,
  isAnnotationDraggable,
  isRemoteLocked,
  overlayToFlowNode,
  flowNodeToOverlay,
  nodeCenter,
  resolveAnchoredArrow,
  computeDroppedAttachment,
  resolveAttachedPosition,
  ICON_INTRINSIC_SIZE,
  VOTE_DOT_INTRINSIC_SIZE,
} from '../utils/annotations';
import { DEFAULT_ANNOTATION_ICON } from '../utils/annotationIcons';
import {
  directNeighborIds,
  neighborStartPositions,
  neighborDragPositions,
} from '../utils/dragConnected';
import { createLongPressDetector } from '../utils/longPress';
import { createFreehandStrokeCapture } from '../utils/freehandStroke';
import { pointsToPathData } from '../utils/freehandPath';

// Phone-sized viewport, matching the host app's mobile breakpoint.
const COMPACT_MEDIA_QUERY = '(max-width: 768px)';
const DEFAULT_FIT_PADDING = 0.2;
const COMPACT_FIT_PADDING = 0.05;
// Flow-coordinate anchor for the focus-view ego layout. Arbitrary but fixed:
// the view is fitted to its contents immediately afterwards.
const FOCUS_CENTER = { x: 0, y: 0 };
const FOCUS_MIN_RADIUS = 260;
const FOCUS_RADIUS_PER_NEIGHBOUR = 55;
// Defaults for a freehand stroke drawn via the toolbox's drawing mode; all
// four are editable afterwards through FreehandAnnotationNode's right-click
// property editor (docs/ANNOTATION_CONTRACT.md's freehand row).
const DEFAULT_FREEHAND_COLOR = '#e6edf3';
const DEFAULT_FREEHAND_STROKE_WIDTH = 2;
const DEFAULT_FREEHAND_SMOOTHING = 0.3;
const DEFAULT_FREEHAND_OPACITY = 1;
// A stroke shorter than this many sampled points is a stray tap, not a
// deliberate drawing gesture — mirrors createFreehandStrokeCapture's own
// `minPoints` default, made explicit here since GraphCanvas passes it in.
const FREEHAND_MIN_POINTS = 2;
import './GraphCanvas.css';

/**
 * Ensure parent (group) nodes appear before their children in the array.
 * ReactFlow requires this ordering for parent-child relationships to work.
 * Groups are placed first so they render behind regular nodes in the DOM,
 * allowing clicks to reach the custom nodes on top.
 */
function reorderNodesForParentChild(nodes) {
  const groups = [];
  const nonGroupWithoutParent = [];
  const withParent = [];

  for (const n of nodes) {
    if (n.parentId) {
      withParent.push(n);
    } else if (n.type === 'group') {
      groups.push(n);
    } else {
      nonGroupWithoutParent.push(n);
    }
  }

  return [...groups, ...nonGroupWithoutParent, ...withParent];
}

/**
 * Compute a dragged graph node's post-drag placement: its final parentId and
 * position, accounting for entering a group (position becomes parent-relative),
 * leaving a group (position becomes absolute), or neither. `currentNodes` is the
 * live ReactFlow node list and `groupNodes` its group nodes. Returns
 * { parentId, position:{x,y} }. Pure, so both the on-screen re-parent and the
 * recorded undo entry derive the same final placement from one source.
 */
function computeGroupPlacement(node, currentNodes, groupNodes) {
  const flowNode = currentNodes.find((cn) => cn.id === node.id);
  const pos = flowNode?.position || node.position;

  const absPos = node.parentId
    ? {
        x: pos.x + (groupNodes.find((g) => g.id === node.parentId)?.position.x || 0),
        y: pos.y + (groupNodes.find((g) => g.id === node.parentId)?.position.y || 0),
      }
    : pos;

  let targetGroup = null;
  for (const g of groupNodes) {
    const gb = {
      left: g.position.x,
      right: g.position.x + (g.style?.width || 300),
      top: g.position.y,
      bottom: g.position.y + (g.style?.height || 200),
    };
    if (
      absPos.x >= gb.left &&
      absPos.x <= gb.right &&
      absPos.y >= gb.top &&
      absPos.y <= gb.bottom
    ) {
      targetGroup = g;
      break;
    }
  }

  if (targetGroup && node.parentId !== targetGroup.id) {
    // Enter group: position becomes relative to the group origin.
    return {
      parentId: targetGroup.id,
      position: { x: absPos.x - targetGroup.position.x, y: absPos.y - targetGroup.position.y },
    };
  }

  if (!targetGroup && node.parentId) {
    // Exit group: position becomes absolute again.
    const oldParent = groupNodes.find((gn) => gn.id === node.parentId);
    return {
      parentId: undefined,
      position: {
        x: pos.x + (oldParent?.position.x || 0),
        y: pos.y + (oldParent?.position.y || 0),
      },
    };
  }

  // No membership change: keep the current parent and the just-dragged position.
  return { parentId: node.parentId, position: { x: pos.x, y: pos.y } };
}

/**
 * GraphCanvas - Main graph visualization component
 */
function GraphCanvasInner({
  nodes: inputNodes = [],
  edges: inputEdges = [],
  highlightedNodeIds = [],
  hiddenNodeIds = [],
  hiddenEdgeIds = [],
  nodeMarks = {},
  pulsedNodeIds = {},
  clearGroupsFlag = false,
  onExpand,
  onEdit,
  onViewNodeHistory,
  onDelete,
  onHide,
  onDeleteMultiple,
  onHideMultiple,
  onHideEdge,
  onDeleteEdge,
  onEditEdge,
  onSetEdgeType,
  onConnect: onConnectCallback,
  onCreateGroup,
  onSaveView,
  onNodePositionChange,
  layoutType = null,
  onCreateSubscription,
  onCreateAgent,
  onDropCreateNode,
  // Called with (dataUrl, position) when a human pastes an image from the
  // clipboard, drops an image file on the canvas, or uses the annotation
  // toolbox's image item to pick a file. Unlike createAnnotation (which adds
  // a node straight to local state), image creation is a server round-trip —
  // the host validates/optimizes/embeds it and this canvas only ever renders
  // the annotation once it comes back over the session's realtime channel
  // (see remoteAnnotationOps), never a client-side guess of the result.
  onImageIngest,
  onShowOnly,
  onSelectionChange,
  onNodeDoubleClick: onNodeDoubleClickCallback,
  focusNodeId = null,
  onFocusComplete,
  createGroupSignal = 0,
  saveViewSignal = 0,
  closeMenusSignal = 0,
  groupsToRestore = null,
  onGroupsRestored,
  annotationsToRestore = null,
  onAnnotationsRestored,
  onAnnotationChange,
  remotePositions = null,
  onRemotePositionsApplied,
  animatedLayout = null,
  onAnimatedLayoutApplied,
  animatedLayoutResetKey = null,
  // Bumped by the host whenever the canvas contents are replaced wholesale (a
  // saved view loaded into the running session, an agent replace/load, the
  // clear-canvas action), establishing a new position baseline. Distinct from
  // the incremental position writes of `remotePositions` and `animatedLayout`,
  // which move nodes within the *same* contents and deliberately leave the
  // undo history intact (last-write-wins, MULTI_USER_SESSIONS_DESIGN D2).
  canvasBaselineEpoch = null,
  agentArrangingLabel = 'Assistant is arranging the view…',
  remoteAnnotationOps = null,
  onRemoteAnnotationsApplied,
  remoteSelections = null,
  federationDepth = 1,
  onFederationDepthChange,
  maxFederationDepth = 4,
  federationDepthLevels = null,
  federationDepthLabel = 'Depth',
  federationDepthTooltip = 'Depth levels are defined by installation configuration',
  lazyLoadShowingLabel = 'Showing {loaded} of {total} nodes',
  lazyLoadMoreLabel = 'Load More',
  lazyLoadHiddenConnectionsLabel = '{count} connections to nodes not yet loaded are hidden — Load More to reveal them',
  showMinimap = false,
  schema = null,
  onContextMenuAction = null,
  nodeColorResolver = null,
  onViewportChange = null,
  // Identity of the active visualization session. Changing it signals a session
  // switch: the canvas discards the previous session's live node positions (so a
  // node shared between sessions is not left at the old coordinates) and refits
  // the view to the newly loaded content instead of lingering on the old camera.
  sessionKey = null,
  nodePreviewEnabled = true,
  contextMenuLabels = {},
  annotationToolboxLabels = {},
  // 'auto' detects a coarse (touch) pointer itself via matchMedia — this
  // package has no access to the host app's viewport-mode hook, so
  // detection must be self-contained. 'on'/'off' force the mode (mainly for
  // hosts that already know the input type, and for tests).
  touchMode = 'auto',
  // 'auto' detects a phone-sized viewport itself via matchMedia, mirroring
  // touchMode above. Width and pointer type are independent signals: a tablet
  // is coarse-pointer but roomy, a narrow desktop window is fine-pointer but
  // cramped, and the canvas chrome is a width problem.
  compactMode = 'auto',
  compactZoomInLabel = 'Zoom in',
  compactZoomOutLabel = 'Zoom out',
  compactFitViewLabel = 'Fit whole graph',
  focusViewLabel = 'Focus on selected node',
  exitFocusViewLabel = 'Back to whole graph',
  compactControlsLabel = 'Canvas controls',
}) {
  const cml = {
    edit: 'Edit',
    hide: 'Hide',
    expand: 'Find related nodes',
    delete: 'Delete',
    nodesSelected: '{count} nodes selected',
    showOnly: 'Show only these',
    selectSameType: 'Select all nodes of the same type',
    selectRelated: 'Select related nodes',
    viewHistory: 'View change history',
    organize: 'Organize',
    autoTidy: 'Auto-tidy',
    organizeCluster: 'Cluster',
    organizeHorizontal: 'List horizontally',
    organizeVertical: 'List vertically',
    organizeTree: 'Arrange as tree',
    organizeHint: 'Organize: A auto-tidy · C cluster · H horizontal · V vertical · T tree',
    hideAll: 'Hide all',
    deleteAll: 'Delete all',
    changeType: 'Change type',
    generalConnection: 'General connection',
    addNote: 'Add note',
    addLabel: 'Add label',
    addArrow: 'Add arrow',
    annotationColor: 'Colour',
    deleteAnnotation: 'Delete',
    unlockAnnotation: 'Unlock',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
    annotationTextSize: 'Text size',
    arrowStartHead: 'Start arrowhead',
    arrowEndHead: 'End arrowhead',
    annotationShape: 'Shape',
    annotationIcon: 'Icon',
    annotationRotation: 'Rotation',
    annotationRotateLeft: 'Rotate left 15°',
    annotationRotateRight: 'Rotate right 15°',
    annotationRotateReset: 'Reset rotation',
    undoNotification: 'Move undone',
    redoNotification: 'Move redone',
    imageIngestFailed: 'Could not add the image',
    annotationRemoteLocked: 'Someone else is editing this annotation',
    annotationLockedSkipped: 'That annotation is locked — unlock it first',
    freehandColor: 'Colour',
    freehandWidth: 'Stroke width',
    freehandSmoothing: 'Smoothing',
    freehandOpacity: 'Opacity',
    freehandDrawingHint: 'Draw a stroke on the canvas — press Escape to cancel',
    freehandConcurrentInputBlocked: 'Finish the current stroke before starting another',
    annotationLayer: 'Layer',
    annotationLayerFront: 'Bring to front',
    annotationLayerBack: 'Send to back',
    annotationVoteValue: 'Value',
    annotationVoteValueDecrease: 'Decrease value',
    annotationVoteValueIncrease: 'Increase value',
    ...contextMenuLabels,
  };
  // Read through a ref inside the freehand pointer-capture effect below, for
  // the same reason as screenToFlowPositionRef/getViewportRef above: `cml` is
  // a fresh object literal every render, and including one of its fields
  // directly in that effect's deps would tear down its listeners on any
  // unrelated re-render.
  const cmlRef = useRef(cml);
  cmlRef.current = cml;

  const atl = {
    toggleExpand: 'Add annotation',
    toggleCollapse: 'Collapse annotation toolbox',
    note: 'Note',
    text: 'Text',
    label: 'Label',
    frame: 'Frame',
    shapeRectangle: 'Rectangle',
    shapeCircle: 'Circle',
    shapeTriangle: 'Triangle',
    shapeRhombus: 'Rhombus',
    shapeHexagon: 'Hexagon',
    shapeProcessArrow: 'Process arrow',
    icon: 'Icon',
    voteDot: 'Vote dot',
    image: 'Image',
    freehand: 'Freehand',
    ...annotationToolboxLabels,
  };

  // Relationship types defined in the schema, used for the edge type picker.
  const relationshipTypes = useMemo(() => {
    const rt = schema?.relationship_types;
    if (!rt || typeof rt !== 'object') return [];
    return Object.entries(rt).map(([name, cfg]) => ({
      type: name,
      description: (cfg && typeof cfg === 'object' && cfg.description) || '',
      source_types: (cfg && typeof cfg === 'object' && cfg.source_types) || [],
      target_types: (cfg && typeof cfg === 'object' && cfg.target_types) || [],
    }));
  }, [schema]);
  const [loadedNodeCount, setLoadedNodeCount] = useState(INITIAL_LOAD_COUNT);
  const [nodeContextMenu, setNodeContextMenu] = useState(null);
  const [multiNodeContextMenu, setMultiNodeContextMenu] = useState(null);
  const [edgeContextMenu, setEdgeContextMenu] = useState(null);
  const [notification, setNotification] = useState(null);
  const [selectedNodes, setSelectedNodes] = useState([]);
  const [selectedEdges, setSelectedEdges] = useState([]);
  const [paneContextMenu, setPaneContextMenu] = useState(null);
  // True while an MCP-driven layout tween is in flight, so the canvas can show a
  // non-interactive badge that an assistant is arranging the view.
  const [agentArranging, setAgentArranging] = useState(false);
  // touchMode 'auto' self-detects a coarse pointer; 'on'/'off' bypass detection.
  const [autoIsCoarsePointer, setAutoIsCoarsePointer] = useState(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    return window.matchMedia('(pointer: coarse)').matches;
  });
  useEffect(() => {
    if (touchMode !== 'auto') return undefined;
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const mql = window.matchMedia('(pointer: coarse)');
    const handleChange = () => setAutoIsCoarsePointer(mql.matches);
    handleChange();
    if (mql.addEventListener) mql.addEventListener('change', handleChange);
    else if (mql.addListener) mql.addListener(handleChange);
    return () => {
      if (mql.removeEventListener) mql.removeEventListener('change', handleChange);
      else if (mql.removeListener) mql.removeListener(handleChange);
    };
  }, [touchMode]);
  const isTouchMode = touchMode === 'off' ? false : touchMode === 'on' ? true : autoIsCoarsePointer;
  // compactMode 'auto' self-detects a phone-sized viewport; 'on'/'off' bypass it.
  // The query matches the host app's own mobile breakpoint so the canvas chrome
  // and the surrounding shell switch together rather than at different widths.
  const [autoIsNarrow, setAutoIsNarrow] = useState(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    return window.matchMedia(COMPACT_MEDIA_QUERY).matches;
  });
  useEffect(() => {
    if (compactMode !== 'auto') return undefined;
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const mql = window.matchMedia(COMPACT_MEDIA_QUERY);
    const handleChange = () => setAutoIsNarrow(mql.matches);
    handleChange();
    if (mql.addEventListener) mql.addEventListener('change', handleChange);
    else if (mql.addListener) mql.addListener(handleChange);
    return () => {
      if (mql.removeEventListener) mql.removeEventListener('change', handleChange);
      else if (mql.removeListener) mql.removeListener(handleChange);
    };
  }, [compactMode]);
  const isCompact = compactMode === 'off' ? false : compactMode === 'on' ? true : autoIsNarrow;
  // A fitted graph on a 390px viewport loses 20% of each edge to the desktop
  // padding, which is most of the screen; compact keeps the framing tight.
  const fitPadding = isCompact ? COMPACT_FIT_PADDING : DEFAULT_FIT_PADDING;

  // Focus view: the id of the node the canvas is currently narrowed to, or null
  // for the whole graph. Only the root and its direct neighbours are rendered.
  const [focusRootId, setFocusRootId] = useState(null);
  // The whole canvas as of the moment focus was entered — every flow node and
  // edge, verbatim. Leaving focus restores it, and a save requested while
  // focused serializes it instead of the temporary ego layout.
  const preFocusRef = useRef(null);
  const lastFocusRootRef = useRef(null);
  // Focus is only ever active while the compact chrome is on screen, because the
  // control that leaves it lives in that pill and nowhere else: a phone rotated
  // to landscape (852px on a current handset) or a window widened past the
  // breakpoint would otherwise strand the canvas on the ego graph with no way
  // back. The same guard covers a focus root that leaves the session — deleted,
  // or a session switch that loaded different content. Derived rather than
  // repaired in an effect, so a stale id stops counting on the very same render.
  const activeFocusRootId =
    isCompact && focusRootId && inputNodes.some((n) => n.id === focusRootId) ? focusRootId : null;
  // Long-press → context menu. The detector lives in a ref so its ~500ms timer
  // isn't torn down and recreated every render; the fire callback is kept in a
  // second ref, updated every render below (once the context-menu handlers it
  // calls are defined), so the fixed-identity timer always invokes the latest
  // closures instead of stale ones.
  const longPressFireRef = useRef(null);
  const longPressDetectorRef = useRef(null);
  if (!longPressDetectorRef.current) {
    longPressDetectorRef.current = createLongPressDetector({
      onLongPress: (payload) => longPressFireRef.current?.(payload),
    });
  }
  const paneMenuRef = useRef(null);
  const reactFlowWrapper = useRef(null);
  // Hidden <input type="file"> the toolbox's image item clicks; the model-space
  // position to ingest the file at is stashed here between that click and the
  // input's change event (there is no click position to carry through a native
  // file picker the way the pane context menu carries one).
  const imageFileInputRef = useRef(null);
  const pendingImagePositionRef = useRef(null);
  // Freehand drawing mode: armed by the toolbox's 'freehand' item, consumed by
  // exactly one stroke (pointerdown through pointerup/cancel), then
  // auto-disarmed — a one-shot tool, not a sticky one, matching every other
  // toolbox item creating exactly one annotation per click. `freehandActive`
  // drives the ReactFlow pan/selection/drag props and the pointer-capture
  // effect below; the rest are refs since they only matter inside that
  // effect's own event handlers, not to rendering.
  const [freehandActive, setFreehandActive] = useState(false);
  const freehandCaptureRef = useRef(null);
  const freehandPrimaryPointerIdRef = useRef(null);
  const freehandPreviewPathRef = useRef(null);
  const freehandModelPointsRef = useRef([]);
  const commitFreehandStrokeRef = useRef(null);
  if (!freehandCaptureRef.current) {
    freehandCaptureRef.current = createFreehandStrokeCapture({
      minPoints: FREEHAND_MIN_POINTS,
      onStrokeComplete: (stroke) => commitFreehandStrokeRef.current?.(stroke),
    });
  }
  const rightDragStart = useRef({ x: 0, y: 0, time: null });
  const mouseDownPos = useRef(null);
  // Two-step "organize" keyboard chord: Ctrl/Cmd+O arms it, then c/h/v/t picks
  // the arrangement. Held in refs so arming doesn't re-render or re-bind keys.
  const organizePendingRef = useRef(false);
  const organizeTimerRef = useRef(null);
  // Positions of the nodes about to move, snapshotted at drag start so the
  // finished drag can be recorded as an undoable move.
  const dragStartPositionsRef = useRef(new Map());
  // Alt+drag "move with neighbours": snapshot of the anchor's start position and
  // the trailing neighbours' start positions, captured at drag start. Held in a
  // ref so the per-frame drag handler doesn't depend on React state timing.
  const connectedDragRef = useRef(null);
  const {
    screenToFlowPosition,
    setCenter,
    getNodes: getFlowNodes,
    getViewport,
    fitView,
    zoomIn,
    zoomOut,
  } = useReactFlow();
  // Read through refs (updated every render below) inside the freehand
  // pointer-capture effect instead of closing over these directly: some
  // `useReactFlow()` implementations (and this package's own test mocks)
  // return a new function identity on every render, and putting either
  // directly in that effect's dependency array would tear down and rebuild
  // its listeners mid-stroke on any unrelated re-render (e.g. the "second
  // pointer ignored" notification's own state update), silently abandoning
  // an in-progress drawing gesture.
  const screenToFlowPositionRef = useRef(screenToFlowPosition);
  screenToFlowPositionRef.current = screenToFlowPosition;
  const getViewportRef = useRef(getViewport);
  getViewportRef.current = getViewport;
  // Last session identity seen by the node-reconciliation effect and the
  // refit-on-switch effect. Seeded with the mount value so the very first render
  // is not treated as a switch (mount already fits via the ReactFlow fitView prop).
  const lastSessionKeyRef = useRef(sessionKey);
  const fitOnSessionKeyRef = useRef(sessionKey);

  // Undo/redo history for node-position moves (drag + organize). Restoring a
  // prior state writes the positions back through the same persistence path a
  // normal move uses (see applyPositionMoves), matching the layout contract's
  // read-then-write reversibility model.
  const {
    record: recordMove,
    undo: undoMove,
    redo: redoMove,
    clear: clearHistory,
  } = useCanvasHistory();

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);
  // Read via ref inside the freehand pointer-capture effect for the same
  // reason as screenToFlowPositionRef/getViewportRef/cmlRef above (though
  // showNotification's own identity is already stable via useCallback's `[]`
  // deps, keeping every value that effect reads through the same ref pattern
  // avoids relying on that staying true).
  const showNotificationRef = useRef(showNotification);
  showNotificationRef.current = showNotification;

  // Stable notifier for annotation nodes (note/label/arrow) to signal the host
  // that an annotation was edited, recoloured or deleted so the session can be
  // persisted. Wrapped in a ref so the callback identity stays stable across
  // renders even as the parent's handler changes.
  const onAnnotationChangeRef = useRef(onAnnotationChange);
  onAnnotationChangeRef.current = onAnnotationChange;
  const notifyAnnotationChange = useCallback((kind) => {
    onAnnotationChangeRef.current?.(kind);
  }, []);
  // Surfaced by an annotation component when a mutation is refused because
  // another client currently holds the annotation's selection claim (leases
  // are exclusive — task-annotation-shared-session-realtime), so the attempt
  // is visible rather than a silent no-op.
  const notifyRemoteLockedAttempt = useCallback(() => {
    showNotification('info', cml.annotationRemoteLocked);
  }, [cml.annotationRemoteLocked, showNotification]);

  const annotationContextValue = useMemo(
    () => ({
      notifyChange: notifyAnnotationChange,
      notifyRemoteLockedAttempt,
      labels: {
        color: cml.annotationColor,
        delete: cml.deleteAnnotation,
        unlock: cml.unlockAnnotation,
        notePlaceholder: cml.notePlaceholder,
        labelPlaceholder: cml.labelPlaceholder,
        textSize: cml.annotationTextSize,
        arrowStartHead: cml.arrowStartHead,
        arrowEndHead: cml.arrowEndHead,
        shape: cml.annotationShape,
        icon: cml.annotationIcon,
        rotation: cml.annotationRotation,
        rotateLeft: cml.annotationRotateLeft,
        rotateRight: cml.annotationRotateRight,
        rotateReset: cml.annotationRotateReset,
        freehandColor: cml.freehandColor,
        freehandWidth: cml.freehandWidth,
        freehandSmoothing: cml.freehandSmoothing,
        freehandOpacity: cml.freehandOpacity,
        layer: cml.annotationLayer,
        layerFront: cml.annotationLayerFront,
        layerBack: cml.annotationLayerBack,
        voteValue: cml.annotationVoteValue,
        voteValueDecrease: cml.annotationVoteValueDecrease,
        voteValueIncrease: cml.annotationVoteValueIncrease,
      },
    }),
    [
      notifyAnnotationChange,
      notifyRemoteLockedAttempt,
      cml.annotationColor,
      cml.deleteAnnotation,
      cml.unlockAnnotation,
      cml.notePlaceholder,
      cml.labelPlaceholder,
      cml.annotationTextSize,
      cml.arrowStartHead,
      cml.arrowEndHead,
      cml.annotationShape,
      cml.annotationIcon,
      cml.annotationRotation,
      cml.annotationRotateLeft,
      cml.annotationRotateRight,
      cml.annotationRotateReset,
      cml.freehandColor,
      cml.freehandWidth,
      cml.freehandSmoothing,
      cml.freehandOpacity,
      cml.annotationLayer,
      cml.annotationLayerFront,
      cml.annotationLayerBack,
      cml.annotationVoteValue,
      cml.annotationVoteValueDecrease,
      cml.annotationVoteValueIncrease,
    ]
  );

  const depthLevels = useMemo(() => {
    if (Array.isArray(federationDepthLevels) && federationDepthLevels.length > 0) {
      const normalized = federationDepthLevels
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value >= 1)
        .sort((a, b) => a - b);
      return Array.from(new Set(normalized));
    }

    const max = Math.max(1, maxFederationDepth || 1);
    return Array.from({ length: max }, (_, index) => index + 1);
  }, [federationDepthLevels, maxFederationDepth]);

  // Track selected nodes and edges
  useOnSelectionChange({
    onChange: ({ nodes: selected, edges: selectedE }) => {
      setSelectedNodes(selected);
      setSelectedEdges(selectedE || []);
      if (onSelectionChange) {
        onSelectionChange(selected);
      }
    },
  });

  // Focus view membership: the root plus every node one edge away from it.
  // Null when focus is off, which every downstream filter reads as "no limit".
  const focusNodeIds = useMemo(() => {
    if (!activeFocusRootId) return null;
    const ids = new Set([activeFocusRootId]);
    for (const edge of inputEdges) {
      if (hiddenEdgeIds.includes(edge.id)) continue;
      if (edge.source === activeFocusRootId) ids.add(edge.target);
      else if (edge.target === activeFocusRootId) ids.add(edge.source);
    }
    return ids;
  }, [activeFocusRootId, inputEdges, hiddenEdgeIds]);

  // Filter out hidden nodes, and everything outside the focus view when one is active
  const visibleNodes = useMemo(
    () =>
      inputNodes.filter(
        (n) => !hiddenNodeIds.includes(n.id) && (!focusNodeIds || focusNodeIds.has(n.id))
      ),
    [inputNodes, hiddenNodeIds, focusNodeIds]
  );

  // How large the canvas is when the focus view is not narrowing it. Every
  // lazy-load decision keys off this rather than off visibleNodes: focus is a
  // lens, and a lens that reset "Load More" progress from 800 nodes back to 100
  // on the way out would not be the reversible view it claims to be.
  const unfocusedNodeCount = useMemo(
    () => inputNodes.filter((n) => !hiddenNodeIds.includes(n.id)).length,
    [inputNodes, hiddenNodeIds]
  );

  // Lazy loading for large graphs
  const nodesToRender = useMemo(() => {
    if (visibleNodes.length <= LAZY_LOAD_THRESHOLD) {
      return visibleNodes;
    }
    return visibleNodes.slice(0, loadedNodeCount);
  }, [visibleNodes, loadedNodeCount]);

  // Filter edges to visible nodes and not hidden edges
  const visibleEdges = useMemo(() => {
    const renderedNodeIds = new Set(nodesToRender.map((n) => n.id));
    return inputEdges.filter(
      (e) =>
        !hiddenNodeIds.includes(e.source) &&
        !hiddenNodeIds.includes(e.target) &&
        !hiddenEdgeIds.includes(e.id) &&
        renderedNodeIds.has(e.source) &&
        renderedNodeIds.has(e.target)
    );
  }, [inputEdges, hiddenNodeIds, hiddenEdgeIds, nodesToRender]);

  // Count connections dropped purely by the lazy-load slice: edges from a
  // rendered node to a real node that exists in the session but has not been
  // loaded yet. These are exactly the edges that reappear on "Load More" — so
  // the banner can honestly disclose how many connections the current view is
  // hiding, instead of silently dropping them on reload of a large session.
  // Dangling edges (an endpoint that is not in the graph at all) are excluded,
  // matching visibleEdges, which never draws them either.
  const hiddenConnectionCount = useMemo(() => {
    if (visibleNodes.length <= LAZY_LOAD_THRESHOLD) {
      return 0;
    }
    const renderedNodeIds = new Set(nodesToRender.map((n) => n.id));
    const graphNodeIds = new Set(visibleNodes.map((n) => n.id));
    let count = 0;
    for (const e of inputEdges) {
      if (
        hiddenNodeIds.includes(e.source) ||
        hiddenNodeIds.includes(e.target) ||
        hiddenEdgeIds.includes(e.id)
      ) {
        continue;
      }
      const sourceRendered = renderedNodeIds.has(e.source);
      const targetRendered = renderedNodeIds.has(e.target);
      if (sourceRendered && targetRendered) {
        continue; // already drawn by visibleEdges
      }
      if (!graphNodeIds.has(e.source) || !graphNodeIds.has(e.target)) {
        continue; // dangling endpoint, not a lazy-hidden connection
      }
      if (sourceRendered || targetRendered) {
        count += 1;
      }
    }
    return count;
  }, [inputEdges, visibleNodes, nodesToRender, hiddenNodeIds, hiddenEdgeIds]);

  // Convert to React Flow edge format
  const reactFlowEdges = useMemo(() => {
    return visibleEdges.map((edge) => {
      const visuals = resolveEdgeVisuals(edge.metadata);
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.type,
        type: 'floating',
        animated: visuals.animated,
        selectable: true,
        style: visuals.style,
        markerStart: visuals.markerStart,
        markerEnd: visuals.markerEnd,
        className: visuals.className,
        data: { type: edge.type, label: edge.label, metadata: edge.metadata || {} },
        labelStyle: { fill: '#888', fontSize: 10, fontWeight: 500 },
        labelBgStyle: { fill: '#1a1a1a', fillOpacity: 0.8 },
      };
    });
  }, [visibleEdges]);

  // Convert to React Flow node format with layout
  const reactFlowNodes = useMemo(() => {
    const hasSavedPositions = nodesToRender.some((n) => n._savedPosition);

    const nodesWithoutPosition = nodesToRender.map((node) => {
      const mark = nodeMarks[node.id];
      return {
        id: node.id,
        type: 'custom',
        data: {
          ...node,
          label: node.name,
          summary: node.summary || node.description?.slice(0, 100),
          nodeType: node.type,
          color: (nodeColorResolver || getNodeColor)(node.type),
          isHighlighted: highlightedNodeIds.includes(node.id),
          markColor: mark?.color ?? null,
          markLabel: mark?.label ?? null,
          pulse: pulsedNodeIds[node.id] ?? null,
          remoteSelection: remoteSelections?.[node.id] ?? null,
          previewEnabled: nodePreviewEnabled,
          onExpand: onExpand ? () => onExpand(node.id, node) : null,
          onEdit: onEdit ? () => onEdit(node.id, node) : null,
        },
        position: node._savedPosition || { x: 0, y: 0 },
      };
    });

    if (nodesWithoutPosition.length === 0) {
      return nodesWithoutPosition;
    }

    // The focus view is an ego graph, so it gets its own arrangement regardless
    // of any saved positions: the root in the middle, its neighbours on a ring
    // sized to hold them apart. Saved positions are untouched on disk — leaving
    // focus restores them from the pre-focus snapshot.
    if (activeFocusRootId) {
      const neighbours = nodesWithoutPosition.filter((n) => n.id !== activeFocusRootId);
      const radius = Math.max(FOCUS_MIN_RADIUS, neighbours.length * FOCUS_RADIUS_PER_NEIGHBOUR);
      const ring = getCircularLayout(neighbours, FOCUS_CENTER.x, FOCUS_CENTER.y, radius);
      const root = nodesWithoutPosition.find((n) => n.id === activeFocusRootId);
      // getCircularLayout centres the ring on the *top-left corners* of its
      // nodes, so a root placed at the same coordinate shares their basis and
      // lands dead centre.
      return root ? [{ ...root, position: { ...FOCUS_CENTER } }, ...ring] : ring;
    }

    if (hasSavedPositions) {
      return nodesWithoutPosition;
    }

    return applyLayout(nodesWithoutPosition, reactFlowEdges, layoutType);
  }, [
    nodesToRender,
    reactFlowEdges,
    layoutType,
    activeFocusRootId,
    onExpand,
    onEdit,
    highlightedNodeIds,
    nodeMarks,
    pulsedNodeIds,
    remoteSelections,
    nodePreviewEnabled,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  const edgeContextRelationshipTypes = useMemo(() => {
    if (!edgeContextMenu?.edge) return relationshipTypes;
    const sourceNode = inputNodes.find((n) => n.id === edgeContextMenu.edge.source);
    const targetNode = inputNodes.find((n) => n.id === edgeContextMenu.edge.target);
    const sourceType = sourceNode?.type || sourceNode?.data?.type;
    const targetType = targetNode?.type || targetNode?.data?.type;
    if (!sourceType || !targetType) return relationshipTypes;
    return relationshipTypes.filter((rt) => {
      const sourceTypes = Array.isArray(rt.source_types) ? rt.source_types : [];
      const targetTypes = Array.isArray(rt.target_types) ? rt.target_types : [];
      const sourceAllowed =
        sourceTypes.length === 0 || sourceTypes.includes('*') || sourceTypes.includes(sourceType);
      const targetAllowed =
        targetTypes.length === 0 || targetTypes.includes('*') || targetTypes.includes(targetType);
      return sourceAllowed && targetAllowed;
    });
  }, [edgeContextMenu, inputNodes, relationshipTypes]);

  // Update nodes when input changes
  useEffect(() => {
    const sessionChanged = lastSessionKeyRef.current !== sessionKey;
    lastSessionKeyRef.current = sessionKey;
    const focusChanged = lastFocusRootRef.current !== activeFocusRootId;
    const enteringFocus = focusChanged && activeFocusRootId !== null;
    const leavingFocus = focusChanged && activeFocusRootId === null;
    lastFocusRootRef.current = activeFocusRootId;
    // A wholesale content replace (session switch, clear, an agent loading a
    // different graph) makes the snapshot describe a canvas that no longer
    // exists. Restoring it would force stale coordinates onto whatever ids the
    // old and new content happen to share, and resurrect groups and overlays the
    // replace deliberately dropped.
    const contentReplaced = sessionChanged || clearGroupsFlag;
    if (contentReplaced) preFocusRef.current = null;
    const snapshot = contentReplaced ? null : preFocusRef.current;
    if (leavingFocus) preFocusRef.current = null;
    setNodes((nds) => {
      // A session switch drops the previous session's manual annotations too:
      // clearVisualization sets clearGroupsFlag, and the new session's overlays
      // arrive through the restore path. Outside a switch, keep the live ones —
      // except while focused, where the ego graph is the whole view and an
      // overlay parked elsewhere would only drag the fitted frame off it.
      let manualNodes;
      if (clearGroupsFlag || sessionChanged) manualNodes = [];
      else if (activeFocusRootId) manualNodes = [];
      else if (leavingFocus) manualNodes = (snapshot?.nodes ?? []).filter(isManualNode);
      else manualNodes = nds.filter(isManualNode);

      const reconciled = reconcileSessionNodes({
        prevNodes: nds,
        incomingNodes: reactFlowNodes,
        incomingEdges: reactFlowEdges,
        sessionChanged,
        inLazyMode: unfocusedNodeCount > LAZY_LOAD_THRESHOLD,
      });

      // Both focus transitions re-base positions wholesale. They are applied
      // here as an explicit override rather than by telling reconcileSessionNodes
      // the session changed: that flag drops `prevNodes` entirely, and with it
      // the parentId and style that live only on the flow node — silently
      // detaching every grouped node from its group.
      let positioned = reconciled;
      if (enteringFocus) {
        // The ego ring is in absolute flow coordinates, so a node still parented
        // to a group would be drawn relative to that group's box. Focus detaches
        // them for the duration; leaving focus re-parents them from the snapshot.
        const ego = new Map(reactFlowNodes.map((n) => [n.id, n.position]));
        positioned = reconciled.map((n) =>
          ego.has(n.id) ? { ...n, position: ego.get(n.id), parentId: undefined } : n
        );
      } else if (leavingFocus && snapshot) {
        const before = new Map(snapshot.nodes.map((n) => [n.id, n]));
        positioned = reconciled.map((n) => {
          const prior = before.get(n.id);
          if (!prior) return n;
          return {
            ...n,
            position: prior.position,
            parentId: prior.parentId,
            style: prior.style || n.style,
          };
        });
      }

      // Groups must appear before their children in the array for ReactFlow
      // parent-child relationships to work. This also ensures groups render
      // behind custom nodes so clicks reach the nodes on top.
      return reorderNodesForParentChild([...positioned, ...manualNodes]);
    });
  }, [
    reactFlowNodes,
    reactFlowEdges,
    unfocusedNodeCount,
    setNodes,
    clearGroupsFlag,
    sessionKey,
    activeFocusRootId,
  ]);

  // Refit the view whenever the session identity changes so switching sessions
  // (e.g. via "recent sessions") frames the newly loaded content instead of
  // leaving the camera on the viewport of the session just left. The mount value
  // is seeded into the ref, so the initial fit still comes from the ReactFlow
  // fitView prop and this effect only reacts to real switches. Deferred a frame
  // so the incoming nodes are committed to the flow before it fits to them.
  useEffect(() => {
    if (fitOnSessionKeyRef.current === sessionKey) return;
    fitOnSessionKeyRef.current = sessionKey;
    const raf = requestAnimationFrame(() => fitView({ padding: fitPadding, duration: 800 }));
    return () => cancelAnimationFrame(raf);
  }, [sessionKey, fitView, fitPadding]);

  // Entering focus narrows the canvas to a handful of nodes and leaving it
  // widens back out; either way the camera has to reframe or the user is left
  // looking at empty space. Deferred a frame so the new node set is committed
  // to the flow before it is fitted, same as the session-switch refit above.
  const fitOnFocusRef = useRef(activeFocusRootId);
  useEffect(() => {
    if (fitOnFocusRef.current === activeFocusRootId) return undefined;
    fitOnFocusRef.current = activeFocusRootId;
    const raf = requestAnimationFrame(() => fitView({ padding: fitPadding, duration: 400 }));
    return () => cancelAnimationFrame(raf);
  }, [activeFocusRootId, fitView, fitPadding]);

  const enterFocusView = useCallback(
    (rootId) => {
      if (!rootId) return;
      preFocusRef.current = { nodes: getFlowNodes(), edges };
      setFocusRootId(rootId);
    },
    [getFlowNodes, edges]
  );

  const exitFocusView = useCallback(() => setFocusRootId(null), []);

  // Falling back through activeFocusRootId is not enough on its own: the id has
  // to go too, or focus would silently re-engage the moment the condition that
  // suspended it lifts — the node returning from an undone delete, or the phone
  // being rotated back to portrait — with the pre-focus snapshot already spent.
  useEffect(() => {
    if (focusRootId && !activeFocusRootId) {
      preFocusRef.current = null;
      setFocusRootId(null);
    }
  }, [focusRootId, activeFocusRootId]);

  // The focus control acts on a single selected graph node. Annotations and
  // groups have no neighbours to draw, so selecting one leaves it disabled.
  const focusCandidateId = useMemo(() => {
    if (selectedNodes.length !== 1) return null;
    const [candidate] = selectedNodes;
    return ANNOTATION_TYPES.has(candidate.type) ? null : candidate.id;
  }, [selectedNodes]);

  // Update edges when input changes
  useEffect(() => {
    setEdges(reactFlowEdges);
  }, [reactFlowEdges, setEdges]);

  // Reset loaded count when visible nodes change significantly
  useEffect(() => {
    if (unfocusedNodeCount <= LAZY_LOAD_THRESHOLD) {
      setLoadedNodeCount(unfocusedNodeCount);
    } else {
      setLoadedNodeCount(INITIAL_LOAD_COUNT);
    }
  }, [unfocusedNodeCount]);

  const onConnect = useCallback(
    (params) => {
      // Persistence-first: when a parent handler is provided it persists the
      // connection and the stored edge flows back in through the store, so what
      // is drawn is always what is saved. Adding a local edge here as well would
      // leave a phantom edge on screen if persistence fails. Only fall back to a
      // local-only edge for consumers that don't wire up persistence.
      if (onConnectCallback) {
        onConnectCallback(params);
      } else {
        setEdges((eds) => addEdge(params, eds));
      }
    },
    [setEdges, onConnectCallback]
  );

  // Close all context menus
  const closeAllMenus = useCallback(() => {
    setNodeContextMenu(null);
    setMultiNodeContextMenu(null);
    setEdgeContextMenu(null);
    setPaneContextMenu(null);
  }, []);

  // Apply a batch of { id, position, parentId } moves to the canvas and persist
  // each one through the same callback a drag/organize uses, so an undo or redo
  // is indistinguishable from the move it reverses. `parentId` is restored too
  // (re-parenting the node into or out of a group), because a grouped node's
  // position is parent-relative — restoring the position without the parent
  // would place it in the wrong coordinate space.
  const applyPositionMoves = useCallback(
    (moves) => {
      if (!moves || moves.length === 0) return;
      const byId = new Map(moves.map((m) => [m.id, m]));
      const typeById = new Map(getFlowNodes().map((n) => [n.id, n.type]));
      setNodes((nds) =>
        reorderNodesForParentChild(
          nds.map((n) => {
            const m = byId.get(n.id);
            if (!m) return n;
            return { ...n, position: { ...m.position }, parentId: m.parentId };
          })
        )
      );
      if (onNodePositionChange) {
        for (const m of moves) onNodePositionChange(m.id, m.position, typeById.get(m.id));
      }
    },
    [setNodes, onNodePositionChange, getFlowNodes]
  );

  const handleUndo = useCallback(() => {
    const moves = undoMove();
    if (!moves) return;
    applyPositionMoves(moves);
    showNotification('info', cml.undoNotification);
  }, [undoMove, applyPositionMoves, showNotification, cml.undoNotification]);

  const handleRedo = useCallback(() => {
    const moves = redoMove();
    if (!moves) return;
    applyPositionMoves(moves);
    showNotification('info', cml.redoNotification);
  }, [redoMove, applyPositionMoves, showNotification, cml.redoNotification]);

  // Discard history whenever a new position baseline is established: the session
  // identity changes, or the canvas contents are replaced wholesale within the
  // session. Either way the recorded "before" positions belong to a layout that
  // is gone, while the node ids they name can still be on the canvas — so an
  // undo would otherwise teleport a node to a coordinate from a discarded view.
  useEffect(() => {
    clearHistory();
  }, [animatedLayoutResetKey, canvasBaselineEpoch, clearHistory]);

  // While any context menu is open, suppress the hover info popup: the node the
  // menu was opened on is still hovered, so its tooltip (portaled to the body at
  // a high z-index) would otherwise render on top of / behind the menu. The
  // tooltip lives in CustomNode and is portaled to the body, so a body-level
  // class is the simplest way to reach it without threading menu state through
  // every node's data.
  const anyMenuOpen = !!(
    nodeContextMenu ||
    multiNodeContextMenu ||
    edgeContextMenu ||
    paneContextMenu
  );
  useEffect(() => {
    document.body.classList.toggle('graph-menu-open', anyMenuOpen);
    return () => document.body.classList.remove('graph-menu-open');
  }, [anyMenuOpen]);

  // Select a node together with every node it is directly connected to.
  const selectRelatedNodes = useCallback(
    (nodeId) => {
      const relatedIds = new Set([nodeId]);
      for (const e of edges) {
        if (e.source === nodeId) relatedIds.add(e.target);
        else if (e.target === nodeId) relatedIds.add(e.source);
      }
      const changes = nodes
        .filter((n) => n.type !== 'group')
        .map((n) => ({ id: n.id, type: 'select', selected: relatedIds.has(n.id) }));
      if (changes.length > 0) onNodesChange(changes);
      closeAllMenus();
    },
    [edges, nodes, onNodesChange, closeAllMenus]
  );

  // Re-arrange the currently selected graph nodes into a chosen structure
  // (cluster / horizontal / vertical / tree), centred on where they already are.
  const organizeSelection = useCallback(
    (mode) => {
      const selIds = new Set(
        selectedNodes.filter((n) => !ANNOTATION_TYPES.has(n.type)).map((n) => n.id)
      );
      if (selIds.size < 2) return;
      // Nodes inside a group hold parent-relative positions; arrange in absolute
      // coordinates so a selection spanning grouped and ungrouped nodes stays
      // consistent, then convert back to relative when writing grouped nodes.
      const groupPos = new Map(
        nodes.filter((n) => n.type === 'group').map((g) => [g.id, g.position])
      );
      const toAbsolute = (n) => {
        const parent = n.parentId ? groupPos.get(n.parentId) : null;
        return parent ? { x: n.position.x + parent.x, y: n.position.y + parent.y } : n.position;
      };
      const targets = nodes
        .filter((n) => selIds.has(n.id))
        .map((n) => ({ ...n, position: toAbsolute(n) }));
      const posById = arrangeNodes(targets, edges, mode);
      if (posById.size === 0) return;
      // Convert the arranged absolute positions back to the parent-relative form
      // ReactFlow stores for grouped nodes. Compute once and use for both the
      // on-screen update and the persistence callback, so the position we render
      // is the position we save (the drag path also persists relative positions).
      const nodeById = new Map(nodes.map((n) => [n.id, n]));
      const finalPos = new Map();
      const moves = [];
      for (const [id, abs] of posById) {
        const parent = nodeById.get(id)?.parentId ? groupPos.get(nodeById.get(id).parentId) : null;
        const pos = parent ? { x: abs.x - parent.x, y: abs.y - parent.y } : abs;
        finalPos.set(id, pos);
        const cur = nodeById.get(id);
        // Organize never changes group membership: carry the node's current
        // parentId on both sides so an undo restores position without detaching
        // a grouped node.
        if (cur) {
          moves.push({
            id,
            from: { x: cur.position.x, y: cur.position.y, parentId: cur.parentId },
            to: { x: pos.x, y: pos.y, parentId: cur.parentId },
          });
        }
      }
      recordMove(moves);
      setNodes((nds) =>
        nds.map((n) => (finalPos.has(n.id) ? { ...n, position: finalPos.get(n.id) } : n))
      );
      if (onNodePositionChange) {
        for (const [id, pos] of finalPos) onNodePositionChange(id, pos);
      }
      closeAllMenus();
    },
    [selectedNodes, nodes, edges, setNodes, onNodePositionChange, closeAllMenus, recordMove]
  );

  // Create a free-floating annotation (note, label, arrow, or one of the
  // generic overlay kinds - text/frame/shape) at the given flow position.
  // These are persisted in the session annotation list via the save-view
  // round-trip; onAnnotationChange schedules that save. `options.shape` picks
  // the shape variant for kind 'shape' (defaults to 'rectangle').
  const createAnnotation = useCallback(
    (kind, position, options = {}) => {
      const id = `${kind}-${Date.now()}`;
      let newNode;
      if (kind === 'note') {
        newNode = {
          id,
          type: 'note',
          position,
          data: { text: '', color: undefined },
          style: { width: 200, height: 140 },
        };
      } else if (kind === 'label') {
        newNode = { id, type: 'label', position, data: { text: '', color: undefined } };
      } else if (kind === 'text') {
        newNode = {
          id,
          type: 'text',
          position,
          data: { text: '', color: undefined, fontSize: undefined },
        };
      } else if (kind === 'frame') {
        newNode = {
          id,
          type: 'frame',
          position,
          data: { color: undefined },
          style: { width: 220, height: 160 },
        };
      } else if (kind === 'shape') {
        newNode = {
          id,
          type: 'shape',
          position,
          data: { shape: options.shape || 'rectangle', color: undefined },
          style: { width: 160, height: 96 },
        };
      } else if (kind === 'icon') {
        // No `style` box — icon renders at a fixed intrinsic size
        // (GenericAnnotationNode.css `.kind-icon`), not a resizable one.
        // `data.size` carries that natural size as silently-preserved
        // geometry (annotations.js's data-only slot for unsized generic
        // kinds — 61d5cc7b) so it survives the session save round trip
        // instead of being dropped and later defaulted to a mismatched
        // 160x96 box. The default icon matches what an icon annotation with
        // no configured name already renders (resolveAnnotationIcon).
        newNode = {
          id,
          type: 'icon',
          position,
          data: {
            icon: DEFAULT_ANNOTATION_ICON,
            color: undefined,
            size: { ...ICON_INTRINSIC_SIZE },
          },
        };
      } else if (kind === 'vote_dot') {
        // Same fixed-intrinsic-size treatment as icon, above.
        newNode = {
          id,
          type: 'vote_dot',
          position,
          data: { value: 1, color: undefined, size: { ...VOTE_DOT_INTRINSIC_SIZE } },
        };
      } else {
        newNode = {
          id,
          type: 'arrow',
          position,
          data: { dx: 160, dy: 0, color: undefined, startArrow: false, endArrow: true },
        };
      }
      setNodes((nds) => reorderNodesForParentChild([...nds, newNode]));
      setPaneContextMenu(null);
      onAnnotationChangeRef.current?.('create');
    },
    [setNodes]
  );

  // Model-space position at the current viewport's centre (used by the bottom
  // toolbox and clipboard paste, neither of which has a click position of its
  // own the way the pane context menu or a file drop does). Mirrors
  // handleAddGroup's centre computation.
  const viewportCenterPosition = useCallback(() => {
    const wrapper = reactFlowWrapper.current;
    const rect = wrapper?.getBoundingClientRect();
    const centerX = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
    const centerY = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;
    return screenToFlowPosition({ x: centerX, y: centerY });
  }, [screenToFlowPosition]);

  // Create an annotation at the current viewport's centre (used by the bottom
  // toolbox). 'image' is not a local-state creation like the other kinds — it
  // opens the hidden file picker instead, and onImageIngest carries the
  // resulting server round-trip (see that prop's docstring above). 'freehand'
  // is not created here at all — there is no click position to draw from —
  // it arms/disarms the one-shot drawing mode the pointer-capture effect
  // below consumes.
  const createAnnotationAtViewportCenter = useCallback(
    (kind, options) => {
      if (kind === 'freehand') {
        setFreehandActive((active) => !active);
        return;
      }
      const position = viewportCenterPosition();
      if (kind === 'image') {
        pendingImagePositionRef.current = position;
        imageFileInputRef.current?.click();
        return;
      }
      createAnnotation(kind, position, options);
    },
    [viewportCenterPosition, createAnnotation]
  );

  // Build and add the freehand annotation node once a stroke completes
  // (createFreehandStrokeCapture's onStrokeComplete). `points` are absolute
  // model-space coordinates; stored node-relative to the first point, the
  // same anchor convention arrow's dx/dy and every other freehand translation
  // layer uses (see annotations.js's GENERIC_OVERLAY_FIELDS comment).
  // `pressureSource` is set only when a real device sample was captured —
  // never invented — matching docs/ANNOTATION_CONTRACT.md's
  // `persist_predicted_points: false` / "only bounded actual points with
  // optional pressure" rule; nothing here ever reads getPredictedEvents().
  const commitFreehandStroke = useCallback(
    ({ points, pointerType }) => {
      if (!Array.isArray(points) || points.length < FREEHAND_MIN_POINTS) return;
      const anchor = points[0];
      const relativePoints = points.map((p) => {
        const point = { x: p.x - anchor.x, y: p.y - anchor.y };
        if (Number.isFinite(p.pressure)) point.pressure = p.pressure;
        return point;
      });
      const hasRealPressure = points.some((p) => Number.isFinite(p.pressure));
      const id = `freehand-${Date.now()}`;
      const newNode = {
        id,
        type: 'freehand',
        position: { x: anchor.x, y: anchor.y },
        data: {
          points: relativePoints,
          color: DEFAULT_FREEHAND_COLOR,
          strokeWidth: DEFAULT_FREEHAND_STROKE_WIDTH,
          smoothing: DEFAULT_FREEHAND_SMOOTHING,
          opacity: DEFAULT_FREEHAND_OPACITY,
          pointerType: pointerType || undefined,
          pressureSource: hasRealPressure ? 'device' : undefined,
        },
      };
      setNodes((nds) => reorderNodesForParentChild([...nds, newNode]));
      onAnnotationChangeRef.current?.('create');
    },
    [setNodes]
  );
  commitFreehandStrokeRef.current = commitFreehandStroke;

  // Read a single File as a data: URL (base64) — the same shape the MCP
  // create_image_annotation tool's image_data param accepts server-side, so
  // both a human paste/upload and an agent's call converge on identical input.
  const readImageFileAsDataUrl = useCallback((file) => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error('failed to read image file'));
      reader.readAsDataURL(file);
    });
  }, []);

  // Hand a File off to the host's ingest round-trip (validate/optimize/embed
  // server-side). Deliberately does not touch local node state — unlike
  // createAnnotation, the canvas only renders this annotation once the
  // session's realtime channel delivers the server's actual result (see
  // onImageIngest's docstring), so a bad read here surfaces as a notification
  // rather than a node that silently never appears.
  const ingestImageFile = useCallback(
    (file, position) => {
      if (!onImageIngest || !file || !file.type || !file.type.startsWith('image/')) return;
      readImageFileAsDataUrl(file)
        .then((dataUrl) => onImageIngest(dataUrl, position))
        .catch(() => showNotification('error', cml.imageIngestFailed));
    },
    [onImageIngest, readImageFileAsDataUrl, showNotification, cml.imageIngestFailed]
  );

  const handleImageFileSelected = useCallback(
    (event) => {
      const file = event.target.files && event.target.files[0];
      const position = pendingImagePositionRef.current || viewportCenterPosition();
      pendingImagePositionRef.current = null;
      event.target.value = ''; // allow re-selecting the same file next time
      if (file) ingestImageFile(file, position);
    },
    [ingestImageFile, viewportCenterPosition]
  );

  // Clipboard image paste (Ctrl/Cmd+V), anywhere the canvas itself has focus —
  // guarded the same way the keydown handler below guards Delete/Backspace, so
  // pasting into a note's text field or any other input is untouched. Always
  // lands at the viewport centre: unlike a drop, a paste carries no cursor
  // position of its own.
  useEffect(() => {
    if (!onImageIngest) return undefined;
    const handlePaste = (event) => {
      const tag = event.target?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || event.target?.isContentEditable) return;
      const items = event.clipboardData?.items;
      if (!items) return;
      const imageItem = Array.from(items).find((item) => item.type?.startsWith('image/'));
      if (!imageItem) return;
      const file = imageItem.getAsFile();
      if (!file) return;
      event.preventDefault();
      ingestImageFile(file, viewportCenterPosition());
    };
    document.addEventListener('paste', handlePaste);
    return () => document.removeEventListener('paste', handlePaste);
  }, [onImageIngest, ingestImageFile, viewportCenterPosition]);

  // Dismiss the pane annotation menu on any outside interaction (e.g. clicking a
  // graph node, which handlePaneClick does not cover), matching the annotation
  // node menus. Escape is handled by the global keydown handler.
  useEffect(() => {
    if (!paneContextMenu) return;
    const handleDismiss = (e) => {
      if (paneMenuRef.current && paneMenuRef.current.contains(e.target)) return;
      setPaneContextMenu(null);
    };
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleDismiss, true);
      document.addEventListener('contextmenu', handleDismiss, true);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('mousedown', handleDismiss, true);
      document.removeEventListener('contextmenu', handleDismiss, true);
    };
  }, [paneContextMenu]);

  const clearSelection = useCallback(() => {
    // Use onNodesChange/onEdgesChange with select events to properly clear ReactFlow's internal selection state
    const nodeDeselects = nodes
      .filter((n) => n.selected)
      .map((n) => ({
        id: n.id,
        type: 'select',
        selected: false,
      }));
    const edgeDeselects = edges
      .filter((e) => e.selected)
      .map((e) => ({
        id: e.id,
        type: 'select',
        selected: false,
      }));
    if (nodeDeselects.length > 0) onNodesChange(nodeDeselects);
    if (edgeDeselects.length > 0) onEdgesChange(edgeDeselects);
  }, [nodes, edges, onNodesChange, onEdgesChange]);

  // Select every node in the current visualization whose type matches any of the
  // given types, regardless of whether it is currently within the viewport.
  const selectNodesByType = useCallback(
    (types) => {
      const typeSet = new Set((types || []).filter(Boolean));
      if (typeSet.size === 0) return;
      const changes = nodes
        .filter((n) => n.type !== 'group')
        .map((n) => {
          const nodeType = n.data?.nodeType || n.data?.type;
          return { id: n.id, type: 'select', selected: typeSet.has(nodeType) };
        });
      if (changes.length > 0) onNodesChange(changes);
      closeAllMenus();
    },
    [nodes, onNodesChange, closeAllMenus]
  );

  const handlePaneClick = useCallback(() => {
    closeAllMenus();
    clearSelection();
  }, [closeAllMenus, clearSelection]);

  const onNodeDragStart = useCallback(
    (event, draggedNode, allDraggedNodes) => {
      // Snapshot the pre-drag positions (and parent) of the graph nodes about to
      // move, so the completed drag can be recorded as one undoable move.
      // Annotation and group nodes are excluded — they persist through their own
      // paths, not the position-move history.
      const set = allDraggedNodes && allDraggedNodes.length > 0 ? allDraggedNodes : [draggedNode];
      const snap = new Map();
      for (const n of set) {
        if (ANNOTATION_TYPES.has(n.type)) continue;
        snap.set(n.id, { x: n.position.x, y: n.position.y, parentId: n.parentId });
      }
      dragStartPositionsRef.current = snap;

      // Alt+drag: arm "move node together with its directly connected
      // neighbours". Alt is chosen to avoid the Shift/Ctrl/Meta multi-select
      // gesture. The neighbour snapshot is taken from ReactFlow's live store so
      // it agrees with the coordinates onNodeDrag/onNodeDragStop read back.
      connectedDragRef.current = null;
      if (!event?.altKey) return;
      // The full set ReactFlow is dragging (may include group nodes when an
      // Alt-drag starts from a multi-selection); used to skip neighbours that a
      // dragged group already carries, so they aren't translated twice.
      const dragged =
        allDraggedNodes && allDraggedNodes.length > 0 ? allDraggedNodes : [draggedNode];
      const draggedIds = new Set(dragged.map((n) => n.id));
      // Only graph nodes carry meaningful edges; annotation/group nodes are
      // excluded both as anchors and as trailing neighbours.
      const anchors = dragged.filter((n) => !ANNOTATION_TYPES.has(n.type));
      if (anchors.length === 0) return;
      const anchorIds = new Set(anchors.map((n) => n.id));
      const neighborIds = directNeighborIds(edges, anchorIds);
      if (neighborIds.size === 0) return;
      const startById = neighborStartPositions(getFlowNodes(), neighborIds, draggedIds);
      if (startById.size === 0) return;
      connectedDragRef.current = {
        anchorId: draggedNode.id,
        anchorStart: { x: draggedNode.position.x, y: draggedNode.position.y },
        neighbors: startById,
      };
    },
    [edges, getFlowNodes]
  );

  const onNodeDrag = useCallback(
    (event, draggedNode) => {
      const state = connectedDragRef.current;
      if (!state || draggedNode.id !== state.anchorId) return;
      const updates = neighborDragPositions(
        state.neighbors,
        state.anchorStart,
        draggedNode.position
      );
      if (updates.size === 0) return;
      setNodes((nds) =>
        nds.map((n) => (updates.has(n.id) ? { ...n, position: updates.get(n.id) } : n))
      );
    },
    [setNodes]
  );

  const onNodeDragStop = useCallback(
    (event, draggedNode, allDraggedNodes) => {
      // Focus view renders a temporary ego layout; dragging there must not
      // overwrite the persisted whole-graph positions of the visible nodes.
      if (activeFocusRootId) {
        connectedDragRef.current = null;
        dragStartPositionsRef.current = new Map();
        return;
      }

      // Persist the final positions of any Alt-drag neighbours that trailed the
      // anchor, then disarm. Group re-parenting below is intentionally left to
      // the explicitly dragged nodes only; trailing neighbours keep their parent.
      const connected = connectedDragRef.current;
      connectedDragRef.current = null;
      if (connected && onNodePositionChange) {
        const latest = getFlowNodes();
        for (const id of connected.neighbors.keys()) {
          const fn = latest.find((cn) => cn.id === id);
          if (fn) onNodePositionChange(id, fn.position);
        }
      }

      if (onNodePositionChange) {
        onNodePositionChange(draggedNode.id, draggedNode.position, draggedNode.type);
      }

      // Get latest node positions directly from ReactFlow's internal store
      const currentNodes = getFlowNodes();

      // Attach/detach a dropped label/text/icon/vote_dot (the contract's
      // node-attachable types): within ATTACH_SNAP_RADIUS of a node or
      // another annotation's centre it (re)attaches and starts following that
      // target (see the "keep attached overlays glued to their target" effect
      // below); dropped outside every snap zone it detaches and keeps the
      // position it was just released at (contract: "snap to the node edge
      // with free fine adjustment and detach outside the snap zone"). Only a
      // solo drag of the attachable overlay itself is handled — a multi-drag
      // that happens to include one is left alone, same restraint the arrow
      // endpoint snap uses.
      if (
        ATTACHABLE_OVERLAY_KINDS.has(draggedNode.type) &&
        (!allDraggedNodes || allDraggedNodes.length <= 1)
      ) {
        const self = currentNodes.find((n) => n.id === draggedNode.id) || draggedNode;
        const attachment = computeDroppedAttachment(self.position, currentNodes, draggedNode.id);
        if (attachment || self.data?.attachment) {
          setNodes((nds) =>
            nds.map((n) =>
              n.id === draggedNode.id
                ? { ...n, data: { ...n.data, attachment: attachment || undefined } }
                : n
            )
          );
          onAnnotationChangeRef.current?.('geometry');
        }
      }

      const groupNodes = currentNodes.filter((n) => n.type === 'group');

      // Determine which draggable graph nodes were part of this drag. Annotation
      // nodes (groups, notes, labels, arrows) never become children of a group.
      const nodesToProcess =
        allDraggedNodes && allDraggedNodes.length > 0
          ? allDraggedNodes.filter((n) => !ANNOTATION_TYPES.has(n.type))
          : !ANNOTATION_TYPES.has(draggedNode.type)
            ? [draggedNode]
            : [];
      const draggedIds = new Set(nodesToProcess.map((n) => n.id));

      const startSnap = dragStartPositionsRef.current;
      dragStartPositionsRef.current = new Map();

      // Compute each dragged graph node's final placement (position + parentId),
      // including any group-membership change, so the on-screen re-parent and the
      // recorded undo entry share one source of truth. Empty when there are no
      // groups to enter/leave — a plain drag needs no re-parenting.
      const finalById = new Map();
      if (draggedIds.size > 0 && groupNodes.length > 0) {
        for (const n of currentNodes) {
          if (!draggedIds.has(n.id) || n.type === 'group') continue;
          finalById.set(n.id, computeGroupPlacement(n, currentNodes, groupNodes));
        }
      }

      // Record the completed drag as one undoable action: from the drag-start
      // snapshot to the final placement. Runs for plain drags too, so a canvas
      // with no groups still records moves. When a node changed group membership,
      // the recorded `to` carries the new parentId and parent-relative position,
      // so an undo restores the correct parent and coordinate space.
      const moves = [];
      for (const [id, from] of startSnap) {
        const placed = finalById.get(id);
        if (placed) {
          moves.push({
            id,
            from,
            to: { x: placed.position.x, y: placed.position.y, parentId: placed.parentId },
          });
        } else {
          const flowNode = currentNodes.find((cn) => cn.id === id);
          if (flowNode) {
            moves.push({
              id,
              from,
              to: { x: flowNode.position.x, y: flowNode.position.y, parentId: flowNode.parentId },
            });
          }
        }
      }
      // Fold in the neighbours an Alt+drag carried along, so undoing the gesture
      // restores the whole cluster, not just the anchor. Neighbours keep their
      // parent (the neighbour drag never re-parents them).
      if (connected) {
        for (const [id, start] of connected.neighbors) {
          const fn = currentNodes.find((cn) => cn.id === id);
          if (!fn) continue;
          moves.push({
            id,
            from: { x: start.x, y: start.y, parentId: fn.parentId },
            to: { x: fn.position.x, y: fn.position.y, parentId: fn.parentId },
          });
        }
      }
      recordMove(moves);

      // Nothing to re-parent: either no non-group nodes dragged or no groups exist
      if (finalById.size === 0) return;

      setNodes((nds) => {
        const mapped = nds.map((n) => {
          const placed = finalById.get(n.id);
          if (!placed) return n;
          // Leave unchanged nodes as-is (preserving extent etc.); only rewrite a
          // node that actually moved parent or position.
          if (
            placed.parentId === n.parentId &&
            placed.position.x === n.position.x &&
            placed.position.y === n.position.y
          ) {
            return n;
          }
          return { ...n, parentId: placed.parentId, position: placed.position, extent: undefined };
        });

        // ReactFlow requires parent nodes before children in the array
        return reorderNodesForParentChild(mapped);
      });
    },
    [setNodes, onNodePositionChange, getFlowNodes, recordMove, activeFocusRootId]
  );

  // Right-click on empty background. A plain right-click opens the annotation
  // creation menu (add note/label/arrow); a right-drag is a pan (panOnDrag
  // includes button 2), so in that case keep the legacy clear-only behaviour.
  const onPaneContextMenu = useCallback(
    (event) => {
      event.preventDefault();
      event.stopPropagation();
      // The pane menu exists only to add notes, labels and arrows, and the focus
      // view sets annotations aside — one created here would be dropped by the
      // next reconcile pass with nothing to say so.
      if (activeFocusRootId) {
        closeAllMenus();
        return;
      }
      const start = rightDragStart.current;
      const movedFar =
        start.time != null &&
        (Math.abs(event.clientX - start.x) > 5 || Math.abs(event.clientY - start.y) > 5);
      rightDragStart.current = { x: 0, y: 0, time: null };
      if (movedFar) {
        closeAllMenus();
        clearSelection();
        return;
      }
      setNodeContextMenu(null);
      setMultiNodeContextMenu(null);
      setEdgeContextMenu(null);
      const flowPosition = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      setPaneContextMenu({ x: event.clientX, y: event.clientY, flowPosition });
    },
    [closeAllMenus, clearSelection, screenToFlowPosition, activeFocusRootId]
  );

  // Right-click on the selection box (multi-node selection)
  const onSelectionContextMenu = useCallback(
    (event) => {
      event.preventDefault();
      event.stopPropagation();

      if (selectedNodes.length > 0) {
        setNodeContextMenu(null);
        setEdgeContextMenu(null);
        setMultiNodeContextMenu({
          x: event.clientX,
          y: event.clientY,
          nodes: selectedNodes,
        });
      }
    },
    [selectedNodes]
  );

  const handleAddGroup = useCallback(() => {
    const wrapper = reactFlowWrapper.current;
    const rect = wrapper?.getBoundingClientRect();
    const centerX = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
    const centerY = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;

    const position = screenToFlowPosition({ x: centerX, y: centerY });

    const newGroupNode = {
      id: `group-${Date.now()}`,
      type: 'group',
      position,
      data: {
        label: 'New Group',
        description: 'Drag nodes here to group them',
        color: '#646cff',
      },
      style: { width: 300, height: 200 },
    };

    setNodes((nds) => reorderNodesForParentChild([...nds, newGroupNode]));

    if (onCreateGroup) {
      onCreateGroup(position, newGroupNode);
    }
  }, [screenToFlowPosition, setNodes, onCreateGroup]);

  // Save view: includes node positions and visible edges from ReactFlow state.
  //
  // Refused outright while the focus view is active. Every persistence path in
  // the host — the debounced autosave as well as the explicit toolbar button —
  // funnels through here and serializes whatever is mounted, which under focus
  // is a handful of nodes at ego-ring coordinates. Saving that would overwrite
  // the real layout with a temporary lens and drop every node not in the focus
  // set from the snapshot.
  const handleSaveView = useCallback(() => {
    if (!onSaveView) return;
    // While the focus view is active the mounted canvas is a temporary lens: a
    // handful of nodes at ego-ring coordinates, with the annotations set aside.
    // Saving that would overwrite the real layout. Saving the state focus was
    // entered from is both correct and, unlike refusing, keeps the round trip
    // the host waits on intact — App.switchToSession performs the entire switch
    // inside the callback that only fires once onSaveView has been called.
    const snapshot = activeFocusRootId ? preFocusRef.current : null;
    const viewNodes = snapshot ? snapshot.nodes : nodes;
    const viewEdges = snapshot ? snapshot.edges : edges;
    onSaveView({
      nodes: viewNodes.map((n) => ({ id: n.id, position: n.position, parentId: n.parentId })),
      edges: viewEdges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.label,
      })),
      groups: viewNodes
        .filter((n) => n.type === 'group')
        .map((g) => ({
          id: g.id,
          label: g.data.label,
          description: g.data.description,
          position: g.position,
          style: g.style,
          color: g.data.color,
        })),
      annotations: viewNodes.filter((n) => OVERLAY_TYPES.has(n.type)).map(flowNodeToOverlay),
    });
  }, [nodes, edges, onSaveView, activeFocusRootId]);

  const handleLoadMore = useCallback(() => {
    setLoadedNodeCount((prev) => Math.min(prev + 100, visibleNodes.length));
  }, [visibleNodes.length]);

  // Node context menu handler
  const onNodeContextMenu = useCallback(
    (event, node) => {
      event.preventDefault();
      event.stopPropagation();

      // Annotation overlays render their own context menu (colour/delete).
      if (OVERLAY_TYPES.has(node.type)) return;

      const isNodeSelected = selectedNodes.some((n) => n.id === node.id);
      const hasMultipleSelected = selectedNodes.length > 1;

      if (hasMultipleSelected && isNodeSelected) {
        setNodeContextMenu(null);
        setEdgeContextMenu(null);
        setMultiNodeContextMenu({
          x: event.clientX,
          y: event.clientY,
          nodes: selectedNodes,
        });
      } else {
        setMultiNodeContextMenu(null);
        setEdgeContextMenu(null);
        setNodeContextMenu({
          x: event.clientX,
          y: event.clientY,
          node: node,
        });
      }
    },
    [selectedNodes]
  );

  // Double-click on node handler
  const handleNodeDoubleClick = useCallback(
    (event, node) => {
      event.preventDefault();
      // Annotation overlays handle their own double-click (inline text editing).
      if (OVERLAY_TYPES.has(node.type)) return;
      if (onNodeDoubleClickCallback) {
        onNodeDoubleClickCallback(node.id, node.data);
      }
    },
    [onNodeDoubleClickCallback]
  );

  // Edge context menu handler
  const onEdgeContextMenu = useCallback((event, edge) => {
    event.preventDefault();
    event.stopPropagation();
    setNodeContextMenu(null);
    setMultiNodeContextMenu(null);
    setEdgeContextMenu({
      x: event.clientX,
      y: event.clientY,
      edge: edge,
    });
  }, []);

  // Reuses the same context-menu handlers a desktop right-click calls,
  // instead of duplicating their menu-opening logic, for a touch long-press.
  // Kept current via the ref above so the detector's fixed-identity timer
  // always calls the latest closures (fresh selection state, etc.).
  longPressFireRef.current = (payload) => {
    const syntheticEvent = {
      clientX: payload.x,
      clientY: payload.y,
      preventDefault: () => {},
      stopPropagation: () => {},
    };
    if (payload.kind === 'node') {
      const node = getFlowNodes().find((n) => n.id === payload.nodeId);
      if (node) onNodeContextMenu(syntheticEvent, node);
      return;
    }
    if (payload.kind === 'edge') {
      const edge = edges.find((e) => e.id === payload.edgeId);
      if (edge) onEdgeContextMenu(syntheticEvent, edge);
      return;
    }
    if (payload.kind === 'selection') {
      onSelectionContextMenu(syntheticEvent);
      return;
    }
    onPaneContextMenu(syntheticEvent);
  };

  // Touch: long-press on pane/node/edge/selection reaches the same actions a
  // desktop right-click reaches. A one-finger drag pans and marquee selection
  // is disabled instead (see selectionOnDrag/panOnDrag on <ReactFlow> below);
  // tap-to-select and double-tap keep using ReactFlow's own click/double-click
  // handling. Attached only in touch mode, so desktop mouse behaviour —
  // right-drag pans, left-drag marquee-selects — is untouched. Also suppressed
  // while freehand drawing mode is armed: a deliberately slow, still stroke
  // start can otherwise exceed the long-press delay and pop the pane context
  // menu mid-gesture.
  useEffect(() => {
    if (!isTouchMode || freehandActive) return undefined;
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return undefined;
    const detector = longPressDetectorRef.current;

    const resolveTouchTarget = (target) => {
      const nodeEl = target?.closest?.('.react-flow__node');
      if (nodeEl) return { kind: 'node', nodeId: nodeEl.getAttribute('data-id') };
      const edgeEl = target?.closest?.('.react-flow__edge');
      if (edgeEl) {
        const testId = edgeEl.getAttribute('data-testid') || '';
        const edgeId = testId.startsWith('rf__edge-') ? testId.slice('rf__edge-'.length) : null;
        return { kind: 'edge', edgeId };
      }
      const selectionEl = target?.closest?.('.react-flow__nodesselection-rect');
      if (selectionEl) return { kind: 'selection' };
      return { kind: 'pane' };
    };

    const handlePointerDown = (event) => {
      if (event.pointerType !== 'touch') return;
      const info = resolveTouchTarget(event.target);
      detector.onPointerDown(event.pointerId, event.clientX, event.clientY, {
        ...info,
        x: event.clientX,
        y: event.clientY,
      });
    };
    const handlePointerMove = (event) => {
      if (event.pointerType !== 'touch') return;
      detector.onPointerMove(event.pointerId, event.clientX, event.clientY);
    };
    const handlePointerUp = (event) => {
      if (event.pointerType !== 'touch') return;
      detector.onPointerUp(event.pointerId);
    };
    const handlePointerCancel = (event) => {
      if (event.pointerType !== 'touch') return;
      detector.onPointerCancel(event.pointerId);
    };

    wrapper.addEventListener('pointerdown', handlePointerDown);
    wrapper.addEventListener('pointermove', handlePointerMove);
    wrapper.addEventListener('pointerup', handlePointerUp);
    wrapper.addEventListener('pointercancel', handlePointerCancel);
    return () => {
      wrapper.removeEventListener('pointerdown', handlePointerDown);
      wrapper.removeEventListener('pointermove', handlePointerMove);
      wrapper.removeEventListener('pointerup', handlePointerUp);
      wrapper.removeEventListener('pointercancel', handlePointerCancel);
      detector.reset();
    };
  }, [isTouchMode, freehandActive]);

  // Freehand drawing mode's pointer-capture wiring: mirrors the long-press
  // effect above (listeners on the wrapper, not on ReactFlow's own pane, so
  // bubbling from any child — node, edge, pane background — reaches it the
  // same way), but for every pointer type, gated on `freehandActive` instead
  // of touch mode. `createFreehandStrokeCapture` (freehandStroke.js) already
  // tracks only the first ("primary") pointer of a stroke and ignores a
  // second one going down mid-stroke; this effect adds the surfaced guidance
  // for that case (remaining_scope item 4: "suppress concurrent touch input
  // while a pen stroke is active, with user-facing guidance").
  //
  // Coalesced samples (`getCoalescedEvents()`) are fed to the capture one at a
  // time when the browser provides them, so a fast stylus stroke is not
  // undersampled between animation frames — and only ever *actual* samples;
  // `getPredictedEvents()` is never called, matching
  // `persist_predicted_points: false`.
  //
  // Deliberately depends on nothing but `freehandActive`: every other value
  // the handlers below need (screenToFlowPosition, getViewport, showNotification,
  // the notification copy) is read through a ref at call time instead of
  // being closed over directly, so an unrelated re-render mid-stroke (e.g.
  // the "second pointer ignored" notification's own state update) can never
  // tear down and rebuild these listeners and silently abandon an
  // in-progress gesture the way including them in the dependency array
  // would.
  useEffect(() => {
    if (!freehandActive) return undefined;
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return undefined;
    const capture = freehandCaptureRef.current;

    const toModelPoint = (event) =>
      screenToFlowPositionRef.current({ x: event.clientX, y: event.clientY });

    const clearPreview = () => {
      freehandPreviewPathRef.current?.setAttribute('d', '');
    };

    const updatePreview = () => {
      const pathEl = freehandPreviewPathRef.current;
      if (!pathEl) return;
      const viewport = getViewportRef.current();
      const screenPoints = freehandModelPointsRef.current.map((p) => ({
        x: p.x * viewport.zoom + viewport.x,
        y: p.y * viewport.zoom + viewport.y,
      }));
      pathEl.setAttribute('d', pointsToPathData(screenPoints));
    };

    const abandonStroke = () => {
      freehandPrimaryPointerIdRef.current = null;
      freehandModelPointsRef.current = [];
      clearPreview();
    };

    // Single-shot: one completed (or cancelled) stroke disarms the tool, the
    // same way clicking any other toolbox item creates exactly one
    // annotation. Drawing a second stroke means clicking "Freehand" again.
    const finishStroke = () => {
      abandonStroke();
      setFreehandActive(false);
    };

    const handlePointerDown = (event) => {
      // A non-primary mouse button (right/middle click) is not a draw
      // gesture; leave it for the pane's own context-menu/pan handling.
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      if (freehandPrimaryPointerIdRef.current != null) {
        // A second pointer while a stroke is already in progress —
        // createFreehandStrokeCapture already ignores it structurally; this
        // only surfaces that to the user instead of a silent no-op.
        showNotificationRef.current('info', cmlRef.current.freehandConcurrentInputBlocked);
        return;
      }
      freehandPrimaryPointerIdRef.current = event.pointerId;
      freehandModelPointsRef.current = [toModelPoint(event)];
      capture.onPointerDown(event, toModelPoint);
      updatePreview();
    };

    const handlePointerMove = (event) => {
      if (event.pointerId !== freehandPrimaryPointerIdRef.current) return;
      const coalesced =
        typeof event.getCoalescedEvents === 'function' ? event.getCoalescedEvents() : null;
      const samples = coalesced && coalesced.length > 0 ? coalesced : [event];
      for (const sample of samples) {
        capture.onPointerMove(sample, toModelPoint);
        freehandModelPointsRef.current.push(toModelPoint(sample));
      }
      updatePreview();
    };

    const handlePointerUp = (event) => {
      if (event.pointerId !== freehandPrimaryPointerIdRef.current) return;
      capture.onPointerUp(event);
      finishStroke();
    };

    const handlePointerCancel = (event) => {
      if (event.pointerId !== freehandPrimaryPointerIdRef.current) return;
      capture.onPointerCancel(event);
      finishStroke();
    };

    wrapper.addEventListener('pointerdown', handlePointerDown);
    wrapper.addEventListener('pointermove', handlePointerMove);
    wrapper.addEventListener('pointerup', handlePointerUp);
    wrapper.addEventListener('pointercancel', handlePointerCancel);
    return () => {
      wrapper.removeEventListener('pointerdown', handlePointerDown);
      wrapper.removeEventListener('pointermove', handlePointerMove);
      wrapper.removeEventListener('pointerup', handlePointerUp);
      wrapper.removeEventListener('pointercancel', handlePointerCancel);
      // Deactivating mid-stroke (Escape, or the mode toggled off some other
      // way) abandons cleanly rather than leaving the capture wedged active
      // for whatever comes next.
      if (capture.isActive()) capture.reset();
      abandonStroke();
    };
  }, [freehandActive]);

  // Always prevent browser context menu on the canvas wrapper
  useEffect(() => {
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return;
    const handleNativeContextMenu = (e) => {
      e.preventDefault();
    };
    wrapper.addEventListener('contextmenu', handleNativeContextMenu);
    return () => wrapper.removeEventListener('contextmenu', handleNativeContextMenu);
  }, []);

  // Left-click on empty space clears selection (handles cases where onPaneClick doesn't fire,
  // e.g. when ReactFlow's selection overlay intercepts the click).
  // Track mousedown position to distinguish genuine clicks from drag-selects.
  useEffect(() => {
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return;
    const handleMouseDown = (e) => {
      if (e.button === 0) {
        mouseDownPos.current = { x: e.clientX, y: e.clientY };
      }
      // When shift/ctrl/meta-clicking nodes to multi-select, the browser would
      // otherwise extend a text selection into surrounding UI (session id,
      // search terms, labels), highlighting them blue. Suppress text selection
      // for the duration of the interaction and clear any stray selection.
      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        document.body.classList.add('graph-suppress-selection');
        const selection = window.getSelection?.();
        if (selection && selection.rangeCount > 0) {
          selection.removeAllRanges();
        }
      }
    };
    const handleMouseUp = () => {
      document.body.classList.remove('graph-suppress-selection');
    };
    const handleClick = (e) => {
      if (e.button !== 0) return;
      // If the mouse moved significantly between mousedown and click, this was a
      // drag operation (e.g. marquee select). Do not clear the selection.
      if (mouseDownPos.current) {
        const dx = e.clientX - mouseDownPos.current.x;
        const dy = e.clientY - mouseDownPos.current.y;
        if (dx * dx + dy * dy > 25) {
          mouseDownPos.current = null;
          return;
        }
      }
      mouseDownPos.current = null;
      // Don't clear selection when modifier keys are held (multi-select)
      if (e.ctrlKey || e.metaKey || e.shiftKey) return;
      const nodeEl = e.target.closest('.react-flow__node');
      const edgeEl = e.target.closest('.react-flow__edge');
      const menuEl =
        e.target.closest('.graph-context-menu') || e.target.closest('.graph-group-context-menu');
      const controlsEl = e.target.closest('.react-flow__controls');
      const minimapEl = e.target.closest('.react-flow__minimap');
      const selectionEl = e.target.closest('.react-flow__selection');
      if (!nodeEl && !edgeEl && !menuEl && !controlsEl && !minimapEl && !selectionEl) {
        clearSelection();
        closeAllMenus();
      }
    };
    wrapper.addEventListener('mousedown', handleMouseDown);
    wrapper.addEventListener('click', handleClick);
    // Listen for mouseup on the document so the suppression is lifted even when
    // the pointer is released outside the canvas wrapper.
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      wrapper.removeEventListener('mousedown', handleMouseDown);
      wrapper.removeEventListener('click', handleClick);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.classList.remove('graph-suppress-selection');
    };
  }, [clearSelection, closeAllMenus]);

  // Handle external drag-and-drop (from toolbar)
  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event) => {
      event.preventDefault();

      // An OS file drag (an image dragged in from the desktop or another
      // app/tab) carries files but never the toolbar's custom nodetype MIME
      // type, so it is distinguished up front rather than falling through to
      // the nodeType branch below and being silently ignored.
      const droppedFile = event.dataTransfer.files && event.dataTransfer.files[0];
      if (droppedFile && droppedFile.type?.startsWith('image/')) {
        const dropPosition = screenToFlowPosition({ x: event.clientX, y: event.clientY });
        ingestImageFile(droppedFile, dropPosition);
        return;
      }

      const nodeType = event.dataTransfer.getData('application/reactflow-nodetype');
      if (!nodeType) return;

      const position = screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      // Handle Group drop directly in GraphCanvas
      if (nodeType === 'Group') {
        const newGroupNode = {
          id: `group-${Date.now()}`,
          type: 'group',
          position,
          data: {
            label: 'New Group',
            description: 'Drag nodes here to group them',
            color: '#646cff',
          },
          style: { width: 300, height: 200 },
        };
        setNodes((nds) => reorderNodesForParentChild([...nds, newGroupNode]));
        if (onCreateGroup) {
          onCreateGroup(position, newGroupNode);
        }
        return;
      }

      if (onDropCreateNode) {
        onDropCreateNode(nodeType, position);
      }
    },
    [screenToFlowPosition, onDropCreateNode, setNodes, onCreateGroup, ingestImageFile]
  );

  // Delete/Backspace hides selected nodes/edges, Escape clears selection
  useEffect(() => {
    const cancelOrganizePending = () => {
      organizePendingRef.current = false;
      if (organizeTimerRef.current) {
        clearTimeout(organizeTimerRef.current);
        organizeTimerRef.current = null;
      }
    };
    const handleKeyDown = (e) => {
      const tag = e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

      // Undo/redo of node-position moves. Ctrl/Cmd+Z undoes; Ctrl/Cmd+Shift+Z
      // and Ctrl/Cmd+Y redo (covering both the mac and Windows conventions).
      if ((e.ctrlKey || e.metaKey) && !e.altKey) {
        const key = e.key?.toLowerCase();
        if (key === 'z') {
          e.preventDefault();
          if (e.shiftKey) handleRedo();
          else handleUndo();
          return;
        }
        if (key === 'y') {
          e.preventDefault();
          handleRedo();
          return;
        }
      }

      // Second step of the organize chord: a pending Ctrl/Cmd+O is resolved by
      // the next c/h/v/t keystroke.
      if (organizePendingRef.current && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const mode = { a: 'tidy', c: 'cluster', h: 'horizontal', v: 'vertical', t: 'tree' }[
          e.key?.toLowerCase()
        ];
        if (mode) {
          e.preventDefault();
          cancelOrganizePending();
          organizeSelection(mode);
          return;
        }
        if (e.key === 'Escape') cancelOrganizePending();
      }

      // First step of the organize chord: Ctrl/Cmd+O arms it when a
      // multi-selection of graph nodes exists.
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key?.toLowerCase() === 'o') {
        const selectable = selectedNodes.filter((n) => !ANNOTATION_TYPES.has(n.type));
        if (selectable.length >= 2) {
          e.preventDefault();
          organizePendingRef.current = true;
          showNotification('info', cml.organizeHint);
          if (organizeTimerRef.current) clearTimeout(organizeTimerRef.current);
          organizeTimerRef.current = setTimeout(() => {
            organizePendingRef.current = false;
          }, 3000);
          return;
        }
      }

      if (e.key === 'Escape') {
        if (freehandActive) {
          setFreehandActive(false);
          return;
        }
        closeAllMenus();
        clearSelection();
        return;
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (selectedEdges.length > 0 && onHideEdge) {
          e.preventDefault();
          selectedEdges.forEach((edge) => onHideEdge(edge.id));
        }

        if (selectedNodes.length > 0) {
          e.preventDefault();
          // Delete removes overlay annotations — every kind in OVERLAY_TYPES,
          // which is all eleven v1 kinds except `group` — from the canvas;
          // graph nodes are hidden, not deleted. Excluding `group` leaves it to
          // its own context menu, so its children stay correctly parented.
          // Two kinds are skipped: one held by another client's live selection
          // claim (leases are exclusive — task-annotation-shared-session-realtime)
          // and one that is locked, which stays selectable but offers only
          // unlock or copy — the rule every *overlay* annotation's context menu
          // applies. `group`'s menu ignores that flag, but the exclusion above
          // means Delete never reaches a group to begin with.
          const deletableOverlays = selectedNodes.filter(
            (n) => OVERLAY_TYPES.has(n.type) && !isRemoteLocked(n.data) && !n.data?.locked
          );
          const overlayIds = deletableOverlays.map((n) => n.id);
          const skippedLocked = selectedNodes.some(
            (n) => OVERLAY_TYPES.has(n.type) && isRemoteLocked(n.data)
          );
          const skippedOwnLocked = selectedNodes.some(
            (n) => OVERLAY_TYPES.has(n.type) && n.data?.locked
          );
          const graphNodeIds = selectedNodes
            .filter((n) => !ANNOTATION_TYPES.has(n.type))
            .map((n) => n.id);
          if (overlayIds.length > 0) {
            const removeSet = new Set(overlayIds);
            setNodes((nds) => nds.filter((n) => !removeSet.has(n.id)));
            onAnnotationChangeRef.current?.('delete');
          }
          // A remote claim wins the notice when the selection mixes both: it is
          // the one the user cannot resolve alone.
          if (skippedLocked) showNotification('info', cml.annotationRemoteLocked);
          else if (skippedOwnLocked) showNotification('info', cml.annotationLockedSkipped);
          if (graphNodeIds.length > 0) {
            if (onHideMultiple) {
              onHideMultiple(graphNodeIds);
            } else if (onHide) {
              graphNodeIds.forEach((id) => onHide(id));
            }
          }
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      cancelOrganizePending();
    };
  }, [
    selectedNodes,
    selectedEdges,
    onHideMultiple,
    onHide,
    onHideEdge,
    closeAllMenus,
    clearSelection,
    organizeSelection,
    showNotification,
    cml.organizeHint,
    cml.annotationRemoteLocked,
    cml.annotationLockedSkipped,
    handleUndo,
    handleRedo,
    setNodes,
    freehandActive,
  ]);

  // Create group when signal changes (triggered from toolbar)
  useEffect(() => {
    if (createGroupSignal > 0 && !activeFocusRootId) {
      handleAddGroup();
    }
  }, [createGroupSignal, handleAddGroup, activeFocusRootId]);

  // Save view when signal changes (triggered from toolbar)
  useEffect(() => {
    if (saveViewSignal > 0) {
      handleSaveView();
    }
  }, [saveViewSignal, handleSaveView]);

  // Close open context menus when signal changes (triggered by search box / chat input focus)
  useEffect(() => {
    if (closeMenusSignal > 0) {
      closeAllMenus();
    }
  }, [closeMenusSignal, closeAllMenus]);

  // Restore groups from a saved view
  useEffect(() => {
    // Support both legacy array format and new object format with parentIds
    const groups = Array.isArray(groupsToRestore) ? groupsToRestore : groupsToRestore?.groups;
    const parentIds = Array.isArray(groupsToRestore) ? {} : groupsToRestore?.parentIds || {};

    if (groups && groups.length > 0) {
      const groupNodes = groups.map((g) => ({
        id: g.id,
        type: 'group',
        position: g.position,
        data: {
          label: g.label || 'Group',
          description: g.description || '',
          color: g.color || '#646cff',
        },
        style: g.style || { width: 300, height: 200 },
      }));
      const groupIdSet = new Set(groups.map((g) => g.id));
      setNodes((nds) => {
        const nonGroups = nds
          .filter((n) => n.type !== 'group' && !n.id.startsWith('group-'))
          .map((n) => {
            const savedParent = parentIds[n.id];
            if (savedParent && groupIdSet.has(savedParent)) {
              return { ...n, parentId: savedParent };
            }
            return n;
          });
        return reorderNodesForParentChild([...nonGroups, ...groupNodes]);
      });
      onGroupsRestored?.();
    }
  }, [groupsToRestore, setNodes, onGroupsRestored]);

  // Restore free-floating annotations (note/label/arrow) from a loaded session.
  useEffect(() => {
    if (!annotationsToRestore || annotationsToRestore.length === 0) return;
    const overlayNodes = annotationsToRestore
      .filter((a) => OVERLAY_TYPES.has(a?.kind))
      .map(overlayToFlowNode);
    if (overlayNodes.length === 0) {
      onAnnotationsRestored?.();
      return;
    }
    const restoreIds = new Set(overlayNodes.map((n) => n.id));
    setNodes((nds) =>
      reorderNodesForParentChild([...nds.filter((n) => !restoreIds.has(n.id)), ...overlayNodes])
    );
    onAnnotationsRestored?.();
  }, [annotationsToRestore, setNodes, onAnnotationsRestored]);

  // Keep arrow/line endpoints anchored to a node/annotation glued to that
  // target's centre as it moves or resizes. The stored anchor id is never
  // cleared here — a target that leaves the view (filtered, collapsed, not yet
  // loaded, or deleted) is indistinguishable from this vantage, so the anchor is
  // preserved and re-glues if the target returns. While the target is absent the
  // arrow is not held and becomes draggable again (so a deleted target can't
  // strand it). resolveAnchoredArrow returns null when geometry already matches,
  // keeping this idempotent and loop-free despite depending on `nodes`.
  useEffect(() => {
    const centers = new Map();
    const existing = new Set();
    let hasAnchored = false;
    for (const n of nodes) {
      if (n.type === 'arrow') {
        if (n.data?.startAnchor || n.data?.endAnchor) hasAnchored = true;
        continue;
      }
      existing.add(n.id);
      const c = nodeCenter(n);
      if (c) centers.set(n.id, c);
    }
    if (!hasAnchored) return;
    const updates = new Map();
    let geometryChanged = false;
    for (const n of nodes) {
      if (n.type !== 'arrow') continue;
      if (!n.data?.startAnchor && !n.data?.endAnchor) continue;
      const resolved = resolveAnchoredArrow(n, centers);
      // Folds in the same remote-claim exclusivity the selection-claim effect
      // applies, so this effect (which runs on every `nodes` change, far more
      // often) never resets `draggable` back to true out from under a claim
      // another client is holding.
      const desiredDraggable =
        !n.data?.locked && !isArrowHeld(n.data, existing) && !isRemoteLocked(n.data);
      if (resolved || n.draggable !== desiredDraggable) {
        updates.set(n.id, { resolved, desiredDraggable });
        if (resolved) geometryChanged = true;
      }
    }
    if (updates.size === 0) return;
    setNodes((nds) =>
      nds.map((n) => {
        const u = updates.get(n.id);
        if (!u) return n;
        let next = n;
        if (u.resolved) {
          next = {
            ...next,
            position: u.resolved.position,
            data: { ...next.data, dx: u.resolved.dx, dy: u.resolved.dy },
          };
        }
        if (next.draggable !== u.desiredDraggable) {
          next = { ...next, draggable: u.desiredDraggable };
        }
        return next;
      })
    );
    // Only geometry is persisted; a draggable-only flip needs no save.
    if (geometryChanged) onAnnotationChangeRef.current?.('geometry');
  }, [nodes, setNodes]);

  // Keep an attached label/text/icon/vote_dot glued to its attachment
  // target's centre (plus the offset captured when it (re)attached) as the
  // target moves — the same follow contract as the arrow anchor effect above,
  // but for the generic `content.attachment` binding instead of an arrow
  // endpoint. Unlike an anchored arrow, an attached overlay stays draggable
  // (the contract's "free fine adjustment"), so a node currently mid-drag is
  // skipped here — ReactFlow marks it `dragging: true` on every position
  // change event, and repositioning it from this effect on the same render
  // would fight the user's own drag. `resolveAttachedPosition` returns null
  // once the target is absent, so a removed/hidden target leaves the overlay
  // at its last resolved position rather than resetting it (contract:
  // "detaches and keeps its last resolved model-space geometry").
  useEffect(() => {
    const centers = new Map();
    let hasAttached = false;
    for (const n of nodes) {
      if (n.type === 'arrow') continue;
      const c = nodeCenter(n);
      if (c) centers.set(n.id, c);
      if (ATTACHABLE_OVERLAY_KINDS.has(n.type) && n.data?.attachment) hasAttached = true;
    }
    if (!hasAttached) return;
    const updates = new Map();
    for (const n of nodes) {
      if (!ATTACHABLE_OVERLAY_KINDS.has(n.type) || !n.data?.attachment || n.dragging) continue;
      const nextPosition = resolveAttachedPosition(n, centers);
      if (nextPosition) updates.set(n.id, nextPosition);
    }
    if (updates.size === 0) return;
    setNodes((nds) =>
      nds.map((n) => (updates.has(n.id) ? { ...n, position: updates.get(n.id) } : n))
    );
    onAnnotationChangeRef.current?.('geometry');
  }, [nodes, setNodes]);

  // Mirror live remote selection claims onto annotation nodes (design 3.5,
  // extended to annotations — task-annotation-shared-session-realtime). Graph
  // ('custom') nodes get their `remoteSelection` marker recomputed fresh every
  // render, sourced from the host's `inputNodes` prop (see the
  // `reactFlowNodes` memo above); annotation nodes live only in ReactFlow's own
  // node state instead, so the same live claim needs an effect to push it in.
  // A claim held by another client also blocks local dragging here —
  // annotation leases are exclusive, not merely advisory, unlike the
  // pre-existing graph-node selection markers, which are visual-only.
  useEffect(() => {
    setNodes((nds) =>
      nds.map((n) => {
        if (!ANNOTATION_TYPES.has(n.type)) return n;
        const marker = remoteSelections?.[n.id] ?? null;
        if (!marker && !n.data?.remoteSelection) return n;
        const nextData = { ...n.data, remoteSelection: marker };
        return { ...n, data: nextData, draggable: isAnnotationDraggable({ ...n, data: nextData }) };
      })
    );
  }, [remoteSelections, setNodes]);

  // Apply node positions arriving from another client (design step 6), holding
  // positions for not-yet-mounted nodes until they appear.
  useRemotePositions({ remotePositions, onRemotePositionsApplied, nodes, setNodes });

  // Tween an MCP-initiated batch layout (contract §9–§10) into place, honouring
  // reduced motion and never disturbing a node the user is dragging.
  useAnimatedLayout({
    animatedLayout,
    onAnimatedLayoutApplied,
    onAgentArrangingChange: setAgentArranging,
    setNodes,
    getNodes: getFlowNodes,
    resetKey: animatedLayoutResetKey,
  });

  // Apply a queue of group/overlay annotation changes from other clients (design
  // step 6): upsert or delete annotation nodes and reassign group membership.
  // A queue (not a single op) so a burst arriving in one render is not coalesced.
  useEffect(() => {
    if (!remoteAnnotationOps || remoteAnnotationOps.length === 0) return;
    for (const op of remoteAnnotationOps) {
      if (op.action === 'upsert-overlay' && op.overlay) {
        const flowNode = overlayToFlowNode(op.overlay);
        setNodes((nds) =>
          reorderNodesForParentChild([...nds.filter((n) => n.id !== flowNode.id), flowNode])
        );
      } else if (op.action === 'upsert-group' && op.group) {
        const g = op.group;
        const groupNode = {
          id: g.id,
          type: 'group',
          position: g.position || { x: 0, y: 0 },
          data: {
            label: g.label || 'Group',
            description: g.description || '',
            color: g.color || '#646cff',
          },
          style: g.style || { width: 300, height: 200 },
        };
        const members = new Set(op.members || []);
        setNodes((nds) =>
          reorderNodesForParentChild(
            [...nds.filter((n) => n.id !== g.id), groupNode].map((n) => {
              if (n.type === 'group') return n;
              if (members.has(n.id)) return { ...n, parentId: g.id };
              if (n.parentId === g.id) return { ...n, parentId: undefined };
              return n;
            })
          )
        );
      } else if (op.action === 'delete' && op.id) {
        setNodes((nds) =>
          nds
            .map((n) => (n.parentId === op.id ? { ...n, parentId: undefined } : n))
            .filter((n) => n.id !== op.id)
        );
      } else if (op.action === 'membership' && op.groupId) {
        const members = new Set(op.members || []);
        setNodes((nds) =>
          reorderNodesForParentChild(
            nds.map((n) => {
              if (n.type === 'group') return n;
              if (members.has(n.id)) return { ...n, parentId: op.groupId };
              if (n.parentId === op.groupId) return { ...n, parentId: undefined };
              return n;
            })
          )
        );
      }
    }
    onRemoteAnnotationsApplied?.();
  }, [remoteAnnotationOps, setNodes, onRemoteAnnotationsApplied]);

  // Focus on a specific node when focusNodeId changes
  // Report initial viewport after fitView animation settles
  useEffect(() => {
    if (!onViewportChange) return;
    const timer = setTimeout(() => onViewportChange(getViewport()), 900);
    return () => clearTimeout(timer);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!focusNodeId) return;
    const targetNode = nodes.find((n) => n.id === focusNodeId);
    if (targetNode && targetNode.position) {
      setCenter(targetNode.position.x + 100, targetNode.position.y + 40, {
        zoom: 1.2,
        duration: 800,
      });
    }
    const timer = setTimeout(() => {
      onFocusComplete?.();
    }, 900);
    return () => clearTimeout(timer);
  }, [focusNodeId, nodes, setCenter, onFocusComplete]);

  const nodeTypes = useMemo(
    () => ({
      custom: CustomNode,
      group: GroupNode,
      note: NoteNode,
      label: LabelNode,
      arrow: ArrowNode,
      text: GenericAnnotationNode,
      frame: GenericAnnotationNode,
      shape: GenericAnnotationNode,
      icon: GenericAnnotationNode,
      vote_dot: GenericAnnotationNode,
      image: GenericAnnotationNode,
      freehand: FreehandAnnotationNode,
    }),
    []
  );

  const marksLegend = useMemo(() => {
    const seen = new Map();
    for (const mark of Object.values(nodeMarks)) {
      const key = `${mark.color}::${mark.label || ''}`;
      if (!seen.has(key)) {
        seen.set(key, { color: mark.color, label: mark.label || '' });
      }
    }
    return Array.from(seen.values());
  }, [nodeMarks]);

  const edgeTypes = useMemo(
    () => ({
      floating: SimpleFloatingEdge,
    }),
    []
  );

  return (
    <AnnotationContext.Provider value={annotationContextValue}>
      <div className="graph-canvas-container">
        {inputNodes.length > 0 &&
          visibleNodes.length > LAZY_LOAD_THRESHOLD &&
          loadedNodeCount < visibleNodes.length && (
            <div className="graph-canvas-controls">
              <div className="graph-lazy-load-panel">
                <div className="graph-lazy-load-info">
                  {lazyLoadShowingLabel
                    .replace('{loaded}', loadedNodeCount)
                    .replace('{total}', visibleNodes.length)}
                  <button className="graph-load-more-button" onClick={handleLoadMore}>
                    {lazyLoadMoreLabel}
                  </button>
                </div>
                {hiddenConnectionCount > 0 && (
                  <div className="graph-lazy-load-hidden-connections">
                    {lazyLoadHiddenConnectionsLabel.replace('{count}', hiddenConnectionCount)}
                  </div>
                )}
              </div>
            </div>
          )}

        <div
          ref={reactFlowWrapper}
          style={{
            width: '100%',
            height: '100%',
            // Stop the browser turning a slow stylus/touch stroke into a
            // scroll/pinch gesture mid-draw, which would otherwise fire
            // pointercancel and abandon the stroke unpredictably.
            touchAction: freehandActive ? 'none' : undefined,
          }}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeDragStart={onNodeDragStart}
            onNodeDrag={onNodeDrag}
            onNodeDragStop={onNodeDragStop}
            onPaneContextMenu={onPaneContextMenu}
            onNodeContextMenu={onNodeContextMenu}
            onEdgeContextMenu={onEdgeContextMenu}
            onSelectionContextMenu={onSelectionContextMenu}
            onNodeDoubleClick={handleNodeDoubleClick}
            onPaneClick={handlePaneClick}
            onDragOver={onDragOver}
            onDrop={onDrop}
            onPaneMouseDown={(event) => {
              if (event.button === 2) {
                rightDragStart.current = {
                  x: event.clientX,
                  y: event.clientY,
                  time: Date.now(),
                };
              }
            }}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            fitViewOptions={{ padding: fitPadding, duration: 800 }}
            minZoom={0.1}
            maxZoom={2}
            defaultEdgeOptions={{ animated: true, style: { strokeWidth: 2 } }}
            panOnDrag={freehandActive ? false : isTouchMode ? true : [0, 2]}
            selectionOnDrag={freehandActive ? false : !isTouchMode}
            selectionMode={SelectionMode.Partial}
            selectNodesOnDrag={true}
            nodesDraggable={!freehandActive}
            deleteKeyCode={null}
            multiSelectionKeyCode={['Shift', 'Meta', 'Control']}
            edgesUpdatable={false}
            onMoveStart={closeAllMenus}
            onMove={onViewportChange ? (_event, vp) => onViewportChange(vp) : undefined}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#333" gap={16} />
            {!isCompact && <Controls />}
            {showMinimap && !isCompact && (
              <MiniMap
                nodeColor={(node) => node.data?.color || '#9CA3AF'}
                maskColor="rgba(0, 0, 0, 0.5)"
                position="bottom-right"
                pannable
                zoomable
              />
            )}
          </ReactFlow>

          {agentArranging && (
            <div className="graph-agent-arranging" role="status" aria-live="polite">
              <span className="graph-agent-arranging-dot" />
              <span className="graph-agent-arranging-label">{agentArrangingLabel}</span>
            </div>
          )}

          {marksLegend.length > 0 && (
            <div className="graph-marks-legend">
              {marksLegend.map((entry, i) => (
                <div key={i} className="graph-marks-legend-entry">
                  <span
                    className="graph-marks-legend-dot"
                    style={{ backgroundColor: entry.color }}
                  />
                  {entry.label && <span className="graph-marks-legend-label">{entry.label}</span>}
                </div>
              ))}
            </div>
          )}

          {isCompact && (
            <div className="graph-compact-controls" role="group" aria-label={compactControlsLabel}>
              <button
                type="button"
                className="graph-compact-control"
                onClick={() => zoomIn({ duration: 200 })}
                aria-label={compactZoomInLabel}
                title={compactZoomInLabel}
              >
                +
              </button>
              <button
                type="button"
                className="graph-compact-control"
                onClick={() => zoomOut({ duration: 200 })}
                aria-label={compactZoomOutLabel}
                title={compactZoomOutLabel}
              >
                −
              </button>
              <button
                type="button"
                className="graph-compact-control"
                onClick={() => fitView({ padding: fitPadding, duration: 400 })}
                aria-label={compactFitViewLabel}
                title={compactFitViewLabel}
              >
                ⤢
              </button>
              {activeFocusRootId ? (
                <button
                  type="button"
                  className="graph-compact-control graph-compact-control-focus active"
                  onClick={exitFocusView}
                  aria-pressed={true}
                  aria-label={exitFocusViewLabel}
                  title={exitFocusViewLabel}
                >
                  ◎
                </button>
              ) : (
                <button
                  type="button"
                  className="graph-compact-control graph-compact-control-focus"
                  onClick={() => enterFocusView(focusCandidateId)}
                  disabled={!focusCandidateId}
                  aria-pressed={false}
                  aria-label={focusViewLabel}
                  title={focusViewLabel}
                >
                  ◎
                </button>
              )}
            </div>
          )}

          {depthLevels.length > 1 && (
            <div className="federation-depth-control" aria-label="Federated search depth selector">
              <span className="federation-depth-label" title={federationDepthTooltip}>
                {federationDepthLabel}
              </span>
              <div
                className="federation-depth-levels"
                role="group"
                aria-label="Federation depth levels"
              >
                {depthLevels.map((level) => {
                  const isActive = level === federationDepth;
                  return (
                    <button
                      key={level}
                      type="button"
                      className={`federation-depth-level${isActive ? ' active' : ''}`}
                      onClick={() => onFederationDepthChange && onFederationDepthChange(level)}
                      aria-pressed={isActive}
                      title={`Search depth ${level}`}
                    >
                      {level}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Focus view sets annotations aside (see onPaneContextMenu above),
              so an annotation created here would be silently dropped by the
              next reconcile - hide the toolbox rather than let it create
              something that disappears. */}
          {!activeFocusRootId && (
            <AnnotationToolbox
              onCreate={(kind, options) => createAnnotationAtViewportCenter(kind, options)}
              labels={atl}
              compact={isCompact}
              activeKind={freehandActive ? 'freehand' : null}
            />
          )}
          {/* Backs the toolbox's image item and is otherwise invisible; the
              file-upload half of image ingest (paste is a document-level
              listener above). Kept mounted unconditionally so its ref is
              stable across focus-view toggling. */}
          <input
            ref={imageFileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            onChange={handleImageFileSelected}
            style={{ display: 'none' }}
            data-testid="graph-image-file-input"
          />
          {freehandActive && (
            <>
              {/* Live in-progress stroke preview, drawn imperatively (see the
                  pointer-capture effect's updatePreview) rather than through
                  React state, so a fast pointermove burst never re-renders
                  the whole canvas — only this path's `d` attribute changes.
                  Screen-space, not model-space: positioned to match the
                  current pan/zoom directly rather than living inside
                  ReactFlow's own transformed pane. pointer-events: none so it
                  never itself intercepts the drawing gesture; the wrapper's
                  own listeners (bubbling up from underneath) do that. */}
              <svg
                className="graph-freehand-preview-overlay"
                data-testid="freehand-preview-overlay"
              >
                <path
                  ref={freehandPreviewPathRef}
                  stroke={DEFAULT_FREEHAND_COLOR}
                  strokeWidth={DEFAULT_FREEHAND_STROKE_WIDTH}
                  fill="none"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <div className="graph-freehand-drawing-hint" role="status" aria-live="polite">
                {cml.freehandDrawingHint}
              </div>
            </>
          )}
        </div>

        <NodeContextMenu
          menu={nodeContextMenu}
          labels={cml}
          schema={schema}
          onEdit={onEdit}
          onHide={onHide}
          onExpand={onExpand}
          onDelete={onDelete}
          onContextMenuAction={onContextMenuAction}
          selectNodesByType={selectNodesByType}
          onSelectRelated={selectRelatedNodes}
          onViewHistory={onViewNodeHistory}
          onClose={() => setNodeContextMenu(null)}
        />

        <MultiNodeContextMenu
          menu={multiNodeContextMenu}
          labels={cml}
          onShowOnly={onShowOnly}
          onHide={onHide}
          onHideMultiple={onHideMultiple}
          onDelete={onDelete}
          onDeleteMultiple={onDeleteMultiple}
          selectNodesByType={selectNodesByType}
          onOrganize={organizeSelection}
          onClose={() => setMultiNodeContextMenu(null)}
        />

        <EdgeContextMenu
          menu={edgeContextMenu}
          labels={cml}
          relationshipTypes={edgeContextRelationshipTypes}
          onSetEdgeType={onSetEdgeType}
          onEditEdge={onEditEdge}
          onHideEdge={onHideEdge}
          onDeleteEdge={onDeleteEdge}
          onClose={() => setEdgeContextMenu(null)}
        />

        <PaneContextMenu
          menu={paneContextMenu}
          labels={cml}
          menuRef={paneMenuRef}
          createAnnotation={createAnnotation}
        />

        {notification && (
          <div className={`graph-notification graph-notification-${notification.type}`}>
            <span>{notification.type === 'success' ? '✓' : 'ℹ'}</span>
            <span>{notification.message}</span>
            <button onClick={() => setNotification(null)}>×</button>
          </div>
        )}
      </div>
    </AnnotationContext.Provider>
  );
}

/**
 * GraphCanvas with ReactFlowProvider wrapper
 */
export default function GraphCanvas(props) {
  return (
    <ReactFlowProvider>
      <GraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
