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
 *
 * @param {Object} props
 * @param {Array} props.nodes - Array of node objects with id, type, name, description, etc.
 * @param {Array} props.edges - Array of edge objects with id, source, target, type
 * @param {Array} props.highlightedNodeIds - Node IDs to highlight
 * @param {Array} props.hiddenNodeIds - Node IDs to hide
 * @param {Function} props.onExpand - Called when expand button clicked (nodeId, nodeData)
 * @param {Function} props.onEdit - Called when edit button clicked (nodeId, nodeData)
 * @param {Function} props.onDelete - Called when delete requested (nodeId)
 * @param {Function} props.onHide - Called when hide requested (nodeId)
 * @param {Function} props.onDeleteMultiple - Called when delete multiple nodes requested (nodeIds)
 * @param {Function} props.onHideMultiple - Called when hide multiple nodes requested (nodeIds)
 * @param {Function} props.onCreateGroup - Called when creating a group (position)
 * @param {Function} props.onSaveView - Called when save view requested (viewData)
 * @param {Function} props.onNodePositionChange - Called when node positions change
 * @param {string} props.layoutType - Force specific layout: 'dagre', 'grid', 'circular', or null for auto
 * @param {boolean} props.clearGroupsFlag - Signal to clear groups when true
 */
function GraphCanvasInner({
  nodes: inputNodes = [],
  edges: inputEdges = [],
  highlightedNodeIds = [],
  hiddenNodeIds = [],
  clearGroupsFlag = false,
  onExpand,
  onEdit,
  onDelete,
  onHide,
  onDeleteMultiple,
  onHideMultiple,
  onCreateGroup,
  onSaveView,
  onNodePositionChange,
  layoutType = null,
}) {
  const [loadedNodeCount, setLoadedNodeCount] = useState(INITIAL_LOAD_COUNT);
  const [contextMenu, setContextMenu] = useState(null);
  const [nodeContextMenu, setNodeContextMenu] = useState(null);
  const [multiNodeContextMenu, setMultiNodeContextMenu] = useState(null);
  const [notification, setNotification] = useState(null);
  const [selectedNodes, setSelectedNodes] = useState([]);
  const reactFlowWrapper = useRef(null);
  const rightDragStart = useRef({ x: 0, y: 0, time: null });
  const { screenToFlowPosition } = useReactFlow();

  // Track selected nodes
  useOnSelectionChange({
    onChange: ({ nodes: selected }) => {
      setSelectedNodes(selected);
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

  // Filter edges to visible nodes
  const visibleEdges = useMemo(() => {
    const renderedNodeIds = new Set(nodesToRender.map(n => n.id));
    return inputEdges.filter(e =>
      !hiddenNodeIds.includes(e.source) &&
      !hiddenNodeIds.includes(e.target) &&
      renderedNodeIds.has(e.source) &&
      renderedNodeIds.has(e.target)
    );
  }, [inputEdges, hiddenNodeIds, nodesToRender]);

  // Convert to React Flow edge format
  const reactFlowEdges = useMemo(() => {
    return visibleEdges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      type: 'floating', // Use the custom floating edge type
      animated: false,
      style: DEFAULT_EDGE_STYLE,
      labelStyle: { fill: '#888', fontSize: 10, fontWeight: 500 },
      labelBgStyle: { fill: '#1a1a1a', fillOpacity: 0.8 }
    }));
  }, [visibleEdges]);

  // Convert to React Flow node format with layout
  const reactFlowNodes = useMemo(() => {
    const nodesWithoutPosition = nodesToRender.map(node => ({
      id: node.id,
      type: 'custom',
      data: {
        ...node,
        label: node.name,
        summary: node.summary || node.description?.slice(0, 100),
        nodeType: node.type,
        color: getNodeColor(node.type),
        isHighlighted: highlightedNodeIds.includes(node.id),
        onExpand: onExpand ? () => onExpand(node.id, node) : null,
        onEdit: onEdit ? () => onEdit(node.id, node) : null,
      },
      position: { x: 0, y: 0 },
    }));

    if (nodesWithoutPosition.length === 0) {
      return nodesWithoutPosition;
    }

    return applyLayout(nodesWithoutPosition, reactFlowEdges, layoutType);
  }, [nodesToRender, highlightedNodeIds, reactFlowEdges, layoutType, onExpand, onEdit]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  // Update nodes when input changes
  useEffect(() => {
    setNodes((nds) => {
      // Only preserve groups if clearGroupsFlag is false
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
            extent: existing.extent,
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

  const onNodeDragStop = useCallback((event, draggedNode) => {
    if (onNodePositionChange) {
      onNodePositionChange(draggedNode.id, draggedNode.position);
    }

    if (draggedNode.type === 'group') return;

    // Check if dropped in a group
    const groupNodes = nodes.filter(n => n.type === 'group');
    let droppedInGroup = null;

    for (const groupNode of groupNodes) {
      const groupBounds = {
        left: groupNode.position.x,
        right: groupNode.position.x + (groupNode.style?.width || 300),
        top: groupNode.position.y,
        bottom: groupNode.position.y + (groupNode.style?.height || 200),
      };

      if (
        draggedNode.position.x >= groupBounds.left &&
        draggedNode.position.x <= groupBounds.right &&
        draggedNode.position.y >= groupBounds.top &&
        draggedNode.position.y <= groupBounds.bottom
      ) {
        droppedInGroup = groupNode.id;
        break;
      }
    }

    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === draggedNode.id) {
          if (droppedInGroup && n.parentId !== droppedInGroup) {
            const groupNode = nodes.find(gn => gn.id === droppedInGroup);
            if (groupNode) {
              return {
                ...n,
                parentId: droppedInGroup,
                position: {
                  x: n.position.x - groupNode.position.x,
                  y: n.position.y - groupNode.position.y,
                },
                extent: 'parent',
              };
            }
          }
          if (!droppedInGroup && n.parentId) {
            const oldParent = nodes.find(gn => gn.id === n.parentId);
            return {
              ...n,
              parentId: undefined,
              position: {
                x: n.position.x + (oldParent?.position.x || 0),
                y: n.position.y + (oldParent?.position.y || 0),
              },
              extent: undefined,
            };
          }
        }
        return n;
      })
    );
  }, [nodes, setNodes, onNodePositionChange]);

  const onPaneContextMenu = useCallback((event) => {
    event.preventDefault();
    event.stopPropagation();

    if (rightDragStart.current.time === null) {
      setContextMenu({ x: event.clientX, y: event.clientY });
      return;
    }

    const timeDiff = Date.now() - rightDragStart.current.time;
    const xDiff = Math.abs(event.clientX - rightDragStart.current.x);
    const yDiff = Math.abs(event.clientY - rightDragStart.current.y);
    const wasDrag = timeDiff > 300 || xDiff > 5 || yDiff > 5;

    if (!wasDrag) {
      setContextMenu({ x: event.clientX, y: event.clientY });
    }

    rightDragStart.current.time = null;
  }, []);

  const handleAddGroup = useCallback(() => {
    if (!contextMenu) return;

    const position = screenToFlowPosition({
      x: contextMenu.x,
      y: contextMenu.y,
    });

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
    setContextMenu(null);

    if (onCreateGroup) {
      onCreateGroup(position, newGroupNode);
    }
  }, [contextMenu, screenToFlowPosition, setNodes, onCreateGroup]);

  const handleSaveView = useCallback(() => {
    setContextMenu(null);
    if (onSaveView) {
      const viewData = {
        nodes: nodes.map(n => ({ id: n.id, position: n.position, parentId: n.parentId })),
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
  }, [nodes, onSaveView]);

  const handleLoadMore = useCallback(() => {
    setLoadedNodeCount(prev => Math.min(prev + 100, visibleNodes.length));
  }, [visibleNodes.length]);

  // Node context menu handler
  const onNodeContextMenu = useCallback((event, node) => {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu(null);

    // Check if multiple nodes are selected and the right-clicked node is one of them
    const isNodeSelected = selectedNodes.some(n => n.id === node.id);
    const hasMultipleSelected = selectedNodes.length > 1;

    if (hasMultipleSelected && isNodeSelected) {
      // Show multi-node context menu
      setNodeContextMenu(null);
      setMultiNodeContextMenu({
        x: event.clientX,
        y: event.clientY,
        nodes: selectedNodes,
      });
    } else {
      // Show single node context menu
      setMultiNodeContextMenu(null);
      setNodeContextMenu({
        x: event.clientX,
        y: event.clientY,
        node: node,
      });
    }
  }, [selectedNodes]);

  // Close all context menus
  const closeAllMenus = useCallback(() => {
    setContextMenu(null);
    setNodeContextMenu(null);
    setMultiNodeContextMenu(null);
  }, []);

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
      {inputNodes.length > 0 && (
        <div className="graph-canvas-controls">
          {onSaveView && (
            <button className="graph-save-button" onClick={handleSaveView}>
              💾 Save View
            </button>
          )}
          {visibleNodes.length > LAZY_LOAD_THRESHOLD && loadedNodeCount < visibleNodes.length && (
            <div className="graph-lazy-load-info">
              Showing {loadedNodeCount} of {visibleNodes.length} nodes
              <button className="graph-load-more-button" onClick={handleLoadMore}>
                Load More
              </button>
            </div>
          )}
        </div>
      )}

      {inputNodes.length === 0 ? (
        <div className="graph-empty-message">
          <h3>No graph to display</h3>
          <p>Search or add nodes to start exploring the knowledge graph.</p>
        </div>
      ) : (
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
            onPaneClick={closeAllMenus}
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
            onMoveStart={closeAllMenus}
          >
            <Background color="#333" gap={16} />
            <Controls />
            <MiniMap
              nodeColor={(node) => node.data?.color || '#9CA3AF'}
              maskColor="rgba(0, 0, 0, 0.5)"
              pannable
              zoomable
            />
          </ReactFlow>
        </div>
      )}

      {contextMenu && (
        <div
          className="graph-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button onClick={handleAddGroup}>📁 Lägg till grupp</button>
          {onSaveView && <button onClick={handleSaveView}>💾 Spara vy</button>}
        </div>
      )}

      {nodeContextMenu && (
        <div
          className="graph-context-menu node-context-menu"
          style={{ left: nodeContextMenu.x, top: nodeContextMenu.y }}
        >
          <div className="context-menu-header">{nodeContextMenu.node.data?.label}</div>
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
          {(onHideMultiple || onHide) && (
            <button onClick={() => {
              const nodeIds = multiNodeContextMenu.nodes.map(n => n.id);
              if (onHideMultiple) {
                onHideMultiple(nodeIds);
              } else if (onHide) {
                // Fallback: call onHide for each node
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
                  // Fallback: call onDelete for each node (but this may not work well)
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
