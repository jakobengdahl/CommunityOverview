import { useState } from 'react';
import { Handle, Position } from 'reactflow';
import useGraphStore from '../store/graphStore';
// import { DEMO_GRAPH_DATA } from '../services/demoData'; // REMOVED
import './CustomNode.css';

function CustomNode({ data, id }) {
  const [showRelatedButton, setShowRelatedButton] = useState(false);
  const [showTooltip, setShowTooltip] = useState(false);
  const { addNodesToVisualization, highlightNodes, nodes: currentNodes } = useGraphStore();

  // Get full node data for tooltip - In real app, this should be in 'data' prop
  const fullNode = data; // Assuming 'data' contains necessary info for now

  const handleShowRelated = () => {
    // In backend integration, this should trigger a fetch
    console.log("Expanding node:", id);
    // TODO: Call backend to get related nodes
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
