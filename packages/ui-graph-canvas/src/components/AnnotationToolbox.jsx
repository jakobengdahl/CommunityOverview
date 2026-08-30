import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useToolSlotSelection } from '../hooks/useToolSlotSelection';
import {
  ANNOTATION_ICONS,
  DEFAULT_ANNOTATION_ICON,
  resolveAnnotationIcon,
} from '../utils/annotationIcons';
import { GENERIC_ANNOTATION_COLORS, DEFAULT_GENERIC_COLOR } from '../utils/annotations';
import { ToolSlotPicker } from './ToolSlotPicker';
import './AnnotationToolbox.css';

// How far the pointer has to travel from the item before a press turns into a
// drag rather than staying a click/tap. Keeps a plain tap (no measurable
// movement) from ever reaching the drag path.
const DRAG_THRESHOLD_PX = 6;

// The toolbox items that precede the shape slot. `frame` used to be a
// distinct, separate item here (a plain box with no fill); it is now folded
// into the shape kind (task-annotation-merge-frame-into-shape-rectangle) —
// creating a shape and setting its fill to transparent, with a coloured
// border, covers what the standalone `frame` button used to make in one
// click. There is no longer a dedicated toolbox entry for it.
const TOOLBOX_ITEMS_LEADING = [
  // The resting tool (task-annotation-tool-modes). Not a creation item at
  // all: arming it is how a user goes back to plain
  // select/drag/marquee on the canvas after using a placement tool, which is
  // otherwise only reachable by re-tapping the armed tool to disarm it — a
  // gesture nothing on screen advertises. Deliberately first in the row, the
  // position every drawing app puts the arrow in. `draggable: false` because
  // there is no object to carry.
  {
    kind: 'select',
    glyph: { kind: 'toolbox-glyph', name: 'select' },
    labelKey: 'select',
    draggable: false,
  },
  { kind: 'note', glyph: { kind: 'toolbox-glyph', name: 'note' }, labelKey: 'note' },
  { kind: 'text', glyph: { kind: 'toolbox-glyph', name: 'text', text: 'T' }, labelKey: 'text' },
  { kind: 'label', glyph: { kind: 'toolbox-glyph', name: 'label' }, labelKey: 'label' },
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
  {
    shape: 'process_arrow',
    glyph: { kind: 'shape-swatch', shape: 'process_arrow' },
    labelKey: 'shapeProcessArrow',
  },
];
const SHAPE_VARIANT_KEYS = SHAPE_VARIANTS.map((variant) => variant.shape);
const DEFAULT_SHAPE = SHAPE_VARIANTS[0].shape;
// Namespaced and versioned by slot identity, not by content — a personal
// "which shape did I use last" preference, not something the shared session
// or graph should ever see. Other collapsed slots use their own keys.
const SHAPE_SLOT_STORAGE_KEY = 'communityoverview:annotation-toolbox:shape-slot';
// Stable regardless of which shape is currently selected, so the drag-click
// suppression queue below (keyed by itemKey) and React's reconciliation both
// keep treating the slot as the same item across a shape change mid-gesture.
const SHAPE_SLOT_ITEM_KEY = 'shape-slot';

const ICON_VARIANTS = Object.keys(ANNOTATION_ICONS).map((icon) => ({
  icon,
  glyph: { kind: 'annotation-icon', icon },
  label: icon.replace(/_/g, ' '),
}));
const ICON_VARIANT_KEYS = ICON_VARIANTS.map((variant) => variant.icon);
const ICON_SLOT_STORAGE_KEY = 'communityoverview:annotation-toolbox:icon-slot';
const ICON_SLOT_ITEM_KEY = 'icon-slot';

// Every colour a vote dot can be created in — the same list its property
// editor offers afterwards (GENERIC_ANNOTATION_COLORS), so the two cannot
// drift apart.
const VOTE_DOT_VARIANTS = GENERIC_ANNOTATION_COLORS.map((color) => ({
  color,
  glyph: { kind: 'vote-dot-swatch', color },
  label: color,
}));
const VOTE_DOT_VARIANT_KEYS = VOTE_DOT_VARIANTS.map((variant) => variant.color);
const VOTE_DOT_SLOT_STORAGE_KEY = 'communityoverview:annotation-toolbox:vote-dot-slot';
const VOTE_DOT_SLOT_ITEM_KEY = 'vote-dot-slot';

