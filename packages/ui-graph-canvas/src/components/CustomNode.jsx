import { useState, memo, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { Handle, Position, useStore, useStoreApi } from 'reactflow';
import { bandTextStyle, nodeShapeBandPath, nodeShapeTextBox } from '../utils/nodeShapes';
import './CustomNode.css';

const MIN_TARGET_PX = 24;

/** The side of the largest axis-aligned square the painted body holds. */
export function shapeInnerSpan(geometry) {
  const { width: w, height: h } = geometry;
  return geometry.vertices ? (w * h) / (w + h) : Math.min(w, h);
}

// The body geometry behind each mounted minimum-size target.
export const shapeHitGeometry = new WeakMap();

/** Screen-px distance from (x, y) to the painted body behind a target span. */
function bodyDistance(span, x, y) {
  const geometry = shapeHitGeometry.get(span);
  const r = span.getBoundingClientRect();
  if (!geometry) return Math.hypot(r.left + r.width / 2 - x, r.top + r.height / 2 - y);
  const zoom = r.width / parseFloat(span.style.width);
  const px = (x - r.left) / zoom + parseFloat(span.style.left);
  const py = (y - r.top) / zoom + parseFloat(span.style.top);
  const { width: w, height: h, radius, vertices } = geometry;
  if (!vertices) {
    // A rounded box is its core grown by the radius.
    const dx = Math.max(0, radius - px, px - (w - radius));
    const dy = Math.max(0, radius - py, py - (h - radius));
    return Math.max(0, Math.hypot(dx, dy) - radius) * zoom;
  }
  // Outside the body (a painted hit wins before ranking), so the nearest edge.
  const edges = vertices.map((a, i) => {
    const b = vertices[(i + 1) % vertices.length];
    const ex = b.x - a.x;
    const ey = b.y - a.y;
    const t = Math.max(0, Math.min(1, ((px - a.x) * ex + (py - a.y) * ey) / (ex * ex + ey * ey)));
    return Math.hypot(a.x + t * ex - px, a.y + t * ey - py);
  });
  return Math.min(...edges) * zoom;
}

/**
 * Give the pointer at (x, y) to at most one minimum-size target: none when any
 * node's painted body or label is there, else the one whose painted body is
 * nearest, ties going to the topmost. The others stop taking pointer events.
 */
export function resolveShapeHitTargets(doc, x, y) {
  for (const span of doc.querySelectorAll('.graph-shape-hit[data-hit-off]')) {
    span.removeAttribute('data-hit-off');
  }
  const elements = doc.elementsFromPoint(x, y);
  const spans = elements.filter((el) => el.classList.contains('graph-shape-hit'));
  if (!spans.length) return;
  const painted = elements.some(
    (el) => !el.classList.contains('graph-shape-hit') && el.closest('.react-flow__node')
  );
  const ranked = spans.map((span) => [bodyDistance(span, x, y), span]);
  // A stable sort keeps elementsFromPoint's topmost-first order on ties.
  const winner = painted ? null : ranked.sort((a, b) => a[0] - b[0])[0][1];
  for (const span of spans) {
    if (span !== winner) span.setAttribute('data-hit-off', '');
  }
}

let hitTargetCount = 0;
let hoverPoint = null;
let resolveFrame = 0;
function resolveFromEvent(event) {
  if (event.type === 'pointermove' && event.pointerType !== 'touch') {
    hoverPoint = { x: event.clientX, y: event.clientY };
  }
  resolveShapeHitTargets(document, event.clientX, event.clientY);
}

// Zoom, pan, layout or a newly mounted target move bodies under a still mouse;
// re-resolve after React commits, so the next press already sees fresh state.
function resolveAtHover() {
  if (!hoverPoint || resolveFrame) return;
  resolveFrame = requestAnimationFrame(() => {
    resolveFrame = 0;
    if (hitTargetCount && hoverPoint) resolveShapeHitTargets(document, hoverPoint.x, hoverPoint.y);
  });
}

// The browser hit-tests a press before the capture listener resolves it, so a
// touch, or a press racing a frame, can land on a target that just lost the
// point. Swallow it rather than drag or select the wrong node; the follow-up
// click and context menu are hit-tested afresh and reach the right one.
function blockLosingTarget(event) {
  if (event.target?.matches?.('.graph-shape-hit[data-hit-off]')) {
    event.stopImmediatePropagation();
  }
}

const HIT_LISTENERS = [
  ['pointermove', resolveFromEvent],
  ['pointerdown', resolveFromEvent],
  ['mousedown', blockLosingTarget],
  ['touchstart', blockLosingTarget],
];

// A tiny body still gets a 24px screen-space target, centred on the body.
function ShapeHitTarget({ geometry }) {
  const store = useStoreApi();
  const size = useStore((s) => {
    const zoom = s.transform[2];
    return shapeInnerSpan(geometry) * zoom < MIN_TARGET_PX ? MIN_TARGET_PX / zoom : 0;
  });
  // Resolved before the press reaches any node: pointermove precedes a mouse
  // press, and the eraser reads elementFromPoint after this capture listener.
  useEffect(() => {
    if (!size || typeof document.elementsFromPoint !== 'function') return undefined;
    if (hitTargetCount++ === 0) {
      for (const [type, listener] of HIT_LISTENERS) {
        document.addEventListener(type, listener, true);
      }
    }
    resolveAtHover();
    const unsubscribe = store.subscribe(resolveAtHover);
    return () => {
      unsubscribe();
      if (--hitTargetCount === 0) {
        for (const [type, listener] of HIT_LISTENERS) {
          document.removeEventListener(type, listener, true);
        }
      }
    };
  }, [size, store]);
  if (!size) return null;
  return (
    <span
      ref={(el) => el && shapeHitGeometry.set(el, geometry)}
      className="graph-shape-hit"
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        left: geometry.width / 2 - size / 2,
        top: geometry.height / 2 - size / 2,
      }}
    />
  );
}

