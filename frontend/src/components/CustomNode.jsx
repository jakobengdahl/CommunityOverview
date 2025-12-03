import { useState } from 'react';
import { Handle, Position } from 'reactflow';
import './CustomNode.css';

function CustomNode({ data }) {
  const [showRelatedButton, setShowRelatedButton] = useState(false);

  const handleShowRelated = () => {
    // TODO: Implement "show related nodes" via MCP
    console.log('Show related nodes for:', data.label);
  };

  return (
    <div
      className={`custom-node ${data.isHighlighted ? 'highlighted' : ''}`}
      style={{ borderColor: data.color }}
      onMouseEnter={() => setShowRelatedButton(true)}
      onMouseLeave={() => setShowRelatedButton(false)}
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

      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export default CustomNode;
