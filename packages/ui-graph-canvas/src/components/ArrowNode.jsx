import { memo, useState, useRef, useEffect, useContext } from 'react';
import { createPortal } from 'react-dom';
import { AnnotationContext } from './AnnotationContext';
import { useReactFlow } from 'reactflow';
import './ArrowNode.css';

/**
 * ArrowNode - A free-floating arrow annotation pointing from its origin
 * (the node position) to an offset (dx, dy). Right-click for colour and delete.
 * Stored in the session's annotation list (kind: "arrow"). Endpoint re-anchoring
 * to nodes/annotations is a later refinement; v1 arrows are free-floating and
 * moved as a whole (design step 5, D12).
 */
const ARROW_COLORS = ['#e6edf3', '#FDE047', '#4ADE80', '#60A5FA', '#F472B6', '#FB923C'];
const PAD = 8;

function ArrowNode({ id, data, selected }) {
  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  const { setNodes } = useReactFlow();
  const { notifyChange, labels } = useContext(AnnotationContext);

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

  const dx = Number(data.dx ?? 160);
  const dy = Number(data.dy ?? 0);
  const color = data.color || ARROW_COLORS[0];

  const boxW = Math.abs(dx) + PAD * 2;
  const boxH = Math.abs(dy) + PAD * 2;
  // Place the arrow origin at the node position by shifting the box up/left.
  const originX = PAD + (dx < 0 ? Math.abs(dx) : 0);
  const originY = PAD + (dy < 0 ? Math.abs(dy) : 0);
  const endX = originX + dx;
  const endY = originY + dy;

  return (
    <>
      <div
        className={`graph-arrow-node${selected ? ' selected' : ''}`}
        style={{ marginLeft: -originX, marginTop: -originY }}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setContextMenu({ x: e.clientX, y: e.clientY });
        }}
      >
        <svg width={boxW} height={boxH} style={{ overflow: 'visible', display: 'block' }}>
          <defs>
            <marker
              id={`graph-arrow-head-${id}`}
              markerWidth="12"
              markerHeight="12"
              refX="9"
              refY="4"
              orient="auto"
              markerUnits="userSpaceOnUse"
            >
              <path d="M0,0 L10,4 L0,8 Z" fill={color} />
            </marker>
          </defs>
          {/* Transparent wide hit target so the thin arrow is easy to grab. */}
          <line x1={originX} y1={originY} x2={endX} y2={endY} stroke="transparent" strokeWidth={16} />
          <line
            x1={originX}
            y1={originY}
            x2={endX}
            y2={endY}
            stroke={color}
            strokeWidth={2.5}
            markerEnd={`url(#graph-arrow-head-${id})`}
          />
        </svg>
      </div>

      {contextMenu && createPortal(
        <div
          ref={contextMenuRef}
          className="graph-annotation-context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <div className="context-menu-title">{labels.color}</div>
          <div className="context-menu-colors">
            {ARROW_COLORS.map((c) => (
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

export default memo(ArrowNode);
