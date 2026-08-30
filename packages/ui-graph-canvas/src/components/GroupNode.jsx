import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked, remoteEditBadge } from '../utils/annotations';
import AnnotationSizeControl from './AnnotationSizeControl';
import { useAnnotationMenuKeyNav } from './ContextMenus';
import { useAnnotationEditLease } from '../hooks/useAnnotationEditLease';
import { GROUP_LAYER_FRONT, GROUP_LAYER_BACK, resolveGroupOrderZ } from '../utils/groupLayers';
import { reorderNodesForParentChild } from './GraphCanvas';
import { useAnnotationEditTrigger } from '../hooks/useAnnotationEditTrigger';
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
  const { setNodes, getNodes } = useReactFlow();
  // Groups are annotations (design 3.1); reuse the annotation change notifier so
  // a rename, recolour, resize or delete schedules a session save (and, in a
  // shared session, an op) the same way note/label/arrow edits do.
  const { notifyChange, notifyRemoteLockedAttempt, labels, beginEditing, endEditing } =
    useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live edit lease
  // (task-annotation-exclusive-edit-leases) refuses every mutation below.
  const remoteLocked = isRemoteLocked(data);
  // The persisted flag, distinct from the remote lease above. It only started
  // reaching this component when the group translators began carrying it;
  // before that a group locked over MCP rendered its full menu.
  const locked = Boolean(data?.locked);
  useAnnotationEditLease(id, isEditing);
  useAnnotationEditLease(id, Boolean(contextMenu));
  // task-annotation-accessible-shared-controls: `group` was the one kind the
  // Edit-button/mobile-sheet work (task-annotation-responsive-bottom-toolbox)
  // named as out of its scope — see docs/ANNOTATION_CONTRACT.md's audit —
  // wired here the same way the other five kinds already are.
  const { editButtonRef, openEditMenu, sheetContainer } = useAnnotationEditTrigger({
    contextMenu,
    setContextMenu,
    menuRef: contextMenuRef,
  });
  const handleMenuKeyDown = useAnnotationMenuKeyNav(contextMenuRef);

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
    // Silently, unlike the remote lease above: that one is somebody else
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

  // Reorders this group among OTHER group backgrounds only — group-vs-group
  // is the only thing dec-annotation-group-background-layering leaves
  // editable; a group background's position behind every graph node and
  // every other annotation kind is structural (reorderNodesForParentChild's
  // own bucketing plus GroupNode.css's `.react-flow__node-group` z-index
  // pin — see utils/groupLayers.js's module docstring) and nothing this
  // handler does can move it out of that bucket. Never touches membership,
  // member position or member z — only this group node's own `data.z`.
  // Mirrors AnnotationLayerControls' useAnnotationLayer guard order exactly:
  // a live remote edit lease refuses and surfaces the attempt; the
  // persisted lock refuses silently (the menu already withholds this
  // section on a locked group below — this is the hook-level backstop the
  // same way useAnnotationLayer's own comment describes for the generic
  // row, so no future call site can reintroduce the hole by rendering the
  // buttons without their own locked branch).
  const handleChangeGroupLayer = (direction) => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    if (locked) return;
    const z = resolveGroupOrderZ(getNodes(), id, direction);
    if (z === null) {
      // Already alone at that end among groups (or the only group on the
      // canvas) — a no-op, not an error, matching resolveLayerZ's own
      // no-op contract.
      setContextMenu(null);
      return;
    }
    setNodes((nds) =>
      reorderNodesForParentChild(
        nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, z } } : n))
      )
    );
    setContextMenu(null);
    notifyChange('style');
    if (beginEditing) {
      beginEditing([id]).then(({ denied } = {}) => {
        if (denied?.[id]) notifyRemoteLockedAttempt();
        endEditing?.([id]);
      });
    }
  };

  const colors = ['#646cff', '#10B981', '#F97316', '#EF4444', '#A855F7', '#3B82F6'];
  const groupBadge = remoteEditBadge(data);

  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        onResizeStart={() => beginEditing?.([id])}
        onResizeEnd={() => {
          notifyChange('geometry');
          endEditing?.([id]);
        }}
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
          outline: groupBadge ? `2px solid ${groupBadge.color}` : undefined,
          outlineOffset: groupBadge ? '2px' : undefined,
        }}
      >
        {groupBadge && (
          <div
            className="graph-node-remote-badge"
            style={{ backgroundColor: groupBadge.color }}
            title={groupBadge.displayName}
          >
            {groupBadge.displayName}
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
      {/* See NoteNode's equivalent comment: a real, focusable button, shown
          only while selected — the keyboard/tap-reachable entry point this
          kind was missing (task-annotation-accessible-shared-controls). */}
      {selected && (
        <button
          ref={editButtonRef}
          type="button"
          className="annotation-edit-trigger nodrag nopan"
          aria-label={labels.editAnnotation}
          aria-haspopup="true"
          aria-expanded={Boolean(contextMenu)}
          onClick={(e) => {
            if (remoteLocked) {
              notifyRemoteLockedAttempt();
              return;
            }
            openEditMenu(e);
          }}
        >
          ✏️
        </button>
      )}

      {contextMenu &&
        (contextMenu.sheet ? sheetContainer : document.body) &&
        createPortal(
          <div
            ref={contextMenuRef}
            className={`graph-group-context-menu graph-annotation-context-menu${contextMenu.sheet ? ' sheet' : ''}`}
            style={contextMenu.sheet ? undefined : { left: contextMenu.x, top: contextMenu.y }}
            onKeyDown={handleMenuKeyDown}
          >
            {locked ? (
              // A locked group offers Unlock and nothing else, the same as
              // every other kind. The unlocked menu has exactly four other
              // actions — recolour, the group-order row, the non-drag size
              // control, all edits, and Delete Group, which destroys the
              // box — so there is nothing a lock could allow through.
              // Anything added to the unlocked menu later has to be decided
              // against this branch as well, not just dropped in.
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
                {/* Group backgrounds relative to each other only — see
                    handleChangeGroupLayer above. Always rendered, like the
                    generic AnnotationLayerControls row: a click is a silent
                    no-op when there is nothing to order this group against
                    (the only group on the canvas, or already at that end),
                    rather than the control disappearing depending on how
                    many other groups happen to exist. */}
                <div className="context-menu-title">{labels.groupLayer}</div>
                <div className="context-menu-layer">
                  <button
                    type="button"
                    className="layer-button"
                    aria-label={labels.groupLayerBack}
                    title={labels.groupLayerBack}
                    onClick={() => handleChangeGroupLayer(GROUP_LAYER_BACK)}
                  >
                    ⤓
                  </button>
                  <button
                    type="button"
                    className="layer-button"
                    aria-label={labels.groupLayerFront}
                    title={labels.groupLayerFront}
                    onClick={() => handleChangeGroupLayer(GROUP_LAYER_FRONT)}
                  >
                    ⤒
                  </button>
                </div>
                {/* Non-drag alternative to the NodeResizer handles above —
                    task-annotation-accessible-shared-controls. */}
                <AnnotationSizeControl id={id} data={data} labels={labels} />
                <button className="context-menu-delete" onClick={handleDeleteGroup}>
                  🗑️ Delete Group
                </button>
              </>
            )}
          </div>,
          contextMenu.sheet ? sheetContainer : document.body
        )}
    </>
  );
}

export default memo(GroupNode);
