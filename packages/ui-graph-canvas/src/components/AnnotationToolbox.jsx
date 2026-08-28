import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useToolSlotSelection } from '../hooks/useToolSlotSelection';
import { ToolSlotPicker } from './ToolSlotPicker';
import './AnnotationToolbox.css';

// How far the pointer has to travel from the item before a press turns into a
// drag rather than staying a click/tap. Keeps a plain tap (no measurable
// movement) from ever reaching the drag path.
const DRAG_THRESHOLD_PX = 6;

// The toolbox items that precede the shape slot. `frame` stays a distinct,
// separate item here — collapsing it into the shape kind is a different,
// not-yet-scheduled task (task-annotation-merge-frame-into-shape-rectangle)
// that changes what the shape slot even contains, so it is out of scope for
// this collapse.
const TOOLBOX_ITEMS_LEADING = [
  { kind: 'note', glyph: '🗒️', labelKey: 'note' },
  { kind: 'text', glyph: 'T', labelKey: 'text' },
  { kind: 'label', glyph: '🏷️', labelKey: 'label' },
  { kind: 'frame', glyph: '▢', labelKey: 'frame' },
];

// Every `content.shape` variant the model accepts (SHAPE_STYLES in
// GenericAnnotationNode.jsx renders each distinctly) — these used to be six
// separate top-level toolbox buttons and are now the options behind one
// collapsed slot (see `renderShapeSlot` below and useToolSlotSelection).
// docs/ANNOTATION_CONTRACT.md's acceptance matrix still applies: creating a
// shape from here is not the same as changing an existing one's subtype.
const SHAPE_VARIANTS = [
  { shape: 'rectangle', glyph: '▭', labelKey: 'shapeRectangle' },
  { shape: 'circle', glyph: '◯', labelKey: 'shapeCircle' },
  { shape: 'triangle', glyph: '△', labelKey: 'shapeTriangle' },
  { shape: 'rhombus', glyph: '◇', labelKey: 'shapeRhombus' },
  { shape: 'hexagon', glyph: '⬡', labelKey: 'shapeHexagon' },
  { shape: 'process_arrow', glyph: '➜', labelKey: 'shapeProcessArrow' },
];
const SHAPE_VARIANT_KEYS = SHAPE_VARIANTS.map((variant) => variant.shape);
const DEFAULT_SHAPE = SHAPE_VARIANTS[0].shape;
// Namespaced and versioned by slot identity, not by content — a personal
// "which shape did I use last" preference, not something the shared session
// or graph should ever see (owner decision 2026-08-26 on
// task-annotation-shapes-under-one-toolbox-slot). The icon slot
// task-annotation-icon-slot-and-visuals should give itself a different key
// here, not reuse this one.
const SHAPE_SLOT_STORAGE_KEY = 'communityoverview:annotation-toolbox:shape-slot';
// Stable regardless of which shape is currently selected, so the drag-click
// suppression queue below (keyed by itemKey) and React's reconciliation both
// keep treating the slot as the same item across a shape change mid-gesture.
const SHAPE_SLOT_ITEM_KEY = 'shape-slot';

const TOOLBOX_ITEMS_TRAILING = [
  { kind: 'icon', glyph: '🔘', labelKey: 'icon' },
  { kind: 'vote_dot', glyph: '⚫', labelKey: 'voteDot' },
  // Opens a file picker rather than creating an object directly — there is
  // nothing to pick up and carry, so this item stays click-only (see
  // `isDraggableKind` below).
  { kind: 'image', glyph: '🖼️', labelKey: 'image', draggable: false },
  // Unlike every other item, clicking this one does not create an annotation
  // immediately — GraphCanvas's onCreate special-cases 'freehand' to arm a
  // one-stroke drawing mode instead (docs/ANNOTATION_CONTRACT.md's "Physical
  // device acceptance" gap: this is the GUI creation entry point stylus input
  // needed). `activeKind` reflects that armed state back onto this button.
  // Arming a mode isn't an object to carry either, so it stays click-only too.
  { kind: 'freehand', glyph: '✏️', labelKey: 'freehand', draggable: false },
];

const TOOLTIP_ID = 'annotation-toolbox-tooltip';

