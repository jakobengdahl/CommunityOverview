import { useCallback, useMemo, useEffect, useState, useRef } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  useReactFlow,
} from 'reactflow';
import 'reactflow/dist/style.css';
import useGraphStore from '../store/graphStore';
import CustomNode from './CustomNode';
import GroupNode from './GroupNode';
import StatsPanel from './StatsPanel';
import SaveViewDialog from './SaveViewDialog';
import ContextMenu from './ContextMenu';
import NodeContextMenu from './NodeContextMenu';
import EditNodeDialog from './EditNodeDialog';
import { executeTool } from '../services/api';
import { getLayoutedElements, getCircularLayout, getGridLayout } from '../utils/graphLayout';
import { useMemoizedLayout } from '../hooks/useMemoizedLayout';
import './VisualizationPanel.css';

// Lazy loading threshold - only render first N nodes if graph is large
const LAZY_LOAD_THRESHOLD = 200;
const INITIAL_LOAD_COUNT = 100;

// Node color mapping from metamodel
const NODE_COLORS = {
  Actor: '#3B82F6',
  Community: '#A855F7',
  Initiative: '#10B981',
  Capability: '#F97316',
  Resource: '#FBBF24',
  Legislation: '#EF4444',
  Theme: '#14B8A6',
  VisualizationView: '#6B7280',
};

