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
  const { notifyChange, notifyRemoteLockedAttempt, labels } = useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live claim makes
  // this group's lease exclusive (task-annotation-shared-session-realtime).
  const remoteLocked = isRemoteLocked(data);
  // The persisted flag, distinct from the remote claim above. It only started
  // reaching this component when the group translators began carrying it;
  // before that a group locked over MCP rendered its full menu.
  const locked = Boolean(data?.locked);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // A lock can arrive from MCP or a collaborator while the rename input is
  // open. Close it the moment that happens rather than letting the user keep
  // typing into a field whose commit will refuse the text: the blur guard
  // below is correct but silent, and silence there reads as the edit having
  // been accepted. Adjusted during render rather than in an effect — React's
  // documented pattern for state that must follow a prop, and the one that
  // does not cost a second render pass.
  const [lockedWhenLastRendered, setLockedWhenLastRendered] = useState(locked);
  if (locked !== lockedWhenLastRendered) {
    setLockedWhenLastRendered(locked);
    if (locked && isEditing) {
      setIsEditing(false);
      setEditedLabel(data.label || 'Group');
    }
  }

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
    // A rename is an edit, so the persisted lock refuses it as the menu does.
    // Silently, unlike the remote claim above: that one is somebody else
    // holding the object right now and worth surfacing, while this one is a
    // standing state the menu already explains with its Unlock button.
    if (locked) return;
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
    // Backstop for the lock arriving while the input is open. The render-phase
    // adjustment above closes the editor first, so no ordinary path reaches
    // this — it stays because this is the branch that would otherwise write,
    // and a guard on a write is cheap next to a rename nobody asked for.
    if (locked) {
      setEditedLabel(data.label || 'Group');
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

  const handleDeleteGroup = () => {
    removeGroupKeepChildren();
  };

  // The only action a locked group's context menu offers (see the menu below).
  // Locking is MCP-only; this is the sole GUI path back out of it.
  const unlock = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    // `draggable` is recomputed here, not just cleared on `data`: no local
    // path recomputes it, so leaving it false would unlock the menu while the
    // box stayed pinned until reload. (A remote upsert-group rebuilds the node
    // and the remote-selection effect recomputes the flag, but neither is
    // reachable from a local unlock.) ArrowNode's patchData recomputes it for
    // the same reason. `undefined`, not `true` — see the builders in
    // GraphCanvas for why an explicit boolean is the wrong value here.
    setNodes((nds) =>
      nds.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, locked: false }, draggable: undefined } : n
      )
    );
    setContextMenu(null);
    notifyChange('style');
  };

  const colors = ['#646cff', '#10B981', '#F97316', '#EF4444', '#A855F7', '#3B82F6'];

  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        onResizeEnd={() => notifyChange('geometry')}
        isVisible={selected && !remoteLocked && !locked}
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
            {locked ? (
              // A locked group offers Unlock and nothing else, the same as
              // every other kind. The unlocked menu has exactly two other
              // actions — recolour, which is an edit, and Delete Group, which
              // destroys the box — so there is nothing a lock could allow
              // through. Anything added to the unlocked menu later has to be
              // decided against this branch as well, not just dropped in.
              <button type="button" className="context-menu-unlock" onClick={unlock}>
                🔓 {labels.unlock}
              </button>
            ) : (
              <>
                <div className="context-menu-title">Group Color</div>
                <div className="context-menu-colors">
                  {colors.map((c) => (
                    <button
                      key={c}
                      className={`color-button${(data.color || '#646cff') === c ? ' active' : ''}`}
                      style={{ backgroundColor: c }}
                      onClick={() => handleChangeColor(c)}
                    />
                  ))}
                </div>
                <button className="context-menu-delete" onClick={handleDeleteGroup}>
                  🗑️ Delete Group
                </button>
              </>
            )}
          </div>,
          document.body
        )}
    </>
  );
}

export default memo(GroupNode);
