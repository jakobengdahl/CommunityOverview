import { useCallback, useMemo, useEffect, useState, useRef } from 'react';
import { createPortal } from 'react-dom';
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
import GenericAnnotationNode, {
  newShapeSize,
  regularShapeSize,
  regularShapeAspect,
} from './GenericAnnotationNode';
import AnnotationErrorBoundary from './AnnotationErrorBoundary';
import AnnotationToolbox from './AnnotationToolbox';
import FreehandAnnotationNode, { DEFAULT_FREEHAND_COLOR } from './FreehandAnnotationNode';
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
  alignNodes,
  distributeNodes,
} from '../utils/graphLayout';
import {
  getNodeColor,
  LAZY_LOAD_THRESHOLD,
  INITIAL_LOAD_COUNT,
  resolveEdgeVisuals,
  resolveEdgeOpacity,
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
  nodeSize,
  resolveAnchoredArrow,
  computeDroppedAttachment,
  computeAttachmentToTarget,
  isEligibleAttachTarget,
  computeAnnotationAriaLabel,
  nodesAtPoint,
  resolveAttachedPosition,
  ICON_INTRINSIC_SIZE,
  VOTE_DOT_INTRINSIC_SIZE,
  NEARBY_ATTACH_OFFSET,
} from '../utils/annotations';
import { DEFAULT_ANNOTATION_ICON } from '../utils/annotationIcons';
import { defaultAnnotationZ } from '../utils/annotationModel';
import {
  directNeighborIds,
  neighborStartPositions,
  neighborDragPositions,
} from '../utils/dragConnected';
import { createLongPressDetector } from '../utils/longPress';
import { createFreehandStrokeCapture } from '../utils/freehandStroke';
import { pointsToPathData, buildPressureSegments, hasPressureData } from '../utils/freehandPath';

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
// property editor (docs/ANNOTATION_CONTRACT.md's freehand row). The colour is
// imported rather than restated: this file used to hold its own copy, and
// because a GUI-drawn stroke is always written WITH an explicit colour, that
// copy — not the node's fallback — was the value every drawn stroke actually
// got. Two constants for one default is what let the invisible near-white
// survive a fix aimed at the fallback.
// A stylus's inverted tip. Chromium reports it as its own pointer type; other
// stacks report a normal `pen` and flag the eraser in the button bits (button
// 5 / buttons bit 0x20, per the Pointer Events spec). Shared by the erase
// gesture and the placement gesture — placement has to exclude EXACTLY what
// erase accepts, or on the button-bits stacks (the Surface pen case this
// exists for) flipping the pen over would erase along the stroke and commit a
// new object on release.
function isEraserTipEvent(event) {
  if (event.pointerType === 'eraser') return true;
  return event.pointerType === 'pen' && (event.button === 5 || (event.buttons & 32) !== 0);
}

// How far the pointer must travel before the eraser will act again. See
// eraseAt: without it a sweep cascades down a stack of objects at one spot.
const ERASE_RESTEP_PX = 8;

// GenericAnnotationNode's own MIN_SIZE. A drawn box may never come out under
// it, before or after a subtype's proportion is applied.
const MIN_ANNOTATION_SIZE = 40;