function VisualizationPanel() {
  const {
    nodes: storeNodes,
    edges: storeEdges,
    highlightedNodeIds,
    hiddenNodeIds,
    toggleNodeVisibility,
    addNodesToVisualization,
    clearGroupsFlag,
  } = useGraphStore();

  const [isSaveDialogOpen, setIsSaveDialogOpen] = useState(false);
  const [loadedNodeCount, setLoadedNodeCount] = useState(INITIAL_LOAD_COUNT);
  const [contextMenu, setContextMenu] = useState(null);
  const [nodeContextMenu, setNodeContextMenu] = useState(null);
  const [editingNode, setEditingNode] = useState(null);
  const [reactFlowInstance, setReactFlowInstance] = useState(null);
  const [notification, setNotification] = useState(null);
  const reactFlowWrapper = useRef(null);
  const [isRightDragging, setIsRightDragging] = useState(false);
  const rightDragStart = useRef({ x: 0, y: 0, time: null });

  // Filter out hidden nodes and their edges
  const visibleNodes = useMemo(() =>
    storeNodes.filter(n => !hiddenNodeIds.includes(n.id)),
    [storeNodes, hiddenNodeIds]
  );

  // Lazy loading - only show first N nodes if graph is large
  const nodesToRender = useMemo(() => {
    if (visibleNodes.length <= LAZY_LOAD_THRESHOLD) {
      return visibleNodes;
    }
    // For large graphs, progressively load nodes
    return visibleNodes.slice(0, loadedNodeCount);
  }, [visibleNodes, loadedNodeCount]);

  const visibleEdges = useMemo(() => {
    const renderedNodeIds = new Set(nodesToRender.map(n => n.id));
    return storeEdges.filter(e =>
      !hiddenNodeIds.includes(e.source) &&
      !hiddenNodeIds.includes(e.target) &&
      renderedNodeIds.has(e.source) &&
      renderedNodeIds.has(e.target)
    );
  }, [storeEdges, hiddenNodeIds, nodesToRender]);

  // Convert edges first (needed for layout calculation)
  const reactFlowEdges = useMemo(() => {
    return visibleEdges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      type: 'default', // Use default bezier edges for better routing
      animated: false,
      style: {
        stroke: '#666',
        strokeWidth: 2
      },
      labelStyle: {
        fill: '#888',
        fontSize: 10,
        fontWeight: 500
      },
      labelBgStyle: {
        fill: '#1a1a1a',
        fillOpacity: 0.8
      }
    }));
  }, [visibleEdges]);

  // Convert store data to React Flow format with automatic layout
  const reactFlowNodes = useMemo(() => {
    const nodesWithoutPosition = nodesToRender.map(node => ({
      id: node.id,
      type: 'custom',
      data: {
        ...node, // Pass full node data for editing
        label: node.name,
        summary: node.summary || node.description?.slice(0, 100),
        nodeType: node.type,
        color: NODE_COLORS[node.type] || '#9CA3AF',
        isHighlighted: highlightedNodeIds.includes(node.id),
        onEdit: (nodeToEdit) => setEditingNode({ id: node.id, data: nodeToEdit }), // Pass edit callback
      },
      position: { x: 0, y: 0 }, // Will be set by layout algorithm
    }));

    if (nodesWithoutPosition.length === 0) {
      return nodesWithoutPosition;
    }

    // Use grid layout for many nodes with sparse edges
    // This prevents long horizontal lines when expanding nodes with many connections
    const nodeCount = nodesWithoutPosition.length;
    const edgeCount = reactFlowEdges.length;
    const shouldUseGrid = nodeCount > 15 && edgeCount < nodeCount * 1.5;

    if (shouldUseGrid) {
      // Grid layout for many nodes (e.g., 40 nodes → ~7x6 grid in 4:3 ratio)
      return getGridLayout(nodesWithoutPosition);
    }

    // Calculate positions using dagre layout if we have edges
    if (edgeCount > 0) {
      return getLayoutedElements(nodesWithoutPosition, reactFlowEdges, 'TB');
    }

    // Fallback to circular layout if no edges (isolated nodes)
    return getCircularLayout(nodesWithoutPosition, 400, 300, 250);
  }, [nodesToRender, highlightedNodeIds, reactFlowEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  const { updateNodePositions } = useGraphStore();

  // Clear groups when flag is set (e.g., when loading new view or adding nodes)
  useEffect(() => {
    if (clearGroupsFlag) {
      setNodes((nds) => nds.filter(n => n.type !== 'group' && !n.id.startsWith('group-')));
    }
  }, [clearGroupsFlag, setNodes]);

  // Update nodes when reactFlowNodes changes (for layout recalculation)
  useEffect(() => {
    // Only update if IDs changed or we have a massive shift, to avoid resetting dragged positions
    // This is tricky with React Flow controlled mode.
    // Ideally we merge positions and preserve manually created nodes (like groups).
    setNodes((nds) => {
        // Preserve manually created nodes that aren't from backend (e.g., groups)
        // Unless clearGroupsFlag is set
        const manualNodes = clearGroupsFlag ? [] : nds.filter(n => n.type === 'group' || n.id.startsWith('group-'));

        const newNodes = reactFlowNodes.map(n => {
            const existing = nds.find(curr => curr.id === n.id);
            if (existing && existing.position.x !== 0) {
                // Keep existing position, parentId, extent, and style
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

        // Combine backend nodes with manually created nodes
        return [...newNodes, ...manualNodes];
    });
  }, [reactFlowNodes, clearGroupsFlag, setNodes]);

  // Update edges when reactFlowEdges changes
  useEffect(() => {
    setEdges(reactFlowEdges);
  }, [reactFlowEdges, setEdges]);

  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges]
  );

  const onNodeContextMenu = useCallback(
    (event, node) => {
      event.preventDefault();
      event.stopPropagation();

      setNodeContextMenu({
        x: event.clientX,
        y: event.clientY,
        node: node,
      });
    },
    []
  );

  const handleEditNode = useCallback((node) => {
    setEditingNode(node);
  }, []);

  const handleHideNode = useCallback((nodeId) => {
    toggleNodeVisibility(nodeId);
  }, [toggleNodeVisibility]);

  const handleDeleteNode = useCallback(async (nodeId) => {
    // TODO: Implement delete node from graph
    console.log('Delete node:', nodeId);
    // For now, just hide it
    toggleNodeVisibility(nodeId);
  }, [toggleNodeVisibility]);

  const handleExpandNode = useCallback(async (nodeId) => {
    // Fetch related nodes and add them to visualization
    try {
      const result = await executeTool('get_related_nodes', {
        node_id: nodeId,
        depth: 1
      });

      if (result.nodes && result.nodes.length > 0) {
        // Get current state and merge with new nodes/edges (same logic as + icon)
        const { nodes: currentNodes, edges: currentEdges } = useGraphStore.getState();
        const existingNodeIds = new Set(currentNodes.map(n => n.id));
        const existingEdgeIds = new Set(currentEdges.map(e => e.id));

        const newNodes = result.nodes.filter(n => !existingNodeIds.has(n.id));
        const newEdges = (result.edges || []).filter(e => !existingEdgeIds.has(e.id));

        if (newNodes.length > 0 || newEdges.length > 0) {
          useGraphStore.getState().updateVisualization(
            [...currentNodes, ...newNodes],
            [...currentEdges, ...newEdges],
            newNodes.map(n => n.id) // Highlight new nodes
          );
        }
      }

      setNotification({
        type: 'success',
        message: `Expanderade nod med ${result.nodes?.length || 0} relaterade noder`
      });
      setTimeout(() => setNotification(null), 3000);
    } catch (error) {
      console.error('Error expanding node:', error);
      setNotification({
        type: 'error',
        message: 'Kunde inte expandera nod'
      });
      setTimeout(() => setNotification(null), 3000);
    }
  }, []);

  const handleShowInNewView = useCallback(async (nodeId) => {
    // Clear current view and show only this node + its connections
    try {
      const result = await executeTool('get_related_nodes', {
        node_id: nodeId,
        depth: 1
      });

      // Replace visualization with only this node and its connections
      if (result.nodes && result.nodes.length > 0) {
        useGraphStore.getState().updateVisualization(
          result.nodes,
          result.edges || [],
          [] // No highlighting
        );
        // Clear hidden nodes
        useGraphStore.setState({ hiddenNodeIds: [] });
      }

      setNotification({
        type: 'success',
        message: 'Visar nod i ny visualisering'
      });
      setTimeout(() => setNotification(null), 3000);
    } catch (error) {
      console.error('Error showing in new view:', error);
      setNotification({
        type: 'error',
        message: 'Kunde inte visa i ny visualisering'
      });
      setTimeout(() => setNotification(null), 3000);
    }
  }, []);

  const onPaneContextMenu = useCallback(
    (event) => {
      console.log('[VisualizationPanel] onPaneContextMenu triggered');
      // Prevent default browser menu
      event.preventDefault();
      event.stopPropagation();

      // Only show context menu if it was a quick click (not a drag)
      // If no mousedown was tracked, always show context menu
      if (rightDragStart.current.time === null) {
        setContextMenu({
          x: event.clientX,
          y: event.clientY,
        });
        console.log('[VisualizationPanel] Context menu shown (no drag tracked)');
        return;
      }

      // Check if mouse moved less than 5px and time was less than 300ms
      const timeDiff = Date.now() - rightDragStart.current.time;
      const xDiff = Math.abs(event.clientX - rightDragStart.current.x);
      const yDiff = Math.abs(event.clientY - rightDragStart.current.y);
      const wasDrag = timeDiff > 300 || xDiff > 5 || yDiff > 5;

      if (!wasDrag) {
        setContextMenu({
          x: event.clientX,
          y: event.clientY,
        });
        console.log('[VisualizationPanel] Context menu shown (quick click)');
      } else {
        console.log('[VisualizationPanel] Context menu suppressed (was a drag)');
      }

      // Reset tracking after handling
      rightDragStart.current.time = null;
    },
    []
  );

  const onNodeDragStop = useCallback((event, draggedNode) => {
    // Sync only the dragged node position to store
    updateNodePositions([{ id: draggedNode.id, position: draggedNode.position }]);

    // Check if the node was dropped inside a group
    if (draggedNode.type === 'group') return; // Don't process groups themselves

    const groupNodes = nodes.filter(n => n.type === 'group');
    let droppedInGroup = null;

    // Check each group to see if the dragged node is inside it
    for (const groupNode of groupNodes) {
      const groupBounds = {
        left: groupNode.position.x,
        right: groupNode.position.x + (groupNode.style?.width || 300),
        top: groupNode.position.y,
        bottom: groupNode.position.y + (groupNode.style?.height || 200),
      };

      const nodeBounds = {
        x: draggedNode.position.x,
        y: draggedNode.position.y,
      };

      // Check if node center is within group bounds
      if (
        nodeBounds.x >= groupBounds.left &&
        nodeBounds.x <= groupBounds.right &&
        nodeBounds.y >= groupBounds.top &&
        nodeBounds.y <= groupBounds.bottom
      ) {
        droppedInGroup = groupNode.id;
        break;
      }
    }

    // Update parentId if needed
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === draggedNode.id) {
          // Convert absolute position to relative if dropping into group
          if (droppedInGroup && n.parentId !== droppedInGroup) {
            const groupNode = nodes.find(gn => gn.id === droppedInGroup);
            return {
              ...n,
              parentId: droppedInGroup,
              position: {
                x: n.position.x - groupNode.position.x,
                y: n.position.y - groupNode.position.y,
              },
              extent: 'parent', // Keep node within parent bounds
            };
          }
          // Remove from group if dragged outside
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
  }, [updateNodePositions, nodes, setNodes]);

  const handleLoadMore = useCallback(() => {
    setLoadedNodeCount(prev => Math.min(prev + 100, visibleNodes.length));
  }, [visibleNodes.length]);

  // Reset loaded count when visible nodes change significantly
  useEffect(() => {
    if (visibleNodes.length <= LAZY_LOAD_THRESHOLD) {
      setLoadedNodeCount(visibleNodes.length);
    } else {
      setLoadedNodeCount(INITIAL_LOAD_COUNT);
    }
  }, [visibleNodes.length]);

  const handleSaveView = async (name) => {
    // Gather current state with standardized format
    const positions = {};
    nodes.forEach(n => {
      positions[n.id] = n.position;
    });

    const nodeIds = nodes.map(n => n.id);

    // Create a node representing the view
    const viewNode = {
      name: name,
      type: 'VisualizationView',
      description: `Saved view: ${name}`,
      summary: `Contains ${nodeIds.length} nodes`,
      metadata: {
        node_ids: nodeIds,
        positions: positions,
        hidden_node_ids: hiddenNodeIds
      },
      communities: [] // Optional: inherit from current selection?
    };

    // Use add_nodes tool to save it
    await executeTool('add_nodes', {
      nodes: [viewNode],
      edges: []
    });

    // Show success notification
    setNotification({
      type: 'success',
      message: `View '${name}' saved successfully!`,
      shareUrl: `?view=${encodeURIComponent(name)}`
    });

    // Auto-hide after 5 seconds
    setTimeout(() => setNotification(null), 5000);
  };

  const handleAddGroup = useCallback(() => {
    if (!reactFlowInstance || !contextMenu) return;

    // Convert screen coordinates to flow coordinates
    const position = reactFlowInstance.screenToFlowPosition({
      x: contextMenu.x,
      y: contextMenu.y,
    });

    // Create a new group node
    const newGroupNode = {
      id: `group-${Date.now()}`,
      type: 'group',
      position,
      data: {
        label: 'New Group',
        description: 'Drag nodes here to group them',
        color: '#646cff'
      },
      style: {
        width: 300,
        height: 200,
      },
    };

    setNodes((nds) => [...nds, newGroupNode]);
    setContextMenu(null);
  }, [reactFlowInstance, contextMenu, setNodes]);

  // Custom node types
  const nodeTypes = useMemo(() => ({
    custom: CustomNode,
    group: GroupNode,
  }), []);

  return (
    <div className="visualization-panel">
      <SaveViewDialog
        isOpen={isSaveDialogOpen}
        onClose={() => setIsSaveDialogOpen(false)}
        onSave={handleSaveView}
      />

      {storeNodes.length > 0 && (
         <div className="view-controls">
            <button
              className="save-view-button"
              onClick={() => setIsSaveDialogOpen(true)}
            >
              💾 Save View
            </button>
            {hiddenNodeIds.length > 0 && (
              <div className="hidden-nodes-indicator">
                {hiddenNodeIds.length} hidden nodes
                <button
                   className="unhide-button"
                   onClick={() => useGraphStore.getState().setHiddenNodeIds([])}
                >
                  Show All
                </button>
              </div>
            )}
            {visibleNodes.length > LAZY_LOAD_THRESHOLD && loadedNodeCount < visibleNodes.length && (
              <div className="lazy-load-info">
                Showing {loadedNodeCount} of {visibleNodes.length} nodes
                <button
                   className="load-more-button"
                   onClick={handleLoadMore}
                >
                  Load More
                </button>
              </div>
            )}
         </div>
      )}

      {storeNodes.length === 0 ? (
        <div className="empty-graph-message">
          <h3>No graph to display yet</h3>
          <p>Ask a question in the chat to start exploring the knowledge graph.</p>
        </div>
      ) : (
        <>
          <StatsPanel />
          <div
            ref={reactFlowWrapper}
            style={{ width: '100%', height: '100%', position: 'relative' }}
          >
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeContextMenu={onNodeContextMenu}
              onNodeDragStop={onNodeDragStop}
              onPaneContextMenu={onPaneContextMenu}
              onPaneClick={() => {
                // Close any open context menus when clicking on background
                setContextMenu(null);
                setNodeContextMenu(null);
              }}
              onPaneMouseDown={(event) => {
                // Track right-click start position and time
                if (event.button === 2) {
                  rightDragStart.current = {
                    x: event.clientX,
                    y: event.clientY,
                    time: Date.now()
                  };
                }
              }}
              onInit={setReactFlowInstance}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{
                padding: 0.2,
                duration: 800,
              }}
              attributionPosition="bottom-right"
              defaultEdgeOptions={{
                animated: true,
                style: { strokeWidth: 2 }
              }}
              panOnDrag={[0, 2]} // Enable panning with left (0) and right (2) mouse buttons
              selectionOnDrag={false} // Disable selection box to allow panning
            >
              <Background color="#333" gap={16} />
              <Controls />
              <MiniMap
                nodeColor={(node) => node.data.color}
                maskColor="rgba(0, 0, 0, 0.5)"
              />
            </ReactFlow>
          </div>

          {/* Pane context menu (for adding groups) */}
          {contextMenu && (
            <ContextMenu
              x={contextMenu.x}
              y={contextMenu.y}
              onClose={() => setContextMenu(null)}
              onAddGroup={handleAddGroup}
            />
          )}

          {/* Node context menu */}
          {nodeContextMenu && (
            <NodeContextMenu
              x={nodeContextMenu.x}
              y={nodeContextMenu.y}
              node={nodeContextMenu.node}
              onClose={() => setNodeContextMenu(null)}
              onEdit={handleEditNode}
              onHide={handleHideNode}
              onDelete={handleDeleteNode}
              onExpand={handleExpandNode}
              onShowInNewView={handleShowInNewView}
            />
          )}

          {/* Edit node dialog */}
          {editingNode && (
            <EditNodeDialog
              node={editingNode}
              onClose={() => setEditingNode(null)}
              onSave={async (updatedNode) => {
                try {
                  // Call backend to update node
                  await executeTool('update_node', {
                    node_id: updatedNode.id,
                    updates: {
                      name: updatedNode.data.name,
                      type: updatedNode.data.type,
                      description: updatedNode.data.description,
                      summary: updatedNode.data.summary,
                      tags: updatedNode.data.tags || [],
                    }
                  });

                  // Update local state
                  const { nodes, updateVisualization } = useGraphStore.getState();
                  const updatedNodes = nodes.map(n =>
                    n.id === updatedNode.id
                      ? {
                          ...n,
                          name: updatedNode.data.name,
                          type: updatedNode.data.type,
                          description: updatedNode.data.description,
                          summary: updatedNode.data.summary,
                          tags: updatedNode.data.tags || [],
                        }
                      : n
                  );
                  updateVisualization(updatedNodes, useGraphStore.getState().edges);

                  setNotification({
                    type: 'success',
                    message: 'Nod uppdaterad'
                  });
                  setTimeout(() => setNotification(null), 3000);
                  setEditingNode(null);
                } catch (error) {
                  console.error('Error updating node:', error);
                  setNotification({
                    type: 'error',
                    message: 'Kunde inte uppdatera nod'
                  });
                  setTimeout(() => setNotification(null), 3000);
                }
              }}
            />
          )}
        </>
      )}

      {/* Notification toast */}
      {notification && (
        <div className={`notification notification-${notification.type}`}>
          <div className="notification-content">
            <span className="notification-icon">
              {notification.type === 'success' ? '✓' : 'ℹ'}
            </span>
            <div className="notification-message">
              <strong>{notification.message}</strong>
              {notification.shareUrl && (
                <div className="notification-share">
                  Share: <code>{notification.shareUrl}</code>
                </div>
              )}
            </div>
            <button
              className="notification-close"
              onClick={() => setNotification(null)}
            >
              ×
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default VisualizationPanel;
