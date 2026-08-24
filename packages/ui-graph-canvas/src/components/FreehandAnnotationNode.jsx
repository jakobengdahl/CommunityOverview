import { memo } from 'react';
import { buildFreehandPath } from '../utils/freehandPath';
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
 * (freehand is one of the GENERIC_OVERLAY_TYPES); this component only adds
 * the visual selection outline and the locked cursor.
 */
const DEFAULT_COLOR = '#e6edf3';
const DEFAULT_STROKE_WIDTH = 2;
const PAD = 8;

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

function FreehandAnnotationNode({ data, selected }) {
  const rawPoints = Array.isArray(data?.points) ? data.points : [];
  const color = data?.color || DEFAULT_COLOR;
  const strokeWidth = Number.isFinite(data?.strokeWidth) ? data.strokeWidth : DEFAULT_STROKE_WIDTH;
  const smoothing = data?.smoothing ?? 0;
  const locked = Boolean(data?.locked);
  // Another client's live selection claim (task-annotation-shared-session-
  // realtime): dragging is already refused centrally via `draggable`
  // (GraphCanvas's remote-selection effect); this only adds the visual cue,
  // since freehand strokes have no per-component mutation UI of their own to
  // guard.
  const remoteSelection = data?.remoteSelection || null;

  const { minX, minY, maxX, maxY } = boundingBox(rawPoints);
  const boxW = Math.max(1, maxX - minX) + PAD * 2;
  const boxH = Math.max(1, maxY - minY) + PAD * 2;
  // Shift every point into positive local SVG space (like ArrowNode's
  // originX/originY), then pull the wrapping div back by the same amount so
  // the stroke's own anchor point still lines up with the node's flow
  // position.
  const originX = PAD - minX;
  const originY = PAD - minY;
  const localPoints = rawPoints.map((p) => ({ x: p.x + originX, y: p.y + originY }));
  const { d } = buildFreehandPath(localPoints, smoothing);

  return (
    <div
      className={`graph-freehand-node${selected ? ' selected' : ''}${locked ? ' locked' : ''}`}
      style={{
        marginLeft: -originX,
        marginTop: -originY,
        outline: remoteSelection ? `2px solid ${remoteSelection.color}` : undefined,
        outlineOffset: remoteSelection ? '2px' : undefined,
      }}
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
        <path
          d={d}
          stroke={color}
          strokeWidth={strokeWidth}
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

export default memo(FreehandAnnotationNode);