// How far the erased-footprint gate may extend from the pointer. Bounds the
// measured rect so a large shape cannot black out the rest of the stroke.
const ERASE_BLOCK_MAX_PX = 96;

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
 *
 * Within that groups-first bucket, groups are further ordered against EACH
 * OTHER by their own `data.z` (ascending — a lower z paints earlier, i.e.
 * further back among group backgrounds), never against any other bucket:
 * every group still precedes every non-group node regardless of z, so this
 * sort can only change which group backdrop sits closest to the front of
 * the group layer, not whether a group sits behind regular content
 * (dec-annotation-group-background-layering; see utils/groupLayers.js for
 * the arithmetic behind the layer-row click that writes this z, and its own
 * docstring for why a group's z is a separate space from an overlay's
 * CSS-facing `zIndex`). The sort is stable on ties via the explicit index
 * tie-break below (not relying on Array.prototype.sort's own stability),
 * so the overwhelmingly common case — every group still at the shared
 * default z of 0, nobody having used the new control — keeps exactly the
 * relative order this function already produced before this sort existed.
 */
export function reorderNodesForParentChild(nodes) {
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

  const orderedGroups = groups
    .map((n, index) => ({ n, index, z: Number.isFinite(n.data?.z) ? n.data.z : 0 }))
    .sort((a, b) => a.z - b.z || a.index - b.index)
    .map(({ n }) => n);

  return [...orderedGroups, ...nonGroupWithoutParent, ...withParent];
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
  // Session-local focus (task-session-focus-dimming-controls): dimmed ids
  // stay on the canvas (unlike hidden*) but render at reduced prominence.
  // edgeIntensity is the session's global baseline opacity for every
  // non-dimmed edge (1 = full prominence); a dimmed edge always renders
  // below that baseline. Never a graph edit — visualization-session state.
  dimmedNodeIds = [],
  dimmedEdgeIds = [],
  edgeIntensity = 1,
  nodeMarks = {},
  pulsedNodeIds = {},
  clearGroupsFlag = false,
  onExpand,
  onEdit,
  onViewNodeHistory,
  onDelete,
  onHide,
  onHideMultiple,
  onHideEdge,
  onDeleteEdge,
  // Bulk dim/restore primitives (arrays of node/edge ids). A single-object
  // context-menu action passes a one-element array; a multi-selection or an
  // incident-edges action passes the whole set in one call.
  onDimNodes,
  onRestoreNodes,
  onDimEdges,
  onRestoreEdges,
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
  remoteLeases = null,
  onBeginEditing,
  onEndEditing,
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
  // The mobile shared-surface integration point (task-annotation-responsive-
  // bottom-toolbox): when set, the compact/mobile toolbox below is portaled
  // into this host-owned DOM node (typically a BottomSheet's content area)
  // instead of rendering as its own fixed-position element, so annotation
  // creation joins the same "at most one bottom surface open" system as
  // search/create/chat/menu rather than floating unmanaged above them. Null
  // (the default) means "no mobile portal wiring" - compact mode then falls
  // back to the pre-integration always-on compact strip, rendered inline
  // here, so a host that has not opted into the shared-surface system (e.g.
  // the standalone widget embed, which has no BottomSheet to portal into)
  // still gets a working annotation toolbox rather than losing it silently.
  // Desktop (isCompact false) never consults this prop.
  annotationToolboxPortalContainer = null,
  // The EDIT-time counterpart of annotationToolboxPortalContainer above
  // (task-annotation-responsive-bottom-toolbox): the contextual "Edit"
  // surface for an already-selected annotation portals its property editor
  // into this host-owned DOM node on a compact/integrated host, instead of
  // the floating menu every kind's right-click path already renders. Read
  // only through AnnotationContext's `editSheet.container` (see that
  // context's doc comment); node components never read this prop directly.
  annotationEditSheetPortalContainer = null,
  // Asks the host to open its mobile edit sheet (bound to `MobileShell`'s
  // `'detail'` surface in `frontend/web`) — called by a node's Edit button
  // before the container above has necessarily mounted; the button's own
  // menu state is set to sheet mode regardless, and picks up the container
  // once the host's next render supplies it. Its presence (not
  // `isCompact`/`touch` alone) is what makes `editSheet.capable` true, the
  // same "does the host support this" signal `annotationToolboxPortalContainer`
  // effectively already is for the creation toolbox.
  onRequestAnnotationEditSheet = null,
  // Asks the host to close the mobile edit sheet again — called when the
  // node's own menu-dismiss logic (outside click, Escape, an action that
  // closes the menu) fires while it was opened in sheet mode.
  onCloseAnnotationEditSheet = null,
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
    align: 'Align',
    alignLeft: 'Align left',
    alignCenterHorizontal: 'Align horizontal centers',
    alignRight: 'Align right',
    alignTop: 'Align top',
    alignCenterVertical: 'Align vertical middles',
    alignBottom: 'Align bottom',
    distribute: 'Distribute',
    distributeHorizontal: 'Distribute horizontally',
    distributeVertical: 'Distribute vertically',
    annotationAttachedSkipped: 'Attached items follow their target and were left out',
    hideAll: 'Hide all',
    deleteAll: 'Delete all',
    dimNode: 'Dim node',
    restoreNode: 'Restore node',
    dimSelected: 'Dim selected',
    restoreSelected: 'Restore selected',
    dimIncidentEdges: 'Dim incident edges',
    restoreIncidentEdges: 'Restore incident edges',
    dimEdge: 'Dim connection',
    restoreEdge: 'Restore connection',
    changeType: 'Change type',
    generalConnection: 'General connection',
    addNote: 'Add note',
    addLabel: 'Add label',
    addArrow: 'Add arrow',
    annotationColor: 'Colour',
    annotationFill: 'Fill',
    annotationBorder: 'Border',
    annotationTransparent: 'Transparent',
    deleteAnnotation: 'Delete',
    unlockAnnotation: 'Unlock',
    duplicateAnnotation: 'Duplicate',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
    annotationTextSize: 'Text size',
    annotationTextAlign: 'Alignment',
    annotationAlignTop: 'Top',
    annotationAlignMiddle: 'Middle',
    annotationAlignBottom: 'Bottom',
    annotationAlignLeft: 'Left',
    annotationAlignCenter: 'Center',
    annotationAlignRight: 'Right',
    annotationFontFamily: 'Font',
    annotationFontDefault: 'Default',
    annotationFontFamilySerif: 'Serif',
    annotationFontFamilyMonospace: 'Monospace',
    annotationFontFamilyCursive: 'Cursive',
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
    annotationBroken: 'An annotation could not be drawn and was left out',
    freehandColor: 'Colour',
    freehandWidth: 'Stroke width',
    freehandSmoothing: 'Smoothing',
    freehandOpacity: 'Opacity',
    freehandDrawingHint: 'Draw a stroke on the canvas — press Escape to cancel',
    freehandConcurrentInputBlocked: 'Finish the current stroke before starting another',
    annotationLayer: 'Layer',
    annotationLayerFront: 'Bring to front',
    annotationLayerBack: 'Send to back',
    // Group backgrounds relative to each other only
    // (dec-annotation-group-background-layering) — a separate control from
    // the annotationLayer* row above, not a relabelling of it. See
    // GroupNode.jsx and utils/groupLayers.js.
    groupLayer: 'Group order',
    groupLayerFront: 'Bring forward',
    groupLayerBack: 'Send backward',
    annotationNearbyMenu: 'Add nearby',
    annotationNearbyLabel: 'Label',
    annotationNearbyIcon: 'Icon',
    annotationNearbyText: 'Text',
    annotationOpacity: 'Opacity',
    editAnnotation: 'Edit',
    // task-annotation-accessible-shared-controls.
    ariaKindNote: 'Sticky note',
    ariaKindLabel: 'Label',
    ariaKindText: 'Text',
    ariaKindShape: 'shape',
    ariaKindIcon: 'icon',
    ariaKindVoteDot: 'Vote dot',
    ariaKindImage: 'Image',
    ariaKindArrow: 'Arrow',
    ariaKindFreehand: 'Freehand stroke',
    ariaKindGroup: 'Group',
    annotationWidth: 'Width',
    annotationHeight: 'Height',
    annotationSize: 'Size',
    annotationMoreActions: 'More actions',
    annotationApplySize: 'Apply size',
    annotationAttachTo: 'Attach to…',
    annotationDetach: 'Detach',
    annotationAttachToHint: 'Choose a target to attach to — Escape to cancel',
    annotationAttachToCancel: 'Cancel attaching',
    annotationMultiSelectMode: 'Select multiple',
    annotationOverlapPickerTitle: 'Multiple objects here — choose one',
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
    shapeRectangle: 'Rectangle',
    shapeCircle: 'Circle',
    shapeTriangle: 'Triangle',
    shapeRhombus: 'Rhombus',
    shapeHexagon: 'Hexagon',
    shapeProcessArrow: 'Process arrow',
    shapePickerOpen: 'Choose a shape',
    shapePicker: 'Shapes',
    icon: 'Icon',
    iconPickerOpen: 'Choose an icon',
    iconPicker: 'Icons',
    voteDot: 'Vote dot',
    image: 'Image',
    freehand: 'Freehand',
    select: 'Select',
    eraser: 'Eraser',
    voteDotPickerOpen: 'Choose a vote dot colour',
    voteDotPicker: 'Vote dot colours',
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
  // Mirrors `selectedNodes`, read (not subscribed to) by `onNodeClick`'s
  // multi-select-mode branch below, so that handler's own identity does not
  // change on every selection change.
  const selectedNodesRef = useRef([]);
  const [selectedEdges, setSelectedEdges] = useState([]);
  const [paneContextMenu, setPaneContextMenu] = useState(null);
  // Non-drag "Attach to…" target-tap mode (task-annotation-accessible-shared-
  // controls) — the id of the annotation currently choosing a target, or null
  // when the mode is inactive. Only one attach-in-progress at a time; opening
  // the mode for a different annotation, or cancelling, simply replaces this.
  const [attachModeId, setAttachModeId] = useState(null);
  // Explicit touch multi-select mode (task-annotation-accessible-shared-
  // controls, closing the accessibility audit's "no touch equivalent of
  // holding Shift/Ctrl while clicking" gap): while true, tapping a node ADDS
  // it to the current selection instead of replacing it — see the ReactFlow
  // `onNodeClick` handler below.
  const [multiSelectMode, setMultiSelectMode] = useState(false);
  // The overlap-object picker (task-annotation-accessible-shared-controls) —
  // shown when a click's flow position lands inside more than one
  // annotation's box, so a user can say which one they meant instead of
  // whichever ReactFlow's own DOM-order hit test happened to resolve to.
  const [overlapPicker, setOverlapPicker] = useState(null);
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
  // Last createGroupSignal value actually acted on. The host never resets
  // createGroupSignal back to 0 the way it does saveViewSignal (small-fix:
  // createGroupSignal never reset), so the create-group effect below cannot
  // rely on `> 0` alone once a group has ever been created. See that effect
  // for the full reasoning.
  const handledCreateGroupSignalRef = useRef(0);
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
  // The armed annotation tool (task-annotation-tool-modes). `kind: 'select'`
  // is the resting state and means "behave exactly as the canvas always has":
  // click selects, drag marquees or moves. Any other kind is a STICKY
  // placement tool — every tap on empty canvas creates one more of that kind
  // at the tapped point, until the user arms a different tool. That
  // stickiness is the whole point: placing eight vote dots used to be eight
  // trips to the toolbox, each one dropping its dot at the viewport centre on
  // top of the last.
  //
  // 'eraser' is a mode rather than a placement kind: dragging over an
  // annotation deletes it, over a graph node or edge hides it. 'freehand'
  // keeps its own separate `freehandActive` flag rather than being folded in
  // here, because it is genuinely one-shot (auto-disarms after a stroke)
  // while these are sticky; the two are kept in sync at every entry point
  // below rather than by making one derive from the other.
  const [activeTool, setActiveTool] = useState({ kind: 'select', options: undefined });
  // Read by pointer handlers that must not re-subscribe every time the tool
  // changes (the eraser effect below), the same refs-for-handlers convention
  // the freehand capture already uses.
  const activeToolRef = useRef(activeTool);
  activeToolRef.current = activeTool;
  const eraserActive = activeTool.kind === 'eraser';
  // Any tool that places an object on the canvas: everything except the three
  // modes and `image` (which opens a file picker instead of placing).
  const placementArmed = !['select', 'eraser', 'freehand', 'image'].includes(activeTool.kind);
  // The erase gesture's own state, deliberately in refs rather than inside the
  // effect below. The effect must not re-subscribe mid-gesture: erasing
  // changes `nodes`, and a `nodes` dependency would tear the listeners down
  // and rebuild them after the FIRST object, losing `erasingPointerId` and
  // stranding the rest of the sweep — the user had to lift and press again
  // per object. These survive that, and `latestNodesRef` gives the handler a
  // current node list without the effect having to depend on one.
  // The in-flight drag-to-draw gesture, and the DOM node its live size preview
  // is drawn on. Refs for the same reason the erase gesture uses them: the
  // handlers must survive re-renders mid-gesture, and the preview must not
  // re-render the canvas on every pointermove.
  const placementRef = useRef(null);
  const placementPreviewRef = useRef(null);
  const placementSuspendedRef = useRef(false);
  const pendingPlacementClickRef = useRef(false);
  const createAnnotationRef = useRef(null);
  const erasingPointerIdRef = useRef(null);
  const eraseBlockRectRef = useRef(null);
  const erasedThisStrokeRef = useRef(new Set());
  const latestNodesRef = useRef([]);
  // The mobile annotate sheet unmounts the toolbox on close (its portal
  // container goes from a real node back to null - see
  // annotationToolboxPortalContainer above), which would otherwise strand
  // freehand armed with no visible toolbox button left to disarm it and the
  // canvas silently still eating pointer gestures as strokes. Only the
  // container-present -> container-null transition (the sheet actually
  // closing) disarms it; every other render (including toggling isCompact,
  // or the container simply not being wired at all) leaves freehandActive
  // alone, matching desktop's unchanged behaviour.
  const prevAnnotationPortalContainerRef = useRef(null);
  useEffect(() => {
    const wasOpen = !!prevAnnotationPortalContainerRef.current;
    prevAnnotationPortalContainerRef.current = annotationToolboxPortalContainer;
    if (isCompact && wasOpen && !annotationToolboxPortalContainer) {
      setFreehandActive(false);
      // The armed tool has to fall back with it (task-annotation-tool-modes).
      // `freehandActive` and `activeTool` are two records of the same fact —
      // resetting only the first left the toolbox reporting freehand as still
      // pressed, and would leave a placement tool armed with no visible
      // toolbox left to disarm it, the exact stranding this effect exists to
      // prevent.
      setActiveTool({ kind: 'select' });
    }
  }, [annotationToolboxPortalContainer, isCompact]);
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
  // `attachNearbyAnnotation` (the "Nearby object menu" creation entry point)
  // is declared later, next to `createAnnotation` it depends on — but every
  // annotation-node context menu needs to reach it through
  // AnnotationContext, which this component builds well before that point.
  // A ref (same idiom as onAnnotationChangeRef above) lets the context value
  // below call whatever the current implementation is without forcing it out
  // of its natural place.
  const attachNearbyAnnotationRef = useRef(null);
  const attachNearby = useCallback(
    (targetId, kind) => attachNearbyAnnotationRef.current?.(targetId, kind),
    []
  );
  // Non-drag "Attach to…" target-tap mode (task-annotation-accessible-
  // shared-controls) — entering it is as simple as remembering which
  // annotation is choosing a target; resolving a chosen target happens in
  // `onNodeClick` below, next to the overlap picker it shares a click with.
  const enterAttachMode = useCallback((id) => setAttachModeId(id), []);
  const cancelAttachMode = useCallback(() => setAttachModeId(null), []);
  // Surfaced by an annotation component when a mutation is refused because
  // another client currently holds a live edit lease on the annotation
  // (task-annotation-exclusive-edit-leases — first-actual-editor-wins, never
  // a mere selection), so the attempt is visible rather than a silent no-op.
  const notifyRemoteLockedAttempt = useCallback(() => {
    showNotification('info', cml.annotationRemoteLocked);
  }, [cml.annotationRemoteLocked, showNotification]);

  // AnnotationContext's edit-lease acquire/release pair — every real
  // edit-start entry point (text field open, geometry gesture, property
  // editor, bulk mutation/undo) calls these before mutating
  // (task-annotation-exclusive-edit-leases). Wraps the host-supplied
  // `onBeginEditing`/`onEndEditing` (bound to sessionSyncClient.
  // beginEditing/endEditing in App.jsx) with the same "no host wired up"
  // fail-open default AnnotationContext's own default carries, so a caller
  // that renders GraphCanvas without a live session (e.g. a standalone demo)
  // degrades to the pre-lease local-only behaviour instead of throwing.
  const beginEditing = useCallback(
    async (elementIds) => {
      if (!onBeginEditing) return { granted: elementIds || [], denied: {} };
      return onBeginEditing(elementIds);
    },
    [onBeginEditing]
  );
  const endEditing = useCallback(
    (elementIds) => {
      onEndEditing?.(elementIds);
    },
    [onEndEditing]
  );
  // Reached from the erase gesture's pointer handlers, which must not
  // re-subscribe when these callbacks' identities change.
  const beginEditingRef = useRef(beginEditing);
  beginEditingRef.current = beginEditing;
  const endEditingRef = useRef(endEditing);
  endEditingRef.current = endEditing;

  // An annotation that could not be drawn is reported once per session rather
  // than once per annotation: a graph carrying several of the same broken
  // shape would otherwise fire a burst of identical notices, which reads as
  // something being badly wrong rather than as one decoration being skipped.
  // The console entry is not deduplicated — that one is for whoever has to
  // find the defect.
  const reportedBrokenRef = useRef(false);
  const reportAnnotationRenderError = useCallback(
    (nodeId, error) => {
      console.error(`Annotation ${nodeId} could not be rendered:`, error);
      if (reportedBrokenRef.current) return;
      reportedBrokenRef.current = true;
      showNotification('info', cml.annotationBroken);
    },
    [cml.annotationBroken, showNotification]
  );

  const annotationContextValue = useMemo(
    () => ({
      notifyChange: notifyAnnotationChange,
      notifyRemoteLockedAttempt,
      notifyRenderFailure: reportAnnotationRenderError,
      beginEditing,
      endEditing,
      attachNearby,
      enterAttachMode,
      // task-annotation-responsive-bottom-toolbox's edit-surface half — see
      // AnnotationContext's own doc comment on this field for what each part
      // means. `isCompact` alone (not `isTouchMode`) is the gate, mirroring
      // exactly how `annotationToolboxPortalContainer`'s own compact-vs-desktop
      // branch below is decided.
      editSheet: {
        capable: isCompact && Boolean(onRequestAnnotationEditSheet),
        container: annotationEditSheetPortalContainer,
        requestOpen: onRequestAnnotationEditSheet,
        requestClose: onCloseAnnotationEditSheet,
      },
      labels: {
        color: cml.annotationColor,
        fill: cml.annotationFill,
        border: cml.annotationBorder,
        transparent: cml.annotationTransparent,
        delete: cml.deleteAnnotation,
        unlock: cml.unlockAnnotation,
        duplicate: cml.duplicateAnnotation,
        notePlaceholder: cml.notePlaceholder,
        labelPlaceholder: cml.labelPlaceholder,
        textSize: cml.annotationTextSize,
        textAlign: cml.annotationTextAlign,
        alignTop: cml.annotationAlignTop,
        alignMiddle: cml.annotationAlignMiddle,
        alignBottom: cml.annotationAlignBottom,
        alignLeft: cml.annotationAlignLeft,
        alignCenter: cml.annotationAlignCenter,
        alignRight: cml.annotationAlignRight,
        fontFamily: cml.annotationFontFamily,
        fontDefault: cml.annotationFontDefault,
        fontFamilySerif: cml.annotationFontFamilySerif,
        fontFamilyMonospace: cml.annotationFontFamilyMonospace,
        fontFamilyCursive: cml.annotationFontFamilyCursive,
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
        groupLayer: cml.groupLayer,
        groupLayerFront: cml.groupLayerFront,
        groupLayerBack: cml.groupLayerBack,
        brokenAnnotation: cml.annotationBroken,
        nearbyMenu: cml.annotationNearbyMenu,
        nearbyLabel: cml.annotationNearbyLabel,
        nearbyIcon: cml.annotationNearbyIcon,
        nearbyText: cml.annotationNearbyText,
        opacity: cml.annotationOpacity,
        editAnnotation: cml.editAnnotation,
        ariaKindNote: cml.ariaKindNote,
        ariaKindLabel: cml.ariaKindLabel,
        ariaKindText: cml.ariaKindText,
        ariaKindShape: cml.ariaKindShape,
        ariaKindIcon: cml.ariaKindIcon,
        ariaKindVoteDot: cml.ariaKindVoteDot,
        ariaKindImage: cml.ariaKindImage,
        ariaKindArrow: cml.ariaKindArrow,
        ariaKindFreehand: cml.ariaKindFreehand,
        ariaKindGroup: cml.ariaKindGroup,
        width: cml.annotationWidth,
        height: cml.annotationHeight,
        size: cml.annotationSize,
        moreActions: cml.annotationMoreActions,
        applySize: cml.annotationApplySize,
        attachTo: cml.annotationAttachTo,
        detach: cml.annotationDetach,
      },
    }),
    [
      notifyAnnotationChange,
      notifyRemoteLockedAttempt,
      beginEditing,
      endEditing,
      attachNearby,
      enterAttachMode,
      isCompact,
      onRequestAnnotationEditSheet,
      annotationEditSheetPortalContainer,
      onCloseAnnotationEditSheet,
      cml.annotationOpacity,
      cml.annotationSize,
      cml.annotationMoreActions,
      cml.editAnnotation,
      cml.annotationColor,
      cml.annotationFill,
      cml.annotationBorder,
      cml.annotationTransparent,
      cml.deleteAnnotation,
      cml.unlockAnnotation,
      cml.duplicateAnnotation,
      cml.notePlaceholder,
      cml.labelPlaceholder,
      cml.annotationTextSize,
      cml.annotationTextAlign,
      cml.annotationAlignTop,
      cml.annotationAlignMiddle,
      cml.annotationAlignBottom,
      cml.annotationAlignLeft,
      cml.annotationAlignCenter,
      cml.annotationAlignRight,
      cml.annotationFontFamily,
      cml.annotationFontDefault,
      cml.annotationFontFamilySerif,
      cml.annotationFontFamilyMonospace,
      cml.annotationFontFamilyCursive,
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
      cml.groupLayer,
      cml.groupLayerFront,
      cml.groupLayerBack,
      cml.annotationBroken,
      cml.annotationNearbyMenu,
      cml.annotationNearbyLabel,
      cml.annotationNearbyIcon,
      cml.annotationNearbyText,
      cml.ariaKindNote,
      cml.ariaKindLabel,
      cml.ariaKindText,
      cml.ariaKindShape,
      cml.ariaKindIcon,
      cml.ariaKindVoteDot,
      cml.ariaKindImage,
      cml.ariaKindArrow,
      cml.ariaKindFreehand,
      cml.ariaKindGroup,
      cml.annotationWidth,
      cml.annotationHeight,
      cml.annotationApplySize,
      cml.annotationAttachTo,
      cml.annotationDetach,
      reportAnnotationRenderError,
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
      selectedNodesRef.current = selected;
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
      const opacity = resolveEdgeOpacity(edgeIntensity, dimmedEdgeIds.includes(edge.id));
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.type,
        type: 'floating',
        animated: visuals.animated,
        selectable: true,
        // `--edge-opacity` mirrors `strokeOpacity` as a CSS custom property:
        // the rf-edge-pulse keyframe (GraphCanvas.css) reads it via calc() so
        // an animated edge's pulse scales relative to this opacity instead of
        // the animation silently overriding it back to a fixed 1/0.35.
        style: { ...visuals.style, strokeOpacity: opacity, '--edge-opacity': opacity },
        markerStart: visuals.markerStart,
        markerEnd: visuals.markerEnd,
        className: visuals.className,
        data: { type: edge.type, label: edge.label, metadata: edge.metadata || {} },
        labelStyle: { fill: '#888', fontSize: 10, fontWeight: 500 },
        labelBgStyle: { fill: '#1a1a1a', fillOpacity: 0.8 },
      };
    });
  }, [visibleEdges, dimmedEdgeIds, edgeIntensity]);

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
          isDimmed: dimmedNodeIds.includes(node.id),
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
    dimmedNodeIds,
    nodeMarks,
    pulsedNodeIds,
    remoteSelections,
    nodePreviewEnabled,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  // Kept current here, next to the state it mirrors — the ref is declared far
  // above because the erase gesture's other refs live together, but it cannot
  // be initialised there: `nodes` does not exist yet at that point.
  latestNodesRef.current = nodes;
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

  const hideSelectedGraphNodes = useCallback(() => {
    const graphNodeIds = selectedNodes
      .filter((n) => !ANNOTATION_TYPES.has(n.type))
      .map((n) => n.id);
    if (graphNodeIds.length === 0) return;
    if (onHideMultiple) {
      onHideMultiple(graphNodeIds);
    } else if (onHide) {
      graphNodeIds.forEach((id) => onHide(id));
    }
  }, [selectedNodes, onHideMultiple, onHide]);

  const deleteSelectedNodes = useCallback(() => {
    // Delete removes overlay annotations — every kind in OVERLAY_TYPES,
    // which is all eleven v1 kinds except `group` — from the canvas;
    // graph nodes are hidden, not deleted. Excluding `group` leaves it to
    // its own context menu, so its children stay correctly parented.
    // Two are skipped: one held by another client's live edit lease
    // (task-annotation-exclusive-edit-leases — first-actual-editor-wins)
    // and one that is locked, which stays selectable but offers only
    // unlock or copy — the rule every overlay annotation's context menu
    // applies, and `group`'s menu now applies it too. Delete never reaches
    // a group anyway, because of the exclusion above.
    const deletableOverlays = selectedNodes.filter(
      (n) => OVERLAY_TYPES.has(n.type) && !isRemoteLocked(n.data) && !n.data?.locked
    );
    const overlayIds = deletableOverlays.map((n) => n.id);
    const skippedLocked = selectedNodes.some(
      (n) => OVERLAY_TYPES.has(n.type) && isRemoteLocked(n.data)
    );
    const skippedOwnLocked = selectedNodes.some((n) => OVERLAY_TYPES.has(n.type) && n.data?.locked);
    if (overlayIds.length > 0) {
      // A bulk delete is itself an edit-start entry point (task-annotation-
      // exclusive-edit-leases): applies optimistically like every other
      // mutation here (no round trip before the user sees the result) while
      // acquiring/releasing the lease in the background around it — the
      // server remains the authoritative backstop for a lost race
      // (LeaseConflict) regardless. Locally-known-locked ids never reach
      // here at all (filtered above), so the only thing the background
      // acquisition covers is the narrow race window since that local read.
      setNodes((nds) => {
        const removeSet = new Set(overlayIds);
        return nds.filter((n) => !removeSet.has(n.id));
      });
      onAnnotationChangeRef.current?.('delete');
      beginEditing(overlayIds).then(() => endEditing(overlayIds));
    }
    // A remote lease wins the notice when the selection mixes both: it is
    // the one the user cannot resolve alone.
    if (skippedLocked) showNotification('info', cml.annotationRemoteLocked);
    else if (skippedOwnLocked) showNotification('info', cml.annotationLockedSkipped);
    hideSelectedGraphNodes();
  }, [
    selectedNodes,
    setNodes,
    showNotification,
    cml.annotationRemoteLocked,
    cml.annotationLockedSkipped,
    hideSelectedGraphNodes,
    beginEditing,
    endEditing,
  ]);

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

  // Eligible members of the current selection for the multi-select Align and
  // Distribute actions (task-annotation-render-direct-manipulation). Unlike
  // Organize above (graph nodes only), these two also move overlay
  // annotations — but not every kind, and not unconditionally:
  //
  // - `arrow` is excluded the same way a graph edge is: its geometry is a
  //   pair of connected endpoints, not an independently movable box, so
  //   there is nothing sensible to align/distribute — it already moves with
  //   whatever it is anchored to, same as an edge follows its nodes.
  // - `group` is excluded, matching deleteSelectedNodes's own exclusion —
  //   its box is manipulated from its own context menu, not this one.
  // - A locked or remote-leased overlay is excluded exactly like
  //   deleteSelectedNodes excludes it: this action skips the ineligible
  //   member and proceeds with the rest, rather than refusing the whole
  //   selection — the more forgiving of the two options and the one delete
  //   already established.
  // - An attached label/text/icon (`content.attachment`, ATTACHABLE_OVERLAY_
  //   KINDS) is excluded even though it is neither locked nor claimed: the
  //   attachment-follow effect further down re-glues it to its target's
  //   centre on every `nodes` change, so moving it here would be
  //   immediately undone by that effect on the very next render — a fight
  //   between the two mechanisms that would show up as visible jitter
  //   rather than the alignment the user asked for. It stays out of the
  //   move set and simply follows if its own attachment target moves as
  //   part of the same align/distribute.
  const alignDistributeEligibility = useMemo(() => {
    const isMovableOverlay = (n) =>
      OVERLAY_TYPES.has(n.type) && n.type !== 'arrow' && !isRemoteLocked(n.data) && !n.data?.locked;
    const isAttachedOverlay = (n) => ATTACHABLE_OVERLAY_KINDS.has(n.type) && !!n.data?.attachment;
    const candidates = selectedNodes.filter(
      (n) => !ANNOTATION_TYPES.has(n.type) || isMovableOverlay(n)
    );
    const skippedLocked = selectedNodes.some(
      (n) => OVERLAY_TYPES.has(n.type) && n.type !== 'arrow' && isRemoteLocked(n.data)
    );
    const skippedOwnLocked = selectedNodes.some(
      (n) => OVERLAY_TYPES.has(n.type) && n.type !== 'arrow' && n.data?.locked
    );
    const skippedAttached = candidates.some(isAttachedOverlay);
    return {
      movable: candidates.filter((n) => !isAttachedOverlay(n)),
      skippedLocked,
      skippedOwnLocked,
      skippedAttached,
    };
  }, [selectedNodes]);

  // Resolve each eligible node's absolute-coordinate bounding box (position +
  // measured size), converting a grouped graph node's parent-relative
  // position to absolute the same way organizeSelection does above — a
  // selection spanning grouped and ungrouped nodes must align/distribute in
  // one consistent coordinate space.
  const alignmentBounds = useCallback(
    (movable) => {
      const groupPos = new Map(
        nodes.filter((n) => n.type === 'group').map((g) => [g.id, g.position])
      );
      const toAbsolute = (n) => {
        const parent = n.parentId ? groupPos.get(n.parentId) : null;
        return parent ? { x: n.position.x + parent.x, y: n.position.y + parent.y } : n.position;
      };
      return movable.map((n) => {
        const { w, h } = nodeSize(n);
        return { id: n.id, position: toAbsolute(n), width: w, height: h };
      });
    },
    [nodes]
  );

  // Shared tail for alignSelectedNodes/distributeSelectedNodes: converts the
  // computed absolute positions back to parent-relative for grouped nodes,
  // records one undoable move, applies it locally, and persists it through
  // exactly the paths a drag or Organize already use — onNodePositionChange
  // for graph nodes (the same callback onNodeDragStop and applyPositionMoves
  // call), onAnnotationChange('geometry') once for any annotation moved (the
  // same notifier the attach-follow effects and onNodeDragStop's own
  // attach/detach branch use) — so the new positions reach the realtime
  // publish path and MCP-visible session state the same way any other
  // geometry change does, not just local React state.
  const applyAlignedPositions = useCallback(
    (positionsById) => {
      if (positionsById.size === 0) return;
      const groupPos = new Map(
        nodes.filter((n) => n.type === 'group').map((g) => [g.id, g.position])
      );
      const nodeById = new Map(nodes.map((n) => [n.id, n]));
      const finalPos = new Map();
      const moves = [];
      let touchedAnnotation = false;
      for (const [id, abs] of positionsById) {
        const cur = nodeById.get(id);
        if (!cur) continue;
        const parent = cur.parentId ? groupPos.get(cur.parentId) : null;
        const pos = parent ? { x: abs.x - parent.x, y: abs.y - parent.y } : abs;
        finalPos.set(id, pos);
        moves.push({
          id,
          from: { x: cur.position.x, y: cur.position.y, parentId: cur.parentId },
          to: { x: pos.x, y: pos.y, parentId: cur.parentId },
        });
        if (ANNOTATION_TYPES.has(cur.type)) touchedAnnotation = true;
      }
      if (finalPos.size === 0) return;
      recordMove(moves);
      setNodes((nds) =>
        nds.map((n) => (finalPos.has(n.id) ? { ...n, position: finalPos.get(n.id) } : n))
      );
      if (onNodePositionChange) {
        for (const [id, pos] of finalPos) {
          const cur = nodeById.get(id);
          if (cur && !ANNOTATION_TYPES.has(cur.type)) onNodePositionChange(id, pos);
        }
      }
      if (touchedAnnotation) onAnnotationChangeRef.current?.('geometry');
      closeAllMenus();
    },
    [nodes, setNodes, onNodePositionChange, closeAllMenus, recordMove]
  );

  // A remote lease wins the notice when the selection mixes several skip
  // reasons, matching deleteSelectedNodes's own priority — it is the one the
  // user cannot resolve alone by e.g. detaching an annotation themselves.
  const notifySkippedAlignment = useCallback(
    ({ skippedLocked, skippedOwnLocked, skippedAttached }) => {
      if (skippedLocked) showNotification('info', cml.annotationRemoteLocked);
      else if (skippedOwnLocked) showNotification('info', cml.annotationLockedSkipped);
      else if (skippedAttached) showNotification('info', cml.annotationAttachedSkipped);
    },
    [
      showNotification,
      cml.annotationRemoteLocked,
      cml.annotationLockedSkipped,
      cml.annotationAttachedSkipped,
    ]
  );

  // Align/distribute are bulk mutations (task-annotation-exclusive-edit-
  // leases): acquire a lease on every *annotation* member of `movable`
  // before repositioning it (graph nodes carry no lease — leases are an
  // annotation-only concept). Members already excluded locally by
  // alignDistributeEligibility never reach here; this only covers the race
  // window since that local read, same reasoning as deleteSelectedNodes.
  // Fire-and-forget: applying the move itself stays synchronous/optimistic
  // (below), matching every other mutation in this package, so this only
  // ever runs in the background around it — never gates the move on the
  // round trip.
  const acquireLeasesForOverlayMove = useCallback(
    (movable) => {
      const overlayIds = movable.filter((n) => ANNOTATION_TYPES.has(n.type)).map((n) => n.id);
      if (overlayIds.length) beginEditing(overlayIds).then(() => endEditing(overlayIds));
    },
    [beginEditing, endEditing]
  );

  // Align every eligible selected node/annotation's bounding box to a shared
  // edge or centre line (mode: 'left' | 'centerX' | 'right' | 'top' |
  // 'centerY' | 'bottom'). Absent from the menu (see the MultiNodeContextMenu
  // wiring below) below 2 eligible members; the guard here is defence in
  // depth for any other caller.
  const alignSelectedNodes = useCallback(
    (mode) => {
      const { movable, ...skip } = alignDistributeEligibility;
      if (movable.length < 2) return;
      applyAlignedPositions(alignNodes(alignmentBounds(movable), mode));
      acquireLeasesForOverlayMove(movable);
      notifySkippedAlignment(skip);
    },
    [
      alignDistributeEligibility,
      alignmentBounds,
      applyAlignedPositions,
      notifySkippedAlignment,
      acquireLeasesForOverlayMove,
    ]
  );

  // Spread every eligible selected node/annotation evenly (equal gaps) along
  // one axis. Only meaningful with 3+ eligible members (see distributeNodes'
  // own doc comment) — absent from the menu below that, same defence-in-depth
  // guard as alignSelectedNodes above.
  const distributeSelectedNodes = useCallback(
    (axis) => {
      const { movable, ...skip } = alignDistributeEligibility;
      if (movable.length < 3) return;
      applyAlignedPositions(distributeNodes(alignmentBounds(movable), axis));
      acquireLeasesForOverlayMove(movable);
      notifySkippedAlignment(skip);
    },
    [
      alignDistributeEligibility,
      alignmentBounds,
      applyAlignedPositions,
      notifySkippedAlignment,
      acquireLeasesForOverlayMove,
    ]
  );

  // Create a free-floating annotation (note, label, arrow, or one of the
  // generic overlay kinds - text/shape/icon) at the given flow position.
  // These are persisted in the session annotation list via the save-view
  // round-trip; onAnnotationChange schedules that save. `options.shape` picks
  // the shape variant for kind 'shape' (defaults to 'rectangle');
  // `options.icon` picks the icon for kind 'icon' (defaults to circle).
  // `options.attachment` (label/text/icon only — the "Nearby object
  // menu" creation entry point below) is written straight onto
  // `data.attachment`, the exact same `{target_id, target_type, offset}`
  // shape and field `computeDroppedAttachment`/`resolveAttachedPosition`
  // already read and write for the post-creation drag-to-attach path, so a
  // pre-wired annotation follows its target from the moment it exists with
  // no separate mechanism of its own.
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
          style: options.box || { width: 200, height: 140 },
        };
      } else if (kind === 'label') {
        newNode = {
          id,
          type: 'label',
          position,
          data: { text: '', color: undefined, attachment: options.attachment },
        };
      } else if (kind === 'text') {
        newNode = {
          id,
          type: 'text',
          position,
          data: { text: '', color: undefined, fontSize: undefined, attachment: options.attachment },
        };
      } else if (kind === 'shape') {
        // A subtype whose clip-path only draws a regular figure at one ratio
        // is created at that ratio instead of the generic 160x96 box, so it
        // comes out equal-sided rather than squashed. The resizer then keeps
        // the ratio (GenericAnnotationNode's keepAspectRatio), so it stays
        // that way.
        const shape = options.shape || 'rectangle';
        // An explicit box from a drag-to-draw gesture wins over the subtype's
        // default size, and the mirror flags come with it — but a subtype with
        // a regular ratio is re-proportioned to it first.
        //
        // This is load-bearing, not tidiness. The resizer locks
        // `keepAspectRatio` to whatever ratio the node MEASURES at drag start
        // (GenericAnnotationNode's own comment says so), which is only safe
        // because every creation path used to size from `regularShapeSize`.
        // Drag-to-draw is a new creation path: writing the swept box verbatim
        // would start a circle/triangle/hexagon at an arbitrary ratio and the
        // aspect lock would then cement the distortion — reintroducing exactly
        // the squashed-shape bug that code exists to prevent. The drag still
        // decides the SIZE; the subtype decides the proportion.
        // Re-proportion FIRST, then re-floor: `regularShapeSize` recomputes
        // height from width, so a height clamped before it is simply
        // discarded — at the minimum a triangle came out 40x35, under the
        // resizer's own MIN_SIZE that the clamp exists to respect.
        const proportioned = options.box
          ? (regularShapeSize(shape, options.box.width) ?? options.box)
          : null;
        // Floor the WIDTH so the derived height clears the minimum too —
        // flooring both independently breaks the very ratio the
        // re-proportioning exists to keep. `regularShapeSize(40)` gives a
        // triangle 40x35; raising that 35 to 40 produced a 1:1 "equal-sided"
        // triangle, and NodeResizer's keepAspectRatio then locked the
        // distortion in.
        const aspect = regularShapeAspect(shape);
        const drawnBox = proportioned
          ? aspect
            ? (regularShapeSize(
                shape,
                Math.max(proportioned.width, Math.ceil(MIN_ANNOTATION_SIZE * aspect))
              ) ?? proportioned)
            : {
                width: Math.max(MIN_ANNOTATION_SIZE, proportioned.width),
                height: Math.max(MIN_ANNOTATION_SIZE, proportioned.height),
              }
          : null;
        newNode = {
          id,
          type: 'shape',
          position,
          // `text: ''` (not omitted) matches the `text`-kind branch above —
          // a freshly created shape has no caption yet, but the field itself
          // already exists rather than being absent until first edit.
          //
          // `fill`/`border` left undefined (task-annotation-merge-frame-into-
          // shape-rectangle) — GenericAnnotationNode.jsx defaults an unset
          // fill to its previous solid grey and an unset border to
          // transparent, so a freshly toolbox-created shape looks exactly as
          // a plain shape always did. The retired `frame` toolbox button's
          // look (a transparent box with a coloured border) is now reached by
          // right-clicking a created shape and setting fill to transparent,
          // not by a separate creation-time default.
          //
          // `zIndex` is the one per-kind semantic default this creation
          // function sets (task-annotation-render-direct-manipulation's
          // "semantic default layers" — see annotationModel.js's
          // `defaultAnnotationZ` and docs/ANNOTATION_CONTRACT.md's Layer
          // order section for the reasoning): a shape starts one layer
          // behind the 0 every other kind (and every graph node) still
          // starts at, so it opens already behind content instead of
          // needing a manual send-to-back. Every other branch below omits
          // `zIndex` on purpose, which resolves to that same unchanged 0
          // through the existing `zIndex ?? 0` fallbacks
          // (annotationLayers.js's `layerOf`, annotations.js's
          // `flowNodeToOverlay`) — nothing here regresses their behavior.
          data: {
            shape,
            text: '',
            fill: undefined,
            border: undefined,
            flipX: options.flipX || undefined,
            flipY: options.flipY || undefined,
          },
          style: drawnBox || newShapeSize(shape),
          zIndex: defaultAnnotationZ(kind),
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
        const icon = options.icon || DEFAULT_ANNOTATION_ICON;
        newNode = {
          id,
          type: 'icon',
          position,
          data: {
            icon,
            color: undefined,
            size: { ...ICON_INTRINSIC_SIZE },
            attachment: options.attachment,
          },
        };
      } else if (kind === 'vote_dot') {
        // A plain coloured dot (task-annotation-vote-dot-simplify): no
        // `value` (there is nothing to count any more) and no `attachment`
        // (it is not one of ATTACHABLE_OVERLAY_KINDS, so it is never
        // pre-wired to a target the way label/icon/text are above). Same
        // fixed-intrinsic-size treatment as icon otherwise.
        newNode = {
          id,
          type: 'vote_dot',
          position,
          data: {
            // The colour chosen in the toolbox's vote-dot slot, when there is
            // one. `undefined` still means "the default", so a dot created
            // any other way (drag from a host that passes no colour, MCP) is
            // unchanged. Placing a run of dots in one colour is the whole
            // reason the slot exists — recolouring each one afterwards
            // through the property editor was the laborious path.
            color: options.color,
            size: { ...VOTE_DOT_INTRINSIC_SIZE },
          },
        };
      } else {
        newNode = {
          id,
          type: 'arrow',
          position,
          data: { dx: 160, dy: 0, color: undefined, startArrow: false, endArrow: true },
        };
      }
      // Selected and focused immediately (task-annotation-accessible-shared-
      // controls, closing the audit's "Activating one creates at a sensible
      // location, focused, ready to edit | ⚠ PARTIAL" row — the location was
      // already sensible; selection/focus were the missing half): a keyboard
      // user who activates a toolbox button no longer has to Tab/click
      // around to find what they just created — its Edit button (or, for a
      // keyboard user, Shift+F10/Tab+Enter on the now-focused wrapper) is
      // immediately reachable. Mirrors `selectAndFocusNode`'s own
      // deselect-others-then-select-this shape, inlined here (rather than
      // reused via that callback) because `newNode` is not yet in `nds` when
      // this runs, so there is nothing for `selectAndFocusNode`'s own
      // `nodes.find` to have selected yet.
      setNodes((nds) =>
        reorderNodesForParentChild([
          ...nds.map((n) => (n.selected ? { ...n, selected: false } : n)),
          { ...newNode, selected: true },
        ])
      );
      setPaneContextMenu(null);
      onAnnotationChangeRef.current?.('create');
      // The focus-after-create above must never fire while the mobile
      // `'annotate'` BottomSheet (aria-modal="true") is still open — that
      // sheet never auto-closes on creation, so moving DOM focus onto the new
      // node (rendered behind the still-visible sheet) would silently break
      // the sheet's own modal-focus contract, which only intercepts Tab, not
      // a programmatic .focus() from here. `isCompact && annotationToolbox
      // PortalContainer` is exactly the host's existing signal for "that
      // sheet is currently mounted" (the same condition that portals the
      // sheet-variant AnnotationToolbox into it above, and disarms
      // prevAnnotationPortalContainerRef's stash), so this reuses it rather
      // than inventing a new one. Desktop (isCompact false, no portal
      // container) always takes the focus branch unchanged — the
      // keyboard-accessibility case this behaviour exists for in the first
      // place.
      const modalSheetOpen = isCompact && Boolean(annotationToolboxPortalContainer);
      if (!modalSheetOpen) {
        requestAnimationFrame(() => {
          const el = reactFlowWrapper.current?.querySelector(
            `.react-flow__node[data-id="${window.CSS && CSS.escape ? CSS.escape(id) : id}"]`
          );
          el?.focus();
        });
      }
    },
    [setNodes, isCompact, annotationToolboxPortalContainer]
  );

  // The "Nearby object menu" creation entry point
  // (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces"): creates a new
  // label/icon/text pre-wired to attach to an existing node or
  // annotation, from that target's own context menu — rather than the
  // create-then-drag-near two-step the toolbox's one-click creation still
  // requires. `targetId` may be a graph node or any existing annotation
  // except `group`/`arrow` (the same candidates `findSnapTarget` accepts for
  // a post-creation drop via `computeDroppedAttachment`; `group` is a
  // "containment/visual construct, not an attachment target" per the
  // contract's Attachment section, and an arrow has no stable centre in the
  // attachment-follow effect below, so it can never be resolved as a target)
  // — callers only ever offer this control from an eligible target's own
  // menu, but the type check here is a second, structural guarantee
  // independent of which menus happen to render it.
  //
  // `frame` used to be excluded here too, on the same reasoning as `group`.
  // Now that it is folded into `shape` (task-annotation-merge-frame-into-
  // shape-rectangle), a `shape` — whatever its fill/border — stays a valid
  // target, the same decision `computeDroppedAttachment` makes (see its own
  // doc comment in utils/annotations.js for the full reasoning).
  //
  // Positions the new annotation at NEARBY_ATTACH_OFFSET from the target's
  // current centre and writes `data.attachment` in exactly the shape
  // `computeDroppedAttachment` would have produced for a drop at that same
  // offset, so the pre-existing "keep an attached overlay glued to its
  // target" effect (above) starts following it immediately — this reuses
  // that mechanism outright rather than adding a second one. The new
  // annotation is never locked: `createAnnotation` never sets `data.locked`
  // for any kind, matching today's plain one-click creation.
  //
  // No remote-lock check on the target: every annotation kind's own
  // `openContextMenu`/`onContextMenu` already refuses to open its menu at all
  // while remote-locked (so this is never reached for an annotation target
  // in that state), and NodeContextMenu opens for a remote-locked graph node
  // exactly like any other — edit/hide/delete included — with no lock check
  // of its own to match. Adding one here alone would make this the one
  // action on that menu that silently refuses while every sibling action
  // proceeds, which is a worse inconsistency than having none.
  const attachNearbyAnnotation = useCallback(
    (targetId, kind) => {
      const target = getFlowNodes().find((n) => n.id === targetId);
      if (!target || target.type === 'group' || target.type === 'arrow') return;
      const center = nodeCenter(target);
      if (!center) return;
      const attachment = {
        target_id: targetId,
        target_type: ANNOTATION_TYPES.has(target.type) ? 'annotation' : 'node',
        offset: { ...NEARBY_ATTACH_OFFSET },
      };
      const position = {
        x: center.x + NEARBY_ATTACH_OFFSET.x,
        y: center.y + NEARBY_ATTACH_OFFSET.y,
      };
      createAnnotation(kind, position, { attachment });
    },
    [getFlowNodes, createAnnotation]
  );
  attachNearbyAnnotationRef.current = attachNearbyAnnotation;

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

  // Arm a toolbox tool (task-annotation-tool-modes). Three of the entries are
  // not placement tools at all and are handled here rather than by pretending
  // they are:
  //   - 'image' has nothing to place until a file exists, so it opens the
  //     picker immediately and leaves the armed tool alone; the ingest lands
  //     the annotation at the viewport centre as it always has.
  //   - 'freehand' owns the separate one-shot `freehandActive` flag; arming it
  //     from here keeps the toolbox's pressed state and that flag in step, and
  //     re-tapping it disarms as before.
  //   - 'select' and 'eraser' are modes with no `options`.
  // Everything else becomes the sticky placement tool `handlePaneClick` reads.
  const handleSelectTool = useCallback(
    (kind, options) => {
      if (kind === 'image') {
        pendingImagePositionRef.current = viewportCenterPosition();
        imageFileInputRef.current?.click();
        // Return to plain selection rather than leaving whatever was armed
        // before still armed: after picking a file the user's next tap on the
        // canvas would otherwise place a shape they had forgotten was live.
        setFreehandActive(false);
        setActiveTool({ kind: 'select' });
        return;
      }
      if (kind === 'freehand') {
        // Sequenced, not nested: a state updater must be pure, and StrictMode
        // double-invokes them. `activeTool` is the authority on what is armed,
        // so freehand's own flag is derived from it here.
        const next = activeToolRef.current?.kind !== 'freehand';
        setFreehandActive(next);
        setActiveTool(next ? { kind: 'freehand', options: undefined } : { kind: 'select' });
        return;
      }
      // Arming anything else always leaves freehand: two drawing modes armed
      // at once would both claim the same pointer gesture.
      setFreehandActive(false);
      // Re-arming the tool that is already armed is deliberately a no-op
      // rather than a toggle back to select. A toggle reads well for a single
      // button but breaks the two-step gesture the shape and icon slots need:
      // picking "circle" from the fold-out arms the shape tool, and the click
      // that lands on the slot button right after would then disarm it, so
      // choosing a variant would leave no tool armed at all. `select` is the
      // way back, which is the whole reason it is in the row.
      setActiveTool({ kind, options });
    },
    [viewportCenterPosition]
  );

  // The toolbox's coarse-pointer (touch/stylus) drag-to-create path — see
  // AnnotationToolbox's component doc comment. `clientPosition` is the
  // release point in screen space; `image`/`freehand` never reach here since
  // AnnotationToolbox never starts a pointer-drag for either.
  // The placement effect below listens once and must not re-subscribe when
  // this callback's identity changes, so it reaches it through a ref.
  createAnnotationRef.current = createAnnotation;

  const handleAnnotationDragCreate = useCallback(
    (kind, options, clientPosition) => {
      const position = screenToFlowPosition(clientPosition);
      createAnnotation(kind, position, options);
    },
    [screenToFlowPosition, createAnnotation]
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
      if (!file) return;
      // Every rejection below used to be a silent `return`, which is the worst
      // possible answer to "I picked a file and nothing happened": the canvas
      // did not change and nothing said why. Each one now reports.
      if (!onImageIngest) {
        showNotification('error', cml.imageIngestFailed);
        return;
      }
      // A MISSING type is not a rejection. Browsers report an empty
      // `file.type` for plenty of ordinary files depending on the platform and
      // how the file was picked, and dropping those silently made image upload
      // look broken for exactly the users it happened to. The server validates
      // and optimises the bytes anyway (backend/core/image_ingest.py), and the
      // file picker's own `accept` list already does the coarse filtering — so
      // an unknown type is passed along and judged on its content. A type that
      // IS present and is not an image is still refused, but out loud.
      if (file.type && !file.type.startsWith('image/')) {
        showNotification('error', cml.imageIngestFailed);
        return;
      }
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
    // Clicking empty canvas cancels an in-progress "Attach to…" pick — the
    // same "click away to back out" convention every menu here already uses.
    setAttachModeId(null);
    setOverlapPicker(null);
    // Placement itself is NOT done here. It lives in the wrapper's own
    // pointer gesture (see the drag-to-draw effect below), because
    // `onPaneClick` never fires while the pane is in selection mode — which
    // is the desktop default — and because a click carries no drag to size
    // the object with.
  }, [closeAllMenus, clearSelection]);

  // Shared by the "Attach to…" target-tap mode and the overlap-object picker
  // (task-annotation-accessible-shared-controls) — both need to know exactly
  // which node a click landed on and, crucially, whether OTHER annotations'
  // boxes also cover that same point, so they share the one handler that
  // computes it rather than each re-deriving the click's flow position.
  //
  // Deliberately never calls `event.preventDefault()`/`stopPropagation()`:
  // ReactFlow's own default click-to-select behaviour for `clickedNode` must
  // keep running exactly as it does today (a plain click still selects that
  // node) — this only ever ADDS a follow-up action (resolving an attach
  // target, or offering a picker) alongside it, never replaces it. That is
  // also why the overlap picker does not suppress the normal selection: the
  // node ReactFlow resolved is still selected, and the picker is an
  // additional way to say "no, I meant this OTHER one".
  const onNodeClick = useCallback(
    (event, clickedNode) => {
      if (attachModeId) {
        if (attachModeId === clickedNode.id) return;
        if (!isEligibleAttachTarget(clickedNode, attachModeId)) return;
        const source = nodes.find((n) => n.id === attachModeId);
        if (!source) {
          setAttachModeId(null);
          return;
        }
        if (isRemoteLocked(source.data) || source.data?.locked) {
          notifyRemoteLockedAttempt();
          setAttachModeId(null);
          return;
        }
        const attachment = computeAttachmentToTarget(source, clickedNode);
        if (attachment) {
          setNodes((nds) =>
            nds.map((n) => (n.id === attachModeId ? { ...n, data: { ...n.data, attachment } } : n))
          );
          notifyAnnotationChange('style');
        }
        setAttachModeId(null);
        return;
      }
      // Explicit touch multi-select mode: ReactFlow's own click-to-select
      // already ran (clearing every other selection and selecting only
      // `clickedNode`) by the time this handler fires — restore whichever
      // OTHER nodes were selected immediately before this click, so the net
      // effect is "add `clickedNode` to the selection" rather than replacing
      // it. `selectedNodes` (state, updated by the onSelectionChange
      // subscription below) is read via a ref rather than as a dependency
      // here so this handler's own identity does not change on every
      // selection change — see `selectedNodesRef` above.
      if (multiSelectMode) {
        const previouslySelected = selectedNodesRef.current.filter((n) => n.id !== clickedNode.id);
        if (previouslySelected.length > 0) {
          onNodesChange(
            previouslySelected.map((n) => ({ id: n.id, type: 'select', selected: true }))
          );
        }
      }
      const flowPoint = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      const candidates = nodesAtPoint(nodes, flowPoint);
      if (candidates.length > 1) {
        setOverlapPicker({
          x: event.clientX,
          y: event.clientY,
          candidates: candidates.map((n) => ({
            id: n.id,
            label:
              computeAnnotationAriaLabel(n.type, n.data, annotationContextValue.labels) || n.id,
          })),
        });
      } else {
        setOverlapPicker(null);
      }
    },
    [
      attachModeId,
      multiSelectMode,
      nodes,
      setNodes,
      onNodesChange,
      notifyAnnotationChange,
      notifyRemoteLockedAttempt,
      screenToFlowPosition,
      annotationContextValue.labels,
    ]
  );

  // Selects exactly one node (used by the overlap picker's own buttons and by
  // Escape-cancelling attach mode's "nothing to select" case alike) and moves
  // DOM focus to it — the same "land on the thing you just picked" contract
  // as `createAnnotation`'s own focus-after-create below.
  const selectAndFocusNode = useCallback(
    (id) => {
      const deselects = nodes.filter((n) => n.selected && n.id !== id);
      const changes = [
        ...deselects.map((n) => ({ id: n.id, type: 'select', selected: false })),
        { id, type: 'select', selected: true },
      ];
      onNodesChange(changes);
      requestAnimationFrame(() => {
        const el = reactFlowWrapper.current?.querySelector(
          `.react-flow__node[data-id="${window.CSS && CSS.escape ? CSS.escape(id) : id}"]`
        );
        el?.focus();
      });
    },
    [nodes, onNodesChange]
  );

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

      // Geometry gesture (task-annotation-exclusive-edit-leases): a plain
      // drag is centralised here for every annotation type, unlike resize/
      // rotate which stay per-component. Acquired fire-and-forget (ReactFlow
      // already started the visual drag by the time this fires — an
      // annotation already refused via `draggable` in isAnnotationDraggable
      // never reaches here at all) and released in onNodeDragStop below.
      const draggedAnnotationIds = set.filter((n) => ANNOTATION_TYPES.has(n.type)).map((n) => n.id);
      if (draggedAnnotationIds.length) beginEditing(draggedAnnotationIds);

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
    [edges, getFlowNodes, beginEditing]
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
      // Release whatever onNodeDragStart acquired, regardless of which
      // branch below this drag ends up taking (including the focus-view
      // early return just below) — otherwise a lease acquired for a drag
      // that never persists would sit held until its TTL expires instead of
      // freeing up immediately.
      const draggedSet =
        allDraggedNodes && allDraggedNodes.length > 0 ? allDraggedNodes : [draggedNode];
      const draggedAnnotationIds = draggedSet
        .filter((n) => ANNOTATION_TYPES.has(n.type))
        .map((n) => n.id);
      if (draggedAnnotationIds.length) endEditing(draggedAnnotationIds);

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

      // Attach/detach a dropped label/text/icon (the contract's
      // node-attachable types — `vote_dot` was one until task-annotation-
      // vote-dot-simplify removed it): within ATTACH_SNAP_RADIUS of a node or
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
    [setNodes, onNodePositionChange, getFlowNodes, recordMove, activeFocusRootId, endEditing]
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
        const graphActionNodes = selectedNodes.filter((n) => !ANNOTATION_TYPES.has(n.type));
        setNodeContextMenu(null);
        setEdgeContextMenu(null);
        setMultiNodeContextMenu({
          x: event.clientX,
          y: event.clientY,
          nodes: selectedNodes,
          actionNodes: graphActionNodes,
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
          // Re-emitted so a value that arrived from the server survives the
          // canvas leg. Dropping either here is what made a locked group's
          // flag revert on the next autosave, whatever the translators did.
          z: g.data.z ?? 0,
          locked: Boolean(g.data.locked),
          // Same envelope treatment as z/locked above, for the same reason:
          // server-owned same-field-conflict bookkeeping
          // (dec-annotation-field-patches-and-conflicts) that must survive
          // the canvas leg unchanged, not be dropped and re-invented on the
          // next autosave.
          version: g.data.version,
          field_versions: g.data.field_versions,
        })),
      annotations: viewNodes
        .filter((n) => OVERLAY_TYPES.has(n.type))
        .map(flowNodeToOverlay)
        .filter(Boolean),
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
        const graphActionNodes = selectedNodes.filter((n) => !ANNOTATION_TYPES.has(n.type));
        setNodeContextMenu(null);
        setEdgeContextMenu(null);
        setMultiNodeContextMenu({
          x: event.clientX,
          y: event.clientY,
          nodes: selectedNodes,
          actionNodes: graphActionNodes,
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

  // Edge context menu handler. When the right-clicked edge is part of a
  // larger multi-edge selection, dim/restore (and the future bulk actions
  // this same array shape supports) apply to the whole selection — mirroring
  // onNodeContextMenu's single-vs-multi-selection branch above — rather than
  // silently acting on just the one edge under the cursor.
  const onEdgeContextMenu = useCallback(
    (event, edge) => {
      event.preventDefault();
      event.stopPropagation();
      setNodeContextMenu(null);
      setMultiNodeContextMenu(null);
      const isMultiSelected =
        selectedEdges.length > 1 && selectedEdges.some((e) => e.id === edge.id);
      setEdgeContextMenu({
        x: event.clientX,
        y: event.clientY,
        edge: edge,
        edgeIds: isMultiSelected ? selectedEdges.map((e) => e.id) : [edge.id],
      });
    },
    [selectedEdges]
  );

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
    if (freehandActive) return undefined;
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return undefined;
    const detector = longPressDetectorRef.current;

    // Which pointers can hold to open a context menu. A stylus reports
    // `pointerType: 'pen'`, not 'touch', so restricting this to 'touch' meant
    // a pen long-press armed nothing at all and never reached a menu — on a
    // device where the pen is the primary input and has no right-click of its
    // own. `resolveTouchTarget` below already resolves the object under the
    // press, so admitting the pen is all it takes for a hold ON an annotation
    // to open THAT annotation's menu rather than the pane's.
    //
    // Touch stays gated on `isTouchMode` (the host's coarse-pointer signal)
    // exactly as before; the pen is admitted regardless, because a hybrid
    // device driven by pen can legitimately report a fine pointer. Desktop
    // mouse is unaffected either way — it never matches here and keeps
    // right-click.
    const isLongPressPointer = (event) =>
      event.pointerType === 'pen' || (isTouchMode && event.pointerType === 'touch');

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
      if (!isLongPressPointer(event)) return;
      // A hold that is about to erase is not a hold that wants a menu.
      if (event.pointerType === 'eraser' || activeToolRef.current?.kind === 'eraser') return;
      // A press that lands on a real control — the ✎ Edit trigger, a resize
      // handle, any button or field inside a node — belongs to that control,
      // not to the canvas. A stylus rests on a target noticeably longer than
      // a mouse click does, so without this the 500ms timer routinely won the
      // race against the tap: pressing ✎ with a pen opened the pane/node
      // context menu at the press point and the button's own activation
      // arrived afterwards to close it again, which read as "the Edit button
      // does nothing with a pen".
      // Real controls only — deliberately NOT `[role="button"]`. ReactFlow puts
      // `role="button"` on every focusable NODE wrapper
      // (@reactflow/core's NodeWrapper: `role: isFocusable ? 'button' :
      // undefined`), so matching that selector made this bail on every node
      // and long-press stopped opening any node's context menu at all — the
      // interaction this whole guard was added next to. A `<button>` element
      // still matches, which is what the ✎ Edit trigger and the node's own
      // expand/edit buttons are.
      if (event.target?.closest?.('button, input, textarea, select, .react-flow__resize-control')) {
        return;
      }
      const info = resolveTouchTarget(event.target);
      detector.onPointerDown(event.pointerId, event.clientX, event.clientY, {
        ...info,
        x: event.clientX,
        y: event.clientY,
      });
    };
    const handlePointerMove = (event) => {
      if (!isLongPressPointer(event)) return;
      detector.onPointerMove(event.pointerId, event.clientX, event.clientY);
    };
    const handlePointerUp = (event) => {
      if (!isLongPressPointer(event)) return;
      detector.onPointerUp(event.pointerId);
    };
    const handlePointerCancel = (event) => {
      if (!isLongPressPointer(event)) return;
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

    // The preview's own sample: the model point PLUS the pressure, using the
    // exact rule `samplePoint` (freehandStroke.js) applies to the samples that
    // get committed — so what the preview draws and what lands on the canvas
    // are derived from the same data. `toModelPoint` drops pressure because
    // the capture reads it from the event itself; the preview array has no
    // such second source and has to carry it.
    const toPreviewPoint = (event) => {
      const point = toModelPoint(event);
      if (Number.isFinite(event.pressure) && event.pressure > 0) point.pressure = event.pressure;
      return point;
    };

    const clearPreview = () => {
      const groupEl = freehandPreviewPathRef.current;
      if (groupEl) groupEl.replaceChildren();
    };

    // The preview draws the SAME variable-width stroke the committed
    // annotation will (FreehandAnnotationNode's own pressure rendering), not a
    // uniform line. It used to be a single <path> at a constant width, so a
    // pressure-sensitive stroke only revealed its thick and thin parts after
    // the pointer came up — the one moment the feedback is useless, since the
    // stroke is already finished. One <path> per pressure segment is what
    // makes the width visible while the hand is still drawing it.
    //
    // Still imperative (no React state) for the same reason as before: a fast
    // pointermove burst must not re-render the canvas. A pressure-less stroke
    // keeps the single-path fast route unchanged.
    const updatePreview = () => {
      const groupEl = freehandPreviewPathRef.current;
      if (!groupEl) return;
      const viewport = getViewportRef.current();
      const points = freehandModelPointsRef.current;
      const screenPoints = points.map((p) => {
        const point = { x: p.x * viewport.zoom + viewport.x, y: p.y * viewport.zoom + viewport.y };
        if (Number.isFinite(p.pressure)) point.pressure = p.pressure;
        return point;
      });

      const ns = 'http://www.w3.org/2000/svg';
      const paint = (el, d, width) => {
        el.setAttribute('d', d);
        el.setAttribute('stroke', DEFAULT_FREEHAND_COLOR);
        el.setAttribute('stroke-width', String(width));
        el.setAttribute('fill', 'none');
        el.setAttribute('stroke-linecap', 'round');
        el.setAttribute('stroke-linejoin', 'round');
      };

      if (!hasPressureData(screenPoints)) {
        // Zoom scales the on-screen width the same way the committed stroke's
        // own transform does, so the preview matches what lands.
        const width = DEFAULT_FREEHAND_STROKE_WIDTH * viewport.zoom;
        let pathEl = groupEl.firstElementChild;
        if (!pathEl || groupEl.childElementCount !== 1) {
          groupEl.replaceChildren();
          pathEl = document.createElementNS(ns, 'path');
          groupEl.appendChild(pathEl);
        }
        paint(pathEl, pointsToPathData(screenPoints), width);
        return;
      }

      const segments = buildPressureSegments(
        screenPoints,
        DEFAULT_FREEHAND_SMOOTHING,
        DEFAULT_FREEHAND_STROKE_WIDTH * viewport.zoom
      );
      // Reuse the existing path elements and only add/remove the difference,
      // rather than replacing the whole subtree on every pointermove.
      while (groupEl.childElementCount > segments.length) groupEl.lastElementChild.remove();
      while (groupEl.childElementCount < segments.length) {
        groupEl.appendChild(document.createElementNS(ns, 'path'));
      }
      segments.forEach((segment, i) => paint(groupEl.children[i], segment.d, segment.width));
    };

    const abandonStroke = () => {
      freehandPrimaryPointerIdRef.current = null;
      freehandModelPointsRef.current = [];
      clearPreview();
    };

    // Sticky, like every other tool. It used to disarm itself after one
    // stroke, so lifting the pen and putting it down again panned the canvas
    // instead of drawing the next line — the tool was gone without anything
    // saying so, and a drawing is rarely one stroke. Escape, the toolbox's own
    // button and the mobile sheet closing all still disarm it.
    const finishStroke = () => {
      abandonStroke();
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
      freehandModelPointsRef.current = [toPreviewPoint(event)];
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
        freehandModelPointsRef.current.push(toPreviewPoint(sample));
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

  // Erase-by-drag (task-annotation-tool-modes). Two independent ways in, which
  // is why this listens unconditionally rather than only while the tool is
  // armed:
  //   - the toolbox's eraser tool, for any pointer;
  //   - `pointerType === 'eraser'`, the inverted (back) tip of a stylus such
  //     as the Surface pen, which browsers report as its own pointer type.
  //     That one deliberately ignores whichever tool is armed: flipping the
  //     pen over IS the gesture, and having to arm a tool first would defeat
  //     the point. Nothing else in the app claims that pointer type, so this
  //     cannot shadow another gesture.
  //
  // Erasing is per-gesture idempotent (`erasedThisStroke`): dragging back and
  // forth over the same object must not delete it, then delete whatever
  // ReactFlow re-renders under that point next. Annotations are deleted
  // (they are the user's own scratch marks); graph nodes and edges are only
  // HIDDEN, never deleted — the eraser is a canvas-tidying tool, and a
  // dragged-over node is far too easy to hit to let it destroy graph data.
  useEffect(() => {
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return undefined;

    // A stylus's inverted tip. Chromium reports it as its own pointer type;
    // some stacks instead report a normal `pen` while flagging the eraser in
    // the button bits (button 5 / buttons bit 0x20, per the Pointer Events
    // spec). Accepting both is what makes the Surface pen's back end work
    // rather than only the one platform that takes the first route.
    const wantsErase = (event) =>
      isEraserTipEvent(event) || activeToolRef.current?.kind === 'eraser';

    // Resolve what sits under the pointer through the DOM rather than through
    // ReactFlow's own hit-testing: the eraser has to work against whatever is
    // actually painted at that pixel, including a node under a translucent
    // annotation, and elementFromPoint already answers exactly that. Edge ids
    // come from ReactFlow's own `rf__edge-<id>` test id, the only stable id
    // carrier on an edge group.
    const resolveTarget = (clientX, clientY) => {
      const el = document.elementFromPoint(clientX, clientY);
      if (!el) return null;
      const nodeEl = el.closest('.react-flow__node');
      if (nodeEl?.dataset?.id) return { kind: 'node', id: nodeEl.dataset.id };
      const edgeEl = el.closest('.react-flow__edge');
      const testId = edgeEl?.getAttribute('data-testid');
      if (testId?.startsWith('rf__edge-')) {
        return { kind: 'edge', id: testId.slice('rf__edge-'.length) };
      }
      return null;
    };

    // The footprint of the element being erased, measured before it is
    // removed. Falls back to a small box around the pointer when the element
    // cannot be measured (jsdom, or an element already detached).
    const rectOfErased = (clientX, clientY) => {
      const el = document.elementFromPoint(clientX, clientY)?.closest?.('.react-flow__node');
      const rect = el?.getBoundingClientRect?.();
      if (rect && (rect.width || rect.height)) {
        // Clamped around the pointer. An unbounded rect can exceed the
        // viewport for a large shape at high zoom, so the pointer could never
        // leave it and nothing was erasable for the rest of the stroke; it
        // also blocked a rotated annotation's whole bounding box rather than
        // its figure.
        const cx = clientX;
        const cy = clientY;
        const box = {
          left: Math.max(rect.left, cx - ERASE_BLOCK_MAX_PX),
          right: Math.min(rect.right, cx + ERASE_BLOCK_MAX_PX),
          top: Math.max(rect.top, cy - ERASE_BLOCK_MAX_PX),
          bottom: Math.min(rect.bottom, cy + ERASE_BLOCK_MAX_PX),
        };
        // `closest` can return an ancestor whose box does not contain the
        // pointer (a child overflowing it — rotated content, handles), which
        // would invert the clamp and leave a box that blocks nothing.
        if (box.left > box.right || box.top > box.bottom) return null;
        return box;
      }
      return {
        left: clientX - ERASE_RESTEP_PX,
        right: clientX + ERASE_RESTEP_PX,
        top: clientY - ERASE_RESTEP_PX,
        bottom: clientY + ERASE_RESTEP_PX,
      };
    };

    const eraseAt = (clientX, clientY) => {
      // Never act on what this stroke's own deletion just revealed. Erasing a
      // small annotation sitting on a graph node would otherwise hide that
      // node the moment the pointer moved — the per-object key cannot prevent
      // it, because whatever is underneath is simply a different key.
      //
      // The gate is the erased element's own rectangle rather than a fixed
      // radius: a radius only delays the cascade until the pointer has moved
      // past it, and picks a distance that is wrong for both a vote dot and a
      // full-width note. Leaving the erased object's footprint is the honest
      // signal that the user has moved on to something else.
      const blocked = eraseBlockRectRef.current;
      if (blocked) {
        if (
          clientX >= blocked.left &&
          clientX <= blocked.right &&
          clientY >= blocked.top &&
          clientY <= blocked.bottom
        ) {
          return;
        }
        eraseBlockRectRef.current = null;
      }

      const target = resolveTarget(clientX, clientY);
      if (!target) return;
      const key = `${target.kind}:${target.id}`;
      if (erasedThisStrokeRef.current.has(key)) return;

      if (target.kind === 'edge') {
        erasedThisStrokeRef.current.add(key);
        eraseBlockRectRef.current = rectOfErased(clientX, clientY);
        onHideEdge?.(target.id);
        return;
      }

      const node = latestNodesRef.current.find((n) => n.id === target.id);
      if (!node) return;
      // A group is deliberately NOT erasable. Removing one is not a filter:
      // its children have to be un-parented and their parent-relative
      // positions converted back to absolute (GroupNode's
      // removeGroupKeepChildren). Deleting it here would leave children
      // pointing at a parent that no longer exists. `deleteSelectedNodes`
      // excludes groups for the same reason (it gates on OVERLAY_TYPES, not
      // ANNOTATION_TYPES) — and a group's large empty interior is exactly the
      // kind of thing a sweeping eraser lands on by accident.
      if (node.type === 'group') return;

      if (OVERLAY_TYPES.has(node.type)) {
        // Same guards the bulk delete applies: an annotation another client
        // is editing, or one the user locked on purpose, is not erasable by
        // sweeping a pointer over it. Checked BEFORE anything is recorded, so
        // sweeping over a locked annotation does not also block whatever sits
        // near it.
        if (isRemoteLocked(node.data) || node.data?.locked) return;
        erasedThisStrokeRef.current.add(key);
        eraseBlockRectRef.current = rectOfErased(clientX, clientY);
        setNodes((nds) => nds.filter((n) => n.id !== target.id));
        onAnnotationChangeRef.current?.('delete');
        // The same edit-lease handshake every other annotation delete performs.
        beginEditingRef.current?.([target.id]).then(() => endEditingRef.current?.([target.id]));
        return;
      }
      // Armed for the hide half as well. Recording it only for annotation
      // deletes left the cascade live for the other half: hiding a graph node
      // revealed the edge running under it, and the next pointermove hid that
      // too.
      erasedThisStrokeRef.current.add(key);
      eraseBlockRectRef.current = rectOfErased(clientX, clientY);
      onHide?.(target.id);
    };

    const handlePointerDown = (event) => {
      if (!wantsErase(event) || erasingPointerIdRef.current !== null) return;
      erasingPointerIdRef.current = event.pointerId;
      erasedThisStrokeRef.current.clear();
      eraseBlockRectRef.current = null;
      // Capture the pointer so `pointerup` is delivered here even when the
      // sweep ends outside the wrapper (over browser chrome, or off-window).
      // Without it that release is lost, `erasingPointerIdRef` stays set, and
      // the guard above then rejects every later erase for the life of the
      // component — the eraser silently dies.
      try {
        wrapper.setPointerCapture(event.pointerId);
      } catch {
        // Capture is a convenience, not a requirement: the window-level
        // release listeners below are the backstop.
      }
      // Claim the gesture before ReactFlow can start a pan or a marquee with
      // it. Without this the canvas pans away underneath the stroke.
      event.preventDefault();
      event.stopPropagation();
      eraseAt(event.clientX, event.clientY);
    };

    const handlePointerMove = (event) => {
      if (event.pointerId !== erasingPointerIdRef.current) return;
      event.preventDefault();
      eraseAt(event.clientX, event.clientY);
    };

    const endStroke = (event) => {
      if (event.pointerId !== erasingPointerIdRef.current) return;
      try {
        wrapper.releasePointerCapture(event.pointerId);
      } catch {
        // Already released, or never captured.
      }
      erasingPointerIdRef.current = null;
      erasedThisStrokeRef.current.clear();
      eraseBlockRectRef.current = null;
    };

    // Capture phase so the gesture is claimed before ReactFlow's own pane
    // handlers see it, for the same reason the freehand capture above runs
    // where it does.
    wrapper.addEventListener('pointerdown', handlePointerDown, true);
    wrapper.addEventListener('pointermove', handlePointerMove, true);
    wrapper.addEventListener('pointerup', endStroke, true);
    wrapper.addEventListener('pointercancel', endStroke, true);
    // Belt to the pointer-capture braces: a release the wrapper never sees at
    // all still ends the stroke.
    window.addEventListener('pointerup', endStroke, true);
    window.addEventListener('pointercancel', endStroke, true);
    return () => {
      wrapper.removeEventListener('pointerdown', handlePointerDown, true);
      wrapper.removeEventListener('pointermove', handlePointerMove, true);
      wrapper.removeEventListener('pointerup', endStroke, true);
      wrapper.removeEventListener('pointercancel', endStroke, true);
      window.removeEventListener('pointerup', endStroke, true);
      window.removeEventListener('pointercancel', endStroke, true);
      // Never leave the gesture latched across a teardown.
      erasingPointerIdRef.current = null;
      erasedThisStrokeRef.current.clear();
      eraseBlockRectRef.current = null;
    };
    // No `nodes` dependency on purpose — see erasingPointerIdRef above.
  }, [setNodes, onHide, onHideEdge]);

  // Drag-to-draw placement (task-annotation-drag-to-draw).
  //
  // Placement used to ride on ReactFlow's `onPaneClick`, which does not fire
  // while the pane is in selection mode (`selectionOnDrag`, the desktop
  // default here) — the pane swallowed the click as a marquee gesture, so an
  // armed tool produced nothing at all with a mouse. Owning the gesture on the
  // wrapper, the way the freehand and erase gestures already do, removes that
  // dependency entirely and is also what makes sizing possible: a press
  // fixes one corner, the drag sizes the box, and the release commits it, so
  // the user sees the shape they are going to get before letting go.
  //
  // A press with no meaningful drag still creates at the pressed point in the
  // kind's default size — the plain click-to-place gesture, unchanged.
  useEffect(() => {
    const wrapper = reactFlowWrapper.current;
    if (!wrapper) return undefined;

    // Only the kinds with a real box can be sized by dragging. A vote dot, an
    // icon, a label and a text annotation have a fixed or content-driven size
    // (RESIZABLE_KINDS/SIZED_GENERIC_KINDS exclude them), so a drag would have
    // nothing to apply; those place at the press point and ignore the rest.
    const SIZABLE = new Set(['shape', 'note']);
    const MIN_DRAG_PX = 6;
    // Matches GenericAnnotationNode's own MIN_SIZE, so a drawn box can never
    // be smaller than the resizer would allow it to be dragged to.
    const MIN_DRAWN_SIZE = MIN_ANNOTATION_SIZE;

    const armedPlacement = () => {
      const tool = activeToolRef.current;
      if (!tool) return null;
      if (tool.kind === 'select' || tool.kind === 'eraser' || tool.kind === 'freehand') return null;
      if (tool.kind === 'image') return null;
      return tool;
    };

    const handlePointerDown = (event) => {
      const tool = armedPlacement();
      if (!tool) return;
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      // A stylus's eraser tip erases whatever is armed — never draws. This has
      // to reject exactly what the erase gesture accepts, including the
      // button-bits form, or on those stacks one flip of the pen would erase
      // along the stroke AND commit an object on release.
      if (isEraserTipEvent(event)) return;
      // A second pointer is the start of a pinch, not a second placement.
      // Abandoning rather than hijacking is what keeps two-finger zoom usable
      // while a tool is armed — which matters because these tools are sticky,
      // so "armed" is the resting state, not a moment.
      //
      // Suspended rather than merely nulled: a third contact (a palm landing
      // during a two-finger zoom, or the first finger re-landing while the
      // second is still down) would otherwise find no gesture in flight, start
      // a fresh one, and commit an annotation when it lifted. The suspension
      // lasts until every pointer is up.
      if (placementRef.current || placementSuspendedRef.current) {
        placementRef.current = null;
        placementSuspendedRef.current = true;
        hidePreview();
        return;
      }
      // Only a press on EMPTY canvas starts a placement. Landing on an
      // existing node, an edge or a control has to keep meaning what it
      // already means — selecting, dragging, pressing a button — or an armed
      // tool makes the rest of the canvas unusable, and since these tools are
      // sticky that is the resting state rather than a moment.
      //
      // Tested by exclusion, not by requiring `.react-flow__pane`: in
      // ReactFlow the nodes live INSIDE the pane
      // (.react-flow__pane > .react-flow__viewport > .react-flow__nodes >
      // .react-flow__node), so a "did this land in the pane?" check is
      // satisfied by every node too and let a press on an existing annotation
      // create a second one on top of it.
      const target = event.target;
      if (!target?.closest) return;
      // `.react-flow__nodesselection` is a DIRECT child of the pane, outside
      // the viewport, so no node/edge selector reaches it — and its rect
      // carries `pointer-events: all` across the whole multi-selection box.
      // Without it here, pressing a multi-selection to drag it created an
      // annotation on top of the selection instead.
      if (
        target.closest(
          '.react-flow__node, .react-flow__edge, .react-flow__resize-control, .react-flow__nodesselection'
        )
      ) {
        return;
      }
      if (target.closest('button, input, textarea, select, [role="button"]')) return;
      if (!target.closest('.react-flow__pane')) return;

      // Any earlier gesture's pending click is stale by now. On touch and pen
      // the compatibility click is suppressed by the preventDefault below, so
      // the flag would otherwise stay armed and the swallow would eat the
      // user's NEXT tap on the canvas instead.
      pendingPlacementClickRef.current = false;
      placementRef.current = {
        pointerId: event.pointerId,
        tool,
        startX: event.clientX,
        startY: event.clientY,
        dragged: false,
      };
      event.preventDefault();
      event.stopPropagation();
    };

    // Claiming the gesture from ReactFlow needs a SECOND block, on the legacy
    // events. d3-zoom — which is what `panOnDrag` drives — binds
    // `mousedown.zoom` and `touchstart.zoom`, not pointer events, so
    // `stopPropagation` on a pointerdown never reached it and the canvas
    // panned along with the drag that was sizing the object. Both corners are
    // then converted at release with the moved transform, collapsing the box
    // to nothing — which the minimum-size clamp would have quietly turned into
    // a 40x40 shape at the wrong place instead of an obvious failure.
    //
    // Blocked only while THIS gesture owns the pointer, and only for a single
    // contact: a second finger abandons the placement (see above) and its own
    // `touchstart` is let through carrying both touches, so d3-zoom can still
    // start a pinch. That is what keeps zoom usable while a sticky tool is
    // armed instead of trading one regression for the other.
    const blockCanvasGesture = (event) => {
      if (!placementRef.current) return;
      if (event.type === 'touchstart' && event.touches && event.touches.length > 1) return;
      event.preventDefault();
      event.stopPropagation();
    };

    // Swallow the click the browser synthesizes at the end of the gesture.
    // Blocking `mousedown` above means the canvas never recorded a press
    // position, so its own click handler cannot tell this click apart from a
    // plain one and falls through to clearing the selection — deselecting the
    // annotation that was just created and focused. One click, once, per
    // completed placement.
    const swallowSyntheticClick = (event) => {
      if (!pendingPlacementClickRef.current) return;
      pendingPlacementClickRef.current = false;
      // Only a click on the canvas surface. The toolbox lives inside this
      // wrapper too, so an unscoped swallow ate the user's NEXT button press —
      // arming a different tool right after drawing silently did nothing.
      if (event.target?.closest?.('button, input, textarea, select, [role="button"]')) return;
      if (!event.target?.closest?.('.react-flow__pane')) return;
      event.preventDefault();
      event.stopPropagation();
    };

    const hidePreview = () => {
      const previewEl = placementPreviewRef.current;
      if (previewEl) previewEl.style.display = 'none';
    };

    const updatePreview = (event) => {
      const placement = placementRef.current;
      const previewEl = placementPreviewRef.current;
      if (!placement || !previewEl) return;
      const left = Math.min(placement.startX, event.clientX);
      const top = Math.min(placement.startY, event.clientY);
      const width = Math.abs(event.clientX - placement.startX);
      const height = Math.abs(event.clientY - placement.startY);
      const rect = wrapper.getBoundingClientRect();
      previewEl.style.display = 'block';
      previewEl.style.left = `${left - rect.left}px`;
      previewEl.style.top = `${top - rect.top}px`;
      previewEl.style.width = `${width}px`;
      previewEl.style.height = `${height}px`;
    };

    const handlePointerMove = (event) => {
      const placement = placementRef.current;
      if (!placement || event.pointerId !== placement.pointerId) return;
      const far =
        Math.abs(event.clientX - placement.startX) > MIN_DRAG_PX ||
        Math.abs(event.clientY - placement.startY) > MIN_DRAG_PX;
      if (far) placement.dragged = true;
      if (placement.dragged && SIZABLE.has(placement.tool.kind)) updatePreview(event);
    };

    const handlePointerUp = (event) => {
      const placement = placementRef.current;
      if (!placement || event.pointerId !== placement.pointerId) return;
      placementRef.current = null;
      pendingPlacementClickRef.current = true;
      hidePreview();

      const { tool, startX, startY } = placement;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      const sized = placement.dragged && SIZABLE.has(tool.kind);

      if (!sized) {
        createAnnotationRef.current(
          tool.kind,
          screenToFlowPositionRef.current({ x: startX, y: startY }),
          tool.options
        );
        return;
      }

      // The box is anchored at the press point and grows toward the release
      // point in flow space, so it is correct at any zoom. Dragging back past
      // the press point mirrors rather than inverting the box: the drawn
      // figure points the way the hand moved, which for a triangle or a
      // process arrow is the whole reason to drag in that direction.
      const a = screenToFlowPositionRef.current({ x: startX, y: startY });
      const b = screenToFlowPositionRef.current({ x: event.clientX, y: event.clientY });
      // `MIN_DRAG_PX` is satisfied by EITHER axis, so a drag along one axis
      // alone leaves the other at zero. Both are therefore floored at the
      // resizer's own minimum — and a regular subtype, whose proportion is
      // recomputed from the WIDTH alone (`regularShapeSize`), is sized from
      // the longer side rather than from `dx`: sizing it from a near-zero
      // width meant a mostly-vertical drag always produced a minimum-size
      // triangle no matter how far the user actually dragged.
      const sweptWidth = Math.round(Math.abs(b.x - a.x));
      const sweptHeight = Math.round(Math.abs(b.y - a.y));
      const regular = regularShapeAspect(tool.options?.shape || 'rectangle') !== null;
      const box = regular
        ? { width: Math.max(MIN_DRAWN_SIZE, sweptWidth, sweptHeight), height: MIN_DRAWN_SIZE }
        : {
            width: Math.max(MIN_DRAWN_SIZE, sweptWidth),
            height: Math.max(MIN_DRAWN_SIZE, sweptHeight),
          };
      createAnnotationRef.current(
        tool.kind,
        { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y) },
        {
          ...tool.options,
          box,
          // Thresholded, not a raw sign test: a long downward drag with a
          // couple of pixels of leftward jitter would otherwise silently
          // mirror the shape and aim a process arrow the wrong way.
          flipX: dx < -MIN_DRAG_PX,
          flipY: dy < -MIN_DRAG_PX,
        }
      );
    };

    const handlePointerCancel = (event) => {
      const placement = placementRef.current;
      if (!placement || event.pointerId !== placement.pointerId) return;
      placementRef.current = null;
      hidePreview();
    };

    // Lifting the LAST contact clears a suspension, so the next press is a
    // fresh placement again. Tracked with a real set of live pointer ids: the
    // previous `event.buttons` check was dead code, because `buttons` is 0 on
    // every pointerup by spec (the contact is gone). The suspension therefore
    // ended at the first finger up, and a third contact during the rest of a
    // pinch could start a placement and commit an object — the exact case it
    // was added to close.
    const livePointers = new Set();
    const trackPointerDown = (event) => livePointers.add(event.pointerId);
    const clearSuspension = (event) => {
      livePointers.delete(event.pointerId);
      if (livePointers.size === 0) placementSuspendedRef.current = false;
    };

    wrapper.addEventListener('pointerdown', handlePointerDown, true);
    wrapper.addEventListener('pointermove', handlePointerMove, true);
    wrapper.addEventListener('pointerup', handlePointerUp, true);
    wrapper.addEventListener('pointercancel', handlePointerCancel, true);
    wrapper.addEventListener('pointerdown', trackPointerDown, true);
    wrapper.addEventListener('pointerup', clearSuspension, true);
    wrapper.addEventListener('pointercancel', clearSuspension, true);
    // A release the wrapper never sees (pen or mouse let go outside the
    // window) would otherwise leave the id in `livePointers` forever, so the
    // set never empties and the suspension never lifts — placement would be
    // silently dead for the rest of the session. The erase gesture above
    // carries the same backstop for the same reason.
    window.addEventListener('pointerup', clearSuspension, true);
    window.addEventListener('pointercancel', clearSuspension, true);
    wrapper.addEventListener('mousedown', blockCanvasGesture, true);
    wrapper.addEventListener('click', swallowSyntheticClick, true);
    wrapper.addEventListener('touchstart', blockCanvasGesture, { capture: true, passive: false });
    return () => {
      wrapper.removeEventListener('pointerdown', handlePointerDown, true);
      wrapper.removeEventListener('pointermove', handlePointerMove, true);
      wrapper.removeEventListener('pointerup', handlePointerUp, true);
      wrapper.removeEventListener('pointercancel', handlePointerCancel, true);
      wrapper.removeEventListener('pointerdown', trackPointerDown, true);
      wrapper.removeEventListener('pointerup', clearSuspension, true);
      wrapper.removeEventListener('pointercancel', clearSuspension, true);
      window.removeEventListener('pointerup', clearSuspension, true);
      window.removeEventListener('pointercancel', clearSuspension, true);
      wrapper.removeEventListener('mousedown', blockCanvasGesture, true);
      wrapper.removeEventListener('click', swallowSyntheticClick, true);
      wrapper.removeEventListener('touchstart', blockCanvasGesture, { capture: true });
      placementRef.current = null;
      placementSuspendedRef.current = false;
    };
  }, []);

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

      // The annotation toolbox's fine-pointer (mouse) drag path — matches
      // FloatingToolbar's own dataTransfer convention, under its own MIME key
      // so the two palettes' payloads never collide. image/freehand never
      // reach here: AnnotationToolbox never makes either draggable.
      const annotationPayload = event.dataTransfer.getData('application/annotation-kind');
      if (annotationPayload) {
        let parsed = null;
        try {
          parsed = JSON.parse(annotationPayload);
        } catch {
          parsed = null;
        }
        if (parsed?.kind) {
          const dropPosition = screenToFlowPosition({ x: event.clientX, y: event.clientY });
          const { kind, ...options } = parsed;
          createAnnotation(kind, dropPosition, Object.keys(options).length ? options : undefined);
        }
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
    [
      screenToFlowPosition,
      onDropCreateNode,
      setNodes,
      onCreateGroup,
      ingestImageFile,
      createAnnotation,
    ]
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

      // Keyboard way IN to the property menu (task-annotation-accessible-
      // shared-controls, closing the accessibility audit's still-real
      // "Shift+F10/Menu key on the FOCUSED node wrapper never opens the
      // menu" gap — see docs/ANNOTATION_CONTRACT.md's audit, area 1). A
      // keyboard-dispatched `contextmenu` event still never reaches any
      // kind's own `onContextMenu` handler (bound on a DOM descendant of the
      // focused ReactFlow node wrapper, so a bubbling event whose target IS
      // that wrapper never reaches it) — rather than reworking six kinds'
      // event wiring, or trying to intercept a `contextmenu` event ReactFlow
      // itself never lets escape its own node wrapper, this reuses the
      // Edit button task-annotation-responsive-bottom-toolbox already added
      // to five of six kinds (and this task adds to the sixth, `group`) as
      // the keyboard target: Shift+F10 or the Menu key, while focus is on an
      // annotation's node wrapper, clicks that same visible, focus-managed
      // button — the one real entry point every kind already has, rather
      // than a second, parallel mechanism.
      if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
        const wrapper = e.target.closest?.('.react-flow__node');
        const trigger = wrapper?.querySelector('.annotation-edit-trigger');
        if (trigger) {
          e.preventDefault();
          trigger.click();
          return;
        }
      }

      if (e.key === 'Escape') {
        // The non-drag "Attach to…" mode takes priority over the generic
        // Escape-clears-selection/closes-menus branch below — cancelling it
        // must not also drop whatever selection the user had before opening
        // the annotation's menu.
        if (attachModeId) {
          setAttachModeId(null);
          return;
        }
        if (overlapPicker) {
          setOverlapPicker(null);
          return;
        }
        if (freehandActive) {
          setFreehandActive(false);
          setActiveTool({ kind: 'select' });
          return;
        }
        // Escape also puts a sticky placement tool (or the eraser) away, the
        // same "back out of the current mode" meaning it already has for
        // freehand, an open menu and a live selection.
        if (activeToolRef.current?.kind !== 'select') {
          setActiveTool({ kind: 'select' });
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
          deleteSelectedNodes();
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
    onHideEdge,
    closeAllMenus,
    clearSelection,
    organizeSelection,
    showNotification,
    cml.organizeHint,
    handleUndo,
    handleRedo,
    deleteSelectedNodes,
    freehandActive,
    attachModeId,
    overlapPicker,
  ]);

  // Create group when signal changes (triggered from toolbar). Unlike
  // saveViewSignal, the host never resets createGroupSignal back to 0 after
  // handling it, so a plain `> 0` guard stays true forever once the first
  // group has been created — and this effect re-runs on every render where
  // handleAddGroup changes identity, not only when createGroupSignal itself
  // changes. That's safe today only because handleAddGroup's own deps
  // (screenToFlowPosition, setNodes, onCreateGroup) happen to stay
  // referentially stable; if any of them ever lost memoization, every such
  // re-render would create a spurious group. Latching the last signal value
  // actually handled makes this robust without depending on host
  // cooperation. The latch updates only inside the branch that calls
  // handleAddGroup — not on every observed signal change — so a signal that
  // arrives while a focus view is active is still honoured once focus exits,
  // same as before this fix.
  useEffect(() => {
    if (
      createGroupSignal > 0 &&
      createGroupSignal !== handledCreateGroupSignalRef.current &&
      !activeFocusRootId
    ) {
      handledCreateGroupSignalRef.current = createGroupSignal;
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
      // The sibling of the overlay restore's own filter below. A stored group
      // that is not a usable object throws on `g.id` here — inside an effect,
      // so outside every AnnotationErrorBoundary, taking the whole canvas with
      // it. A group is an annotation kind and answers to the same rule as the
      // rest: skip it, keep the others.
      const groupNodes = groups
        .filter((g) => g && typeof g === 'object' && typeof g.id === 'string' && g.id)
        .map((g) => ({
          id: g.id,
          type: 'group',
          // Defaulted like every sibling path (overlayToFlowNode, the remote
          // upsert-group branch below, sessionAnnotations.js). A node with no
          // position throws inside ReactFlow's store, which no boundary reaches.
          position: g.position || { x: 0, y: 0 },
          data: {
            label: g.label || 'Group',
            description: g.description || '',
            color: g.color || '#646cff',
            z: g.z ?? 0,
            locked: Boolean(g.locked),
            version: g.version,
            field_versions: g.field_versions,
          },
          style: g.style || { width: 300, height: 200 },
          // A locked group box stays selectable but cannot be dragged; its
          // members are separate nodes and keep their own draggability.
          // `undefined` rather than `true` when unlocked, deliberately:
          // ReactFlow resolves `node.draggable || (nodesDraggable &&
          // typeof node.draggable === 'undefined')`, so an explicit boolean
          // overrides the canvas-wide switch while `undefined` (present or
          // absent alike) defers to it. A plain `!g.locked` would therefore
          // keep an unlocked group draggable while a freehand stroke is being
          // drawn, which is exactly what that switch turns off.
          draggable: g.locked ? false : undefined,
        }));
      // Built from what survived, not the raw input. A group with a numeric id
      // — which JSON permits — is dropped by the filter but would land in a
      // raw-derived set, and a matching `parentIds` value is truthy, so a node
      // would be parented onto a group that is not on the canvas. That throws
      // inside ReactFlow's store, where no boundary reaches.
      const groupIdSet = new Set(groupNodes.map((g) => g.id));
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
    // A stored annotation the translator cannot make a node out of is dropped
    // rather than allowed to become a null in the node list, which ReactFlow
    // would throw on. Annotation shapes may change without migrating what is
    // stored, so this is an expected condition, not a defensive flourish.
    const overlayNodes = annotationsToRestore
      .filter((a) => OVERLAY_TYPES.has(a?.kind))
      .map(overlayToFlowNode)
      .filter(Boolean);
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
  //
  // `locked` freezes ALL geometry change (dec-annotation-lock-semantics point
  // 1), not only user-initiated edits — including this resolve. A locked
  // arrow's endpoints stay exactly where they were the moment it was locked,
  // even while its anchored target keeps moving; locking already drops the
  // anchor itself (set_annotation_lock, backend/service/mcp_tools.py), so
  // there is nothing left claiming a binding this effect isn't honouring.
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
      const resolved = n.data?.locked ? null : resolveAnchoredArrow(n, centers);
      // Folds in the same remote-lease exclusivity the remote-lease effect
      // applies, so this effect (which runs on every `nodes` change, far more
      // often) never resets `draggable` back to true out from under a lease
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

  // Keep an attached label/text/icon glued to its attachment
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
  //
  // `locked` freezes ALL geometry change (dec-annotation-lock-semantics point
  // 1), so a locked attached overlay is skipped here too, exactly like the
  // arrow anchor effect above — locking already dropped the attachment
  // itself (set_annotation_lock, backend/service/mcp_tools.py).
  useEffect(() => {
    const centers = new Map();
    let hasAttached = false;
    for (const n of nodes) {
      if (n.type === 'arrow') continue;
      const c = nodeCenter(n);
      if (c) centers.set(n.id, c);
      if (ATTACHABLE_OVERLAY_KINDS.has(n.type) && n.data?.attachment && !n.data?.locked)
        hasAttached = true;
    }
    if (!hasAttached) return;
    const updates = new Map();
    for (const n of nodes) {
      if (
        !ATTACHABLE_OVERLAY_KINDS.has(n.type) ||
        !n.data?.attachment ||
        n.data?.locked ||
        n.dragging
      )
        continue;
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
  // Purely cosmetic (task-annotation-exclusive-edit-leases): selection never
  // blocks local dragging — see the remote-lease effect right below for that.
  useEffect(() => {
    setNodes((nds) =>
      nds.map((n) => {
        if (!ANNOTATION_TYPES.has(n.type)) return n;
        const marker = remoteSelections?.[n.id] ?? null;
        if (!marker && !n.data?.remoteSelection) return n;
        return { ...n, data: { ...n.data, remoteSelection: marker } };
      })
    );
  }, [remoteSelections, setNodes]);

  // Mirror live remote *edit leases* onto annotation nodes
  // (task-annotation-exclusive-edit-leases). A lease held by another client
  // blocks local dragging here — leases are exclusive (first-actual-editor-
  // wins), unlike the purely cosmetic selection markers the effect above
  // stamps. Same shape/effect-per-source-of-truth split the selection effect
  // above uses, and for the same reason: annotation nodes live only in
  // ReactFlow's own node state, not the host's `inputNodes` prop, so pushing
  // a live map onto them needs an effect rather than a render-time memo.
  useEffect(() => {
    setNodes((nds) =>
      nds.map((n) => {
        if (!ANNOTATION_TYPES.has(n.type)) return n;
        const marker = remoteLeases?.[n.id] ?? null;
        if (!marker && !n.data?.remoteLease) return n;
        const nextData = { ...n.data, remoteLease: marker };
        const draggable = isAnnotationDraggable({ ...n, data: nextData });
        // A group that may be dragged resolves `draggable` to `undefined`, so
        // it keeps deferring to the canvas-wide `nodesDraggable` switch — the
        // same value the two group builders write. Writing an explicit `true`
        // here would pin that decision for the rest of the session the first
        // time a collaborator's lease on the group cleared, leaving it
        // draggable during a freehand stroke. Overlays keep the explicit
        // boolean they are hydrated with.
        return {
          ...n,
          data: nextData,
          draggable: n.type === 'group' && draggable ? undefined : draggable,
        };
      })
    );
  }, [remoteLeases, setNodes]);

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
        // A malformed op from a peer or an agent must not take this client's
        // canvas down with it.
        if (flowNode) {
          setNodes((nds) =>
            reorderNodesForParentChild([...nds.filter((n) => n.id !== flowNode.id), flowNode])
          );
        }
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
            z: g.z ?? 0,
            locked: Boolean(g.locked),
            version: g.version,
            field_versions: g.field_versions,
          },
          style: g.style || { width: 300, height: 200 },
          draggable: g.locked ? false : undefined,
        };
        const members = new Set(op.members || []);
        setNodes((nds) => {
          // A member named here may arrive with no parent at all — most
          // notably right after removeGroupKeepChildren (GroupNode.jsx)
          // re-based it to absolute coordinates on delete — or with a
          // different parent. Either way its position was computed in a
          // different coordinate space than this group's, so re-parenting
          // has to convert it the same way the local drag path does
          // (computeGroupPlacement above): resolve to absolute using
          // whatever parent it had, then subtract this group's origin.
          // Skipped when the member already belongs here so a redundant
          // upsert (e.g. a label-only edit) does not re-subtract the origin.
          const priorById = new Map(nds.map((n) => [n.id, n]));
          return reorderNodesForParentChild(
            [...nds.filter((n) => n.id !== g.id), groupNode].map((n) => {
              if (n.type === 'group') return n;
              if (members.has(n.id)) {
                if (n.parentId === g.id) return n;
                const priorParent = n.parentId ? priorById.get(n.parentId) : null;
                const absPos = priorParent
                  ? {
                      x: n.position.x + (priorParent.position?.x || 0),
                      y: n.position.y + (priorParent.position?.y || 0),
                    }
                  : n.position;
                return {
                  ...n,
                  parentId: g.id,
                  position: {
                    x: absPos.x - groupNode.position.x,
                    y: absPos.y - groupNode.position.y,
                  },
                  extent: undefined,
                };
              }
              if (n.parentId === g.id) {
                // The inverse of the join case just above: a member dropped
                // from the group (present here, absent from `members`) keeps
                // its parent-relative position but loses its parentId, so it
                // must be converted back to absolute — the same transform
                // removeGroupKeepChildren (GroupNode.jsx) applies on delete.
                // Use the group's position from BEFORE this upsert (priorById),
                // since the member's stored position is relative to that
                // origin, not to a position bundled into the same op (e.g. the
                // group being dragged in the same autosave that shrank it).
                const priorGroup = priorById.get(g.id);
                const groupPos = priorGroup?.position || groupNode.position;
                return {
                  ...n,
                  parentId: undefined,
                  position: {
                    x: n.position.x + (groupPos.x || 0),
                    y: n.position.y + (groupPos.y || 0),
                  },
                  extent: undefined,
                };
              }
              return n;
            })
          );
        });
      } else if (op.action === 'delete' && op.id) {
        setNodes((nds) =>
          nds
            .map((n) => (n.parentId === op.id ? { ...n, parentId: undefined } : n))
            .filter((n) => n.id !== op.id)
        );
      } else if (op.action === 'membership' && op.groupId) {
        // The store requires a group annotation to exist before it will accept a
        // membership change for it (session_manager.set_group_members), so a
        // same-client race between creation and membership cannot happen; what
        // reaches here naming a group this client doesn't have is a client that
        // hasn't caught up yet, or one that filtered the group out of its view
        // on purpose. Either way there is no node to reparent onto, and setting
        // parentId to a missing id throws from inside ReactFlow's own store
        // update - outside every React error boundary - and takes down the
        // whole canvas. Drop the op rather than queue it: a client that filtered
        // the group out would never resolve a queued entry, and the group's own
        // upsert (when it does arrive) carries its full current membership, so
        // nothing is lost by not replaying this one.
        const members = new Set(op.members || []);
        setNodes((nds) => {
          const groupNode = nds.find((n) => n.id === op.groupId);
          if (!groupNode) return nds;
          // Unlike upsert-group, this op never touches the group's own
          // properties — only membership — so there is no before/after
          // group-position split to worry about: `groupNode` here is the
          // right origin for both directions of the conversion below, the
          // same computeGroupPlacement pattern the local drag path and the
          // upsert-group branch above both use.
          const priorById = new Map(nds.map((n) => [n.id, n]));
          return reorderNodesForParentChild(
            nds.map((n) => {
              if (n.type === 'group') return n;
              if (members.has(n.id)) {
                if (n.parentId === op.groupId) return n;
                const priorParent = n.parentId ? priorById.get(n.parentId) : null;
                const absPos = priorParent
                  ? {
                      x: n.position.x + (priorParent.position?.x || 0),
                      y: n.position.y + (priorParent.position?.y || 0),
                    }
                  : n.position;
                return {
                  ...n,
                  parentId: op.groupId,
                  position: {
                    x: absPos.x - groupNode.position.x,
                    y: absPos.y - groupNode.position.y,
                  },
                  extent: undefined,
                };
              }
              if (n.parentId === op.groupId) {
                return {
                  ...n,
                  parentId: undefined,
                  position: {
                    x: n.position.x + groupNode.position.x,
                    y: n.position.y + groupNode.position.y,
                  },
                  extent: undefined,
                };
              }
              return n;
            })
          );
        });
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

  // Every annotation type is wrapped; `custom` (a graph node) deliberately is
  // not. A graph node failing to render is a defect that should be loud, and
  // its data is the user's real work rather than a decoration whose stored
  // shape this project reserves the right to change.
  const nodeTypes = useMemo(() => {
    // The boundary takes the reporter and its label from AnnotationContext,
    // which every annotation already consumes. That keeps this map's
    // dependency list empty: ReactFlow remounts every node when `nodeTypes`
    // changes identity, so a map that rebuilt on a label or callback change
    // would blow away the canvas's DOM for a language switch.
    // The computed key names the component without assigning to it after the
    // fact — React treats a component as immutable, and devtools read `name`
    // when there is no displayName.
    const guard = (Component, kind) =>
      ({
        [`Guarded(${kind})`]: (props) => (
          <AnnotationErrorBoundary nodeId={props.id}>
            <Component {...props} />
          </AnnotationErrorBoundary>
        ),
      })[`Guarded(${kind})`];
    return {
      custom: CustomNode,
      group: guard(GroupNode, 'group'),
      note: guard(NoteNode, 'note'),
      label: guard(LabelNode, 'label'),
      arrow: guard(ArrowNode, 'arrow'),
      text: guard(GenericAnnotationNode, 'text'),
      shape: guard(GenericAnnotationNode, 'shape'),
      icon: guard(GenericAnnotationNode, 'icon'),
      vote_dot: guard(GenericAnnotationNode, 'vote_dot'),
      image: guard(GenericAnnotationNode, 'image'),
      freehand: guard(FreehandAnnotationNode, 'freehand'),
    };
  }, []);

  // Screen-reader accessible name (task-annotation-accessible-shared-
  // controls, closing the accessibility audit's ten-row "no kind has a
  // designed accessible name" gap): the one place every annotation/group
  // node's `ariaLabel` field is derived, rather than ten separate call sites
  // across every creation/hydration/remote-op path (createAnnotation,
  // overlayToFlowNode, commitFreehandStroke, handleAddGroup, the drop-Group
  // branch, the saved-view group restore, the remote upsert-group branch —
  // several of which build a fresh node object on every edit, e.g. a rename).
  // Deriving it here instead means it is always current, for every kind,
  // with none of those call sites needing to know this field exists — a
  // stale label after a rename/recolour/retype is structurally impossible.
  // Reuses the identity of any node whose computed label already matches
  // (including every non-annotation graph node, for which
  // computeAnnotationAriaLabel returns '' and this leaves the node
  // untouched), so `memo()`-wrapped node components still skip re-rendering
  // when nothing about them actually changed.
  const nodesWithAriaLabels = useMemo(
    () =>
      nodes.map((n) => {
        const ariaLabel = computeAnnotationAriaLabel(n.type, n.data, annotationContextValue.labels);
        if (!ariaLabel || n.ariaLabel === ariaLabel) return n;
        return { ...n, ariaLabel };
      }),
    [nodes, annotationContextValue.labels]
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
            // `none` hands every touch gesture to the canvas's own handlers.
            // Correct while a stroke or an erase sweep owns the pointer, but
            // it must NOT be the resting state: d3-zoom needs the browser to
            // keep delivering multi-touch for pinch, so the default is left
            // alone otherwise.
            // Not keyed on `placementArmed`: `none` would kill pinch-zoom for as
            // long as a sticky tool is armed. Drawing gestures still need it,
            // and those are genuinely momentary modes.
            touchAction: freehandActive || eraserActive ? 'none' : undefined,
          }}
        >
          <ReactFlow
            nodes={nodesWithAriaLabels}
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
            onNodeClick={onNodeClick}
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
            // An armed eraser owns the drag the same way freehand does: a
            // sweep across the canvas has to erase, not pan or marquee-select.
            // A placement tool owns the pane the same way freehand and the
            // eraser do. Leaving `selectionOnDrag` on was what swallowed the
            // click entirely: ReactFlow's pane treats a press in selection
            // mode as the start of a marquee and never reports the click, so
            // an armed tool created nothing at all with a mouse.
            // `placementArmed` deliberately does NOT disable panning or zooming.
            // Placement tools are sticky, so armed is the resting state —
            // killing pan and pinch for its whole duration would mean
            // disarming just to move the viewport between placements. The
            // placement gesture claims only its own primary pointer and
            // abandons as soon as a second one arrives, which leaves pinch to
            // ReactFlow. Marquee selection IS suppressed: it is what swallowed
            // the press before this gesture existed.
            panOnDrag={freehandActive || eraserActive ? false : isTouchMode ? true : [0, 2]}
            selectionOnDrag={
              freehandActive || eraserActive || placementArmed ? false : !isTouchMode
            }
            selectionMode={SelectionMode.Partial}
            selectNodesOnDrag={true}
            nodesDraggable={!freehandActive && !eraserActive}
            // Two-finger pinch zooms. This is ReactFlow's default; it is
            // stated explicitly because the actual fix for pinch lives in CSS
            // (GraphCanvas.css's touch-action rules) and this is the prop a
            // future reader would otherwise suspect first and "fix" by
            // toggling. The prop was never the problem.
            zoomOnPinch
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
              {/* Explicit touch multi-select mode (task-annotation-
                  accessible-shared-controls, closing the accessibility
                  audit's "Multi-select ... reachable via tap-based multi-
                  selection | MISSING — pointer/modifier-key-only today"
                  row): the touch-equivalent of holding Shift/Ctrl while
                  clicking. While active, `onNodeClick` above adds each
                  tapped node to the selection instead of replacing it;
                  tapping empty canvas (handlePaneClick) clears the
                  selection, the same as it always has. */}
              <button
                type="button"
                className={`graph-compact-control${multiSelectMode ? ' active' : ''}`}
                onClick={() => setMultiSelectMode((v) => !v)}
                aria-pressed={multiSelectMode}
                aria-label={cml.annotationMultiSelectMode}
                title={cml.annotationMultiSelectMode}
              >
                ⛶
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
          {/* Desktop (isCompact false) is entirely unchanged: the toolbox
              always renders here, in its own fixed-position wrapper, exactly
              as before this prop existed - a container prop is irrelevant on
              desktop. Compact/mobile has two hosts to serve: a host that has
              wired annotationToolboxPortalContainer (e.g. App.jsx's
              MobileShell) gets the toolbox portaled into its mobile-shell
              annotate sheet instead, so it never becomes a second,
              uncoordinated bottom surface fighting the mobile bottom nav for
              space - the bug this integration exists to close. A host that
              has NOT wired the portal (e.g. the standalone widget embed,
              which has no MobileShell to portal into) falls back to the
              pre-integration behaviour: the toolbox renders inline here in
              its own always-on compact strip, exactly as it did before this
              prop existed - never nothing. Either way it is the same
              AnnotationToolbox instance and the same onCreate/onDragCreate
              handlers below; only where its DOM lands, and its variant,
              differ. */}
          {!activeFocusRootId && !(isCompact && annotationToolboxPortalContainer) && (
            <AnnotationToolbox
              onCreate={(kind, options) => createAnnotationAtViewportCenter(kind, options)}
              onSelectTool={handleSelectTool}
              onDragCreate={handleAnnotationDragCreate}
              labels={atl}
              compact={isCompact}
              // Distinct from `compact`, which is a viewport-WIDTH signal
              // (COMPACT_MEDIA_QUERY) and so captions the wrong people in both
              // directions. This is the pointer signal, and it covers what the
              // stylesheet's own `@media (hover: none)` cannot: a hybrid device
              // that reports hover while being driven by finger, and an
              // explicit touchMode="on" override.
              touch={isTouchMode}
              activeKind={activeTool.kind}
            />
          )}
          {!activeFocusRootId &&
            isCompact &&
            annotationToolboxPortalContainer &&
            createPortal(
              <AnnotationToolbox
                onCreate={(kind, options) => createAnnotationAtViewportCenter(kind, options)}
                onSelectTool={handleSelectTool}
                onDragCreate={handleAnnotationDragCreate}
                labels={atl}
                variant="sheet"
                touch={isTouchMode}
                activeKind={activeTool.kind}
              />,
              annotationToolboxPortalContainer
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
          {/* Live size preview for a drag-to-draw placement. Screen-space and
              driven imperatively (see the placement effect's updatePreview) so
              a pointermove burst changes only this element's own style, never
              the canvas's React tree. Hidden until a drag actually starts. */}
          <div
            ref={placementPreviewRef}
            className="graph-placement-preview"
            data-testid="placement-preview"
            aria-hidden="true"
            style={{ display: 'none' }}
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
                {/* A group, not a single path: updatePreview fills it with one
                    path per pressure segment so the stroke's thick and thin
                    parts show while it is being drawn. Each path carries its
                    own stroke/width, so none are set here. */}
                <g ref={freehandPreviewPathRef} />
              </svg>
              <div className="graph-freehand-drawing-hint" role="status" aria-live="polite">
                {cml.freehandDrawingHint}
              </div>
            </>
          )}

          {/* Non-drag "Attach to…" target-tap mode's own visible status
              (task-annotation-accessible-shared-controls) — mirrors the
              freehand drawing hint above: a real, focus-independent `status`
              region (works for a screen-reader user too) plus a real button,
              never a bare instruction to "long-press" or hold a gesture. */}
          {attachModeId && (
            <div className="graph-attach-mode-hint" role="status" aria-live="polite">
              <span>{cml.annotationAttachToHint}</span>
              <button type="button" onClick={cancelAttachMode}>
                {cml.annotationAttachToCancel}
              </button>
            </div>
          )}
        </div>

        {/* The overlap-object picker (task-annotation-accessible-shared-
            controls) — offered whenever a click's flow position lands
            inside more than one annotation's box (see `onNodeClick` above).
            A plain, keyboard/touch-reachable button list, the same shape as
            every other menu here; picking one selects exactly that node and
            moves focus to it, the same "land on the thing you picked"
            contract `createAnnotation` now gives a fresh annotation. */}
        {overlapPicker && (
          <div
            className="graph-context-menu graph-overlap-picker"
            style={{ left: overlapPicker.x, top: overlapPicker.y }}
          >
            <div className="context-menu-title">{cml.annotationOverlapPickerTitle}</div>
            {overlapPicker.candidates.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => {
                  selectAndFocusNode(c.id);
                  setOverlapPicker(null);
                }}
              >
                {c.label}
              </button>
            ))}
          </div>
        )}

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
          dimmedNodeIds={dimmedNodeIds}
          dimmedEdgeIds={dimmedEdgeIds}
          graphEdges={inputEdges}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onDimEdges={onDimEdges}
          onRestoreEdges={onRestoreEdges}
          onAttachNearby={attachNearbyAnnotation}
          onClose={() => setNodeContextMenu(null)}
        />

        <MultiNodeContextMenu
          menu={multiNodeContextMenu}
          labels={cml}
          onShowOnly={onShowOnly}
          onHideSelection={onHideMultiple || onHide ? hideSelectedGraphNodes : undefined}
          onDeleteSelection={
            selectedNodes.some((n) => OVERLAY_TYPES.has(n.type)) || onHideMultiple || onHide
              ? deleteSelectedNodes
              : undefined
          }
          selectNodesByType={selectNodesByType}
          onOrganize={organizeSelection}
          onAlign={alignDistributeEligibility.movable.length >= 2 ? alignSelectedNodes : undefined}
          onDistribute={
            alignDistributeEligibility.movable.length >= 3 ? distributeSelectedNodes : undefined
          }
          dimmedNodeIds={dimmedNodeIds}
          dimmedEdgeIds={dimmedEdgeIds}
          graphEdges={inputEdges}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onDimEdges={onDimEdges}
          onRestoreEdges={onRestoreEdges}
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
          dimmedEdgeIds={dimmedEdgeIds}
          onDimEdges={onDimEdges}
          onRestoreEdges={onRestoreEdges}
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
