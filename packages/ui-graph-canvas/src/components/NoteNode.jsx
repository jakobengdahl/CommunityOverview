import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import {
  DEFAULT_NOTE_FONT_SIZE,
  rotationStyle,
  isRemoteLocked,
  isAnnotationDraggable,
  resolveRotatedResizeGeometry,
} from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
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
  const [isEditing, setIsEditing] = useState(false);
  const [text, setText] = useState(data.text || '');
  const [contextMenu, setContextMenu] = useState(null);
  const inputRef = useRef(null);
  const contextMenuRef = useRef(null);
  // Snapshot of {x, y, width, height} at the start of the current resize
  // gesture, read by handleResizeEnd to map the gesture's net delta back
  // through this note's rotation (resolveRotatedResizeGeometry).
  const resizeStartRef = useRef(null);
  const { setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt, labels } = useContext(AnnotationContext);
  // Another client's live selection claim makes this note's lease exclusive
  // (task-annotation-shared-session-realtime): every mutation below refuses
  // to run while it is held, surfacing the attempt instead of silently
  // dropping it or letting two clients race.
  const remoteLocked = isRemoteLocked(data);
  const changeLayer = useAnnotationLayer(id, data);
  const locked = Boolean(data?.locked);

  useEffect(() => {
    setText(data.text || '');
  }, [data.text]);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

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

  const commitText = () => {
    setIsEditing(false);
    if (remoteLocked) {
      setText(data.text || '');
      notifyRemoteLockedAttempt();
      return;
    }
    const trimmed = text.trim();
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, text: trimmed } } : n))
    );
    notifyChange('text');
  };

  // Live per-keystroke sync (docs/ANNOTATION_CONTRACT.md's "300 ms text
  // debounce": in-progress edits coalesce and publish at most every 300 ms
  // while typing, not only on blur). Pushes the raw, untrimmed value on
  // every change — `commitText` still trims on blur/close, the authoritative
  // final write. The host's scheduler (annotationChangeScheduler.js)
  // debounces the 'text' kind, so a burst of keystrokes coalesces into one
  // publish regardless of how often this fires.
  const handleTextChange = (e) => {
    const next = e.target.value;
    setText(next);
    if (remoteLocked) return;
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, text: next } } : n))
    );
    notifyChange('text');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setText(data.text || '');
      setIsEditing(false);
    }
  };

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

  // The only action a locked note's context menu offers (besides the
  // capability baseline's "copy", which has no GUI action yet at all) —
  // everything else (colour/size/rotation/delete) stays out of reach while
  // `locked` is set, matching resize/drag already refusing it.
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

  const handleResizeStart = (event, params) => {
    resizeStartRef.current = params;
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
  };

  const color = data.color || NOTE_COLORS[0];
  const fontSize = data.fontSize || DEFAULT_NOTE_FONT_SIZE;

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
            outline: remoteLocked ? `2px solid ${data.remoteSelection.color}` : undefined,
            outlineOffset: remoteLocked ? '2px' : undefined,
          }}
          onDoubleClick={(e) => {
            e.stopPropagation();
            if (remoteLocked) {
              notifyRemoteLockedAttempt();
              return;
            }
            setIsEditing(true);
          }}
          onContextMenu={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setContextMenu({ x: e.clientX, y: e.clientY });
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
      </div>

      {contextMenu &&
        createPortal(
          <div
            ref={contextMenuRef}
            className="graph-annotation-context-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
          >
            {locked ? (
              // The only action a locked note's context menu offers (besides
              // the capability baseline's "copy", which has no GUI action
              // yet at all) — everything else stays out of reach while
              // `locked` is set, matching resize/drag already refusing it.
              <button type="button" className="context-menu-unlock" onClick={unlock}>
                🔓 {labels.unlock}
              </button>
            ) : (
              <>
                <div className="context-menu-title">{labels.color}</div>
                <div className="context-menu-colors">
                  {NOTE_COLORS.map((c) => (
                    <button
                      key={c}
                      className="color-button"
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
                <AnnotationLayerControls
                  labels={labels}
                  locked={data.locked}
                  onChangeLayer={changeLayer}
                />
                <button className="context-menu-delete" onClick={remove}>
                  🗑️ {labels.delete}
                </button>
              </>
            )}
          </div>,
          document.body
        )}
    </>
  );
}

export default memo(NoteNode);