const TOOLBOX_ITEMS_TRAILING = [
  // Opens a file picker rather than creating an object directly — there is
  // nothing to pick up and carry, so this item stays click-only (see
  // `isDraggableKind` below).
  {
    kind: 'image',
    glyph: { kind: 'toolbox-glyph', name: 'image' },
    labelKey: 'image',
    draggable: false,
  },
  // Unlike every other item, clicking this one does not create an annotation
  // immediately — GraphCanvas's onCreate special-cases 'freehand' to arm a
  // one-stroke drawing mode instead (docs/ANNOTATION_CONTRACT.md's "Physical
  // device acceptance" gap: this is the GUI creation entry point stylus input
  // needed). `activeKind` reflects that armed state back onto this button.
  // Arming a mode isn't an object to carry either, so it stays click-only too.
  {
    kind: 'freehand',
    glyph: { kind: 'toolbox-glyph', name: 'freehand' },
    labelKey: 'freehand',
    draggable: false,
  },
  // Erase-by-drag (task-annotation-tool-modes): dragging over an annotation
  // deletes it, dragging over a graph node or edge hides it. Like `select`
  // it arms a mode rather than creating anything, so it is click-only. It is
  // also what a stylus's inverted (eraser) tip does implicitly, without
  // arming anything — see GraphCanvas's pointer handling.
  {
    kind: 'eraser',
    glyph: { kind: 'toolbox-glyph', name: 'eraser' },
    labelKey: 'eraser',
    draggable: false,
  },
];

// Tools that arm a mode instead of producing an object. They never drag-create
// and never open a picker; GraphCanvas owns what each one then does.
const MODE_KINDS = new Set(['select', 'eraser', 'freehand']);

const TOOLTIP_ID = 'annotation-toolbox-tooltip';

