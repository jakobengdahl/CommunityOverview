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

const DEFAULT_GROUP_COLOR = '#646cff';
const DARK_GROUP_TEXT = '#1e293b';
const LIGHT_GROUP_TEXT = '#ffffff';
export const GROUP_TEXT_COLORS = [DARK_GROUP_TEXT, LIGHT_GROUP_TEXT, '#FDE047', '#1E3A8A'];

const RGB_FN = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/i;

const computedCssColors = new Map();

// A CSS colour painted onto a 1x1 canvas, as an rgba() string, or null
// without a 2D context. Covers what computed style keeps in its own notation
// (oklch(), lab(), color(), color-mix()): the canvas pixel is always sRGB.
function rasterizedCssColor(color) {
  const ctx = document.createElement('canvas').getContext?.('2d');
  if (!ctx) return null;
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${a / 255})`;
}

// Any other CSS colour (named, hsl(), oklch(), ...) as an rgb()/rgba()
// string, or null when it is not a colour or there is no DOM to ask.
function computedCssColor(color) {
  if (typeof document === 'undefined' || !document.body) return null;
  if (!computedCssColors.has(color)) {
    const probe = document.createElement('span');
    probe.style.color = color;
    let computed = null;
    if (probe.style.color) {
      // Computed style needs an attached element in browsers.
      document.body.appendChild(probe);
      computed = getComputedStyle(probe).color || null;
      probe.remove();
      if (!RGB_FN.test(computed || '')) computed = rasterizedCssColor(color);
    }
    computedCssColors.set(color, computed);
  }
  return computedCssColors.get(color);
}

// [r, g, b] in 0-255 composited over the white canvas, or null for a value
// that is not a CSS colour.
function parseFill(color) {
  const s = String(color || '').trim();
  let rgba = null;
  const hex = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(s);
  if (hex) {
    const d = hex[1].length <= 4 ? [...hex[1]].map((c) => c + c).join('') : hex[1];
    rgba = [0, 2, 4, 6].map((i) => (i < d.length ? parseInt(d.slice(i, i + 2), 16) : 255));
    rgba[3] /= 255;
  } else {
    const fn = RGB_FN.exec(s) || RGB_FN.exec(computedCssColor(s) || '');
    if (!fn) return null;
    const a = fn[4] === undefined ? 1 : Number(fn[4]) / (fn[5] ? 100 : 1);
    rgba = [Number(fn[1]), Number(fn[2]), Number(fn[3]), a];
  }
  const a = Math.min(Math.max(rgba[3], 0), 1);
  return rgba.slice(0, 3).map((c) => Math.min(c, 255) * a + 255 * (1 - a));
}

function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * The header label's text colour: the group's chosen `textColor`, else
 * whichever of dark/white contrasts more with the header fill (the label sits
 * on the fill, not the canvas). A fill that is not a colour draws nothing, so
 * the label sits on the light canvas and gets the dark text.
 */
export function resolveGroupTextColor(fill, textColor) {
  if (textColor) return textColor;
  const rgb = parseFill(fill || DEFAULT_GROUP_COLOR);
  if (rgb === null) return DARK_GROUP_TEXT;
  const l = relativeLuminance(rgb);
  // WCAG contrast against white (L=1) vs the dark text (L≈0.012).
  return 1.05 / (l + 0.05) >= (l + 0.05) / 0.062 ? LIGHT_GROUP_TEXT : DARK_GROUP_TEXT;
}

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

  // Shared by the fill and text-colour swatches; `textColor: ''` is "Auto".
  const patchStyle = (patch) => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === id) {
          return { ...n, data: { ...n.data, ...patch } };
        }
        return n;
      })
    );
    setContextMenu(null);
    notifyChange('style');
  };
  const handleChangeColor = (color) => patchStyle({ color });
  const handleChangeTextColor = (textColor) => patchStyle({ textColor });

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

  const colors = [DEFAULT_GROUP_COLOR, '#10B981', '#F97316', '#EF4444', '#A855F7', '#3B82F6'];
  const fill = data.color || DEFAULT_GROUP_COLOR;
  const textColor = resolveGroupTextColor(fill, data.textColor);
  // MCP may store any casing; the swatch constants mix cases.
  const activeTextColor = String(data.textColor || '').toLowerCase();
  const groupBadge = remoteEditBadge(data);
  // Groups persist rotation for round-trip compatibility, but the current group
  // renderer is not visually rotated. Keep that stored value out of the size
  // control, whose resize math intentionally compensates for rendered rotation.
  const sizeControlData = { ...data, rotation: 0 };

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
        lineStyle={{ stroke: fill, strokeWidth: 4 }}
        handleStyle={{
          width: 14,
          height: 14,
          background: fill,
          border: '2px solid white',
          borderRadius: '3px',
        }}
      />
      <div
        ref={groupRef}
        className="graph-group-node"
        style={{
          borderColor: fill,
          // The `15` alpha suffix only tints a 6-digit hex; on #rgba it would
          // spell an opaque #rrggbb under a label judged against the canvas.
          backgroundColor: /^#[0-9a-f]{6}$/i.test(fill) ? `${fill}15` : undefined,
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
          style={{ backgroundColor: fill, color: textColor }}
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
              // every other kind. The unlocked menu has exactly five other
              // actions — recolour, text colour, the group-order row, the
              // non-drag size control, all edits, and Delete Group, which destroys the
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
                      className={`color-button${fill === c ? ' active' : ''}`}
                      style={{ backgroundColor: c }}
                      onClick={() => handleChangeColor(c)}
                    />
                  ))}
                </div>
                <div className="context-menu-title">{labels.groupTextColor}</div>
                <div className="context-menu-colors">
                  {/* "Auto" previews the contrast-derived colour on the fill. */}
                  <button
                    type="button"
                    className={`color-button text-color-button${data.textColor ? '' : ' active'}`}
                    style={{ backgroundColor: fill, color: resolveGroupTextColor(fill) }}
                    aria-label={labels.groupTextColorAuto}
                    title={labels.groupTextColorAuto}
                    onClick={() => handleChangeTextColor('')}
                  >
                    A
                  </button>
                  {GROUP_TEXT_COLORS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className={`color-button text-color-button${activeTextColor === c.toLowerCase() ? ' active' : ''}`}
                      style={{ backgroundColor: c }}
                      title={c}
                      onClick={() => handleChangeTextColor(c)}
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
                <AnnotationSizeControl id={id} data={sizeControlData} labels={labels} />
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
