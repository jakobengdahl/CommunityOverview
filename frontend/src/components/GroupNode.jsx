import { memo, useState, useRef, useEffect } from 'react';
import { NodeResizer, useReactFlow } from 'reactflow';
import GroupContextMenu from './GroupContextMenu';
import './GroupNode.css';

/**
 * GroupNode - React Flow group node for organizing related nodes
 * Uses React Flow's native parentId system for node containment
 */
function GroupNode({ id, data, selected }) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedLabel, setEditedLabel] = useState(data.label || 'Group');
  const [contextMenu, setContextMenu] = useState(null);
  const inputRef = useRef(null);
  const groupRef = useRef(null);
  const { setNodes } = useReactFlow();

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // Close context menu when clicking elsewhere or dragging
  useEffect(() => {
    if (!contextMenu) return;

    const handleGlobalClick = (e) => {
      // Close menu if clicking outside the group entirely
      if (groupRef.current && !groupRef.current.contains(e.target)) {
        setContextMenu(null);
      }
    };

    const handleNodeDrag = () => {
      // Close menu when any node is dragged
      setContextMenu(null);
    };

    // Listen to global clicks and React Flow events
    document.addEventListener('mousedown', handleGlobalClick);
    window.addEventListener('react-flow-node-drag-start', handleNodeDrag);

    return () => {
      document.removeEventListener('mousedown', handleGlobalClick);
      window.removeEventListener('react-flow-node-drag-start', handleNodeDrag);
    };
  }, [contextMenu]);

  const handleDoubleClick = (e) => {
    e.stopPropagation();
    setIsEditing(true);
  };

  const handleLabelChange = (e) => {
    setEditedLabel(e.target.value);
  };

  const handleLabelBlur = () => {
    setIsEditing(false);
    if (editedLabel.trim()) {
      // Update the node's label
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id === id) {
            return {
              ...n,
              data: {
                ...n.data,
                label: editedLabel.trim(),
              },
            };
          }
          return n;
        })
      );
    } else {
      setEditedLabel(data.label || 'Group');
    }
  };

  const handleLabelKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleLabelBlur();
    } else if (e.key === 'Escape') {
      setIsEditing(false);
      setEditedLabel(data.label || 'Group');
    }
  };

  const handleHeaderContextMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({
      x: e.clientX,
      y: e.clientY,
    });
  };

  const handleChangeColor = (color) => {
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === id) {
          return {
            ...n,
            data: {
              ...n.data,
              color: color,
            },
          };
        }
        return n;
      })
    );
  };

  const handleDeleteGroup = () => {
    // Remove this group node and unparent any child nodes
    setNodes((nds) => {
      // Find children of this group
      const children = nds.filter(n => n.parentId === id);

      // Calculate absolute positions for children
      const groupNode = nds.find(n => n.id === id);
      const updatedChildren = children.map(child => ({
        ...child,
        parentId: undefined,
        extent: undefined,
        position: {
          x: child.position.x + (groupNode?.position.x || 0),
          y: child.position.y + (groupNode?.position.y || 0),
        },
      }));

      // Remove group and update children
      return nds
        .filter(n => n.id !== id)
        .map(n => {
          const updated = updatedChildren.find(c => c.id === n.id);
          return updated || n;
        });
    });
  };

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
        ref={groupRef}
        className="group-node"
        style={{
          borderColor: data.color || '#646cff',
          backgroundColor: `${data.color || '#646cff'}15` // 15 = ~8% opacity in hex
        }}
        onContextMenu={(e) => {
          // Prevent pane context menu from showing when right-clicking inside group (but not on header)
          if (e.target.classList.contains('group-node') || e.target.classList.contains('group-node-description')) {
            e.stopPropagation();
            e.preventDefault();
          }
        }}
      >
        <div
          className="group-node-header"
          style={{
            backgroundColor: data.color || '#646cff',
            color: 'white'
          }}
          onContextMenu={handleHeaderContextMenu}
          onDoubleClick={handleDoubleClick}
        >
          <span className="group-node-icon">📁</span>
          {isEditing ? (
            <input
              ref={inputRef}
              type="text"
              className="group-node-label-input"
              value={editedLabel}
              onChange={handleLabelChange}
              onBlur={handleLabelBlur}
              onKeyDown={handleLabelKeyDown}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <span className="group-node-label">{data.label || 'Group'}</span>
          )}
        </div>
        {data.description && (
          <div className="group-node-description">{data.description}</div>
        )}
      </div>

      {contextMenu && (
        <GroupContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          onClose={() => setContextMenu(null)}
          onChangeColor={handleChangeColor}
          onDelete={handleDeleteGroup}
        />
      )}
    </>
  );
}

export default memo(GroupNode);
