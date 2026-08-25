import { memo, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { buildFreehandPath, hasPressureData, buildPressureSegments } from '../utils/freehandPath';
import { isRemoteLocked } from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
import './FreehandAnnotationNode.css';

/**
 * FreehandAnnotationNode - renders a freehand/vector-stroke annotation as an
 * SVG path, following the same per-node inline-SVG pattern as ArrowNode.
 * `data.points` are node-relative (relative to the node's own `position`,
 * the stroke's anchor point — see `annotations.js`'s `GENERIC_OVERLAY_FIELDS`
 * comment), so a plain ReactFlow drag moves the whole stroke without this
 * component touching point coordinates itself.
 *
 * Selection, move (drag) and the envelope's z/locked semantics are handled
 * generically by GraphCanvas/overlayToFlowNode for every annotation type
 * (freehand is one of the GENERIC_OVERLAY_TYPES); this component adds the
 * visual selection outline, the locked cursor, and — new here — a right-click
 * property editor (smoothing/color/width/opacity), matching the pattern
 * GenericAnnotationNode's shape/rotation editor established. When any sampled
 * point carries pressure the stroke renders as several short width-varying
 * segments (buildPressureSegments) instead of one uniform-width path — see
 * that helper's doc comment for why segments rather than one continuous
 * variable-width curve.
 */
// Black, because a stroke has to be visible on the canvas the app actually
// renders. The previous default was the near-white '#e6edf3' this package's
// palettes were picked for when the canvas was dark; on the light canvas a
// freehand stroke drawn with it is invisible, so the tool reads as broken
// rather than as mis-coloured. Kept in the swatch list too — a white stroke is
// still wanted for a dark background — but it is no longer what you get by
// drawing without choosing.
export const DEFAULT_FREEHAND_COLOR = '#111827';
const DEFAULT_COLOR = DEFAULT_FREEHAND_COLOR;
const DEFAULT_STROKE_WIDTH = 2;
const PAD = 8;

const FREEHAND_COLORS = [
  DEFAULT_COLOR,
  '#e6edf3',
  '#FDE047',
  '#4ADE80',
  '#60A5FA',
  '#F472B6',
  '#FB923C',
];
const FREEHAND_WIDTHS = [1.5, 2, 3, 5, 8];
const FREEHAND_SMOOTHING_LEVELS = [0, 0.3, 0.6, 1];
const FREEHAND_OPACITY_LEVELS = [0.3, 0.5, 0.75, 1];

function boundingBox(points) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 0, maxY: 0 };
  return { minX, minY, maxX, maxY };
}

