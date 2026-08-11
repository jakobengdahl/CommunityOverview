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
import {
  applyLayout,
  getGridLayout,
  getCircularLayout,
  getLayoutedElements,
  positionNewNodes,
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
  isManualNode,
  isArrowHeld,
  overlayToFlowNode,
  flowNodeToOverlay,
  nodeCenter,
  resolveAnchoredArrow,
} from '../utils/annotations';
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
 * GraphCanvas - Main graph visualization component
 */
function GraphCanvasInner({
  nodes: inputNodes = [],
  edges: inputEdges = [],
  highlightedNodeIds = [],
  hiddenNodeIds = [],
  hiddenEdgeIds = [],
  nodeMarks = {},
  clearGroupsFlag = false,
  onExpand,
  onEdit,
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
  showMinimap = false,
  schema = null,
  onContextMenuAction = null,
  nodeColorResolver = null,
  onViewportChange = null,
  nodePreviewEnabled = true,
  contextMenuLabels = {},
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
    organize: 'Organize',
    organizeCluster: 'Cluster',
    organizeHorizontal: 'List horizontally',
    organizeVertical: 'List vertically',
    organizeTree: 'Arrange as tree',
    organizeHint: 'Organize: C cluster · H horizontal · V vertical · T tree',
    hideAll: 'Hide all',
    deleteAll: 'Delete all',
    changeType: 'Change type',
    generalConnection: 'General connection',
    addNote: 'Add note',
    addLabel: 'Add label',
    addArrow: 'Add arrow',
    annotationColor: 'Colour',
    deleteAnnotation: 'Delete',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
    annotationTextSize: 'Text size',
    arrowStartHead: 'Start arrowhead',
    arrowEndHead: 'End arrowhead',
    ...contextMenuLabels,
  };

  // Relationship types defined in the schema, used for the edge type picker.
  const relationshipTypes = useMemo(() => {
    const rt = schema?.relationship_types;
    if (!rt || typeof rt !== 'object') return [];
    return Object.entries(rt).map(([name, cfg]) => ({
      type: name,
      description: (cfg && typeof cfg === 'object' && cfg.description) || '',
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
  const paneMenuRef = useRef(null);
  const reactFlowWrapper = useRef(null);
  const rightDragStart = useRef({ x: 0, y: 0, time: null });
  const mouseDownPos = useRef(null);
  // Two-step "organize" keyboard chord: Ctrl/Cmd+O arms it, then c/h/v/t picks
  // the arrangement. Held in refs so arming doesn't re-render or re-bind keys.
  const organizePendingRef = useRef(false);
  const organizeTimerRef = useRef(null);
  const { screenToFlowPosition, setCenter, getNodes: getFlowNodes, getViewport } = useReactFlow();

  // Stable notifier for annotation nodes (note/label/arrow) to signal the host
  // that an annotation was edited, recoloured or deleted so the session can be
  // persisted. Wrapped in a ref so the callback identity stays stable across
  // renders even as the parent's handler changes.
  const onAnnotationChangeRef = useRef(onAnnotationChange);
  onAnnotationChangeRef.current = onAnnotationChange;
  const notifyAnnotationChange = useCallback(() => {
    onAnnotationChangeRef.current?.();
  }, []);
  const annotationContextValue = useMemo(
    () => ({
      notifyChange: notifyAnnotationChange,
      labels: {
        color: cml.annotationColor,
        delete: cml.deleteAnnotation,
        notePlaceholder: cml.notePlaceholder,
        labelPlaceholder: cml.labelPlaceholder,
        textSize: cml.annotationTextSize,
        arrowStartHead: cml.arrowStartHead,
        arrowEndHead: cml.arrowEndHead,
      },
    }),
    [
      notifyAnnotationChange,
      cml.annotationColor,
      cml.deleteAnnotation,
      cml.notePlaceholder,
      cml.labelPlaceholder,
      cml.annotationTextSize,
      cml.arrowStartHead,
      cml.arrowEndHead,
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

  // Filter out hidden nodes
  const visibleNodes = useMemo(
    () => inputNodes.filter((n) => !hiddenNodeIds.includes(n.id)),
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

    if (hasSavedPositions) {
      return nodesWithoutPosition;
    }

    return applyLayout(nodesWithoutPosition, reactFlowEdges, layoutType);
  }, [
    nodesToRender,
    reactFlowEdges,
    layoutType,
    onExpand,
    onEdit,
    highlightedNodeIds,
    nodeMarks,
    remoteSelections,
    nodePreviewEnabled,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  // Update nodes when input changes
  useEffect(() => {
    setNodes((nds) => {
      const manualNodes = clearGroupsFlag ? [] : nds.filter(isManualNode);
      const prevById = new Map(nds.map((n) => [n.id, n]));
      const mapped = reactFlowNodes.map((n) => {
        const existing = prevById.get(n.id);
        if (existing && existing.position.x !== 0) {
          return {
            ...n,
            position: existing.position,
            parentId: existing.parentId,
            style: existing.style || n.style,
          };
        }
        return n;
      });

      // Place freshly-added nodes (e.g. from expanding a node) near the nodes
      // they connect to instead of stacking them at the origin or scattering
      // them via a full re-layout. Only when there is already a positioned
      // layout to anchor against, only for nodes without an explicit saved
      // position (a loaded/remote position is authoritative), and never in
      // lazy-paging mode where "new" nodes are just more of the same result set.
      const isPlaced = (n) => n.position && (n.position.x !== 0 || n.position.y !== 0);
      const existingPlaced = mapped.filter((n) => prevById.has(n.id) && isPlaced(n));
      const freshNodes = mapped.filter((n) => !prevById.has(n.id) && !n.data?._savedPosition);
      const inLazyMode = visibleNodes.length > LAZY_LOAD_THRESHOLD;
      if (freshNodes.length > 0 && existingPlaced.length > 0 && !inLazyMode) {
        const posById = new Map(
          positionNewNodes(freshNodes, existingPlaced, reactFlowEdges).map((n) => [
            n.id,
            n.position,
          ])
        );
        const placed = mapped.map((n) =>
          posById.has(n.id) ? { ...n, position: posById.get(n.id) } : n
        );
        return reorderNodesForParentChild([...placed, ...manualNodes]);
      }

      // Groups must appear before their children in the array for ReactFlow
      // parent-child relationships to work. This also ensures groups render
      // behind custom nodes so clicks reach the nodes on top.
      return reorderNodesForParentChild([...mapped, ...manualNodes]);
    });
  }, [reactFlowNodes, reactFlowEdges, visibleNodes.length, setNodes, clearGroupsFlag]);

  // Update edges when input changes
  useEffect(() => {
    setEdges(reactFlowEdges);
  }, [reactFlowEdges, setEdges]);

  // Reset loaded count when visible nodes change significantly
  useEffect(() => {
    if (visibleNodes.length <= LAZY_LOAD_THRESHOLD) {
      setLoadedNodeCount(visibleNodes.length);
    } else {
      setLoadedNodeCount(INITIAL_LOAD_COUNT);
    }
  }, [visibleNodes.length]);

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

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);

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
      for (const [id, abs] of posById) {
        const parent = nodeById.get(id)?.parentId ? groupPos.get(nodeById.get(id).parentId) : null;
        finalPos.set(id, parent ? { x: abs.x - parent.x, y: abs.y - parent.y } : abs);
      }
      setNodes((nds) =>
        nds.map((n) => (finalPos.has(n.id) ? { ...n, position: finalPos.get(n.id) } : n))
      );
      if (onNodePositionChange) {
        for (const [id, pos] of finalPos) onNodePositionChange(id, pos);
      }
      closeAllMenus();
    },
    [selectedNodes, nodes, edges, setNodes, onNodePositionChange, closeAllMenus]
  );

  // Create a free-floating annotation (note, label or arrow) at the given flow
  // position. Notes/labels/arrows are persisted in the session annotation list
  // via the save-view round-trip; onAnnotationChange schedules that save.
  const createAnnotation = useCallback(
    (kind, position) => {
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
      onAnnotationChangeRef.current?.();
    },
    [setNodes]
  );

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

  const onNodeDragStop = useCallback(
    (event, draggedNode, allDraggedNodes) => {
      if (onNodePositionChange) {
        onNodePositionChange(draggedNode.id, draggedNode.position);
      }

      // Get latest node positions directly from ReactFlow's internal store
      const currentNodes = getFlowNodes();
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

      // Nothing to process: either no non-group nodes dragged or no groups exist
      if (draggedIds.size === 0 || groupNodes.length === 0) return;

      setNodes((nds) => {
        const mapped = nds.map((n) => {
          if (!draggedIds.has(n.id) || n.type === 'group') return n;

          // Use position from ReactFlow's store for accurate post-drag coordinates
          const flowNode = currentNodes.find((cn) => cn.id === n.id);
          const pos = flowNode?.position || n.position;

          // Calculate absolute position (account for parent offset)
          const absPos = n.parentId
            ? {
                x: pos.x + (groupNodes.find((g) => g.id === n.parentId)?.position.x || 0),
                y: pos.y + (groupNodes.find((g) => g.id === n.parentId)?.position.y || 0),
              }
            : pos;

          // Find which group this node is inside
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

          if (targetGroup && n.parentId !== targetGroup.id) {
            // Enter group
            return {
              ...n,
              parentId: targetGroup.id,
              position: {
                x: absPos.x - targetGroup.position.x,
                y: absPos.y - targetGroup.position.y,
              },
              extent: undefined,
            };
          }

          if (!targetGroup && n.parentId) {
            // Exit group
            const oldParent = groupNodes.find((gn) => gn.id === n.parentId);
            return {
              ...n,
              parentId: undefined,
              position: {
                x: pos.x + (oldParent?.position.x || 0),
                y: pos.y + (oldParent?.position.y || 0),
              },
              extent: undefined,
            };
          }

          return n;
        });

        // ReactFlow requires parent nodes before children in the array
        return reorderNodesForParentChild(mapped);
      });
    },
    [setNodes, onNodePositionChange, getFlowNodes]
  );

  // Right-click on empty background. A plain right-click opens the annotation
  // creation menu (add note/label/arrow); a right-drag is a pan (panOnDrag
  // includes button 2), so in that case keep the legacy clear-only behaviour.
  const onPaneContextMenu = useCallback(
    (event) => {
      event.preventDefault();
      event.stopPropagation();
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
    [closeAllMenus, clearSelection, screenToFlowPosition]
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

  // Save view: includes node positions and visible edges from ReactFlow state
  const handleSaveView = useCallback(() => {
    if (onSaveView) {
      const viewData = {
        nodes: nodes.map((n) => ({ id: n.id, position: n.position, parentId: n.parentId })),
        edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, label: e.label })),
        groups: nodes
          .filter((n) => n.type === 'group')
          .map((g) => ({
            id: g.id,
            label: g.data.label,
            description: g.data.description,
            position: g.position,
            style: g.style,
            color: g.data.color,
          })),
        annotations: nodes.filter((n) => OVERLAY_TYPES.has(n.type)).map(flowNodeToOverlay),
      };
      onSaveView(viewData);
    }
  }, [nodes, edges, onSaveView]);

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
    [screenToFlowPosition, onDropCreateNode, setNodes, onCreateGroup]
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

      // Second step of the organize chord: a pending Ctrl/Cmd+O is resolved by
      // the next c/h/v/t keystroke.
      if (organizePendingRef.current && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const mode = { c: 'cluster', h: 'horizontal', v: 'vertical', t: 'tree' }[
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
          // Delete removes overlay annotations (note/label/arrow) from the
          // canvas; graph nodes are hidden, not deleted. Groups are left to
          // their own context menu so their children stay correctly parented.
          const overlayIds = selectedNodes
            .filter((n) => OVERLAY_TYPES.has(n.type))
            .map((n) => n.id);
          const graphNodeIds = selectedNodes
            .filter((n) => !ANNOTATION_TYPES.has(n.type))
            .map((n) => n.id);
          if (overlayIds.length > 0) {
            const removeSet = new Set(overlayIds);
            setNodes((nds) => nds.filter((n) => !removeSet.has(n.id)));
            onAnnotationChangeRef.current?.();
          }
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
  ]);

  // Create group when signal changes (triggered from toolbar)
  useEffect(() => {
    if (createGroupSignal > 0) {
      handleAddGroup();
    }
  }, [createGroupSignal, handleAddGroup]);

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
      const desiredDraggable = !isArrowHeld(n.data, existing);
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
    if (geometryChanged) onAnnotationChangeRef.current?.();
  }, [nodes, setNodes]);

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
              <div className="graph-lazy-load-info">
                Showing {loadedNodeCount} of {visibleNodes.length} nodes
                <button className="graph-load-more-button" onClick={handleLoadMore}>
                  Load More
                </button>
              </div>
            </div>
          )}

        <div ref={reactFlowWrapper} style={{ width: '100%', height: '100%' }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
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
            fitViewOptions={{ padding: 0.2, duration: 800 }}
            minZoom={0.1}
            maxZoom={2}
            defaultEdgeOptions={{ animated: true, style: { strokeWidth: 2 } }}
            panOnDrag={[0, 2]}
            selectionOnDrag={true}
            selectionMode={SelectionMode.Partial}
            selectNodesOnDrag={true}
            deleteKeyCode={null}
            multiSelectionKeyCode={['Shift', 'Meta', 'Control']}
            edgesUpdatable={false}
            onMoveStart={closeAllMenus}
            onMove={onViewportChange ? (_event, vp) => onViewportChange(vp) : undefined}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#333" gap={16} />
            <Controls />
            {showMinimap && (
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
          relationshipTypes={relationshipTypes}
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
