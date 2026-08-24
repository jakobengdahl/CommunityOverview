import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked } from '../utils/annotations';
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
  const contextMenuRef = useRef(null);
  const { setNodes } = useReactFlow();
  // Groups are annotations (design 3.1); reuse the annotation change notifier so
  // a rename, recolour, resize or delete schedules a session save (and, in a
  // shared session, an op) the same way note/label/arrow edits do.
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live claim makes
  // this group's lease exclusive (task-annotation-shared-session-realtime).
  const remoteLocked = isRemoteLocked(data);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  useEffect(() => {
    if (!contextMenu) return;

    const handleDismiss = (e) => {
      // Don't close if clicking inside the context menu itself (portal)
      if (contextMenuRef.current && contextMenuRef.current.contains(e.target)) return;
      setContextMenu(null);
    };

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') setContextMenu(null);
    };

    // Use setTimeout so the opening right-click doesn't immediately close the menu.
    // Listen in capture phase so the event is caught before any child element
    // can call stopPropagation (e.g. ReactFlow pane overlays).
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleDismiss, true);
      document.addEventListener('contextmenu', handleDismiss, true);
      // focusin covers keyboard-only navigation (Tab into search/chat inputs),
      // which never produces a mousedown.
      document.addEventListener('focusin', handleDismiss, true);
      document.addEventListener('keydown', handleKeyDown, true);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('mousedown', handleDismiss, true);
      document.removeEventListener('contextmenu', handleDismiss, true);
      document.removeEventListener('focusin', handleDismiss, true);
      document.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [contextMenu]);

  const handleDoubleClick = (e) => {
    e.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setIsEditing(true);
  };

  const handleLabelChange = (e) => {
    setEditedLabel(e.target.value);
  };

  const handleLabelBlur = () => {
    setIsEditing(false);
    if (remoteLocked) {
      setEditedLabel(data.label || 'Group');
      notifyRemoteLockedAttempt();
      return;
    }
    if (editedLabel.trim() && editedLabel.trim() !== data.label) {
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id === id) {
            return { ...n, data: { ...n.data, label: editedLabel.trim() } };
          }
          return n;
        })
      );
      notifyChange('text');
    } else if (!editedLabel.trim()) {
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
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const handleChangeColor = (color) => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === id) {
          return { ...n, data: { ...n.data, color } };
        }
        return n;
      })
    );
    setContextMenu(null);
    notifyChange('style');
  };

  // Un-parent children and remove the group node from the canvas
  const removeGroupKeepChildren = () => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => {
      const children = nds.filter((n) => n.parentId === id);
      const groupNode = nds.find((n) => n.id === id);
      const updatedChildren = children.map((child) => ({
        ...child,
        parentId: undefined,
        extent: undefined,
        position: {
          x: child.position.x + (groupNode?.position.x || 0),
          y: child.position.y + (groupNode?.position.y || 0),
        },
      }));

      return nds
        .filter((n) => n.id !== id)
        .map((n) => {
          const updated = updatedChildren.find((c) => c.id === n.id);
          return updated || n;
        });
    });
    notifyChange('delete');
  };

  const handleHideGroup = () => {
    removeGroupKeepChildren();
    setContextMenu(null);
  };

  const handleDeleteGroup = () => {
    removeGroupKeepChildren();
  };

  const colors = ['#646cff', '#10B981', '#F97316', '#EF4444', '#A855F7', '#3B82F6'];

  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        onResizeEnd={() => notifyChange('geometry')}
        isVisible={selected && !remoteLocked}
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
          backgroundColor: `${data.color || '#646cff'}15`,
          outline: remoteLocked ? `2px solid ${data.remoteSelection.color}` : undefined,
          outlineOffset: remoteLocked ? '2px' : undefined,
        }}
      >
        {remoteLocked && (
          <div
            className="graph-node-remote-badge"
            style={{ backgroundColor: data.remoteSelection.color }}
            title={data.remoteSelection.displayName}
          >
            {data.remoteSelection.displayName}
          </div>
        )}
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
              className="graph-group-label-input nodrag"
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
        {data.description && <div className="graph-group-description">{data.description}</div>}
      </div>

      {contextMenu &&
        createPortal(
          <div
            ref={contextMenuRef}
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
            <button className="context-menu-action" onClick={handleHideGroup}>
              👁️ Hide Group
            </button>
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
