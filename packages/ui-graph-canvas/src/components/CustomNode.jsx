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

const BAND_TEXT_OUTSIDE = Object.freeze({
  color: 'var(--graph-card-text-primary)',
  background: 'var(--graph-card-surface)',
});
const JUSTIFY = { top: 'flex-start', middle: 'safe center', centre: 'safe center' };
const ALIGN = { left: 'flex-start', centre: 'center', right: 'flex-end' };

// Everything a shaped node derives from its geometry, placement and buttons,
// built once and shared by every node with the same inputs: a large canvas
// then holds one copy of each style object and string, not one per node.
const shapeLayouts = new WeakMap();

function shapeLayout(geometry, placement, hasExpand, hasEdit) {
  if (!shapeLayouts.has(geometry)) shapeLayouts.set(geometry, new Map());
  const cache = shapeLayouts.get(geometry);
  const key = `${placement}|${hasExpand}|${hasEdit}`;
  if (cache.has(key)) return cache.get(key);
  const triangle = Boolean(geometry.vertices);
  const [vertical, horizontal] = triangle ? [placement, 'centre'] : placement.split('-');
  const box = nodeShapeTextBox(geometry, placement);
  const actions = nodeShapeActionLayout(geometry, placement, hasExpand + hasEdit);
  const [expandSlot, editSlot] = hasExpand ? actions.buttons : [null, actions.buttons[0]];
  const slotStyle = (slot, em) =>
    slot &&
    Object.freeze({
      left: slot.x,
      top: slot.y,
      width: slot.size,
      height: slot.size,
      fontSize: `${(em * slot.size) / 24}rem`,
    });
  const text = {
    left: box.x,
    top: box.y,
    width: box.width,
    height: box.height,
    justifyContent: JUSTIFY[vertical] || 'safe flex-end',
    alignItems: ALIGN[horizontal],
    textAlign: horizontal === 'centre' ? 'center' : horizontal,
    paddingRight: actions.inset,
  };
  const layout = Object.freeze({
    root: Object.freeze({ width: geometry.width, height: geometry.height }),
    viewBox: `0 0 ${geometry.width} ${geometry.height}`,
    bandPath: nodeShapeBandPath(geometry),
    band: Object.freeze({
      top: triangle ? -geometry.bandHeight : 0,
      height: geometry.bandHeight,
      padding: `0 ${geometry.padding}px`,
      fontSize: `${geometry.text.type}rem`,
      justifyContent: ALIGN[horizontal],
    }),
    text: Object.freeze(text),
    // Keep at least one title line visible when rem text outgrows the pixel box.
    textWithTitle: Object.freeze({ ...text, minHeight: `${geometry.text.title * 1.2}rem` }),
    title: Object.freeze({ fontSize: `${geometry.text.title}rem` }),
    summary: Object.freeze({ fontSize: `${geometry.text.summary}rem` }),
    expand: slotStyle(expandSlot, 1),
    edit: slotStyle(editSlot, 0.8),
  });
  cache.set(key, layout);
  return layout;
}

// Every character a CSS colour can use, and none that could leave the style
// attribute or start another declaration. Anything else is not a colour, so
// the paint is left out, as the style property would have ignored it.
const CSS_COLOR = /^[\w\s#(),.%/*+-]+$/;

function paint(property, color) {
  return typeof color === 'string' && CSS_COLOR.test(color) ? ` style="${property}: ${color}"` : '';
}

const shapeLayerMarkup = new WeakMap();

/**
 * The SVG layers of a shaped node as markup, cached per geometry and state:
 * a canvas has a few type colours and states, so its nodes share a handful of
 * strings. Paint order: the shadow under the boundary, so its translucent
 * fill never dims the 3:1 band; the boundary under every ring, as a state
 * ring, where shown, is the boundary; rings widest first, so each shows a
 * band beyond the next one in.
 */
function shapeLayers(geometry, bandPath, color, outline, remoteColor, focused, markColor) {
  if (!shapeLayerMarkup.has(geometry)) shapeLayerMarkup.set(geometry, new Map());
  const cache = shapeLayerMarkup.get(geometry);
  const key = JSON.stringify([color, outline, remoteColor, Boolean(focused), markColor]);
  if (!cache.has(key)) {
    const path = (className, extra = '', d = geometry.path) =>
      `<path class="${className}" d="${d}"${extra}></path>`;
    const ring = (state, extra) => path(`graph-shape-ring graph-shape-ring-${state}`, extra);
    cache.set(key, {
      __html: [
        path('graph-shape-shadow'),
        path('graph-shape-boundary'),
        ring('keyboard'),
        typeof remoteColor === 'string' ? ring('remote', paint('stroke', remoteColor)) : '',
        focused ? ring('focus') : '',
        markColor ? ring('mark', paint('stroke', `${markColor}66`)) : '',
        path('graph-shape-body'),
        path('graph-shape-band', paint('fill', color), bandPath),
        path('graph-shape-outline', paint('stroke', outline)),
      ].join(''),
    });
  }
  return cache.get(key);
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
      key="skill"
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
    const values = data.shapeRender.values;
    const layout = shapeLayout(
      geometry,
      data.shapeRender.definition.placement,
      Boolean(data.onExpand),
      Boolean(data.onEdit)
    );
    const outline = data.markColor || data.color;
    const bandText = triangle ? BAND_TEXT_OUTSIDE : bandTextStyle(data.color);
    return (
      <div
        ref={nodeRef}
        className={`graph-shaped-node graph-shape-${geometry.base} ${data.isHighlighted ? 'highlighted' : ''} ${selected ? 'selected' : ''} ${data.markColor ? 'marked' : ''} ${isSkill ? 'skill-node' : ''} ${remote ? 'remote-selected' : ''} ${data.isDimmed ? 'dimmed' : ''} ${staged ? `staged staged-${staged}` : ''}`}
        style={layout.root}
        {...mouseHandlers}
      >
        <Handle type="target" position={Position.Top} />
        <svg
          className="graph-shape-svg"
          aria-hidden="true"
          focusable="false"
          width={geometry.width}
          height={geometry.height}
          viewBox={layout.viewBox}
          // Decoration only: no state, handlers or text, so one shared markup
          // string per look replaces a fiber and props object per layer.
          dangerouslySetInnerHTML={shapeLayers(
            geometry,
            layout.bandPath,
            data.color,
            outline,
            remote ? String(remote.color ?? '') : null,
            selected || data.isHighlighted,
            data.markColor
          )}
        />

        {stateOverlays}

        <div
          className={`graph-shape-band-text${triangle ? ' graph-shape-outside-label' : ''}`}
          style={layout.band}
        >
          {(skillBadge || values.first) && (
            <span className="graph-shape-label-chip" style={bandText}>
              {/* A lone string child needs no text fiber of its own. */}
              {skillBadge ? [skillBadge, values.first] : values.first}
            </span>
          )}
        </div>

        <div
          className="graph-shape-text"
          style={values.second ? layout.textWithTitle : layout.text}
        >
          {values.second && (
            <div className="graph-node-label" style={layout.title}>
              {values.second}
            </div>
          )}
          {values.third && (
            <div className="graph-node-summary" style={layout.summary}>
              {values.third}
            </div>
          )}
        </div>

        {/* A tiny body is hit only where it is painted; the minimum hit target
            is node-shape-phase3b-min-hit-target. */}

        {/* Inside the body, so no hover path leaves it; also shown while
            selected, so a keyboard user can Tab to them. */}
        {actionButtons(showButtons || selected, layout.expand, layout.edit)}
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
