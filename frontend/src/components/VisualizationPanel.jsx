import { useCallback, useMemo } from 'react';
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
import './VisualizationPanel.css';

// Node color mapping från metamodell
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

  // Konvertera store-data till React Flow format
  const reactFlowNodes = useMemo(() => {
    return storeNodes.map(node => ({
      id: node.id,
      type: 'custom',
      data: {
        label: node.name,
        summary: node.summary || node.description?.slice(0, 100),
        nodeType: node.type,
        color: NODE_COLORS[node.type] || '#9CA3AF',
        isHighlighted: highlightedNodeIds.includes(node.id),
      },
      position: { x: Math.random() * 500, y: Math.random() * 500 }, // TODO: Bättre layout
    }));
  }, [storeNodes, highlightedNodeIds]);

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

  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(reactFlowEdges);

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
          <h3>Ingen graf att visa ännu</h3>
          <p>Ställ en fråga i chatten för att börja utforska kunskapsgrafen.</p>
        </div>
      ) : (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          nodeTypes={nodeTypes}
          fitView
          attributionPosition="bottom-right"
        >
          <Background color="#333" gap={16} />
          <Controls />
          <MiniMap
            nodeColor={(node) => node.data.color}
            maskColor="rgba(0, 0, 0, 0.5)"
          />
        </ReactFlow>
      )}
    </div>
  );
}

export default VisualizationPanel;
