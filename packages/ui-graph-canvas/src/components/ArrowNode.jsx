import { memo, useState, useRef, useEffect, useContext, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { AnnotationContext } from './AnnotationContext';
import { useReactFlow } from 'reactflow';
import { findSnapTarget, isArrowAnchored } from '../utils/annotations';
import './ArrowNode.css';

/**
 * ArrowNode - A free-floating connector annotation drawn from its origin
 * (the node position) to an offset (dx, dy). Each end is independently either
 * an arrowhead (startArrow/endArrow) or a bare line end, so the connector can be
 * a plain line, a single arrow (default: head at the end) or a double arrow.
 *
 * When selected, both endpoints expose drag handles. Dragging an endpoint near a
 * node/annotation centre snaps onto it (magnetic) and records an anchor
 * (startAnchor/endAnchor) so the endpoint follows that target as it moves.
 * Arrows never anchor to other arrows. Stored in the session's annotation list
 * (kind: "arrow").
 */
const ARROW_COLORS = ['#e6edf3', '#FDE047', '#4ADE80', '#60A5FA', '#F472B6', '#FB923C'];
const PAD = 12;

function ArrowNode({ id, data, selected }) {
  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  const draggingRef = useRef(null);
  const { setNodes, screenToFlowPosition, getNodes } = useReactFlow();
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

  const patchData = (patch) => {
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== id) return n;
        const nextData = { ...n.data, ...patch };
        return { ...n, data: nextData, draggable: !isArrowAnchored(nextData) };
      })
    );
  };

  const changeColor = (color) => {
    patchData({ color });
    setContextMenu(null);
    notifyChange();
  };

  const toggleHead = (end) => {
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, [end]: !n.data[end] } } : n))
    );
    notifyChange();
  };

  const remove = () => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange();
  };

  // Move one endpoint to a new flow-coordinate point, snapping onto a nearby
  // node/annotation centre when close and recording/clearing its anchor.
  const moveEndpoint = useCallback((endpoint, flowPoint) => {
    const targetId = findSnapTarget(flowPoint, getNodes(), { excludeId: id });
    let snapPoint = flowPoint;
    if (targetId) {
      const target = getNodes().find((n) => n.id === targetId);
      const pos = target?.positionAbsolute || target?.position;
      if (pos) {
        const w = target.width || target.style?.width || 0;
        const h = target.height || target.style?.height || 0;
        snapPoint = { x: pos.x + w / 2, y: pos.y + h / 2 };
      }
    }
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== id) return n;
        const dx = Number(n.data.dx ?? 160);
        const dy = Number(n.data.dy ?? 0);
        let position = n.position;
        let nextData = { ...n.data };
        if (endpoint === 'start') {
          const end = { x: n.position.x + dx, y: n.position.y + dy };
          position = { x: snapPoint.x, y: snapPoint.y };
          nextData.dx = end.x - snapPoint.x;
          nextData.dy = end.y - snapPoint.y;
          nextData.startAnchor = targetId || undefined;
        } else {
          nextData.dx = snapPoint.x - n.position.x;
          nextData.dy = snapPoint.y - n.position.y;
          nextData.endAnchor = targetId || undefined;
        }
        return { ...n, position, data: nextData, draggable: !isArrowAnchored(nextData) };
      })
    );
  }, [getNodes, id, setNodes]);

  // Teardown for an in-flight endpoint drag; kept in a ref so it can also run on
  // unmount (arrow deleted/deselected mid-drag) without leaking window listeners.
  const dragTeardownRef = useRef(null);
  useEffect(() => () => dragTeardownRef.current?.(), []);

  const startEndpointDrag = (endpoint) => (e) => {
    e.stopPropagation();
    e.preventDefault();
    draggingRef.current = endpoint;
    const handleMove = (ev) => {
      const flowPoint = screenToFlowPosition({ x: ev.clientX, y: ev.clientY });
      moveEndpoint(endpoint, flowPoint);
    };
    const teardown = () => {
      draggingRef.current = null;
      dragTeardownRef.current = null;
      window.removeEventListener('pointermove', handleMove, true);
      window.removeEventListener('pointerup', handleUp, true);
      window.removeEventListener('pointercancel', handleUp, true);
    };
    const handleUp = () => {
      teardown();
      notifyChange();
    };
    dragTeardownRef.current = teardown;
    window.addEventListener('pointermove', handleMove, true);
    window.addEventListener('pointerup', handleUp, true);
    window.addEventListener('pointercancel', handleUp, true);
  };

  const dx = Number(data.dx ?? 160);
  const dy = Number(data.dy ?? 0);
  const color = data.color || ARROW_COLORS[0];
  const startArrow = Boolean(data.startArrow);
  const endArrow = data.endArrow ?? true;

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
            <marker
              id={`graph-arrow-tail-${id}`}
              markerWidth="12"
              markerHeight="12"
              refX="1"
              refY="4"
              orient="auto"
              markerUnits="userSpaceOnUse"
            >
              <path d="M10,0 L0,4 L10,8 Z" fill={color} />
            </marker>
          </defs>
          {/* Transparent wide hit target so the thin line is easy to grab. */}
          <line x1={originX} y1={originY} x2={endX} y2={endY} stroke="transparent" strokeWidth={16} />
          <line
            x1={originX}
            y1={originY}
            x2={endX}
            y2={endY}
            stroke={color}
            strokeWidth={2.5}
            markerStart={startArrow ? `url(#graph-arrow-tail-${id})` : undefined}
            markerEnd={endArrow ? `url(#graph-arrow-head-${id})` : undefined}
          />
          {selected && (
            <>
              <circle
                className="graph-arrow-handle nodrag"
                cx={originX}
                cy={originY}
                r={6}
                fill={data.startAnchor ? '#4ADE80' : '#fff'}
                stroke={color}
                strokeWidth={2}
                onPointerDown={startEndpointDrag('start')}
                onContextMenu={(e) => e.stopPropagation()}
              />
              <circle
                className="graph-arrow-handle nodrag"
                cx={endX}
                cy={endY}
                r={6}
                fill={data.endAnchor ? '#4ADE80' : '#fff'}
                stroke={color}
                strokeWidth={2}
                onPointerDown={startEndpointDrag('end')}
                onContextMenu={(e) => e.stopPropagation()}
              />
            </>
          )}
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
          <button className="context-menu-toggle" onClick={() => toggleHead('startArrow')}>
            <span>{labels.arrowStartHead}</span>
            <span>{startArrow ? '✔' : ''}</span>
          </button>
          <button className="context-menu-toggle" onClick={() => toggleHead('endArrow')}>
            <span>{labels.arrowEndHead}</span>
            <span>{endArrow ? '✔' : ''}</span>
          </button>
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
