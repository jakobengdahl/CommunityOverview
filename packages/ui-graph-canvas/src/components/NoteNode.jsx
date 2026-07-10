import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { DEFAULT_NOTE_FONT_SIZE } from '../utils/annotations';
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

function NoteNode({ id, data, selected }) {
  const [isEditing, setIsEditing] = useState(false);
  const [text, setText] = useState(data.text || '');
  const [contextMenu, setContextMenu] = useState(null);
  const inputRef = useRef(null);
  const contextMenuRef = useRef(null);
  const { setNodes } = useReactFlow();
  const { notifyChange, labels } = useContext(AnnotationContext);

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
    const trimmed = text.trim();
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, text: trimmed } } : n))
    );
    notifyChange();
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setText(data.text || '');
      setIsEditing(false);
    }
  };

  const changeColor = (color) => {
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, color } } : n))
    );
    setContextMenu(null);
    notifyChange();
  };

  const changeFontSize = (fontSize) => {
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, fontSize } } : n))
    );
    notifyChange();
  };

  const remove = () => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange();
  };

  const color = data.color || NOTE_COLORS[0];
  const fontSize = data.fontSize || DEFAULT_NOTE_FONT_SIZE;

  return (
    <>
      <NodeResizer
        minWidth={120}
        minHeight={80}
        isVisible={selected}
        lineStyle={{ stroke: color, strokeWidth: 2 }}
        handleStyle={{ width: 10, height: 10, background: color, border: '2px solid white' }}
        onResizeEnd={notifyChange}
      />
      <div
        className="graph-note-node"
        style={{ backgroundColor: color }}
        onDoubleClick={(e) => {
          e.stopPropagation();
          setIsEditing(true);
        }}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setContextMenu({ x: e.clientX, y: e.clientY });
        }}
      >
        {isEditing ? (
          <textarea
            ref={inputRef}
            className="graph-note-input nodrag"
            style={{ fontSize }}
            value={text}
            onChange={(e) => setText(e.target.value)}
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

      {contextMenu && createPortal(
        <div
          ref={contextMenuRef}
          className="graph-annotation-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
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
          <button className="context-menu-delete" onClick={remove}>
            🗑️ {labels.delete}
          </button>
        </div>,
        document.body
      )}
    </>
  );
}

export default memo(NoteNode);
