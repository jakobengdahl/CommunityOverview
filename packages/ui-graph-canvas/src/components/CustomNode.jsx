import { useState, memo, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Handle, Position } from 'reactflow';
import {
  bandTextStyle,
  nodeShapeActionLayout,
  nodeShapeBandPath,
  nodeShapeTextBox,
} from '../utils/nodeShapes';
import './CustomNode.css';

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

  // Graph attributes are spread into data, so require the descriptor's
  // function rather than trusting any attribute that happens to share a name.
  const geometry =
    typeof data.shapeRender?.geometry?.anchor === 'function' ? data.shapeRender.geometry : null;
  const triangle = Boolean(geometry?.vertices);

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
          // A triangle's outside label fills the row above the body; the
          // badge sits on top of that row instead of over it.
          style={{
            backgroundColor: remote.color,
            ...(triangle && { top: -geometry.bandHeight, transform: 'translateY(-100%)' }),
          }}
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

  if (geometry) {
    const { placement } = data.shapeRender.definition;
    const values = data.shapeRender.values;
    const [vertical, horizontal] = triangle ? [placement, 'centre'] : placement.split('-');
    const align = { left: 'flex-start', centre: 'center', right: 'flex-end' }[horizontal];
    const box = nodeShapeTextBox(geometry, placement);
    const actions = nodeShapeActionLayout(
      geometry,
      placement,
      [data.onExpand, data.onEdit].filter(Boolean).length
    );
    const [expandSlot, editSlot] = data.onExpand ? actions.buttons : [null, actions.buttons[0]];
    const slotStyle = (slot, em) =>
      slot && {
        left: slot.x,
        top: slot.y,
        width: slot.size,
        height: slot.size,
        fontSize: `${(em * slot.size) / 24}rem`,
      };
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
          {/* Under every ring: a state ring, where shown, is the boundary. */}
          <path className="graph-shape-boundary" d={geometry.path} />
          {/* Widest first: each ring shows a band beyond the next one in. */}
          <path className="graph-shape-ring graph-shape-ring-keyboard" d={geometry.path} />
          {remote && (
            <path
              className="graph-shape-ring graph-shape-ring-remote"
              d={geometry.path}
              style={{ stroke: remote.color }}
            />
          )}
          {(selected || data.isHighlighted) && (
            <path className="graph-shape-ring graph-shape-ring-focus" d={geometry.path} />
          )}
          {data.markColor && (
            <path
              className="graph-shape-ring graph-shape-ring-mark"
              d={geometry.path}
              style={{ stroke: `${data.markColor}66` }}
            />
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
            // Keep at least one title line visible when rem text outgrows the pixel box.
            minHeight: values.second ? `${geometry.text.title * 1.2}rem` : undefined,
            justifyContent:
              { top: 'flex-start', middle: 'safe center', centre: 'safe center' }[vertical] ||
              'safe flex-end',
            alignItems: align,
            textAlign: horizontal === 'centre' ? 'center' : horizontal,
            paddingRight: actions.inset,
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

        {/* A tiny body is hit only where it is painted; the minimum hit target
            is node-shape-phase3b-min-hit-target. */}

        {/* Inside the body, so no hover path leaves it; also shown while
            selected, so a keyboard user can Tab to them. */}
        {actionButtons(showButtons || selected, slotStyle(expandSlot, 1), slotStyle(editSlot, 0.8))}
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