/**
 * AnnotationToolbox - the bottom-mounted GUI creation surface for the v1
 * annotation model (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces").
 * It is deliberately a different surface from graph-node creation
 * (FloatingToolbar, a floating left-side rail in frontend/web): different
 * position (bottom-anchored, spanning the canvas), different chrome (its own
 * `annotation-toolbox` class namespace), and a different, annotation-only
 * type list, so a user never confuses "create a graph node" with "annotate
 * the canvas".
 *
 * It creates note/text/label/frame, a shape (via the collapsed shape slot,
 * see `renderShapeSlot` below), icon, vote_dot, and image (which opens a file
 * picker rather than adding a node directly — the host's onCreate handles
 * that distinction; see GraphCanvas's onImageIngest). icon/vote_dot each
 * create with a fixed default (a generic glyph / a value of 1 — see
 * GraphCanvas's createAnnotation); an icon's right-click property editor
 * (GenericAnnotationNode.jsx) offers a picker over the full icon vocabulary
 * to change it after creation, the same pattern the shape slot's own picker
 * follows — there is no picker at creation time for icon/vote_dot yet.
 * `activeKind` (currently only meaningful for 'freehand') marks that item as
 * pressed while its armed drawing mode is active, so a user mid-stroke can
 * see which tool is live.
 *
 * The shape slot (task-annotation-shapes-under-one-toolbox-slot) replaces
 * what used to be six separate shape-variant buttons — half the toolbox,
 * which is why it used to wrap to a second row — with one slot that shows
 * the currently selected shape and remembers it in localStorage
 * (useToolSlotSelection; a personal tool preference, not shared session
 * state). A small corner button on the slot, right-clicking the slot, or
 * (per the owner's explicit accessibility direction) Enter/Space on that
 * same corner button all open a fold-out picker (ToolSlotPicker) listing
 * every shape; picking one becomes the new current shape. The corner button
 * is a second real focusable element, not just an invisible gesture, so
 * mouse, touch and keyboard each have a discoverable path to the picker —
 * see `renderShapeSlot` for the concrete wiring. Both pieces
 * (useToolSlotSelection, ToolSlotPicker) are written generically so the
 * planned icon slot (task-annotation-icon-slot-and-visuals) can reuse them
 * rather than duplicating the pattern.
 *
 * Every item that creates an object (all but `image` and `freehand`, see
 * `draggable: false` on those two above) is also drag-to-create: picking it
 * up and moving away from the button is the creation gesture, and releasing
 * places the object where the pointer lands, rather than a click always
 * landing it at the viewport centre via `onCreate`. Two input paths cover
 * this, chosen by `touch` (the host's coarse-pointer signal, matching
 * FloatingToolbar's own `isDraggable`/`isCoarsePointer` split):
 *   - fine pointer (mouse): native HTML5 `dataTransfer` drag, the same
 *     mechanism FloatingToolbar already uses for graph-node creation. The
 *     browser's own drag image is the "in hand" visual; GraphCanvas's onDrop
 *     reads the payload and creates the annotation at the drop position.
 *   - coarse pointer (touch/stylus): HTML5 drag never fires there, so a
 *     Pointer Events-based drag stands in — a small ghost glyph (portalled,
 *     `pointer-events: none`) follows the pointer from the moment it clears
 *     `DRAG_THRESHOLD_PX`, and `onDragCreate` fires on release with the
 *     client position for the host to convert and create from.
 * A plain click/tap that never crosses the threshold still calls `onCreate`
 * exactly as before — drag is additive, not a replacement for the existing
 * keyboard/click path (Enter/Space activation never goes through pointer
 * events at all, so it is unaffected either way). The shape slot's main
 * button participates in this the same way every other item does, using
 * whichever shape is currently selected.
 */
