import { useState } from 'react';
import { createPortal } from 'react-dom';
import './AnnotationToolbox.css';

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
  { kind: 'image', glyph: '🖼️', labelKey: 'image' },
  // Unlike every other item, clicking this one does not create an annotation
  // immediately — GraphCanvas's onCreate special-cases 'freehand' to arm a
  // one-stroke drawing mode instead (docs/ANNOTATION_CONTRACT.md's "Physical
  // device acceptance" gap: this is the GUI creation entry point stylus input
  // needed). `activeKind` reflects that armed state back onto this button.
  { kind: 'freehand', glyph: '✏️', labelKey: 'freehand' },
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
 */
function AnnotationToolbox({
  onCreate,
  labels = {},
  compact = false,
  touch = false,
  activeKind = null,
}) {
  const [expanded, setExpanded] = useState(false);
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
          {TOOLBOX_ITEMS.map(({ kind, glyph, labelKey, shape }) => (
            <button
              key={`${kind}-${labelKey}`}
              type="button"
              className={`annotation-toolbox-item${
                activeKind === kind ? ' annotation-toolbox-item--active' : ''
              }`}
              onClick={() => {
                // A tap on a touch device fires the emulated mouseenter but no
                // mouseleave until the user touches something else, so without
                // this the tooltip stays on screen after the item is used.
                setHovered(null);
                onCreate?.(kind, shape ? { shape } : undefined);
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
            >
              <span className="annotation-toolbox-item-glyph" aria-hidden="true">
                {glyph}
              </span>
              {/* Always rendered; CSS reveals it only where hover is
                  unavailable, so the tooltip's job is covered on touch
                  without the label reintroducing the uneven rows. */}
              <span className="annotation-toolbox-item-label">{lbl[labelKey]}</span>
            </button>
          ))}
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
    </div>
  );
}

export default AnnotationToolbox;
