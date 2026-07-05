import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import './LabelNode.css';

/**
 * LabelNode - A free-floating text label annotation (no container/box).
 *
 * Double-click to edit, right-click for colour and delete. Stored in the
 * session's annotation list (kind: "label").
 */
const LABEL_COLORS = ['#e6edf3', '#FDE047', '#4ADE80', '#60A5FA', '#F472B6', '#FB923C'];

function LabelNode({ id, data, selected }) {
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
    if (e.key === 'Enter') {
      e.preventDefault();
      commitText();
    } else if (e.key === 'Escape') {
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

  const remove = () => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange();
  };

  const color = data.color || LABEL_COLORS[0];

  return (
    <>
      <div
        className={`graph-label-node${selected ? ' selected' : ''}`}
        style={{ color }}
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
          <input
            ref={inputRef}
            type="text"
            className="graph-label-input nodrag"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={commitText}
            onKeyDown={handleKeyDown}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <span className={data.text ? '' : 'graph-label-placeholder'}>
            {data.text || labels.labelPlaceholder}
          </span>
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
            {LABEL_COLORS.map((c) => (
              <button
                key={c}
                className="color-button"
                style={{ backgroundColor: c }}
                onClick={() => changeColor(c)}
              />
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

export default memo(LabelNode);