function AnnotationToolbox({
  onCreate,
  onDragCreate,
  labels = {},
  compact = false,
  touch = false,
  activeKind = null,
}) {
  const [expanded, setExpanded] = useState(false);
  const [currentShape, setCurrentShape] = useToolSlotSelection(
    SHAPE_SLOT_STORAGE_KEY,
    SHAPE_VARIANT_KEYS,
    DEFAULT_SHAPE
  );
  const [shapePickerOpen, setShapePickerOpen] = useState(false);
  const shapeSlotRef = useRef(null);
  const shapeCornerButtonRef = useRef(null);
  // Collapsing the toolbox unmounts the items row (and the slot/corner
  // button inside it) out from under an open picker — close it in step
  // rather than leaving a dangling `shapePickerOpen: true` that a later
  // re-expand would resurrect with no button left to have opened it.
  useEffect(() => {
    if (!expanded) setShapePickerOpen(false);
  }, [expanded]);
  // Sole purpose: swallow the click that follows a completed pointer drag
  // (pointerup fires, then the browser's synthetic click fires next in the
  // same task) so a drag never also creates a second, click-positioned copy.
  // Per item (the same `${kind}-${labelKey}` string used as the React `key`
  // below), a FIFO queue of per-gesture tokens — not a shared boolean, not a
  // Set membership flag, and not a bare count. Each completed drag mints its
  // own token (a fresh, unique object) and queues it; `onClick` consumes the
  // oldest queued token for that item (order doesn't matter semantically,
  // since any pending token means "some drag on this item is still owed a
  // suppressed click" — first-in-first-out is just a deterministic pick).
  // The token, not a count, is why this survives the fallback timeout below:
  // a count can't tell whether the specific unit it decrements belongs to
  // the gesture that scheduled it or to an unrelated later one that reused
  // the same slot, so an early-consumed gesture's stale timeout can end up
  // decrementing a DIFFERENT gesture's pending suppression. A token can only
  // ever remove itself — if `onClick` already consumed it, the timeout's
  // removal of that same token is a harmless no-op on a queue that no longer
  // contains it, and it never touches whatever token(s) a later gesture on
  // the same item queued in the meantime.
  const pendingSuppressionsRef = useRef(new Map());
  // Teardown for every currently-attached window pointermove/up/cancel
  // listener set (see handlePointerDown) — a Set rather than a single slot
  // because two pointers (e.g. two fingers on two different items) can each
  // have a drag in flight at once. Guards an unmount mid-drag — the toolbox
  // collapsing, or the whole component going away — leaving listeners
  // attached to `window` that reference a torn-down closure.
  const activeDragCleanupsRef = useRef(new Set());
  useEffect(
    () => () => {
      activeDragCleanupsRef.current.forEach((cleanup) => cleanup());
      activeDragCleanupsRef.current.clear();
    },
    []
  );
  // The floating glyph shown while a coarse-pointer drag is in progress; null
  // when no drag is active. State (not a ref) because it must re-render the
  // portal — the ghost is a handful of DOM nodes, not the whole canvas.
  const [dragGhost, setDragGhost] = useState(null);
  // Hover description, positioned above the hovered cell. A portal for the
  // same reason FloatingToolbar uses one: the items row clips and the toolbox
  // sits in a stacking context, so a tooltip rendered inline would be cut off
  // by its own container. Hover-only by construction — the visible caption the
  // items no longer show is restored wherever hover is unavailable, so a touch
  // user is never left with an unlabelled grid.
  const [hovered, setHovered] = useState(null);

  const showTip = (event, key) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setHovered({ key, left: rect.left + rect.width / 2, top: rect.top - 8 });
  };

  // Coarse-pointer drag path (see the component doc comment above). Listens
  // on `window` rather than using Pointer Capture: the drag routinely leaves
  // both the button and the toolbox entirely (onto the canvas underneath),
  // and a window listener follows it there with no capture API involved —
  // the same choice GraphCanvas's own freehand-drawing pointer effect makes
  // (there, listening on the canvas wrapper is enough since the gesture never
  // needs to leave it; here it does, so `window` is the listened-on target).
  const handlePointerDown = (event, kind, options, glyph, itemKey) => {
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startY = event.clientY;
    const drag = { kind, options, dragging: false };

    const handleMove = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      if (!drag.dragging) {
        const dx = moveEvent.clientX - startX;
        const dy = moveEvent.clientY - startY;
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
        drag.dragging = true;
        // A tap's hover/description handling is irrelevant once it is a drag.
        setHovered(null);
      }
      setDragGhost({ glyph, left: moveEvent.clientX, top: moveEvent.clientY });
    };

    const finishDrag = (finishEvent, { create }) => {
      if (finishEvent.pointerId !== pointerId) return;
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleCancel);
      activeDragCleanupsRef.current.delete(cleanup);
      setDragGhost(null);
      if (create && drag.dragging) {
        // A fresh, unique token for THIS gesture. Object identity (not a
        // count) is what lets the fallback timeout below remove only its
        // own entry, never a different gesture's — see the ref's
        // declaration for why a count can't guarantee that.
        const token = {};
        const queue = pendingSuppressionsRef.current.get(itemKey) ?? [];
        queue.push(token);
        pendingSuppressionsRef.current.set(itemKey, queue);
        onDragCreate?.(drag.kind, drag.options, {
          x: finishEvent.clientX,
          y: finishEvent.clientY,
        });
        // A real drag usually releases away from this button, so the
        // browser's synthetic click (if it fires one for the touch release
        // at all) never reaches this element's onClick to consume this
        // gesture's token — it would otherwise stay queued and silently
        // swallow a later, unrelated tap on this same item. A click
        // genuinely headed for this button fires synchronously before this
        // callback runs, so clearing on the next tick never masks it. If
        // `onClick` already consumed this exact token, removing it here
        // again is a harmless no-op.
        setTimeout(() => {
          const q = pendingSuppressionsRef.current.get(itemKey);
          if (!q) return;
          const at = q.indexOf(token);
          if (at !== -1) q.splice(at, 1);
          if (q.length === 0) pendingSuppressionsRef.current.delete(itemKey);
        }, 0);
      }
    };

    // Cancel (platform-initiated, e.g. a system gesture interrupting the
    // touch) drops the in-progress drag without creating anything — same as
    // a native HTML5 drag released outside any drop target.
    const handleUp = (upEvent) => finishDrag(upEvent, { create: true });
    const handleCancel = (cancelEvent) => finishDrag(cancelEvent, { create: false });

    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleCancel);
    const cleanup = () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleCancel);
    };
    activeDragCleanupsRef.current.add(cleanup);
  };

  // Consumes a pending drag-click suppression token for `itemKey`, if any.
  // Returns true when the click was consumed (i.e. the caller must NOT also
  // create), matching the logic every plain item's onClick already ran
  // inline — factored out so the shape slot's onClick can share it exactly
  // rather than re-deriving the same queue semantics.
  const consumeSuppressedClick = (itemKey) => {
    const queue = pendingSuppressionsRef.current.get(itemKey);
    if (queue && queue.length > 0) {
      queue.shift();
      if (queue.length === 0) pendingSuppressionsRef.current.delete(itemKey);
      return true;
    }
    return false;
  };

  const lbl = {
    toggleExpand: 'Add annotation',
    toggleCollapse: 'Collapse annotation toolbox',
    note: 'Note',
    text: 'Text',
    label: 'Label',
    frame: 'Frame',
    shapeRectangle: 'Rectangle',
    shapeCircle: 'Circle',
    shapeTriangle: 'Triangle',
    shapeRhombus: 'Rhombus',
    shapeHexagon: 'Hexagon',
    shapeProcessArrow: 'Process arrow',
    // The corner button's own accessible name (a "choose a different shape"
    // affordance, distinct from the slot's own name which is whichever shape
    // is currently selected) and the fold-out picker's group label.
    shapePickerOpen: 'Choose a shape',
    shapePicker: 'Shapes',
    icon: 'Icon',
    voteDot: 'Vote dot',
    image: 'Image',
    freehand: 'Freehand',
    // What each item will add, shown on hover. Separate from the item's name
    // because the name has to stay short enough to be an accessible label and
    // a touch-mode caption, while this is allowed to say what happens.
    noteHint: 'Add a sticky note',
    textHint: 'Add a block of text',
    labelHint: 'Add a label or callout',
    frameHint: 'Add a frame to group things visually',
    shapeRectangleHint: 'Add a rectangle',
    shapeCircleHint: 'Add a circle',
    shapeTriangleHint: 'Add a triangle',
    shapeRhombusHint: 'Add a rhombus',
    shapeHexagonHint: 'Add a hexagon',
    shapeProcessArrowHint: 'Add a process step',
    iconHint: 'Add an icon',
    voteDotHint: 'Add a voting dot',
    imageHint: 'Add an image from a file',
    freehandHint: 'Draw a freehand stroke',
    ...labels,
  };

  // Shared per-item button, used for every toolbox entry except the shape
  // slot (which needs the extra corner button and fold-out — see
  // `renderShapeSlot` below). Unchanged in behaviour from before the shape
  // slot existed; only pulled out into its own function so it can be called
  // for the items on both sides of the slot.
  const renderToolboxItem = ({ kind, glyph, labelKey, shape, draggable }) => {
    // image/freehand have no draggable object to create (a file picker, an
    // armed mode) — both stay click-only: no draggable attribute, no
    // dragstart handler, no pointer-drag handlers, no grab cursor.
    const isDraggableKind = draggable !== false;
    const options = shape ? { shape } : undefined;
    const itemKey = `${kind}-${labelKey}`;
    return (
      <button
        key={itemKey}
        type="button"
        className={`annotation-toolbox-item${
          activeKind === kind ? ' annotation-toolbox-item--active' : ''
        }${isDraggableKind ? ' annotation-toolbox-item--draggable' : ''}`}
        onClick={() => {
          // Swallow the click a completed pointer drag synthesizes right
          // after its pointerup — onDragCreate already created the
          // annotation, so this would otherwise create a second,
          // click-positioned one. See `consumeSuppressedClick`.
          if (consumeSuppressedClick(itemKey)) return;
          // A tap on a touch device fires the emulated mouseenter but no
          // mouseleave until the user touches something else, so without
          // this the tooltip stays on screen after the item is used.
          setHovered(null);
          onCreate?.(kind, options);
        }}
        aria-label={lbl[labelKey]}
        aria-pressed={kind === 'freehand' ? activeKind === kind : undefined}
        // The description is the accessible *description*, referenced
        // rather than duplicated. A `title` would give the same text a
        // second, native tooltip on top of the styled one — the visual
        // clutter this redesign exists to reduce. FloatingToolbar sets
        // no title for the same reason.
        aria-describedby={hovered?.key === labelKey ? TOOLTIP_ID : undefined}
        onMouseEnter={(e) => showTip(e, labelKey)}
        onMouseLeave={() => setHovered(null)}
        onFocus={(e) => showTip(e, labelKey)}
        onBlur={() => setHovered(null)}
        // Fine pointer (mouse): native HTML5 drag, off entirely on a
        // coarse pointer — it never fires there, and leaving it on
        // would fight the pointer-drag path below over the same
        // gesture. Mirrors FloatingToolbar's isDraggable/isCoarsePointer split.
        draggable={isDraggableKind && !touch}
        onDragStart={
          isDraggableKind
            ? (e) => {
                e.dataTransfer.setData(
                  'application/annotation-kind',
                  JSON.stringify(options ? { kind, ...options } : { kind })
                );
                e.dataTransfer.effectAllowed = 'move';
              }
            : undefined
        }
        // Coarse pointer (touch/stylus): Pointer Events stand in for
        // HTML5 drag, which never fires there.
        onPointerDown={
          isDraggableKind && touch
            ? (e) => handlePointerDown(e, kind, options, glyph, itemKey)
            : undefined
        }
      >
        <span className="annotation-toolbox-item-glyph" aria-hidden="true">
          {glyph}
        </span>
        {/* Always rendered; CSS reveals it only where hover is
            unavailable, so the tooltip's job is covered on touch
            without the label reintroducing the uneven rows. */}
        <span className="annotation-toolbox-item-label">{lbl[labelKey]}</span>
      </button>
    );
  };

  // The collapsed shape slot: one cell standing in for every SHAPE_VARIANTS
  // entry (task-annotation-shapes-under-one-toolbox-slot). Its main button
  // behaves exactly like a plain item above (click/drag creates the CURRENT
  // shape) but shows whichever variant `useToolSlotSelection` currently
  // holds; a second, independently focusable corner button opens
  // ToolSlotPicker, and so does a right-click on the main button. Both paths
  // — plus Enter/Space on the corner button, which needs no extra wiring
  // since it is a real `<button>` — are the owner-directed accessible
  // affordances (2026-08-26): mouse, touch and keyboard each get a
  // discoverable, non-gesture-only way to reach the picker.
  const renderShapeSlot = () => {
    const variant =
      SHAPE_VARIANTS.find((candidate) => candidate.shape === currentShape) ?? SHAPE_VARIANTS[0];
    const options = { shape: variant.shape };
    const pickerOptions = SHAPE_VARIANTS.map((candidate) => ({
      key: candidate.shape,
      glyph: candidate.glyph,
      label: lbl[candidate.labelKey],
    }));

    return (
      <div className="annotation-toolbox-slot" key="shape-slot" ref={shapeSlotRef}>
        <button
          type="button"
          className="annotation-toolbox-item annotation-toolbox-item--draggable"
          onClick={() => {
            if (consumeSuppressedClick(SHAPE_SLOT_ITEM_KEY)) return;
            setHovered(null);
            onCreate?.('shape', options);
          }}
          aria-label={lbl[variant.labelKey]}
          aria-describedby={hovered?.key === variant.labelKey ? TOOLTIP_ID : undefined}
          onMouseEnter={(e) => showTip(e, variant.labelKey)}
          onMouseLeave={() => setHovered(null)}
          onFocus={(e) => showTip(e, variant.labelKey)}
          onBlur={() => setHovered(null)}
          // Right-click is a mouse-only, invisible-until-tried path — real,
          // but never the ONLY one (owner direction 2026-08-26). The wrapper
          // canvas already suppresses the native browser context menu
          // (GraphCanvas's own contextmenu listener on reactFlowWrapper),
          // and this slot sits outside ReactFlow's own pane element, so
          // opening the picker here never collides with the canvas's
          // node/edge/pane context menus.
          onContextMenu={(e) => {
            e.preventDefault();
            setShapePickerOpen(true);
          }}
          draggable={!touch}
          onDragStart={(e) => {
            e.dataTransfer.setData(
              'application/annotation-kind',
              JSON.stringify({ kind: 'shape', ...options })
            );
            e.dataTransfer.effectAllowed = 'move';
          }}
          onPointerDown={
            touch
              ? (e) => handlePointerDown(e, 'shape', options, variant.glyph, SHAPE_SLOT_ITEM_KEY)
              : undefined
          }
        >
          <span className="annotation-toolbox-item-glyph" aria-hidden="true">
            {variant.glyph}
          </span>
          <span className="annotation-toolbox-item-label">{lbl[variant.labelKey]}</span>
        </button>
        <button
          ref={shapeCornerButtonRef}
          type="button"
          className="annotation-toolbox-slot-corner"
          aria-label={lbl.shapePickerOpen}
          aria-haspopup="true"
          aria-expanded={shapePickerOpen}
          onClick={() => setShapePickerOpen(true)}
        >
          <span aria-hidden="true">▾</span>
        </button>
        {shapePickerOpen && (
          <ToolSlotPicker
            anchorRef={shapeSlotRef}
            returnFocusRef={shapeCornerButtonRef}
            ariaLabel={lbl.shapePicker}
            options={pickerOptions}
            currentKey={variant.shape}
            onSelect={(key) => {
              setCurrentShape(key);
              setShapePickerOpen(false);
            }}
            onClose={() => setShapePickerOpen(false)}
          />
        )}
      </div>
    );
  };

  return (
    <div
      className={`annotation-toolbox${expanded ? ' annotation-toolbox--expanded' : ''}${
        compact ? ' annotation-toolbox--compact' : ''
      }${touch ? ' annotation-toolbox--touch' : ''}`}
      data-testid="annotation-toolbox"
      role="toolbar"
      aria-label={lbl.toggleExpand}
    >
      <button
        type="button"
        className="annotation-toolbox-toggle"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-label={expanded ? lbl.toggleCollapse : lbl.toggleExpand}
      >
        <span className="annotation-toolbox-toggle-glyph" aria-hidden="true">
          {expanded ? '▾' : '▴'}
        </span>
        <span className="annotation-toolbox-toggle-label">{lbl.toggleExpand}</span>
      </button>

      {expanded && (
        <div className="annotation-toolbox-items">
          {TOOLBOX_ITEMS_LEADING.map(renderToolboxItem)}
          {renderShapeSlot()}
          {TOOLBOX_ITEMS_TRAILING.map(renderToolboxItem)}
        </div>
      )}

      {expanded &&
        hovered &&
        createPortal(
          <div
            id={TOOLTIP_ID}
            className="annotation-toolbox-tooltip"
            role="tooltip"
            style={{ left: hovered.left, top: hovered.top }}
          >
            {lbl[`${hovered.key}Hint`] || lbl[hovered.key]}
          </div>,
          document.body
        )}

      {/* The coarse-pointer drag's "in hand" visual — a mouse drag gets this
          for free from the browser's own drag image, so this only ever
          renders on the touch/stylus path. pointer-events: none so it can
          never itself become a drop target or intercept the gesture it is
          only supposed to illustrate. */}
      {dragGhost &&
        createPortal(
          <div
            className="annotation-toolbox-drag-ghost"
            style={{ left: dragGhost.left, top: dragGhost.top }}
            aria-hidden="true"
          >
            {dragGhost.glyph}
          </div>,
          document.body
        )}
    </div>
  );
}

export default AnnotationToolbox;