function renderGlyph(glyph) {
  // The vote-dot slot previews the colour it will create in, so the toolbox
  // answers "which colour am I about to place?" without opening the picker —
  // the same job the shape slot's outline and the icon slot's glyph do.
  if (glyph?.kind === 'vote-dot-swatch') {
    return (
      <span
        className="annotation-toolbox-visual annotation-toolbox-visual--vote-dot"
        style={{ backgroundColor: glyph.color }}
        aria-hidden="true"
      />
    );
  }
  if (glyph?.kind === 'shape-swatch') {
    return (
      <span
        className={`annotation-toolbox-shape-glyph annotation-toolbox-shape-glyph--${glyph.shape.replaceAll(
          '_',
          '-'
        )}`}
        aria-hidden="true"
      />
    );
  }
  if (glyph?.kind === 'annotation-icon') {
    const icon = resolveAnnotationIcon(glyph.icon);
    return (
      <span
        className={`annotation-toolbox-icon-glyph${
          icon.isGlyph ? '' : ' annotation-toolbox-icon-glyph--abbreviated'
        }`}
        aria-hidden="true"
      >
        {icon.text}
      </span>
    );
  }
  if (glyph?.kind === 'toolbox-glyph') {
    return (
      <span
        className={`annotation-toolbox-visual annotation-toolbox-visual--${glyph.name}`}
        aria-hidden="true"
      >
        {glyph.text}
      </span>
    );
  }
  return glyph;
}

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
 * It creates note/text/label, a shape (via the collapsed shape slot,
 * see `renderShapeSlot` below), an icon (via the collapsed icon slot, see
 * `renderIconSlot` below), vote_dot (a plain coloured dot — task-annotation-
 * vote-dot-simplify), and image (which opens a file
 * picker rather than adding a node directly — the host's onCreate handles
 * that distinction; see GraphCanvas's onImageIngest). The icon slot
 * creates whichever icon is currently selected, using the same vocabulary the
 * icon annotation's right-click property editor offers after creation.
 * `activeKind` (currently only meaningful for 'freehand') marks that item as
 * pressed while its armed drawing mode is active, so a user mid-stroke can
 * see which tool is live.
 *
 * The shape slot replaces what used to be six separate shape-variant buttons
 * — half the toolbox, which is why it used to wrap to a second row — with one
 * slot that shows the currently selected shape and remembers it in localStorage
 * (useToolSlotSelection; a personal tool preference, not shared session
 * state). A small corner button on the slot, right-clicking the slot, or
 * (per the owner's explicit accessibility direction) Enter/Space on that
 * same corner button all open a fold-out picker (ToolSlotPicker) listing
 * every shape; picking one becomes the new current shape. The corner button
 * is a second real focusable element, not just an invisible gesture, so
 * mouse, touch and keyboard each have a discoverable path to the picker —
 * see `renderShapeSlot` for the concrete wiring. Both pieces
 * (useToolSlotSelection, ToolSlotPicker) are shared by the shape and icon
 * slots so the two collapsed tool families behave the same way.
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
  onSelectTool,
  onDragCreate,
  labels = {},
  compact = false,
  touch = false,
  activeKind = null,
  // 'toolbar' (default): the collapsible pill this component has always
  // been, own toggle button, starts collapsed. 'sheet': hosted inside a
  // dedicated BottomSheet (see GraphCanvas's annotationToolboxPortalContainer)
  // whose own header/close button is already the collapse affordance, so this
  // variant renders no toggle of its own and is always expanded - matching
  // FloatingToolbar's variant="sheet" (frontend/web), the same established
  // idiom for "the same component, laid out for a full-width mobile sheet
  // instead of a floating rail/pill".
  variant = 'toolbar',
}) {
  const isSheet = variant === 'sheet';
  const [expandedState, setExpandedState] = useState(false);
  const expanded = isSheet ? true : expandedState;
  const [currentShape, setCurrentShape] = useToolSlotSelection(
    SHAPE_SLOT_STORAGE_KEY,
    SHAPE_VARIANT_KEYS,
    DEFAULT_SHAPE
  );
  const [shapePickerOpen, setShapePickerOpen] = useState(false);
  const [currentIcon, setCurrentIcon] = useToolSlotSelection(
    ICON_SLOT_STORAGE_KEY,
    ICON_VARIANT_KEYS,
    DEFAULT_ANNOTATION_ICON
  );
  const [iconPickerOpen, setIconPickerOpen] = useState(false);
  const [currentVoteDotColor, setCurrentVoteDotColor] = useToolSlotSelection(
    VOTE_DOT_SLOT_STORAGE_KEY,
    VOTE_DOT_VARIANT_KEYS,
    DEFAULT_GENERIC_COLOR
  );
  const [voteDotPickerOpen, setVoteDotPickerOpen] = useState(false);
  const shapeSlotRef = useRef(null);
  const shapeCornerButtonRef = useRef(null);
  const iconSlotRef = useRef(null);
  const iconCornerButtonRef = useRef(null);
  const voteDotSlotRef = useRef(null);
  const voteDotCornerButtonRef = useRef(null);
  const voteDotMainButtonRef = useRef(null);
  // Lets the picker's onSelect callback focus the slot itself after a
  // selection, the same way the slot's own onClick already does — see
  // renderShapeSlot below.
  const shapeMainButtonRef = useRef(null);
  const iconMainButtonRef = useRef(null);
  // Collapsing the toolbox unmounts the items row (and the slot/corner
  // button inside it) out from under an open picker — close it in step
  // rather than leaving a dangling `shapePickerOpen: true` that a later
  // re-expand would resurrect with no button left to have opened it.
  useEffect(() => {
    if (!expanded) {
      setShapePickerOpen(false);
      setIconPickerOpen(false);
      setVoteDotPickerOpen(false);
    }
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
    iconPickerOpen: 'Choose an icon',
    iconPicker: 'Icons',
    voteDot: 'Vote dot',
    voteDotPickerOpen: 'Choose a vote dot colour',
    voteDotPicker: 'Vote dot colours',
    image: 'Image',
    freehand: 'Freehand',
    select: 'Select',
    eraser: 'Eraser',
    // What each item will add, shown on hover. Separate from the item's name
    // because the name has to stay short enough to be an accessible label and
    // a touch-mode caption, while this is allowed to say what happens.
    noteHint: 'Add a sticky note',
    textHint: 'Add a block of text',
    labelHint: 'Add a label or callout',
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
    selectHint: 'Select and move objects',
    eraserHint: 'Drag over objects to erase them',
    ...labels,
  };

  // Arming a tool is what a click does now (task-annotation-tool-modes):
  // the toolbox picks the tool, the canvas decides where the object goes.
  // A click used to drop the object at the viewport centre immediately,
  // which meant placing five vote dots was five round trips between the
  // toolbox and the canvas, each one landing in the same spot the last had
  // to be dragged out of. `onCreate` stays the fallback so a host that has
  // not adopted tool modes (and the drag-to-create path, which carries its
  // own position) behaves exactly as before.
  const activateTool = (kind, options) => {
    if (onSelectTool) onSelectTool(kind, options);
    else onCreate?.(kind, options);
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
    // A mode tool can never be dragged onto the canvas whatever its own entry
    // says — there is no object in hand to drop. Deriving it from MODE_KINDS
    // as well as the explicit flag keeps the two from drifting apart when a
    // tool is added.
    const isDraggableKind = draggable !== false && !MODE_KINDS.has(kind);
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
          activateTool(kind, options);
        }}
        aria-label={lbl[labelKey]}
        // Every item is now a tool that can be armed, not just the one
        // one-shot drawing mode, so the pressed state reports for all of
        // them rather than being special-cased to 'freehand'.
        aria-pressed={activeKind === kind}
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
          {renderGlyph(glyph)}
        </span>
        {/* Always rendered; CSS reveals it only where hover is
            unavailable, so the tooltip's job is covered on touch
            without the label reintroducing the uneven rows. */}
        <span className="annotation-toolbox-item-label">{lbl[labelKey]}</span>
      </button>
    );
  };

  // The collapsed shape slot: one cell standing in for every SHAPE_VARIANTS
  // entry. Its main button behaves exactly like a plain item above
  // (click/drag creates the CURRENT shape) but shows whichever variant
  // `useToolSlotSelection` currently holds; a second, independently focusable
  // corner button opens
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
          ref={shapeMainButtonRef}
          type="button"
          className="annotation-toolbox-item annotation-toolbox-item--draggable"
          // The picker floats above the toolbox, not over the main button,
          // and the button sits inside the picker's own anchor
          // (shapeSlotRef) — so ToolSlotPicker's outside-click check treats
          // an interaction here as "inside the anchor" and leaves it open
          // on its own. Close it explicitly on mousedown (covers both a
          // plain click and the start of an HTML5 drag — neither of which
          // fires this button's own onClick once a real drag completes, see
          // finishDrag's comment above) and again in onClick (the only path
          // a keyboard Enter/Space activation takes, since it never fires
          // mousedown/pointerdown at all). Left button only: mousedown
          // always precedes contextmenu for a real right-click, so without
          // this check, right-clicking an already-open picker would close
          // it here and then immediately reopen it from onContextMenu below
          // — a visible flicker that also resets whatever option had focus.
          onMouseDown={(event) => {
            if (event.button !== 0) return;
            setShapePickerOpen(false);
          }}
          onClick={(event) => {
            setShapePickerOpen(false);
            // Explicit rather than relying on the browser's own
            // click-focuses-the-button behaviour: WebKit (desktop Safari,
            // iOS Safari) does not focus a <button> on click/tap, so without
            // this ToolSlotPicker's cleanup effect would see focus as never
            // having moved and force it onto the corner button instead of
            // leaving it here, on the control the user actually activated.
            event.currentTarget.focus();
            if (consumeSuppressedClick(SHAPE_SLOT_ITEM_KEY)) return;
            setHovered(null);
            activateTool('shape', options);
          }}
          aria-label={lbl[variant.labelKey]}
          aria-pressed={activeKind === 'shape'}
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
              ? (e) => {
                  // Touch has no separate mousedown-vs-click split — this is
                  // the one entry point for both a tap and a drag, so the
                  // picker-close call above's touch-path equivalent lives
                  // here rather than being reachable from onMouseDown. Same
                  // primary-button guard as onMouseDown, for a stylus's
                  // secondary (barrel) button.
                  if (e.button === 0) setShapePickerOpen(false);
                  handlePointerDown(e, 'shape', options, variant.glyph, SHAPE_SLOT_ITEM_KEY);
                }
              : undefined
          }
        >
          <span className="annotation-toolbox-item-glyph" aria-hidden="true">
            {renderGlyph(variant.glyph)}
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
            renderGlyph={renderGlyph}
            onSelect={(key) => {
              setCurrentShape(key);
              setShapePickerOpen(false);
              // Picking a shape arms it (task-annotation-tool-modes). Choosing
              // "circle" from the picker and then drawing on the canvas only
              // to get whatever tool was armed before is the bug this closes:
              // the picker reads as "I want to draw this now", so it selects
              // the tool as well as the variant, exactly as clicking the slot
              // itself does.
              activateTool('shape', { shape: key });
              // Same reasoning as the main button's own onClick: without an
              // explicit focus() here, ToolSlotPicker's cleanup effect would
              // land focus on the corner button instead of the slot the
              // user just gave a new shape to — surprising right after
              // picking one, and it would silently turn a keyboard user's
              // next Enter/Space (expecting to create the shape they just
              // chose) into reopening the picker instead.
              shapeMainButtonRef.current?.focus();
            }}
            onClose={() => setShapePickerOpen(false)}
          />
        )}
      </div>
    );
  };

  const renderIconSlot = () => {
    const variant =
      ICON_VARIANTS.find((candidate) => candidate.icon === currentIcon) ?? ICON_VARIANTS[0];
    const options = { icon: variant.icon };
    const pickerOptions = ICON_VARIANTS.map((candidate) => ({
      key: candidate.icon,
      glyph: candidate.glyph,
      label: candidate.label,
    }));

    return (
      <div className="annotation-toolbox-slot" key="icon-slot" ref={iconSlotRef}>
        <button
          ref={iconMainButtonRef}
          type="button"
          className="annotation-toolbox-item annotation-toolbox-item--draggable"
          onMouseDown={(event) => {
            if (event.button !== 0) return;
            setIconPickerOpen(false);
          }}
          onClick={(event) => {
            setIconPickerOpen(false);
            event.currentTarget.focus();
            if (consumeSuppressedClick(ICON_SLOT_ITEM_KEY)) return;
            setHovered(null);
            activateTool('icon', options);
          }}
          aria-label={`${lbl.icon}: ${variant.label}`}
          aria-pressed={activeKind === 'icon'}
          aria-describedby={hovered?.key === 'icon' ? TOOLTIP_ID : undefined}
          onMouseEnter={(e) => showTip(e, 'icon')}
          onMouseLeave={() => setHovered(null)}
          onFocus={(e) => showTip(e, 'icon')}
          onBlur={() => setHovered(null)}
          onContextMenu={(e) => {
            e.preventDefault();
            setIconPickerOpen(true);
          }}
          draggable={!touch}
          onDragStart={(e) => {
            e.dataTransfer.setData(
              'application/annotation-kind',
              JSON.stringify({ kind: 'icon', ...options })
            );
            e.dataTransfer.effectAllowed = 'move';
          }}
          onPointerDown={
            touch
              ? (e) => {
                  if (e.button === 0) setIconPickerOpen(false);
                  handlePointerDown(e, 'icon', options, variant.glyph, ICON_SLOT_ITEM_KEY);
                }
              : undefined
          }
        >
          <span className="annotation-toolbox-item-glyph" aria-hidden="true">
            {renderGlyph(variant.glyph)}
          </span>
          <span className="annotation-toolbox-item-label">{lbl.icon}</span>
        </button>
        <button
          ref={iconCornerButtonRef}
          type="button"
          className="annotation-toolbox-slot-corner"
          aria-label={lbl.iconPickerOpen}
          aria-haspopup="true"
          aria-expanded={iconPickerOpen}
          onClick={() => setIconPickerOpen(true)}
        >
          <span aria-hidden="true">▾</span>
        </button>
        {iconPickerOpen && (
          <ToolSlotPicker
            anchorRef={iconSlotRef}
            returnFocusRef={iconCornerButtonRef}
            ariaLabel={lbl.iconPicker}
            options={pickerOptions}
            currentKey={variant.icon}
            renderGlyph={renderGlyph}
            onSelect={(key) => {
              setCurrentIcon(key);
              setIconPickerOpen(false);
              // Same reasoning as the shape picker's onSelect above: picking
              // the variant also arms the tool that draws it.
              activateTool('icon', { icon: key });
              iconMainButtonRef.current?.focus();
            }}
            onClose={() => setIconPickerOpen(false)}
          />
        )}
      </div>
    );
  };

  // The vote-dot slot: same collapsed-slot pattern as shape and icon, but the
  // variant IS the colour. A vote dot has no other property worth choosing at
  // creation time, and picking the colour afterwards through the property
  // editor meant every dot was placed in the default grey first — laborious
  // for the one annotation kind people place many of in a row.
  const renderVoteDotSlot = () => {
    const variant =
      VOTE_DOT_VARIANTS.find((candidate) => candidate.color === currentVoteDotColor) ??
      VOTE_DOT_VARIANTS[0];
    const options = { color: variant.color };
    const pickerOptions = VOTE_DOT_VARIANTS.map((candidate) => ({
      key: candidate.color,
      glyph: candidate.glyph,
      label: candidate.label,
    }));

    return (
      <div className="annotation-toolbox-slot" key="vote-dot-slot" ref={voteDotSlotRef}>
        <button
          ref={voteDotMainButtonRef}
          type="button"
          className="annotation-toolbox-item annotation-toolbox-item--draggable"
          onMouseDown={(event) => {
            if (event.button !== 0) return;
            setVoteDotPickerOpen(false);
          }}
          onClick={(event) => {
            setVoteDotPickerOpen(false);
            event.currentTarget.focus();
            if (consumeSuppressedClick(VOTE_DOT_SLOT_ITEM_KEY)) return;
            setHovered(null);
            activateTool('vote_dot', options);
          }}
          aria-label={lbl.voteDot}
          aria-pressed={activeKind === 'vote_dot'}
          aria-describedby={hovered?.key === 'voteDot' ? TOOLTIP_ID : undefined}
          onMouseEnter={(e) => showTip(e, 'voteDot')}
          onMouseLeave={() => setHovered(null)}
          onFocus={(e) => showTip(e, 'voteDot')}
          onBlur={() => setHovered(null)}
          onContextMenu={(e) => {
            e.preventDefault();
            setVoteDotPickerOpen(true);
          }}
          draggable={!touch}
          onDragStart={(e) => {
            e.dataTransfer.setData(
              'application/annotation-kind',
              JSON.stringify({ kind: 'vote_dot', ...options })
            );
            e.dataTransfer.effectAllowed = 'move';
          }}
          onPointerDown={
            touch
              ? (e) => {
                  if (e.button === 0) setVoteDotPickerOpen(false);
                  handlePointerDown(e, 'vote_dot', options, variant.glyph, VOTE_DOT_SLOT_ITEM_KEY);
                }
              : undefined
          }
        >
          <span className="annotation-toolbox-item-glyph" aria-hidden="true">
            {renderGlyph(variant.glyph)}
          </span>
          <span className="annotation-toolbox-item-label">{lbl.voteDot}</span>
        </button>
        <button
          ref={voteDotCornerButtonRef}
          type="button"
          className="annotation-toolbox-slot-corner"
          aria-label={lbl.voteDotPickerOpen}
          aria-haspopup="true"
          aria-expanded={voteDotPickerOpen}
          onClick={() => setVoteDotPickerOpen(true)}
        >
          <span aria-hidden="true">▾</span>
        </button>
        {voteDotPickerOpen && (
          <ToolSlotPicker
            anchorRef={voteDotSlotRef}
            returnFocusRef={voteDotCornerButtonRef}
            ariaLabel={lbl.voteDotPicker}
            options={pickerOptions}
            currentKey={variant.color}
            renderGlyph={renderGlyph}
            onSelect={(key) => {
              setCurrentVoteDotColor(key);
              setVoteDotPickerOpen(false);
              activateTool('vote_dot', { color: key });
              voteDotMainButtonRef.current?.focus();
            }}
            onClose={() => setVoteDotPickerOpen(false)}
          />
        )}
      </div>
    );
  };

  return (
    <div
      className={`annotation-toolbox${expanded ? ' annotation-toolbox--expanded' : ''}${
        compact ? ' annotation-toolbox--compact' : ''
      }${touch ? ' annotation-toolbox--touch' : ''}${isSheet ? ' annotation-toolbox--sheet' : ''}`}
      data-testid="annotation-toolbox"
      role="toolbar"
      aria-label={lbl.toggleExpand}
    >
      {!isSheet && (
        <button
          type="button"
          className="annotation-toolbox-toggle"
          onClick={() => setExpandedState((v) => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? lbl.toggleCollapse : lbl.toggleExpand}
        >
          <span className="annotation-toolbox-toggle-glyph" aria-hidden="true">
            {expanded ? '▾' : '▴'}
          </span>
          <span className="annotation-toolbox-toggle-label">{lbl.toggleExpand}</span>
        </button>
      )}

      {expanded && (
        <div className="annotation-toolbox-items">
          {TOOLBOX_ITEMS_LEADING.map(renderToolboxItem)}
          {renderShapeSlot()}
          {renderIconSlot()}
          {renderVoteDotSlot()}
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
            {renderGlyph(dragGhost.glyph)}
          </div>,
          document.body
        )}
    </div>
  );
}

export default AnnotationToolbox;