function FreehandAnnotationNode({ id, data, selected }) {
  const rawPoints = Array.isArray(data?.points) ? data.points : [];
  const color = data?.color || DEFAULT_COLOR;
  const strokeWidth = Number.isFinite(data?.strokeWidth) ? data.strokeWidth : DEFAULT_STROKE_WIDTH;
  const smoothing = data?.smoothing ?? 0;
  const opacity = Number.isFinite(data?.opacity) ? data.opacity : 1;
  const locked = Boolean(data?.locked);
  const { notifyChange, notifyRemoteLockedAttempt, labels } = useContext(AnnotationContext);
  const remoteLocked = isRemoteLocked(data);
  const changeLayer = useAnnotationLayer(id, data);
  const { setNodes } = useReactFlow();
  // Another client's live selection claim (task-annotation-shared-session-
  // realtime): dragging is already refused centrally via `draggable`
  // (GraphCanvas's remote-selection effect); this only adds the visual cue,
  // since freehand strokes have no per-component mutation UI of their own to
  // guard.
  const remoteSelection = data?.remoteSelection || null;

  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);

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

  const openContextMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const updateStyle = (patch) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
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

  // The only action a locked stroke's context menu offers (besides the
  // capability baseline's "copy", which has no GUI action yet at all) —
  // everything else (colour/width/smoothing/opacity/delete) stays out of
  // reach while `locked` is set, matching NoteNode/LabelNode/
  // GenericAnnotationNode's context menus.
  const unlock = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, locked: false } } : n))
    );
    setContextMenu(null);
    notifyChange('style');
  };

  const { minX, minY, maxX, maxY } = boundingBox(rawPoints);
  const boxW = Math.max(1, maxX - minX) + PAD * 2;
  const boxH = Math.max(1, maxY - minY) + PAD * 2;
  // Shift every point into positive local SVG space (like ArrowNode's
  // originX/originY), then pull the wrapping div back by the same amount so
  // the stroke's own anchor point still lines up with the node's flow
  // position.
  const originX = PAD - minX;
  const originY = PAD - minY;
  const localPoints = rawPoints.map((p) => ({
    x: p.x + originX,
    y: p.y + originY,
    pressure: p.pressure,
  }));
  const pressureAware = hasPressureData(localPoints);
  const { d } = buildFreehandPath(localPoints, smoothing);
  const segments = pressureAware
    ? buildPressureSegments(localPoints, smoothing, strokeWidth)
    : null;

  return (
    <div
      className={`graph-freehand-node${selected ? ' selected' : ''}${locked ? ' locked' : ''}`}
      style={{
        marginLeft: -originX,
        marginTop: -originY,
        outline: remoteSelection ? `2px solid ${remoteSelection.color}` : undefined,
        outlineOffset: remoteSelection ? '2px' : undefined,
      }}
      onContextMenu={openContextMenu}
    >
      {remoteSelection && (
        <div
          className="graph-node-remote-badge"
          style={{ backgroundColor: remoteSelection.color, left: originX - 2, top: originY - 11 }}
          title={remoteSelection.displayName}
        >
          {remoteSelection.displayName}
        </div>
      )}
      <svg width={boxW} height={boxH} style={{ overflow: 'visible', display: 'block' }}>
        {/* Transparent wide hit target so a thin stroke is easy to select/grab. */}
        <path
          d={d}
          stroke="transparent"
          strokeWidth={Math.max(16, strokeWidth + 12)}
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <g
          className="graph-freehand-stroke"
          opacity={opacity}
          style={selected ? { filter: 'drop-shadow(0 0 3px rgba(255, 255, 255, 0.7))' } : undefined}
        >
          {pressureAware ? (
            segments.map((segment, i) => (
              <path
                key={i}
                d={segment.d}
                stroke={color}
                strokeWidth={segment.width}
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))
          ) : (
            <path
              d={d}
              stroke={color}
              strokeWidth={strokeWidth}
              fill="none"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
        </g>
      </svg>
      {contextMenu &&
        createPortal(
          <div
            ref={contextMenuRef}
            className="graph-annotation-context-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
          >
            {locked ? (
              // The only action a locked stroke's context menu offers
              // (besides the capability baseline's "copy", which has no GUI
              // action yet at all) — everything else stays out of reach
              // while `locked` is set, matching the pattern established in
              // NoteNode/LabelNode/GenericAnnotationNode.
              <button type="button" className="context-menu-unlock" onClick={unlock}>
                🔓 {labels.unlock}
              </button>
            ) : (
              <>
                <div className="context-menu-title">{labels.freehandColor}</div>
                <div className="context-menu-colors">
                  {FREEHAND_COLORS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className={`color-button${color === c ? ' active' : ''}`}
                      style={{ backgroundColor: c }}
                      aria-label={c}
                      onClick={() => updateStyle({ color: c })}
                    />
                  ))}
                </div>
                <div className="context-menu-title">{labels.freehandWidth}</div>
                <div className="context-menu-sizes">
                  {FREEHAND_WIDTHS.map((w) => (
                    <button
                      key={w}
                      type="button"
                      className={`size-button${strokeWidth === w ? ' active' : ''}`}
                      onClick={() => updateStyle({ strokeWidth: w })}
                    >
                      {w}
                    </button>
                  ))}
                </div>
                <div className="context-menu-title">{labels.freehandSmoothing}</div>
                <div className="context-menu-sizes">
                  {FREEHAND_SMOOTHING_LEVELS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      className={`size-button${smoothing === s ? ' active' : ''}`}
                      onClick={() => updateStyle({ smoothing: s })}
                    >
                      {Math.round(s * 100)}%
                    </button>
                  ))}
                </div>
                <div className="context-menu-title">{labels.freehandOpacity}</div>
                <div className="context-menu-sizes">
                  {FREEHAND_OPACITY_LEVELS.map((o) => (
                    <button
                      key={o}
                      type="button"
                      className={`size-button${opacity === o ? ' active' : ''}`}
                      onClick={() => updateStyle({ opacity: o })}
                    >
                      {Math.round(o * 100)}%
                    </button>
                  ))}
                </div>
                <AnnotationLayerControls
                  labels={labels}
                  locked={data.locked}
                  onChangeLayer={changeLayer}
                />
                <button type="button" className="context-menu-delete" onClick={remove}>
                  🗑️ {labels.delete}
                </button>
              </>
            )}
          </div>,
          document.body
        )}
    </div>
  );
}

export default memo(FreehandAnnotationNode);
