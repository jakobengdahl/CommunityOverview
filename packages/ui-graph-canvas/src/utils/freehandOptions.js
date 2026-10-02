/**
 * The stroke options a freehand annotation offers: shared by the property
 * editor on an existing stroke (FreehandAnnotationNode) and the pre-draw
 * option picker on the toolbox's pen item, so both list the same choices and
 * a stroke drawn with a picked option can be re-edited to any other.
 */

export const DEFAULT_FREEHAND_COLOR = '#111827';
export const DEFAULT_FREEHAND_STROKE_WIDTH = 2;
export const DEFAULT_FREEHAND_SMOOTHING = 0.3;
export const DEFAULT_FREEHAND_OPACITY = 1;

export const FREEHAND_COLORS = [
  DEFAULT_FREEHAND_COLOR,
  '#e6edf3',
  '#FDE047',
  '#4ADE80',
  '#60A5FA',
  '#F472B6',
  '#FB923C',
];
export const FREEHAND_WIDTHS = [1.5, 2, 3, 5, 8];
export const FREEHAND_SMOOTHING_LEVELS = [0, 0.3, 0.6, 1];
export const FREEHAND_OPACITY_LEVELS = [0.3, 0.5, 0.75, 1];

export const DEFAULT_FREEHAND_OPTIONS = Object.freeze({
  color: DEFAULT_FREEHAND_COLOR,
  strokeWidth: DEFAULT_FREEHAND_STROKE_WIDTH,
  smoothing: DEFAULT_FREEHAND_SMOOTHING,
  opacity: DEFAULT_FREEHAND_OPACITY,
});

/**
 * Coerce an untrusted value (parsed localStorage, a host prop) into a full
 * options object: each field must be one of the offered choices, otherwise it
 * falls back to its default. Never throws.
 */
export function normalizeFreehandOptions(value) {
  const source = value && typeof value === 'object' ? value : {};
  const pick = (list, candidate, fallback) => (list.includes(candidate) ? candidate : fallback);
  return {
    color: pick(FREEHAND_COLORS, source.color, DEFAULT_FREEHAND_OPTIONS.color),
    strokeWidth: pick(FREEHAND_WIDTHS, source.strokeWidth, DEFAULT_FREEHAND_OPTIONS.strokeWidth),
    smoothing: pick(
      FREEHAND_SMOOTHING_LEVELS,
      source.smoothing,
      DEFAULT_FREEHAND_OPTIONS.smoothing
    ),
    opacity: pick(FREEHAND_OPACITY_LEVELS, source.opacity, DEFAULT_FREEHAND_OPTIONS.opacity),
  };
}
