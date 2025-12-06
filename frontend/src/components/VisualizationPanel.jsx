import { useCallback, useMemo, useEffect, useState } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
} from 'reactflow';
import 'reactflow/dist/style.css';
import useGraphStore from '../store/graphStore';
import CustomNode from './CustomNode';
import StatsPanel from './StatsPanel';
import SaveViewDialog from './SaveViewDialog';
import { executeTool } from '../services/api';
import { getLayoutedElements } from '../utils/graphLayout';
import './VisualizationPanel.css';

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
    addNodesToVisualization
  } = useGraphStore();

  const [isSaveDialogOpen, setIsSaveDialogOpen] = useState(false);

  // Filter out hidden nodes and their edges
  const visibleNodes = useMemo(() =>
    storeNodes.filter(n => !hiddenNodeIds.includes(n.id)),
    [storeNodes, hiddenNodeIds]
  );

  const visibleEdges = useMemo(() =>
    storeEdges.filter(e =>
      !hiddenNodeIds.includes(e.source) && !hiddenNodeIds.includes(e.target)
    ),
    [storeEdges, hiddenNodeIds]
  );

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
    const nodesWithoutPosition = visibleNodes.map(node => ({
      id: node.id,
      type: 'custom',
      data: {
        label: node.name,
        summary: node.summary || node.description?.slice(0, 100),
        nodeType: node.type,
        color: NODE_COLORS[node.type] || '#9CA3AF',
        isHighlighted: highlightedNodeIds.includes(node.id),
      },
      position: { x: 0, y: 0 }, // Will be set by layout algorithm
    }));

    // Calculate positions using dagre layout
    if (nodesWithoutPosition.length > 0 && reactFlowEdges.length > 0) {
      return getLayoutedElements(nodesWithoutPosition, reactFlowEdges, 'TB');
    }

    return nodesWithoutPosition;
  }, [visibleNodes, highlightedNodeIds, reactFlowEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  const { updateNodePositions } = useGraphStore();

  const onNodeDragStop = useCallback((event, node) => {
      // Sync only the dragged node position to store
      updateNodePositions([{ id: node.id, position: node.position }]);
  }, [updateNodePositions]);

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
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeContextMenu={onNodeContextMenu}
            onNodeDragStop={onNodeDragStop}
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
        </>
      )}
    </div>
  );
}

export default VisualizationPanel;
