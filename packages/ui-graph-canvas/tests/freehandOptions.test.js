import { describe, it, expect } from 'vitest';
import {
  DEFAULT_FREEHAND_OPTIONS,
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
