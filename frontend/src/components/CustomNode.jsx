import { useState } from 'react';
import { Handle, Position } from 'reactflow';
import useGraphStore from '../store/graphStore';
import { DEMO_GRAPH_DATA } from '../services/demoData';
import './CustomNode.css';

function CustomNode({ data, id }) {
  const [showRelatedButton, setShowRelatedButton] = useState(false);
  const [showTooltip, setShowTooltip] = useState(false);
  const { addNodesToVisualization, highlightNodes, nodes: currentNodes } = useGraphStore();

  // Get full node data for tooltip
  const fullNode = DEMO_GRAPH_DATA.nodes.find(n => n.id === id);

  const handleShowRelated = () => {
    // Find all edges connected to this node
    const relatedEdges = DEMO_GRAPH_DATA.edges.filter(
      edge => edge.source === id || edge.target === id
    );

    // Find related node IDs
    const relatedNodeIds = new Set();
    relatedEdges.forEach(edge => {
      if (edge.source === id) relatedNodeIds.add(edge.target);
      if (edge.target === id) relatedNodeIds.add(edge.source);
    });

    // Get related nodes that aren't already in the visualization
    const currentNodeIds = new Set(currentNodes.map(n => n.id));
    const newNodes = DEMO_GRAPH_DATA.nodes.filter(
      node => relatedNodeIds.has(node.id) && !currentNodeIds.has(node.id)
    );

    // Get edges between the clicked node and new nodes
    const newEdges = relatedEdges.filter(edge =>
      (edge.source === id && !currentNodeIds.has(edge.target)) ||
      (edge.target === id && !currentNodeIds.has(edge.source))
    );

    if (newNodes.length > 0) {
      // Add new nodes and edges to visualization
      addNodesToVisualization(newNodes, newEdges);

      // Highlight the newly added nodes
      highlightNodes(newNodes.map(n => n.id));

      // Remove highlight after 2 seconds
      setTimeout(() => highlightNodes([]), 2000);
    } else {
      console.log('All related nodes are already visible');
    }
  };

  return (
    <div
      className={`custom-node ${data.isHighlighted ? 'highlighted' : ''}`}
      style={{ borderColor: data.color }}
      onMouseEnter={() => {
        setShowRelatedButton(true);
        setShowTooltip(true);
      }}
      onMouseLeave={() => {
        setShowRelatedButton(false);
        setShowTooltip(false);
      }}
    >
      <Handle type="target" position={Position.Top} />

      <div className="node-header" style={{ backgroundColor: data.color }}>
        <span className="node-type">{data.nodeType}</span>
      </div>

      <div className="node-content">
        <div className="node-label">{data.label}</div>
        {data.summary && (
          <div className="node-summary">{data.summary}</div>
        )}
      </div>

      {showRelatedButton && (
        <button
          className="show-related-button"
          onClick={handleShowRelated}
          title="Show related nodes"
        >
          +
        </button>
      )}

      {showTooltip && fullNode && (
        <div className="node-tooltip">
          <div className="tooltip-header">
            <strong>{fullNode.type}:</strong> {fullNode.name}
          </div>
          {fullNode.description && (
            <div className="tooltip-description">
              {fullNode.description}
            </div>
          )}
          {fullNode.communities && fullNode.communities.length > 0 && (
            <div className="tooltip-communities">
              <strong>Communities:</strong> {fullNode.communities.join(', ')}
            </div>
          )}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export default CustomNode;
