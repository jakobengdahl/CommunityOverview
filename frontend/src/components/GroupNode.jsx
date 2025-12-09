import { memo } from 'react';
import { NodeResizer } from 'reactflow';
import './GroupNode.css';

/**
 * GroupNode - React Flow group node for organizing related nodes
 * Uses React Flow's native parentId system for node containment
 */
function GroupNode({ data, selected }) {
  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        isVisible={selected}
        lineStyle={{ stroke: data.color || '#646cff', strokeWidth: 2 }}
        handleStyle={{
          width: 8,
          height: 8,
          background: data.color || '#646cff',
          border: '2px solid white'
        }}
      />
      <div
        className="group-node"
        style={{
          borderColor: data.color || '#646cff',
          backgroundColor: `${data.color || '#646cff'}15` // 15 = ~8% opacity in hex
        }}
      >
        <div
          className="group-node-header"
          style={{
            backgroundColor: data.color || '#646cff',
            color: 'white'
          }}
        >
          <span className="group-node-icon">📁</span>
          <span className="group-node-label">{data.label || 'Group'}</span>
        </div>
        {data.description && (
          <div className="group-node-description">{data.description}</div>
        )}
      </div>
    </>
  );
}

export default memo(GroupNode);
