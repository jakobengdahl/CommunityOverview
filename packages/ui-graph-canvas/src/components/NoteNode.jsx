import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import {
  DEFAULT_NOTE_FONT_SIZE,
  rotationStyle,
  isRemoteLocked,
  isAnnotationDraggable,
  remoteEditBadge,
  resolveRotatedResizeGeometry,
} from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
import AnnotationDuplicateControl, { useAnnotationDuplicate } from './AnnotationDuplicateControl';
import AnnotationOpacityControl, { useAnnotationOpacity } from './AnnotationOpacityControl';
import { NearbyObjectMenuSection } from './ContextMenus';
import { useEditableText } from '../hooks/useEditableText';
import { useAnnotationEditLease } from '../hooks/useAnnotationEditLease';
import { useAnnotationEditTrigger } from '../hooks/useAnnotationEditTrigger';
import './NoteNode.css';

/**
 * NoteNode - A free-floating sticky note annotation.
 *
 * Double-click to edit the text, right-click for colour, text size and delete.
 * The note is stored in the session's annotation list (kind: "note"); its
 * content lives with the session, never in the knowledge graph.
 */
const NOTE_COLORS = ['#FEF08A', '#FDBA74', '#86EFAC', '#93C5FD', '#F9A8D4', '#E9D5FF'];
const NOTE_FONT_SIZES = [12, 14, 18, 24];

