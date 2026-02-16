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
import SimpleFloatingEdge from './SimpleFloatingEdge';
import { applyLayout, getGridLayout, getCircularLayout, getLayoutedElements } from '../utils/graphLayout';
import { getNodeColor, LAZY_LOAD_THRESHOLD, INITIAL_LOAD_COUNT, DEFAULT_EDGE_STYLE } from '../utils/constants';
import './GraphCanvas.css';

/**
 * GraphCanvas - Main graph visualization component
 */
function GraphCanvasInner({
  nodes: inputNodes = [],
  edges: inputEdges = [],
  highlightedNodeIds = [],
  hiddenNodeIds = [],
  hiddenEdgeIds = [],
  clearGroupsFlag = false,
  onExpand,
  onEdit,
  onDelete,
  onHide,
  onDeleteMultiple,
  onHideMultiple,
  onHideEdge,
  onDeleteEdge,
  onCreateGroup,
  onSaveView,
  onNodePositionChange,
  layoutType = null,
  onCreateSubscription,
  onCreateAgent,
  onDropCreateNode,
  onShowOnly,
  focusNodeId = null,
  onFocusComplete,
  createGroupSignal = 0,
  saveViewSignal = 0,
  groupsToRestore = null,
  onGroupsRestored,
}) {
  const [loadedNodeCount, setLoadedNodeCount] = useState(INITIAL_LOAD_COUNT);
  const [nodeContextMenu, setNodeContextMenu] = useState(null);
  const [multiNodeContextMenu, setMultiNodeContextMenu] = useState(null);
  const [edgeContextMenu, setEdgeContextMenu] = useState(null);
  const [notification, setNotification] = useState(null);
  const [selectedNodes, setSelectedNodes] = useState([]);
  const [selectedEdges, setSelectedEdges] = useState([]);
  const reactFlowWrapper = useRef(null);
  const rightDragStart = useRef({ x: 0, y: 0, time: null });
  const mouseDownPos = useRef(null);
  const { screenToFlowPosition, setCenter, getNodes: getFlowNodes } = useReactFlow();

  // Track selected nodes and edges
  useOnSelectionChange({
    onChange: ({ nodes: selected, edges: selectedE }) => {
      setSelectedNodes(selected);
      setSelectedEdges(selectedE || []);
    },
  });

  // Filter out hidden nodes
  const visibleNodes = useMemo(() =>
    inputNodes.filter(n => !hiddenNodeIds.includes(n.id)),
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
    const renderedNodeIds = new Set(nodesToRender.map(n => n.id));
    return inputEdges.filter(e =>
      !hiddenNodeIds.includes(e.source) &&
      !hiddenNodeIds.includes(e.target) &&
      !hiddenEdgeIds.includes(e.id) &&
      renderedNodeIds.has(e.source) &&
      renderedNodeIds.has(e.target)
    );
  }, [inputEdges, hiddenNodeIds, hiddenEdgeIds, nodesToRender]);

  // Convert to React Flow edge format
  const reactFlowEdges = useMemo(() => {
    return visibleEdges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      type: 'floating',
      animated: false,
      selectable: true,
      style: DEFAULT_EDGE_STYLE,
      labelStyle: { fill: '#888', fontSize: 10, fontWeight: 500 },
      labelBgStyle: { fill: '#1a1a1a', fillOpacity: 0.8 }
    }));
  }, [visibleEdges]);

  // Convert to React Flow node format with layout
  const reactFlowNodes = useMemo(() => {
    const hasSavedPositions = nodesToRender.some(n => n._savedPosition);

    const nodesWithoutPosition = nodesToRender.map(node => ({
      id: node.id,
      type: 'custom',
      data: {
        ...node,
        label: node.name,
        summary: node.summary || node.description?.slice(0, 100),
        nodeType: node.type,
        color: getNodeColor(node.type),
        onExpand: onExpand ? () => onExpand(node.id, node) : null,
        onEdit: onEdit ? () => onEdit(node.id, node) : null,
      },
      position: node._savedPosition || { x: 0, y: 0 },
    }));

    if (nodesWithoutPosition.length === 0) {
      return nodesWithoutPosition;
    }

    if (hasSavedPositions) {
      return nodesWithoutPosition;
    }

    return applyLayout(nodesWithoutPosition, reactFlowEdges, layoutType);
  }, [nodesToRender, reactFlowEdges, layoutType, onExpand, onEdit]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  // Update nodes when input changes
  useEffect(() => {
    setNodes((nds) => {
      const manualNodes = clearGroupsFlag
        ? []
        : nds.filter(n => n.type === 'group' || n.id.startsWith('group-'));
      const newNodes = reactFlowNodes.map(n => {
        const existing = nds.find(curr => curr.id === n.id);
        if (existing && existing.position.x !== 0) {
          return {
            ...n,
            position: existing.position,
            parentId: existing.parentId,
            style: existing.style || n.style
          };
        }
        return n;
      });
      return [...newNodes, ...manualNodes];
    });
  }, [reactFlowNodes, setNodes, clearGroupsFlag]);

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
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges]
  );

  // Close all context menus
  const closeAllMenus = useCallback(() => {
    setNodeContextMenu(null);
    setMultiNodeContextMenu(null);
    setEdgeContextMenu(null);
  }, []);

  const clearSelection = useCallback(() => {
    // Use onNodesChange/onEdgesChange with select events to properly clear ReactFlow's internal selection state
    const nodeDeselects = nodes.filter(n => n.selected).map(n => ({
      id: n.id, type: 'select', selected: false,
    }));
    const edgeDeselects = edges.filter(e => e.selected).map(e => ({
      id: e.id, type: 'select', selected: false,
    }));
    if (nodeDeselects.length > 0) onNodesChange(nodeDeselects);
    if (edgeDeselects.length > 0) onEdgesChange(edgeDeselects);
  }, [nodes, edges, onNodesChange, onEdgesChange]);

  const handlePaneClick = useCallback(() => {
    closeAllMenus();
    clearSelection();
  }, [closeAllMenus, clearSelection]);

  const onNodeDragStop = useCallback((event, draggedNode) => {
    if (onNodePositionChange) {
      onNodePositionChange(draggedNode.id, draggedNode.position);
    }

    if (draggedNode.type === 'group') return;

    // Get latest node positions directly from ReactFlow's internal store
    // (React state may not yet reflect the final drag positions)
    const currentNodes = getFlowNodes();

    // Determine which nodes were dragged by reading selection state directly from
    // ReactFlow's store (React state may be stale due to batching / timing).
    const selectedInStore = currentNodes.filter(n => n.selected && n.type !== 'group');
    const isMultiDrag = selectedInStore.length > 1 && selectedInStore.some(n => n.id === draggedNode.id);
    const draggedIds = new Set(
      isMultiDrag
        ? selectedInStore.map(n => n.id)
        : [draggedNode.id]
    );

    const groupNodes = currentNodes.filter(n => n.type === 'group');

    setNodes((nds) => nds.map((n) => {
      if (!draggedIds.has(n.id) || n.type === 'group') return n;

      // Use position from ReactFlow's store for accurate post-drag coordinates
      const flowNode = currentNodes.find(cn => cn.id === n.id);
      const pos = flowNode?.position || n.position;

      // Calculate absolute position (account for parent offset)
      const absPos = n.parentId
        ? {
            x: pos.x + (groupNodes.find(g => g.id === n.parentId)?.position.x || 0),
            y: pos.y + (groupNodes.find(g => g.id === n.parentId)?.position.y || 0),
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
        if (absPos.x >= gb.left && absPos.x <= gb.right &&
            absPos.y >= gb.top && absPos.y <= gb.bottom) {
          targetGroup = g;
          break;
        }
      }

      if (targetGroup && n.parentId !== targetGroup.id) {
        // Enter group (no extent constraint - allows free dragging out from any side)
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
        // Exit group - convert to absolute position
        const oldParent = groupNodes.find(gn => gn.id === n.parentId);
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
    }));
  }, [setNodes, onNodePositionChange, getFlowNodes]);

  // Right-click on empty background: prevent default and clear selection
  const onPaneContextMenu = useCallback((event) => {
    event.preventDefault();
    event.stopPropagation();
    closeAllMenus();
    clearSelection();
  }, [closeAllMenus, clearSelection]);

  // Right-click on the selection box (multi-node selection)
  const onSelectionContextMenu = useCallback((event) => {
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
  }, [selectedNodes]);

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
        color: '#646cff'
      },
      style: { width: 300, height: 200 },
    };

    setNodes((nds) => [...nds, newGroupNode]);

    if (onCreateGroup) {
      onCreateGroup(position, newGroupNode);
    }
  }, [screenToFlowPosition, setNodes, onCreateGroup]);

  // Save view: includes node positions and visible edges from ReactFlow state
  const handleSaveView = useCallback(() => {
    if (onSaveView) {
      const viewData = {
        nodes: nodes.map(n => ({ id: n.id, position: n.position, parentId: n.parentId })),
        edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target, label: e.label })),
        groups: nodes.filter(n => n.type === 'group').map(g => ({
          id: g.id,
          label: g.data.label,
          position: g.position,
          style: g.style,
          color: g.data.color,
        })),
      };
      onSaveView(viewData);
    }
  }, [nodes, edges, onSaveView]);

  const handleLoadMore = useCallback(() => {
    setLoadedNodeCount(prev => Math.min(prev + 100, visibleNodes.length));
  }, [visibleNodes.length]);

  // Node context menu handler
  const onNodeContextMenu = useCallback((event, node) => {
    event.preventDefault();
    event.stopPropagation();

    const isNodeSelected = selectedNodes.some(n => n.id === node.id);
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
  }, [selectedNodes]);

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
      const nodeEl = e.target.closest('.react-flow__node');
      const edgeEl = e.target.closest('.react-flow__edge');
      const menuEl = e.target.closest('.graph-context-menu') || e.target.closest('.graph-group-context-menu');
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
    return () => {
      wrapper.removeEventListener('mousedown', handleMouseDown);
      wrapper.removeEventListener('click', handleClick);
    };
  }, [clearSelection, closeAllMenus]);

  // Handle external drag-and-drop (from toolbar)
  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback((event) => {
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
          color: '#646cff'
        },
        style: { width: 300, height: 200 },
      };
      setNodes((nds) => [...nds, newGroupNode]);
      if (onCreateGroup) {
        onCreateGroup(position, newGroupNode);
      }
      return;
    }

    if (onDropCreateNode) {
      onDropCreateNode(nodeType, position);
    }
  }, [screenToFlowPosition, onDropCreateNode, setNodes, onCreateGroup]);

  // Delete/Backspace hides selected nodes/edges, Escape clears selection
  useEffect(() => {
    const handleKeyDown = (e) => {
      const tag = e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

      if (e.key === 'Escape') {
        closeAllMenus();
        clearSelection();
        return;
      }

      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (selectedEdges.length > 0 && onHideEdge) {
          e.preventDefault();
          selectedEdges.forEach(edge => onHideEdge(edge.id));
        }

        if (selectedNodes.length > 0) {
          e.preventDefault();
          const nodeIds = selectedNodes.map(n => n.id);
          if (onHideMultiple) {
            onHideMultiple(nodeIds);
          } else if (onHide) {
            nodeIds.forEach(id => onHide(id));
          }
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [selectedNodes, selectedEdges, onHideMultiple, onHide, onHideEdge, closeAllMenus, clearSelection]);

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

  // Restore groups from a saved view
  useEffect(() => {
    if (groupsToRestore && groupsToRestore.length > 0) {
      const groupNodes = groupsToRestore.map(g => ({
        id: g.id,
        type: 'group',
        position: g.position,
        data: { label: g.label || 'Group', description: '', color: g.color || '#646cff' },
        style: g.style || { width: 300, height: 200 },
      }));
      setNodes((nds) => {
        const nonGroups = nds.filter(n => n.type !== 'group' && !n.id.startsWith('group-'));
        return [...nonGroups, ...groupNodes];
      });
      onGroupsRestored?.();
    }
  }, [groupsToRestore, setNodes, onGroupsRestored]);

  // Focus on a specific node when focusNodeId changes
  useEffect(() => {
    if (!focusNodeId) return;
    const targetNode = nodes.find(n => n.id === focusNodeId);
    if (targetNode && targetNode.position) {
      setCenter(
        targetNode.position.x + 100,
        targetNode.position.y + 40,
        { zoom: 1.2, duration: 800 }
      );
    }
    const timer = setTimeout(() => {
      onFocusComplete?.();
    }, 900);
    return () => clearTimeout(timer);
  }, [focusNodeId, nodes, setCenter, onFocusComplete]);

  const nodeTypes = useMemo(() => ({
    custom: CustomNode,
    group: GroupNode,
  }), []);

  const edgeTypes = useMemo(() => ({
    floating: SimpleFloatingEdge,
  }), []);

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);

  return (
    <div className="graph-canvas-container">
      {inputNodes.length > 0 && visibleNodes.length > LAZY_LOAD_THRESHOLD && loadedNodeCount < visibleNodes.length && (
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
          onPaneClick={handlePaneClick}
          onDragOver={onDragOver}
          onDrop={onDrop}
          onPaneMouseDown={(event) => {
            if (event.button === 2) {
              rightDragStart.current = {
                x: event.clientX,
                y: event.clientY,
                time: Date.now()
              };
            }
          }}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.2, duration: 800 }}
          minZoom={0.1}
          maxZoom={2}
          attributionPosition="bottom-right"
          defaultEdgeOptions={{ animated: true, style: { strokeWidth: 2 } }}
          panOnDrag={[0, 2]}
          selectionOnDrag={true}
          selectionMode={SelectionMode.Partial}
          selectNodesOnDrag={true}
          deleteKeyCode={null}
          multiSelectionKeyCode="Shift"
          edgesUpdatable={false}
          onMoveStart={closeAllMenus}
        >
          <Background color="#333" gap={16} />
          <Controls />
          <MiniMap
            nodeColor={(node) => node.data?.color || '#9CA3AF'}
            maskColor="rgba(0, 0, 0, 0.5)"
            position="bottom-right"
            pannable
            zoomable
          />
        </ReactFlow>
      </div>

      {nodeContextMenu && (
        <div
          className="graph-context-menu node-context-menu"
          style={{ left: nodeContextMenu.x, top: nodeContextMenu.y }}
        >
          {onEdit && (
            <button onClick={() => {
              onEdit(nodeContextMenu.node.id, nodeContextMenu.node.data);
              setNodeContextMenu(null);
            }}>
              ✏️ Redigera
            </button>
          )}
          {onHide && (
            <button onClick={() => {
              onHide(nodeContextMenu.node.id);
              setNodeContextMenu(null);
            }}>
              👁️ Dölj
            </button>
          )}
          {onExpand && (
            <button onClick={() => {
              onExpand(nodeContextMenu.node.id, nodeContextMenu.node.data);
              setNodeContextMenu(null);
            }}>
              🔍 Expandera
            </button>
          )}
          {onDelete && (
            <>
              <div className="context-menu-separator"></div>
              <button className="context-menu-danger" onClick={() => {
                onDelete(nodeContextMenu.node.id);
                setNodeContextMenu(null);
              }}>
                🗑️ Ta bort
              </button>
            </>
          )}
        </div>
      )}

      {multiNodeContextMenu && (
        <div
          className="graph-context-menu node-context-menu multi-node-context-menu"
          style={{ left: multiNodeContextMenu.x, top: multiNodeContextMenu.y }}
        >
          <div className="context-menu-header">
            {multiNodeContextMenu.nodes.length} noder markerade
          </div>
          {onShowOnly && (
            <button onClick={() => {
              const nodeIds = multiNodeContextMenu.nodes.map(n => n.id);
              onShowOnly(nodeIds);
              setMultiNodeContextMenu(null);
            }}>
              🔍 Visa enbart dessa
            </button>
          )}
          {(onHideMultiple || onHide) && (
            <button onClick={() => {
              const nodeIds = multiNodeContextMenu.nodes.map(n => n.id);
              if (onHideMultiple) {
                onHideMultiple(nodeIds);
              } else if (onHide) {
                nodeIds.forEach(id => onHide(id));
              }
              setMultiNodeContextMenu(null);
            }}>
              👁️ Dölj alla
            </button>
          )}
          {(onDeleteMultiple || onDelete) && (
            <>
              <div className="context-menu-separator"></div>
              <button className="context-menu-danger" onClick={() => {
                const nodeIds = multiNodeContextMenu.nodes.map(n => n.id);
                if (onDeleteMultiple) {
                  onDeleteMultiple(nodeIds);
                } else if (onDelete) {
                  nodeIds.forEach(id => onDelete(id));
                }
                setMultiNodeContextMenu(null);
              }}>
                🗑️ Ta bort alla
              </button>
            </>
          )}
        </div>
      )}

      {edgeContextMenu && (
        <div
          className="graph-context-menu edge-context-menu"
          style={{ left: edgeContextMenu.x, top: edgeContextMenu.y }}
        >
          <div className="context-menu-header">
            {edgeContextMenu.edge.label || 'Edge'}
          </div>
          {onHideEdge && (
            <button onClick={() => {
              onHideEdge(edgeContextMenu.edge.id);
              setEdgeContextMenu(null);
            }}>
              👁️ Dölj
            </button>
          )}
          {onDeleteEdge && (
            <>
              <div className="context-menu-separator"></div>
              <button className="context-menu-danger" onClick={() => {
                onDeleteEdge(edgeContextMenu.edge.id);
                setEdgeContextMenu(null);
              }}>
                🗑️ Ta bort
              </button>
            </>
          )}
        </div>
      )}

      {notification && (
        <div className={`graph-notification graph-notification-${notification.type}`}>
          <span>{notification.type === 'success' ? '✓' : 'ℹ'}</span>
          <span>{notification.message}</span>
          <button onClick={() => setNotification(null)}>×</button>
        </div>
      )}
    </div>
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
