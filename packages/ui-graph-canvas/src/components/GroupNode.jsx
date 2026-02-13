import { memo, useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import './GroupNode.css';

/**
 * GroupNode - A container node for organizing related nodes
 *
 * Features:
 * - Resizable container
 * - Editable label (double-click)
 * - Context menu for color changes
 * - Drag-and-drop node containment
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

  useEffect(() => {
    if (!contextMenu) return;

    const handleGlobalClick = (e) => {
      if (groupRef.current && !groupRef.current.contains(e.target)) {
        setContextMenu(null);
      }
    };

    document.addEventListener('mousedown', handleGlobalClick);
    return () => document.removeEventListener('mousedown', handleGlobalClick);
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
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id === id) {
            return { ...n, data: { ...n.data, label: editedLabel.trim() } };
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

  const handleContextMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const handleChangeColor = (color) => {
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === id) {
          return { ...n, data: { ...n.data, color } };
        }
        return n;
      })
    );
    setContextMenu(null);
  };

  const handleDeleteGroup = () => {
    setNodes((nds) => {
      const children = nds.filter(n => n.parentId === id);
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

      return nds
        .filter(n => n.id !== id)
        .map(n => {
          const updated = updatedChildren.find(c => c.id === n.id);
          return updated || n;
        });
    });
  };

  const colors = ['#646cff', '#10B981', '#F97316', '#EF4444', '#A855F7', '#3B82F6'];

  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        isVisible={selected}
        lineStyle={{ stroke: data.color || '#646cff', strokeWidth: 4 }}
        handleStyle={{
          width: 14,
          height: 14,
          background: data.color || '#646cff',
          border: '2px solid white',
          borderRadius: '3px',
        }}
      />
      <div
        ref={groupRef}
        className="graph-group-node"
        style={{
          borderColor: data.color || '#646cff',
          backgroundColor: `${data.color || '#646cff'}15`
        }}
      >
        <div
          className="graph-group-header"
          style={{ backgroundColor: data.color || '#646cff', color: 'white' }}
          onContextMenu={handleContextMenu}
          onDoubleClick={handleDoubleClick}
        >
          <span className="graph-group-icon">📁</span>
          {isEditing ? (
            <input
              ref={inputRef}
              type="text"
              className="graph-group-label-input"
              value={editedLabel}
              onChange={handleLabelChange}
              onBlur={handleLabelBlur}
              onKeyDown={handleLabelKeyDown}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <span className="graph-group-label">{data.label || 'Group'}</span>
          )}
        </div>
        {data.description && (
          <div className="graph-group-description">{data.description}</div>
        )}
      </div>

      {contextMenu && createPortal(
        <div
          className="graph-group-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <div className="context-menu-title">Group Color</div>
          <div className="context-menu-colors">
            {colors.map((color) => (
              <button
                key={color}
                className="color-button"
                style={{ backgroundColor: color }}
                onClick={() => handleChangeColor(color)}
              />
            ))}
          </div>
          <button className="context-menu-delete" onClick={handleDeleteGroup}>
            🗑️ Delete Group
          </button>
        </div>,
        document.body
      )}
    </>
  );
}

export default memo(GroupNode);