/**
 * CustomNode - A graph node component for displaying entities
 *
 * Props passed via data:
 * - label: Node display name
 * - summary: Short description (optional)
 * - nodeType: Type of node (Actor, Initiative, etc.)
 * - color: Node color
 * - isHighlighted: Whether node is highlighted
 * - isDimmed: Session-local focus (task-session-focus-dimming-controls) —
 *   the node stays on the canvas but renders at reduced prominence; a
 *   selection or hover still shows it at full opacity (see CustomNode.css)
 * - pulse: transient { style: 'glow'|'grow'|'marker', color, seq } when an
 *   external trigger fired a visual pulse on this node, else null
 * - description: Full description for tooltip
 * - communities: Array of community names
 * - onExpand: Callback when expand button clicked
 * - onEdit: Callback when edit button clicked
 * - remoteSelection: { color, displayName } when another collaborator has this
 *   node selected in a shared session (design 3.5), else null
 * - stagedState: 'created' | 'updated' | 'orphaned' | 'deleted' when the node
 *   is staged in the active session and not (yet) what the graph holds
 *   (docs/SESSION_OVERLAY_CONTRACT.md §8.1), else null; stagedLabel is its
 *   translated badge text
 * - shapeRender: { definition, values, geometry } for a node with a resolved
 *   shape: its definition, area texts and the geometry the flow node also
 *   carries for edges and layout. Absent means the built-in card.
 */
