import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { DEFAULT_LABEL_FONT_SIZE, rotationStyle } from '../utils/annotations';
import './LabelNode.css';

/**
 * LabelNode - A free-floating text label annotation (no container/box).
 *
 * Double-click to edit, right-click for colour, text size and delete. Stored in
 * the session's annotation list (kind: "label").
 */
const LABEL_COLORS = ['#e6edf3', '#FDE047', '#4ADE80', '#60A5FA', '#F472B6', '#FB923C'];
const LABEL_FONT_SIZES = [14, 16, 20, 28];

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
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, color } } : n)));
    setContextMenu(null);
    notifyChange();
  };

  const changeFontSize = (fontSize) => {
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, fontSize } } : n)));
    notifyChange();
  };

  const changeRotation = (deg) => {
    const next = ((deg % 360) + 360) % 360;
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, rotation: next } } : n))
    );
    notifyChange();
  };

  const remove = () => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange();
  };

  const color = data.color || LABEL_COLORS[0];
  const fontSize = data.fontSize || DEFAULT_LABEL_FONT_SIZE;

  return (
    <>
      <div
        className={`graph-label-node${selected ? ' selected' : ''}`}
        style={{ color, fontSize, ...rotationStyle('label', data.rotation) }}
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
            style={{ fontSize }}
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

      {contextMenu &&
        createPortal(
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
            <div className="context-menu-title">{labels.textSize}</div>
            <div className="context-menu-sizes">
              {LABEL_FONT_SIZES.map((s) => (
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
