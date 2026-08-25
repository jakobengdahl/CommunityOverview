import { memo, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { ANNOTATION_ICONS, resolveAnnotationIcon } from '../utils/annotationIcons';
import {
  rotationStyle,
  ROTATABLE_OVERLAY_KINDS,
  isRemoteLocked,
  resolveRotatedResizeGeometry,
} from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
import './GenericAnnotationNode.css';

const DEFAULT_COLOR = '#94a3b8';

// A right-click property editor exists for every rotatable generic kind:
// each has at least its rotation and its layer to edit, plus — per kind —
// `shape`'s subtype, `icon`'s configured name, `vote_dot`'s value and, for
// the kinds that paint one, a colour. (`shape`, `icon` and `vote_dot` are
// all already members of ROTATABLE_OVERLAY_KINDS, so this is exactly that
// set.)
const EDITABLE_KINDS = ROTATABLE_OVERLAY_KINDS;

// The generic kinds whose `color` field is actually painted by a branch
// below — text's text colour, frame's and icon's border, shape's and
// vote_dot's fill. `image` carries a `color` in the model
// (GENERIC_OVERLAY_FIELDS in utils/annotations.js) but nothing renders it,
// so offering a recolour there would be a control with no visible effect.
const COLORABLE_KINDS = new Set(['text', 'frame', 'shape', 'icon', 'vote_dot']);

// Palette for the generic kinds' colour picker. Saturated rather than the
// pastels NoteNode/LabelNode use, because these paint borders, glyphs and
// small filled dots rather than a large sticky-note ground — a pastel
// vote_dot on a light canvas is nearly invisible. DEFAULT_COLOR leads so the
// picker can always return an annotation to the colour it was created with.
const GENERIC_COLORS = [
  DEFAULT_COLOR,
  '#ef4444',
  '#f97316',
  '#eab308',
  '#22c55e',
  '#3b82f6',
  '#a855f7',
  '#0f172a',
];

// A vote dot counts votes, so its value is a non-negative integer and the
// stepper never takes it below zero.
const VOTE_VALUE_MIN = 0;

const ROTATE_STEP = 15;
function normalizeAngle(deg) {
  return ((deg % 360) + 360) % 360;
}

// frame/shape/image are the generic kinds that carry an explicit box size
// (SIZED_GENERIC_KINDS in utils/annotations.js) and are the only ones
// resizable in this slice; text/icon/vote_dot render at a fixed intrinsic
// size, so resizing them has no model-space geometry to change.
const RESIZABLE_KINDS = new Set(['frame', 'shape', 'image']);
const MIN_SIZE = 40;

// Every `content.shape` variant the contract accepts, as the CSS that draws
// it. Kept here rather than in the stylesheet so each variant's geometry is
// one testable value: the rectangle/circle-only rendering this replaces
// painted triangle, rhombus, hexagon and process arrow as plain rectangles,
// which no class-name assertion could have caught. Null prototype because the
// key is an annotation's configured shape name (same reason as
// annotationIcons.js and the host app's ICON_REGISTRY).
const SHAPE_STYLES = Object.freeze(
  Object.assign(Object.create(null), {
    rectangle: {},
    circle: { borderRadius: '50%' },
    triangle: { clipPath: 'polygon(50% 0%, 100% 100%, 0% 100%)' },
    rhombus: { clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' },
    hexagon: { clipPath: 'polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%)' },
    process_arrow: {
      clipPath: 'polygon(0% 25%, 70% 25%, 70% 0%, 100% 50%, 70% 100%, 70% 75%, 0% 75%)',
    },
  })
);

// The shape-subtype picker's option order — every variant SHAPE_STYLES draws.
const SHAPE_NAMES = ['rectangle', 'circle', 'triangle', 'rhombus', 'hexagon', 'process_arrow'];

// The icon picker's option order: every canonical name annotationIcons.js
// draws a distinct glyph for (the full set the module doc comment there
// describes — a canonical or aliased entry for every one of the host
// registry's 75 icon names, plus the friendly synonyms that predate it).
// Reusing this set rather than inventing a second vocabulary is the point:
// whatever a user picks here, resolveAnnotationIcon already knows how to
// draw on the annotation itself.
const ICON_NAMES = Object.keys(ANNOTATION_ICONS);

// A clip-path clips the element's outline away too, so the dashed selection
// outline every other generic kind uses is invisible on a triangle, rhombus,
// hexagon or process arrow — and a locked one shows no resize handles either,
// leaving a selected shape with no feedback at all. The halo therefore goes on
// an unclipped wrapper: an element's own filter is rendered *before* its
// clip-path, so a drop shadow on the clipped element itself would be clipped
// away with it.
const SELECTED_SHAPE_HALO = Object.freeze({
  filter: 'drop-shadow(0 0 3px rgba(255, 255, 255, 0.9)) drop-shadow(0 0 1px rgba(0, 0, 0, 0.6))',
});

/**
 * GenericAnnotationNode - a simple visual representation for the v1
 * annotation types that have no dedicated per-type editor yet (text, frame,
 * shape, icon, vote_dot, image; see docs/ANNOTATION_CONTRACT.md). These were
 * previously normalized by annotationModel.js but dropped by the overlay
 * translation layer, so an MCP-created annotation of one of these types never
 * rendered. Selection and move (drag) are handled generically by GraphCanvas
 * for every annotation type; this component adds the visual selection
 * outline, for the sized kinds, model-space resize via ReactFlow's
 * NodeResizer, and — for the kinds EDITABLE_KINDS names — a right-click
 * property editor (colour, shape subtype, icon name, vote value, rotation
 * and layer order).
 */
function GenericAnnotationNode({ id, type, data, selected }) {
  const kind = type;
  const color = data?.color || DEFAULT_COLOR;
  const locked = Boolean(data?.locked);
  const { notifyChange, notifyRemoteLockedAttempt, labels } = useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live claim makes
  // this annotation's lease exclusive (task-annotation-shared-session-realtime).
  const remoteLocked = isRemoteLocked(data);
  const { setNodes } = useReactFlow();
  const selectedClass = selected ? ' selected' : '';
  // Rotation is applied to the rendered element, not to the ReactFlow node
  // wrapper, so drag hit-testing keeps using the unrotated bounding box.
  const rotation = rotationStyle(kind, data?.rotation);

  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  // Snapshot of {x, y, width, height} at the start of the current resize
  // gesture, read by handleResizeEnd to map the gesture's net delta back
  // through this annotation's rotation (resolveRotatedResizeGeometry).
  const resizeStartRef = useRef(null);

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
    if (!EDITABLE_KINDS.has(kind)) return;
    e.preventDefault();
    e.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  const changeShape = (shape) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, shape } } : n)));
    setContextMenu(null);
    notifyChange('style');
  };

  const changeIcon = (name) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, icon: name } } : n))
    );
    setContextMenu(null);
    notifyChange('style');
  };

  const changeColor = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, color: next } } : n))
    );
    notifyChange('style');
  };

  const changeValue = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    const clamped = Math.max(VOTE_VALUE_MIN, Math.round(next));
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, value: clamped } } : n))
    );
    notifyChange('style');
  };

  const changeLayer = useAnnotationLayer(id, data);

  const changeRotation = (deg) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    const next = normalizeAngle(deg);
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, rotation: next } } : n))
    );
    notifyChange('geometry');
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

  // The only action a locked annotation's context menu offers (besides the
  // capability baseline's "copy", which has no GUI action yet at all) —
  // everything else (colour/rotation/shape/font-size/delete) stays out of
  // reach while `locked` is set, matching resize/drag already refusing it.
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

  const handleResizeStart = (event, params) => {
    resizeStartRef.current = params;
  };

  // NodeResizer computes `params` as if this annotation's box were
  // axis-aligned (see resolveRotatedResizeGeometry's comment for why that is
  // wrong once the box is visually rotated); remap the gesture's net delta
  // through the rotation before it lands on the node. Guarded so a caller
  // that invokes this with no params (as an existing test does) still just
  // notifies, unchanged from before this fix.
  const handleResizeEnd = (event, params) => {
    if (params && resizeStartRef.current) {
      const geometry = resolveRotatedResizeGeometry({
        start: resizeStartRef.current,
        end: params,
        rotation: data?.rotation ?? 0,
      });
      setNodes((nds) =>
        nds.map((n) =>
          n.id === id
            ? {
                ...n,
                position: { x: geometry.x, y: geometry.y },
                style: { ...n.style, width: geometry.width, height: geometry.height },
              }
            : n
        )
      );
    }
    resizeStartRef.current = null;
    notifyChange('geometry');
  };

  // Locked annotations already refuse to drag (draggable: !locked in
  // overlayToFlowNode); hide the resize handles too so "locked" reads as one
  // consistent geometry lock rather than only blocking one of two ways to
  // move/resize the object. A remote claim (another client's exclusive lease)
  // hides them the same way.
  const resizer = RESIZABLE_KINDS.has(kind) && (
    <NodeResizer
      minWidth={MIN_SIZE}
      minHeight={MIN_SIZE}
      isVisible={Boolean(selected) && !locked && !remoteLocked}
      lineStyle={{ stroke: color, strokeWidth: 2 }}
      handleStyle={{ width: 10, height: 10, background: color, border: '2px solid white' }}
      onResizeStart={handleResizeStart}
      onResizeEnd={handleResizeEnd}
    />
  );

  const remoteBadge = remoteLocked && (
    <div
      className="graph-node-remote-badge"
      style={{ backgroundColor: data.remoteSelection.color }}
      title={data.remoteSelection.displayName}
    >
      {data.remoteSelection.displayName}
    </div>
  );

  const currentRotation = data?.rotation ?? 0;
  const menu = contextMenu && (
    <ContextMenuPortal
      menuRef={contextMenuRef}
      position={contextMenu}
      kind={kind}
      shape={data.shape || 'rectangle'}
      icon={data.icon}
      color={color}
      value={data.value}
      rotation={currentRotation}
      locked={locked}
      labels={labels}
      onChangeShape={changeShape}
      onChangeIcon={changeIcon}
      onChangeColor={changeColor}
      onChangeValue={changeValue}
      onChangeLayer={changeLayer}
      onChangeRotation={changeRotation}
      onDelete={remove}
      onUnlock={unlock}
    />
  );

  if (kind === 'text') {
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-text${selectedClass}`}
          style={{ color, ...rotation }}
          onContextMenu={openContextMenu}
        >
          {data.text || ''}
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'frame') {
    return (
      <>
        {/* Carries the rotation for both the resizer and the visible frame,
            so the handles rotate with the box instead of staying
            axis-aligned around its unrotated bounds (the ReactFlow node
            wrapper itself deliberately stays unrotated — see the module doc
            comment on `rotation`). */}
        <div
          className="graph-generic-annotation-rotate-wrap"
          style={{ width: '100%', height: '100%', position: 'relative', ...rotation }}
        >
          {resizer}
          <div
            className={`graph-generic-annotation-node kind-frame${selectedClass}`}
            style={{ borderColor: color, width: '100%', height: '100%' }}
            onContextMenu={openContextMenu}
          />
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'shape') {
    const shape = data.shape || 'rectangle';
    return (
      <>
        <div
          className="graph-generic-annotation-shape-halo"
          data-testid="shape-halo"
          style={{
            width: '100%',
            height: '100%',
            position: 'relative',
            ...rotation,
            ...(selected ? SELECTED_SHAPE_HALO : null),
          }}
          onContextMenu={openContextMenu}
        >
          {/* Inside the halo (rather than a sibling of it) so the resize
              handles rotate along with it — see the `frame` branch's
              comment. */}
          {resizer}
          <div
            // No `selected` class: the shared dashed outline it carries is
            // clipped away on the four clipped variants and would be
            // inconsistent on the other two, so a selected shape is marked by
            // the halo above instead — for every variant alike.
            className={`graph-generic-annotation-node kind-shape shape-${shape}`}
            style={{
              backgroundColor: color,
              width: '100%',
              height: '100%',
              ...(SHAPE_STYLES[shape] || SHAPE_STYLES.rectangle),
            }}
          />
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'icon') {
    // An abbreviated name needs the smaller, uppercased treatment the glyphs
    // do not: two letters at glyph size overflow the badge.
    const icon = resolveAnnotationIcon(data.icon);
    const iconClass = icon.isGlyph ? '' : ' kind-icon-abbreviated';
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-icon${iconClass}${selectedClass}`}
          style={{ borderColor: color, ...rotation }}
          title={data.icon}
          onContextMenu={openContextMenu}
        >
          {icon.text}
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'vote_dot') {
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-vote_dot${selectedClass}`}
          style={{ backgroundColor: color, ...rotation }}
          onContextMenu={openContextMenu}
        >
          {data.value ?? ''}
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'image') {
    const url = data.image?.url;
    if (!url) {
      return (
        <>
          <div
            className="graph-generic-annotation-rotate-wrap"
            style={{ width: '100%', height: '100%', position: 'relative', ...rotation }}
          >
            {resizer}
            <div
              className={`graph-generic-annotation-node kind-image kind-image-empty${selectedClass}`}
              onContextMenu={openContextMenu}
            >
              {data.alt || ''}
            </div>
          </div>
          {menu}
          {remoteBadge}
        </>
      );
    }
    return (
      <>
        <div
          className="graph-generic-annotation-rotate-wrap"
          style={{ width: '100%', height: '100%', position: 'relative', ...rotation }}
        >
          {resizer}
          <img
            className={`graph-generic-annotation-node kind-image${selectedClass}`}
            src={url}
            alt={data.alt || ''}
            style={{ width: '100%', height: '100%' }}
            onContextMenu={openContextMenu}
          />
        </div>
        {menu}
        {remoteBadge}
      </>
    );
  }

  return null;
}

// The right-click property editor's portal content, split out only so the
// six kind branches above can each attach it without repeating its JSX.
// Rotation and layer controls show for every EDITABLE_KINDS member (the
// layer row last before Delete, matching the note/label/line/freehand
// menus); the
// colour swatches show for COLORABLE_KINDS, the shape-subtype grid only for
// `kind === 'shape'`, the icon-name grid only for `kind === 'icon'`, and the
// value stepper only for `kind === 'vote_dot'`. A locked annotation gets
// none of them — the capability baseline is "a locked object remains
// selectable but offers only unlock or copy" — so this shows only the unlock
// action instead.
function ContextMenuPortal({
  menuRef,
  position,
  kind,
  shape,
  icon,
  color,
  value,
  rotation,
  locked,
  labels,
  onChangeShape,
  onChangeIcon,
  onChangeColor,
  onChangeValue,
  onChangeLayer,
  onChangeRotation,
  onDelete,
  onUnlock,
}) {
  if (locked) {
    return createPortal(
      <div
        ref={menuRef}
        className="graph-annotation-context-menu"
        style={{ left: position.x, top: position.y }}
      >
        <button type="button" className="context-menu-unlock" onClick={onUnlock}>
          🔓 {labels.unlock}
        </button>
      </div>,
      document.body
    );
  }
  return createPortal(
    <div
      ref={menuRef}
      className="graph-annotation-context-menu"
      style={{ left: position.x, top: position.y }}
    >
      {COLORABLE_KINDS.has(kind) && (
        <>
          <div className="context-menu-title">{labels.color}</div>
          <div className="context-menu-colors">
            {GENERIC_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                className={`color-button${color === c ? ' active' : ''}`}
                style={{ backgroundColor: c }}
                aria-label={c}
                onClick={() => onChangeColor(c)}
              />
            ))}
          </div>
        </>
      )}
      {kind === 'shape' && (
        <>
          <div className="context-menu-title">{labels.shape}</div>
          <div className="context-menu-shapes">
            {SHAPE_NAMES.map((name) => (
              <button
                key={name}
                type="button"
                className={`shape-picker-button${shape === name ? ' active' : ''}`}
                aria-label={name}
                title={name}
                onClick={() => onChangeShape(name)}
              >
                <span
                  className={`shape-picker-swatch shape-${name}`}
                  style={SHAPE_STYLES[name] || SHAPE_STYLES.rectangle}
                />
              </button>
            ))}
          </div>
        </>
      )}
      {kind === 'icon' && (
        <>
          <div className="context-menu-title">{labels.icon}</div>
          <div className="context-menu-icons">
            {ICON_NAMES.map((name) => (
              <button
                key={name}
                type="button"
                className={`icon-picker-button${icon === name ? ' active' : ''}`}
                aria-label={name}
                title={name}
                onClick={() => onChangeIcon(name)}
              >
                {resolveAnnotationIcon(name).text}
              </button>
            ))}
          </div>
        </>
      )}
      {kind === 'vote_dot' && (
        <>
          <div className="context-menu-title">{labels.voteValue}</div>
          <div className="context-menu-vote-value">
            <button
              type="button"
              className="vote-value-button"
              aria-label={labels.voteValueDecrease}
              onClick={() => onChangeValue((value ?? 0) - 1)}
            >
              −
            </button>
            <span className="vote-value-current">{value ?? 0}</span>
            <button
              type="button"
              className="vote-value-button"
              aria-label={labels.voteValueIncrease}
              onClick={() => onChangeValue((value ?? 0) + 1)}
            >
              +
            </button>
          </div>
        </>
      )}
      <div className="context-menu-title">{labels.rotation}</div>
      <div className="context-menu-rotate">
        <button
          type="button"
          className="rotate-button"
          aria-label={labels.rotateLeft}
          onClick={() => onChangeRotation(rotation - ROTATE_STEP)}
        >
          ⟲
        </button>
        <button
          type="button"
          className="rotate-button rotate-reset"
          aria-label={labels.rotateReset}
          onClick={() => onChangeRotation(0)}
        >
          {Math.round(rotation)}°
        </button>
        <button
          type="button"
          className="rotate-button"
          aria-label={labels.rotateRight}
          onClick={() => onChangeRotation(rotation + ROTATE_STEP)}
        >
          ⟳
        </button>
      </div>
      <AnnotationLayerControls labels={labels} onChangeLayer={onChangeLayer} />
      <button type="button" className="context-menu-delete" onClick={onDelete}>
        🗑️ {labels.delete}
      </button>
    </div>,
    document.body
  );
}

export default memo(GenericAnnotationNode);
