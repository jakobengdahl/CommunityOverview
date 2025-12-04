import { useCallback, useMemo, useEffect } from 'react';
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
  const { nodes: storeNodes, edges: storeEdges, highlightedNodeIds } = useGraphStore();

  // Convert edges first (needed for layout calculation)
  const reactFlowEdges = useMemo(() => {
    return storeEdges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      type: 'smoothstep',
      animated: false,
      style: { stroke: '#666' },
    }));
  }, [storeEdges]);

  // Convert store data to React Flow format with automatic layout
  const reactFlowNodes = useMemo(() => {
    const nodesWithoutPosition = storeNodes.map(node => ({
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
  }, [storeNodes, highlightedNodeIds, reactFlowEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

  // Update nodes when reactFlowNodes changes (for layout recalculation)
  useEffect(() => {
    setNodes(reactFlowNodes);
  }, [reactFlowNodes, setNodes]);

  // Update edges when reactFlowEdges changes
  useEffect(() => {
    setEdges(reactFlowEdges);
  }, [reactFlowEdges, setEdges]);

  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge(params, eds)),
    [setEdges]
  );

  // Custom node types
  const nodeTypes = useMemo(() => ({
    custom: CustomNode,
  }), []);

  return (
    <div className="visualization-panel">
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
