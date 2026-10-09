import { describe, it, expect } from 'vitest';
import {
  DEFAULT_FREEHAND_OPTIONS,
  DEFAULT_FREEHAND_STROKE_WIDTH,
  FREEHAND_COLORS,
  normalizeFreehandOptions,
} from '../src/utils/freehandOptions';

describe('normalizeFreehandOptions', () => {
  it('returns the defaults for missing or non-object input', () => {
    expect(normalizeFreehandOptions(undefined)).toEqual(DEFAULT_FREEHAND_OPTIONS);
    expect(normalizeFreehandOptions(null)).toEqual(DEFAULT_FREEHAND_OPTIONS);
    expect(normalizeFreehandOptions('x')).toEqual(DEFAULT_FREEHAND_OPTIONS);
  });

  it('keeps offered values and replaces anything else field by field', () => {
    expect(
      normalizeFreehandOptions({ color: FREEHAND_COLORS[2], strokeWidth: 99, smoothing: 1 })
    ).toEqual({
      ...DEFAULT_FREEHAND_OPTIONS,
      color: FREEHAND_COLORS[2],
      smoothing: 1,
    });
  });
});

describe('normalizeFreehandOptions corrupt values', () => {
  it.each([
    ['colour not in the palette', { color: '#123456' }, 'color'],
    ['colour of the wrong type', { color: 42 }, 'color'],
    ['colour null', { color: null }, 'color'],
    ['opacity not offered', { opacity: 0.1 }, 'opacity'],
    ['opacity as a numeric string', { opacity: '0.5' }, 'opacity'],
    ['opacity NaN', { opacity: NaN }, 'opacity'],
    ['opacity above 1', { opacity: 2 }, 'opacity'],
  ])('falls back to the default for %s', (_name, input, field) => {
    expect(normalizeFreehandOptions(input)[field]).toBe(DEFAULT_FREEHAND_OPTIONS[field]);
  });

  it('only resets the corrupt field and keeps its valid neighbours', () => {
    expect(normalizeFreehandOptions({ color: 'red', opacity: 0.5, strokeWidth: 8 })).toEqual({
      ...DEFAULT_FREEHAND_OPTIONS,
      opacity: 0.5,
      strokeWidth: 8,
    });
  });

  it('does not mutate its input and returns a fresh object', () => {
    const input = { color: 'red' };
    const out = normalizeFreehandOptions(input);
    expect(input).toEqual({ color: 'red' });
    expect(out).not.toBe(DEFAULT_FREEHAND_OPTIONS);
  });
});

describe('DEFAULT_FREEHAND_OPTIONS', () => {
  it('is frozen so a consumer cannot corrupt the shared defaults', () => {
    expect(Object.isFrozen(DEFAULT_FREEHAND_OPTIONS)).toBe(true);
    expect(DEFAULT_FREEHAND_OPTIONS.strokeWidth).toBe(DEFAULT_FREEHAND_STROKE_WIDTH);
  });
});
