import { useState } from 'react';
import { Handle, Position } from 'reactflow';
import useGraphStore from '../store/graphStore';
import { executeTool } from '../services/api';
import './CustomNode.css';

function CustomNode({ data, id }) {
  const [showRelatedButton, setShowRelatedButton] = useState(false);
  const [showTooltip, setShowTooltip] = useState(false);
  const { addNodesToVisualization, highlightNodes, nodes: currentNodes } = useGraphStore();

  // Get full node data for tooltip - In real app, this should be in 'data' prop
  const fullNode = data; // Assuming 'data' contains necessary info for now

  const handleShowRelated = async (e) => {
    e.stopPropagation();
    try {
      const result = await executeTool('get_related_nodes', {
        node_id: id,
        depth: 1
      });

      if (result.nodes && result.nodes.length > 0) {
        // Add to existing visualization instead of replacing
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
    } catch (error) {
      console.error('Error expanding node:', error);
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
        <>
          <button
            className="show-related-button"
            onClick={handleShowRelated}
            title="Show related nodes"
          >
            +
          </button>
          <button
            className="edit-node-button"
            onClick={(e) => {
              e.stopPropagation();
              if (data.onEdit) {
                data.onEdit(fullNode);
              }
            }}
            title="Edit node"
          >
            ✏️
          </button>
        </>
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
