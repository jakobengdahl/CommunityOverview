import { memo, useState, useRef, useEffect, useContext, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { AnnotationContext } from './AnnotationContext';
import { useReactFlow } from 'reactflow';
import { findSnapTarget, isArrowAnchored, isRemoteLocked, remoteEditBadge } from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
import AnnotationDuplicateControl, { useAnnotationDuplicate } from './AnnotationDuplicateControl';
import { useAnnotationEditLease } from '../hooks/useAnnotationEditLease';
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
// Black, because a connector has to be visible on the canvas the app actually
// renders. The previous default was the near-white '#e6edf3' this package's
// palettes were picked for when the canvas was dark; on the light canvas a
// line drawn without choosing a colour is invisible, so the tool reads as
// broken rather than as mis-coloured. Same value and same fix shape as
// FreehandAnnotationNode's DEFAULT_FREEHAND_COLOR — the other kind whose
// whole interaction is "draw and see a line". Not exported: unlike freehand,
// the canvas creates an arrow with `color: undefined` and leaves the fallback
// below as the single source, so there is no second copy to keep in step.
const DEFAULT_ARROW_COLOR = '#111827';

// DEFAULT_ARROW_COLOR leads so the picker can always return a line to the
// colour it was created with; '#e6edf3' stays available for a dark background.
const ARROW_COLORS = [
  DEFAULT_ARROW_COLOR,
  '#e6edf3',
  '#FDE047',
  '#4ADE80',
  '#60A5FA',
  '#F472B6',
  '#FB923C',
];
const PAD = 12;

function ArrowNode({ id, data, selected }) {
  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  const draggingRef = useRef(null);
  const { setNodes, screenToFlowPosition, getNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt, labels, beginEditing, endEditing } =
    useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live edit lease
  // (task-annotation-exclusive-edit-leases) refuses every mutation below.
  const remoteLocked = isRemoteLocked(data);
  const changeLayer = useAnnotationLayer(id, data);
  const duplicate = useAnnotationDuplicate(id, data);
  useAnnotationEditLease(id, Boolean(contextMenu));

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
        return { ...n, data: nextData, draggable: !nextData.locked && !isArrowAnchored(nextData) };
      })
    );
  };

  const changeColor = (color) => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    patchData({ color });
    setContextMenu(null);
    notifyChange('style');
  };

  const toggleHead = (end) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, [end]: !n.data[end] } } : n))
    );
    notifyChange('style');
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
  // baseline names for a locked object: unlock, and duplicate — matching
  // NoteNode/LabelNode/GenericAnnotationNode/FreehandAnnotationNode, which
  // have had this branch all along. Until this existed a locked line was the
  // one annotation kind whose menu still recoloured, toggled arrowheads and
  // deleted, and the one with no way to unlock from the GUI at all.
  const unlock = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    patchData({ locked: false });
    setContextMenu(null);
    notifyChange('style');
  };

  // Move one endpoint to a new flow-coordinate point, snapping onto a nearby
  // node/annotation centre when close and recording/clearing its anchor.
  const moveEndpoint = useCallback(
    (endpoint, flowPoint) => {
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
          if (n.data?.locked || isRemoteLocked(n.data)) return n;
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
          return {
            ...n,
            position,
            data: nextData,
            draggable: !nextData.locked && !isArrowAnchored(nextData),
          };
        })
      );
    },
    [getNodes, id, setNodes]
  );

  // Teardown for an in-flight endpoint drag; kept in a ref so it can also run on
  // unmount (arrow deleted/deselected mid-drag) without leaking window listeners.
  const dragTeardownRef = useRef(null);
  useEffect(() => () => dragTeardownRef.current?.(), []);

  const startEndpointDrag = (endpoint) => (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (data.locked || remoteLocked) {
      if (remoteLocked) notifyRemoteLockedAttempt();
      return;
    }
    // Geometry gesture (task-annotation-exclusive-edit-leases): acquired
    // fire-and-forget at drag start (the pointer-driven drag already begins
    // visually here, same reasoning as NodeResizer's onResizeStart
    // elsewhere) and released when the gesture ends.
    beginEditing?.([id]);
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
      notifyChange('geometry');
      endEditing?.([id]);
    };
    dragTeardownRef.current = teardown;
    window.addEventListener('pointermove', handleMove, true);
    window.addEventListener('pointerup', handleUp, true);
    window.addEventListener('pointercancel', handleUp, true);
  };

  const dx = Number(data.dx ?? 160);
  const dy = Number(data.dy ?? 0);
  const color = data.color || DEFAULT_ARROW_COLOR;
  const locked = Boolean(data.locked);
  const startArrow = Boolean(data.startArrow);
  const endArrow = data.endArrow ?? true;
  const badge = remoteEditBadge(data);

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
            style={{
              backgroundColor: badge.color,
              left: originX - 2,
              top: originY - 11,
            }}
            title={badge.displayName}
          >
            {badge.displayName}
          </div>
        )}
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
          <line
            x1={originX}
            y1={originY}
            x2={endX}
            y2={endY}
            stroke="transparent"
            strokeWidth={16}
          />
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
          {selected && !locked && !remoteLocked && (
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

      {contextMenu &&
        createPortal(
          <div
            ref={contextMenuRef}
            className="graph-annotation-context-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
          >
            {locked ? (
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
                  {ARROW_COLORS.map((c) => (
                    <button
                      key={c}
                      className={`color-button${color === c ? ' active' : ''}`}
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
                <AnnotationLayerControls
                  labels={labels}
                  locked={data.locked}
                  onChangeLayer={changeLayer}
                />
                <AnnotationDuplicateControl labels={labels} onDuplicate={duplicate} />
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

export default memo(ArrowNode);