function CustomNode({ data, id, selected }) {
  const [showButtons, setShowButtons] = useState(false);
  const [showTooltip, setShowTooltip] = useState(false);
  const [tooltipPos, setTooltipPos] = useState(null);
  const nodeRef = useRef(null);

  const isSkill = data.nodeType === 'Skill' || data.type === 'Skill';
  const remote = data.remoteSelection || null;
  const staged = data.stagedState || null;
  const pulse = data.pulse || null;
  const pulseStyle = pulse
    ? ['glow', 'grow', 'marker'].includes(pulse.style)
      ? pulse.style
      : 'glow'
    : null;

  const handleExpand = (e) => {
    e.stopPropagation();
    if (data.onExpand) {
      data.onExpand(id, data);
    }
  };

  const handleEdit = (e) => {
    e.stopPropagation();
    if (data.onEdit) {
      data.onEdit(id, data);
    }
  };

  const mouseHandlers = {
    onMouseEnter: () => {
      setShowButtons(true);
      if (nodeRef.current) {
        const rect = nodeRef.current.getBoundingClientRect();
        setTooltipPos({
          top: rect.bottom + 8,
          left: rect.left + rect.width / 2,
        });
      }
      setShowTooltip(true);
    },
    onMouseLeave: () => {
      setShowButtons(false);
      setShowTooltip(false);
      setTooltipPos(null);
    },
  };

  const stateOverlays = (
    <>
      {pulseStyle && (
        <span
          key={pulse.seq}
          className={`graph-node-pulse graph-node-pulse-${pulseStyle}`}
          style={{ '--pulse-color': pulse.color || data.color || '#646cff' }}
          aria-hidden="true"
        />
      )}

      {remote && (
        <div
          className="graph-node-remote-badge"
          style={{ backgroundColor: remote.color }}
          title={remote.displayName}
        >
          {remote.displayName}
        </div>
      )}

      {staged && (
        <div className="graph-node-staged-badge" data-staged-state={staged}>
          {data.stagedLabel || staged}
        </div>
      )}

      {data.markColor && (
        <div
          className="graph-node-mark-badge"
          style={{ backgroundColor: data.markColor }}
          title={data.markLabel || ''}
        />
      )}
    </>
  );

  const skillBadge = isSkill && (
    <span
      className="skill-node-badge"
      title="Skill — select and ask the AI to apply these instructions"
    >
      ★
    </span>
  );

  const actionButtons = (visible, expandStyle, editStyle) =>
    visible && (
      <>
        {data.onExpand && (
          <button
            className="graph-expand-button"
            onClick={handleExpand}
            title="Show related nodes"
            style={expandStyle}
          >
            +
          </button>
        )}
        {data.onEdit && (
          <button
            className="graph-edit-button"
            onClick={handleEdit}
            title="Edit node"
            style={editStyle}
          >
            ✏️
          </button>
        )}
      </>
    );

  const tooltip =
    showTooltip &&
    tooltipPos &&
    data.previewEnabled !== false &&
    (data.description || data.communities?.length > 0 || data.markLabel) &&
    createPortal(
      <div
        className="graph-node-tooltip"
        style={{ top: `${tooltipPos.top}px`, left: `${tooltipPos.left}px`, zIndex: 99999 }}
      >
        <div className="tooltip-header">
          <strong>{data.nodeType}:</strong> {data.label}
        </div>
        {data.markLabel && (
          <div className="tooltip-mark-label" style={{ borderLeftColor: data.markColor }}>
            {data.markLabel}
          </div>
        )}
        {data.description && <div className="tooltip-description">{data.description}</div>}
        {data.communities && data.communities.length > 0 && (
          <div className="tooltip-communities">
            <strong>Communities:</strong> {data.communities.join(', ')}
          </div>
        )}
      </div>,
      document.body
    );

  // Graph attributes are spread into data, so require the descriptor's
  // function rather than trusting any attribute that happens to share a name.
  const geometry =
    typeof data.shapeRender?.geometry?.anchor === 'function' ? data.shapeRender.geometry : null;
  if (geometry) {
    const { placement } = data.shapeRender.definition;
    const values = data.shapeRender.values;
    const triangle = Boolean(geometry.vertices);
    const [vertical, horizontal] = triangle ? [placement, 'centre'] : placement.split('-');
    const align = { left: 'flex-start', centre: 'center', right: 'flex-end' }[horizontal];
    const box = nodeShapeTextBox(geometry, placement);
    const expand = geometry.anchor({ x: 1, y: 0 });
    const outline = data.markColor || data.color;
    const bandText = triangle
      ? { color: 'var(--graph-card-text-primary)', background: 'var(--graph-card-surface)' }
      : bandTextStyle(data.color);
    return (
      <div
        ref={nodeRef}
        className={`graph-shaped-node graph-shape-${geometry.base} ${data.isHighlighted ? 'highlighted' : ''} ${selected ? 'selected' : ''} ${data.markColor ? 'marked' : ''} ${isSkill ? 'skill-node' : ''} ${remote ? 'remote-selected' : ''} ${data.isDimmed ? 'dimmed' : ''} ${staged ? `staged staged-${staged}` : ''}`}
        style={{ width: geometry.width, height: geometry.height }}
        {...mouseHandlers}
      >
        <Handle type="target" position={Position.Top} />
        <svg
          className="graph-shape-svg"
          aria-hidden="true"
          focusable="false"
          width={geometry.width}
          height={geometry.height}
          viewBox={`0 0 ${geometry.width} ${geometry.height}`}
        >
          {remote && (
            <path
              className="graph-shape-ring graph-shape-ring-remote"
              d={geometry.path}
              style={{ stroke: remote.color }}
            />
          )}
          {data.markColor && (
            <path
              className="graph-shape-ring graph-shape-ring-mark"
              d={geometry.path}
              style={{ stroke: `${data.markColor}66` }}
            />
          )}
          {(selected || data.isHighlighted) && (
            <path className="graph-shape-ring graph-shape-ring-focus" d={geometry.path} />
          )}
          <path className="graph-shape-shadow" d={geometry.path} />
          <path className="graph-shape-body" d={geometry.path} />
          <path
            className="graph-shape-band"
            d={nodeShapeBandPath(geometry)}
            style={{ fill: data.color }}
          />
          <path className="graph-shape-outline" d={geometry.path} style={{ stroke: outline }} />
        </svg>

        {stateOverlays}

        <div
          className={`graph-shape-band-text${triangle ? ' graph-shape-outside-label' : ''}`}
          style={{
            top: triangle ? -geometry.bandHeight : 0,
            height: geometry.bandHeight,
            padding: `0 ${geometry.padding}px`,
            fontSize: `${geometry.text.type}rem`,
            justifyContent: align,
          }}
        >
          {(skillBadge || values.first) && (
            <span className="graph-shape-label-chip" style={bandText}>
              {skillBadge}
              {values.first}
            </span>
          )}
        </div>

        <div
          className="graph-shape-text"
          style={{
            left: box.x,
            top: box.y,
            width: box.width,
            height: box.height,
            justifyContent:
              { top: 'flex-start', middle: 'center', centre: 'center' }[vertical] || 'flex-end',
            alignItems: align,
            textAlign: horizontal === 'centre' ? 'center' : horizontal,
          }}
        >
          {values.second && (
            <div className="graph-node-label" style={{ fontSize: `${geometry.text.title}rem` }}>
              {values.second}
            </div>
          )}
          {values.third && (
            <div className="graph-node-summary" style={{ fontSize: `${geometry.text.summary}rem` }}>
              {values.third}
            </div>
          )}
        </div>

        <ShapeHitTarget geometry={geometry} />

        {/* Also shown while selected, so a keyboard user can Tab to them. */}
        {actionButtons(
          showButtons || selected,
          { left: expand.x - 12, top: expand.y, right: 'auto' },
          { left: expand.x - 12, top: expand.y + 32, right: 'auto' }
        )}
        {tooltip}
        <Handle type="source" position={Position.Bottom} />
      </div>
    );
  }

  return (
    <div
      ref={nodeRef}
      className={`graph-custom-node ${data.isHighlighted ? 'highlighted' : ''} ${selected ? 'selected' : ''} ${data.markColor ? 'marked' : ''} ${isSkill ? 'skill-node' : ''} ${remote ? 'remote-selected' : ''} ${data.isDimmed ? 'dimmed' : ''} ${staged ? `staged staged-${staged}` : ''}`}
      style={{
        borderColor: data.markColor || data.color,
        boxShadow: data.markColor
          ? `0 0 0 2px ${data.markColor}66, 0 2px 8px rgba(0,0,0,0.3)`
          : undefined,
        outline: remote ? `2px solid ${remote.color}` : undefined,
        outlineOffset: remote ? '2px' : undefined,
      }}
      {...mouseHandlers}
    >
      <Handle type="target" position={Position.Top} />

      {stateOverlays}

      <div className="graph-node-header" style={{ backgroundColor: data.color }}>
        {skillBadge}
        <span className="graph-node-type">{data.nodeType}</span>
      </div>

      <div className="graph-node-content">
        <div className="graph-node-label">{data.label}</div>
        {data.summary && <div className="graph-node-summary">{data.summary}</div>}
      </div>

      {actionButtons(showButtons)}

      {tooltip}

      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export default memo(CustomNode);
