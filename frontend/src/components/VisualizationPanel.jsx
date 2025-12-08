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
import StatsPanel from './StatsPanel';
import SaveViewDialog from './SaveViewDialog';
import ShapeRectangle from './ShapeRectangle';
import ContextMenu from './ContextMenu';
import { executeTool } from '../services/api';
import { getLayoutedElements, getCircularLayout } from '../utils/graphLayout';
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
    shapes,
    addShape,
    addNodeToShape
  } = useGraphStore();

  const [isSaveDialogOpen, setIsSaveDialogOpen] = useState(false);
  const [loadedNodeCount, setLoadedNodeCount] = useState(INITIAL_LOAD_COUNT);
  const [contextMenu, setContextMenu] = useState(null);
  const [reactFlowInstance, setReactFlowInstance] = useState(null);
  const reactFlowWrapper = useRef(null);

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
      type: 'smoothstep',
      animated: false,
      style: { stroke: '#666' },
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
      },
      position: { x: 0, y: 0 }, // Will be set by layout algorithm
    }));

    if (nodesWithoutPosition.length === 0) {
      return nodesWithoutPosition;
    }

    // Calculate positions using memoized dagre layout if we have edges
    if (reactFlowEdges.length > 0) {
      return getLayoutedElements(nodesWithoutPosition, reactFlowEdges, 'TB');
    }

    // Fallback to circular layout if no edges (isolated nodes)
    return getCircularLayout(nodesWithoutPosition, 400, 300, 250);
  }, [nodesToRender, highlightedNodeIds, reactFlowEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  const { updateNodePositions } = useGraphStore();

  // Update nodes when reactFlowNodes changes (for layout recalculation)
  useEffect(() => {
    // Only update if IDs changed or we have a massive shift, to avoid resetting dragged positions
    // This is tricky with React Flow controlled mode.
    // Ideally we merge positions.
    setNodes((nds) => {
        const newNodes = reactFlowNodes.map(n => {
            const existing = nds.find(curr => curr.id === n.id);
            if (existing && existing.position.x !== 0) {
                // Keep existing position if valid
                return { ...n, position: existing.position };
            }
            return n;
        });
        return newNodes;
    });
  }, [reactFlowNodes, setNodes]);

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
      // Hide the node on right click
      toggleNodeVisibility(node.id);
    },
    [toggleNodeVisibility]
  );

  const onPaneContextMenu = useCallback(
    (event) => {
      event.preventDefault();
      setContextMenu({
        x: event.clientX,
        y: event.clientY,
      });
    },
    []
  );

  const handleAddRectangle = useCallback(() => {
    if (!reactFlowInstance || !contextMenu || !reactFlowWrapper.current) return;

    // Convert screen coordinates to ReactFlow coordinates
    const viewport = reactFlowInstance.getViewport();
    const bounds = reactFlowWrapper.current.getBoundingClientRect();

    const x = (contextMenu.x - bounds.left - viewport.x) / viewport.zoom;
    const y = (contextMenu.y - bounds.top - viewport.y) / viewport.zoom;

    addShape({
      type: 'rectangle',
      x,
      y,
      width: 300,
      height: 200,
      color: '#3B82F6',
      title: 'New Group',
      nodeIds: []
    });
  }, [contextMenu, reactFlowInstance, addShape, reactFlowWrapper]);

  const handleNodeDrag = useCallback((event, node) => {
    // Check if node is being dragged over any shape
    if (!reactFlowInstance) return;

    const nodePos = node.position;
    shapes.forEach(shape => {
      const isInside =
        nodePos.x >= shape.x &&
        nodePos.x <= shape.x + shape.width &&
        nodePos.y >= shape.y &&
        nodePos.y <= shape.y + shape.height;

      if (isInside && !shape.nodeIds?.includes(node.id)) {
        // Visual feedback could be added here
      }
    });
  }, [shapes, reactFlowInstance]);

  const onNodeDragStop = useCallback((event, node) => {
    // Sync only the dragged node position to store
    updateNodePositions([{ id: node.id, position: node.position }]);

    // Check if node should be added to a shape
    if (!reactFlowInstance) return;

    const nodePos = node.position;
    shapes.forEach(shape => {
      const isInside =
        nodePos.x >= shape.x &&
        nodePos.x <= shape.x + shape.width &&
        nodePos.y >= shape.y &&
        nodePos.y <= shape.y + shape.height;

      if (isInside && !shape.nodeIds?.includes(node.id)) {
        addNodeToShape(shape.id, node.id);
      }
    });
  }, [updateNodePositions, shapes, reactFlowInstance, addNodeToShape]);

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
        hidden_node_ids: hiddenNodeIds,
        shapes: shapes // Save custom shapes
      },
      communities: [] // Optional: inherit from current selection?
    };

    // Use add_nodes tool to save it
    await executeTool('add_nodes', {
      nodes: [viewNode],
      edges: []
    });

    alert(`View '${name}' saved successfully! Share this view with: ?view=${encodeURIComponent(name)}`);
  };

  // Custom node types
  const nodeTypes = useMemo(() => ({
    custom: CustomNode,
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
            onContextMenu={(e) => e.preventDefault()}
          >
            {/* Render shapes layer behind ReactFlow */}
            <div className="shapes-layer">
              {shapes.map(shape => (
                <ShapeRectangle
                  key={shape.id}
                  shape={shape}
                  reactFlowInstance={reactFlowInstance}
                  reactFlowWrapper={reactFlowWrapper}
                />
              ))}
            </div>

            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeContextMenu={onNodeContextMenu}
              onNodeDragStop={onNodeDragStop}
              onNodeDrag={handleNodeDrag}
              onPaneContextMenu={onPaneContextMenu}
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
            >
              <Background color="#333" gap={16} />
              <Controls />
              <MiniMap
                nodeColor={(node) => node.data.color}
                maskColor="rgba(0, 0, 0, 0.5)"
              />
            </ReactFlow>
          </div>

          {/* Context menu */}
          {contextMenu && (
            <ContextMenu
              x={contextMenu.x}
              y={contextMenu.y}
              onClose={() => setContextMenu(null)}
              onAddRectangle={handleAddRectangle}
            />
          )}
        </>
      )}
    </div>
  );
}

export default VisualizationPanel;
