import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import './AnnotationToolbox.css';

// How far the pointer has to travel from the item before a press turns into a
// drag rather than staying a click/tap. Keeps a plain tap (no measurable
// movement) from ever reaching the drag path.
const DRAG_THRESHOLD_PX = 6;

// The v1 annotation kinds this toolbox can create today. Each 'shape' entry
// carries the `shape` option it creates; every variant the model accepts now
// renders as its own visual (SHAPE_STYLES in GenericAnnotationNode.jsx), so
// all six are offered. See docs/ANNOTATION_CONTRACT.md's acceptance matrix for
// the per-type-editor gaps this still doesn't close - creating a shape from
// here is not the same as being able to change an existing one's subtype.
const TOOLBOX_ITEMS = [
  { kind: 'note', glyph: '🗒️', labelKey: 'note' },
  { kind: 'text', glyph: 'T', labelKey: 'text' },
  { kind: 'label', glyph: '🏷️', labelKey: 'label' },
  { kind: 'frame', glyph: '▢', labelKey: 'frame' },
  { kind: 'shape', glyph: '▭', labelKey: 'shapeRectangle', shape: 'rectangle' },
  { kind: 'shape', glyph: '◯', labelKey: 'shapeCircle', shape: 'circle' },
  { kind: 'shape', glyph: '△', labelKey: 'shapeTriangle', shape: 'triangle' },
  { kind: 'shape', glyph: '◇', labelKey: 'shapeRhombus', shape: 'rhombus' },
  { kind: 'shape', glyph: '⬡', labelKey: 'shapeHexagon', shape: 'hexagon' },
  { kind: 'shape', glyph: '➜', labelKey: 'shapeProcessArrow', shape: 'process_arrow' },
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
 * It creates note/text/label/frame, every shape variant, icon, vote_dot, and
 * image (which opens a file picker rather than adding a node directly — the
 * host's onCreate handles that distinction; see GraphCanvas's onImageIngest).
 * icon/vote_dot each create with a fixed default (a generic glyph / a value
 * of 1 — see GraphCanvas's createAnnotation); an icon's right-click property
 * editor (GenericAnnotationNode.jsx) offers a picker over the full icon
 * vocabulary to change it after creation, the same pattern `shape`'s subtype
 * picker already established — there is no picker at creation time itself.
 * `activeKind` (currently only meaningful for 'freehand') marks that item as
 * pressed while its armed drawing mode is active, so a user mid-stroke can
 * see which tool is live.
 *
 * Every item that creates an object (all but `image` and `freehand`, see
 * `draggable: false` on those two in TOOLBOX_ITEMS) is also drag-to-create:
 * picking it up and moving away from the button is the creation gesture, and
 * releasing places the object where the pointer lands, rather than a click
 * always landing it at the viewport centre via `onCreate`. Two input paths
 * cover this, chosen by `touch` (the host's coarse-pointer signal, matching
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
 * events at all, so it is unaffected either way).
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
  // Sole purpose: swallow the click that follows a completed pointer drag
  // (pointerup fires, then the browser's synthetic click fires next in the
  // same task) so a drag never also creates a second, click-positioned copy.
  const suppressClickRef = useRef(false);
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
  const handlePointerDown = (event, kind, options, glyph) => {
    // Defensive: a stale suppress flag would otherwise be able to swallow
    // this fresh gesture's own eventual click (see the note near
    // `finishDrag` below about why the flag can outlive the drag it was set
    // for).
    suppressClickRef.current = false;

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
        suppressClickRef.current = true;
        onDragCreate?.(drag.kind, drag.options, {
          x: finishEvent.clientX,
          y: finishEvent.clientY,
        });
        // A real drag usually releases away from this button, so the
        // browser's synthetic click (if it fires one for the touch release
        // at all) never reaches this element's onClick to consume the flag —
        // it would otherwise stay set and silently swallow the next
        // unrelated tap on some other item. A click genuinely headed for
        // this button fires synchronously before this callback runs, so
        // clearing on the next tick never masks it.
        setTimeout(() => {
          suppressClickRef.current = false;
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
          {TOOLBOX_ITEMS.map(({ kind, glyph, labelKey, shape, draggable }) => {
            // image/freehand have no draggable object to create (a file
            // picker, an armed mode) — both stay click-only: no draggable
            // attribute, no dragstart handler, no pointer-drag handlers, no
            // grab cursor.
            const isDraggableKind = draggable !== false;
            const options = shape ? { shape } : undefined;
            return (
              <button
                key={`${kind}-${labelKey}`}
                type="button"
                className={`annotation-toolbox-item${
                  activeKind === kind ? ' annotation-toolbox-item--active' : ''
                }${isDraggableKind ? ' annotation-toolbox-item--draggable' : ''}`}
                onClick={() => {
                  // Swallow the click a completed pointer drag synthesizes
                  // right after its pointerup — onDragCreate already created
                  // the annotation, so this would otherwise create a second,
                  // click-positioned one.
                  if (suppressClickRef.current) {
                    suppressClickRef.current = false;
                    return;
                  }
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
                    ? (e) => handlePointerDown(e, kind, options, glyph)
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
          })}
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