// `data` is defaulted rather than assumed: a stored annotation missing its
// payload entirely should render as an empty note, not take the canvas down.
// AnnotationErrorBoundary would catch it, but a note with no text is a thing
// this component can draw perfectly well — falling back to the placeholder
// there would lose an annotation the user could otherwise still edit.
function NoteNode({ id, data = {}, selected }) {
  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  // Snapshot of {x, y, width, height} at the start of the current resize
  // gesture, read by handleResizeEnd to map the gesture's net delta back
  // through this note's rotation (resolveRotatedResizeGeometry).
  const resizeStartRef = useRef(null);
  const { setNodes } = useReactFlow();
  const {
    notifyChange,
    notifyRemoteLockedAttempt,
    labels,
    attachNearby,
    beginEditing,
    endEditing,
  } = useContext(AnnotationContext);
  // Another client's live edit lease (task-annotation-exclusive-edit-leases,
  // acquired only when real editing starts — never on mere selection):
  // every mutation below refuses to run while it is held, surfacing the
  // attempt instead of silently dropping it or letting two clients race.
  const remoteLocked = isRemoteLocked(data);
  const changeLayer = useAnnotationLayer(id, data);
  const duplicate = useAnnotationDuplicate(id, data);
  const changeOpacity = useAnnotationOpacity(id, data);
  const locked = Boolean(data?.locked);
  const { isEditing, text, inputRef, startEditing, commitText, handleTextChange, handleKeyDown } =
    useEditableText(id, data);
  useAnnotationEditLease(id, Boolean(contextMenu));
  const { editButtonRef, openEditMenu, sheetContainer } = useAnnotationEditTrigger({
    contextMenu,
    setContextMenu,
    menuRef: contextMenuRef,
  });

  useEffect(() => {
    if (!contextMenu) return;
    const handleDismiss = (e) => {
      if (contextMenuRef.current && contextMenuRef.current.contains(e.target)) return;
      setContextMenu(null);
    };
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') setContextMenu(null);
    };
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleDismiss, true);
      document.addEventListener('contextmenu', handleDismiss, true);
      document.addEventListener('keydown', handleKeyDown, true);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('mousedown', handleDismiss, true);
      document.removeEventListener('contextmenu', handleDismiss, true);
      document.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [contextMenu]);

  const changeColor = (color) => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, color } } : n)));
    setContextMenu(null);
    notifyChange('style');
  };

  const changeFontSize = (fontSize) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, fontSize } } : n)));
    notifyChange('style');
  };

  const changeRotation = (deg) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    const next = ((deg % 360) + 360) % 360;
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, rotation: next } } : n))
    );
    notifyChange('geometry');
  };

  const remove = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange('delete');
  };

  // Locking withholds everything except the two actions the capability
  // baseline names for a locked object: unlock, and duplicate (rendered via
  // AnnotationDuplicateControl below, in both the locked and unlocked
  // branches) — colour/size/rotation/delete stay out of reach while `locked`
  // is set, matching resize/drag already refusing it.
  const unlock = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== id) return n;
        const nextData = { ...n.data, locked: false };
        return { ...n, data: nextData, draggable: isAnnotationDraggable({ ...n, data: nextData }) };
      })
    );
    setContextMenu(null);
    notifyChange('style');
  };

  // Resize is a geometry gesture (task-annotation-exclusive-edit-leases):
  // acquires the lease at the start (fire-and-forget — NodeResizer already
  // started the visual drag by the time this fires, so there is nothing to
  // block on; a lost race is the server-side write's problem, same as a
  // plain drag) and releases it when the gesture ends.
  const handleResizeStart = (event, params) => {
    resizeStartRef.current = params;
    beginEditing?.([id]);
  };

  // NodeResizer computes `params` as if this note's box were axis-aligned
  // (see resolveRotatedResizeGeometry's comment for why that is wrong once
  // the box is visually rotated); remap the gesture's net delta through the
  // rotation before it lands on the node.
  const handleResizeEnd = (event, params) => {
    if (params && resizeStartRef.current) {
      const geometry = resolveRotatedResizeGeometry({
        start: resizeStartRef.current,
        end: params,
        rotation: data?.rotation ?? 0,
      });
      setNodes((nds) =>
        nds.map((n) =>
          n.id === id
            ? {
                ...n,
                position: { x: geometry.x, y: geometry.y },
                style: { ...n.style, width: geometry.width, height: geometry.height },
              }
            : n
        )
      );
    }
    resizeStartRef.current = null;
    notifyChange('geometry');
    endEditing?.([id]);
  };

  const color = data.color || NOTE_COLORS[0];
  const fontSize = data.fontSize || DEFAULT_NOTE_FONT_SIZE;
  const opacity = Number.isFinite(data.opacity) ? data.opacity : 1;
  // Cosmetic "who's here" marker, independent of the edit-lease check above:
  // prefers the active editor when there is one, else whoever merely has
  // this selected (task-annotation-exclusive-edit-leases — selection alone
  // never gates anything, but it is still worth showing).
  const badge = remoteEditBadge(data);

  return (
    <>
      {/* Carries the rotation for both the resizer and the note body, so the
          handles rotate with the note instead of staying axis-aligned around
          its unrotated bounds (the ReactFlow node wrapper itself
          deliberately stays unrotated, for hit-testing/drag). */}
      <div
        className="graph-annotation-rotate-wrap"
        style={{
          width: '100%',
          height: '100%',
          position: 'relative',
          ...rotationStyle('note', data.rotation),
        }}
      >
        <NodeResizer
          minWidth={120}
          minHeight={80}
          isVisible={selected && !locked}
          lineStyle={{ stroke: color, strokeWidth: 2 }}
          handleStyle={{ width: 10, height: 10, background: color, border: '2px solid white' }}
          onResizeStart={handleResizeStart}
          onResizeEnd={handleResizeEnd}
        />
        <div
          className="graph-note-node"
          style={{
            backgroundColor: color,
            opacity,
            outline: badge ? `2px solid ${badge.color}` : undefined,
            outlineOffset: badge ? '2px' : undefined,
          }}
          onDoubleClick={startEditing}
          onContextMenu={(e) => {
            e.preventDefault();
            e.stopPropagation();
            if (remoteLocked) {
              notifyRemoteLockedAttempt();
              return;
            }
            setContextMenu({ x: e.clientX, y: e.clientY });
          }}
        >
          {badge && (
            <div
              className="graph-node-remote-badge"
              style={{ backgroundColor: badge.color }}
              title={badge.displayName}
            >
              {badge.displayName}
            </div>
          )}
          {isEditing ? (
            <textarea
              ref={inputRef}
              className="graph-note-input nodrag"
              style={{ fontSize }}
              value={text}
              onChange={handleTextChange}
              onBlur={commitText}
              onKeyDown={handleKeyDown}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <div
              className={`graph-note-text${data.text ? '' : ' graph-note-placeholder'}`}
              style={{ fontSize }}
            >
              {data.text || labels.notePlaceholder}
            </div>
          )}
        </div>
        {/* The contextual "Edit" surface's visible entry point
            (task-annotation-responsive-bottom-toolbox) — a real, focusable
            button reachable by tap/click/Enter/Space, not only right-click or
            a long-press. Shown only on a selected note, matching the
            resizer's own `isVisible` condition just above. */}
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
      </div>

      {contextMenu &&
        (contextMenu.sheet ? sheetContainer : document.body) &&
        createPortal(
          <div
            ref={contextMenuRef}
            className={`graph-annotation-context-menu${contextMenu.sheet ? ' sheet' : ''}`}
            style={contextMenu.sheet ? undefined : { left: contextMenu.x, top: contextMenu.y }}
          >
            {locked ? (
              // The capability baseline's two actions for a locked object:
              // unlock, and duplicate (a duplicate never mutates the locked
              // source, so locking does not withhold it) — everything else
              // stays out of reach while `locked` is set, matching
              // resize/drag already refusing it.
              <>
                <button type="button" className="context-menu-unlock" onClick={unlock}>
                  🔓 {labels.unlock}
                </button>
                <AnnotationDuplicateControl labels={labels} onDuplicate={duplicate} />
              </>
            ) : (
              <>
                <div className="context-menu-title">{labels.color}</div>
                <div className="context-menu-colors">
                  {NOTE_COLORS.map((c) => (
                    <button
                      key={c}
                      className={`color-button${color === c ? ' active' : ''}`}
                      style={{ backgroundColor: c }}
                      onClick={() => changeColor(c)}
                    />
                  ))}
                </div>
                <div className="context-menu-title">{labels.textSize}</div>
                <div className="context-menu-sizes">
                  {NOTE_FONT_SIZES.map((s) => (
                    <button
                      key={s}
                      className={`size-button${fontSize === s ? ' active' : ''}`}
                      style={{ fontSize: Math.min(s, 18) }}
                      onClick={() => changeFontSize(s)}
                    >
                      A
                    </button>
                  ))}
                </div>
                <div className="context-menu-title">{labels.rotation}</div>
                <div className="context-menu-rotate">
                  <button
                    type="button"
                    className="rotate-button"
                    aria-label={labels.rotateLeft}
                    onClick={() => changeRotation((data.rotation ?? 0) - 15)}
                  >
                    ⟲
                  </button>
                  <button
                    type="button"
                    className="rotate-button rotate-reset"
                    aria-label={labels.rotateReset}
                    onClick={() => changeRotation(0)}
                  >
                    {Math.round(data.rotation ?? 0)}°
                  </button>
                  <button
                    type="button"
                    className="rotate-button"
                    aria-label={labels.rotateRight}
                    onClick={() => changeRotation((data.rotation ?? 0) + 15)}
                  >
                    ⟳
                  </button>
                </div>
                <AnnotationOpacityControl
                  labels={labels}
                  opacity={opacity}
                  onChangeOpacity={changeOpacity}
                />
                <AnnotationLayerControls
                  labels={labels}
                  locked={data.locked}
                  onChangeLayer={changeLayer}
                />
                <NearbyObjectMenuSection
                  labels={labels}
                  onAttach={(kind) => attachNearby(id, kind)}
                />
                <AnnotationDuplicateControl labels={labels} onDuplicate={duplicate} />
                <button className="context-menu-delete" onClick={remove}>
                  🗑️ {labels.delete}
                </button>
              </>
            )}
          </div>,
          contextMenu.sheet ? sheetContainer : document.body
        )}
    </>
  );
}

export default memo(NoteNode);
