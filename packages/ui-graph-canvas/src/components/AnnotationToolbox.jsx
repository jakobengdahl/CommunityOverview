import { useState } from 'react';
import './AnnotationToolbox.css';

// The v1 annotation kinds this toolbox can create today. Each 'shape' entry
// carries the `shape` option it creates; every variant the model accepts now
// renders as its own visual (SHAPE_STYLES in GenericAnnotationNode.jsx), so
// all six are offered. See docs/ANNOTATION_CONTRACT.md's acceptance matrix for
// the per-type-editor gaps this still doesn't close - creating a shape from
// here is not the same as being able to change an existing one's subtype.
const TOOLBOX_ITEMS = [
  { kind: 'note', glyph: '📝', labelKey: 'note' },
  { kind: 'text', glyph: 'T', labelKey: 'text' },
  { kind: 'label', glyph: '🏷️', labelKey: 'label' },
  { kind: 'frame', glyph: '▢', labelKey: 'frame' },
  { kind: 'shape', glyph: '▭', labelKey: 'shapeRectangle', shape: 'rectangle' },
  { kind: 'shape', glyph: '◯', labelKey: 'shapeCircle', shape: 'circle' },
  { kind: 'shape', glyph: '△', labelKey: 'shapeTriangle', shape: 'triangle' },
  { kind: 'shape', glyph: '◇', labelKey: 'shapeRhombus', shape: 'rhombus' },
  { kind: 'shape', glyph: '⬡', labelKey: 'shapeHexagon', shape: 'hexagon' },
  { kind: 'shape', glyph: '➜', labelKey: 'shapeProcessArrow', shape: 'process_arrow' },
  { kind: 'image', glyph: '🖼️', labelKey: 'image' },
];

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
 * It creates note/text/label/frame, every shape variant, and image (which
 * opens a file picker rather than adding a node directly — the host's
 * onCreate handles that distinction; see GraphCanvas's onImageIngest).
 * icon/vote_dot/freehand still have no creation entry point yet (tracked
 * gaps, not silently downgraded here) and are left for follow-up work, per
 * the acceptance matrix's per-type rows.
 */
function AnnotationToolbox({ onCreate, labels = {}, compact = false }) {
  const [expanded, setExpanded] = useState(false);

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
    image: 'Image',
    ...labels,
  };

  return (
    <div
      className={`annotation-toolbox${expanded ? ' annotation-toolbox--expanded' : ''}${
        compact ? ' annotation-toolbox--compact' : ''
      }`}
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
              className="annotation-toolbox-item"
              onClick={() => onCreate?.(kind, shape ? { shape } : undefined)}
              aria-label={lbl[labelKey]}
              title={lbl[labelKey]}
            >
              <span className="annotation-toolbox-item-glyph" aria-hidden="true">
                {glyph}
              </span>
              <span className="annotation-toolbox-item-label">{lbl[labelKey]}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default AnnotationToolbox;
