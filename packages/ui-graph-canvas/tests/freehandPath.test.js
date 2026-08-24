import { describe, it, expect } from 'vitest';
import {
  reduceFreehandPoints,
  pointsToPathData,
  buildFreehandPath,
  hasPressureData,
  buildPressureSegments,
} from '../src/utils/freehandPath';

// A "jittery" hand-drawn line: an L-shaped stroke (roughly horizontal, then
// roughly vertical) with small perpendicular wobble at every sample — the
// shape smoothing/simplification needs to tell apart (keep the sharp elbow,
// drop the wobble) as smoothing rises. The elbow's deviation from the
// straight start-to-end line is kept well above MAX_SIMPLIFY_EPSILON so it
// survives simplification even at smoothing=1.
function jitteryLine() {
  const points = [];
  for (let x = 0; x <= 40; x += 2) {
    points.push({ x, y: x % 4 === 0 ? 0.5 : -0.5 });
  }
  for (let y = 2; y <= 40; y += 2) {
    points.push({ x: 40 + (y % 4 === 0 ? 0.5 : -0.5), y });
  }
  return points;
}

describe('reduceFreehandPoints', () => {
  it('keeps every distinct point at smoothing=0 (raw path)', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 1, y: 1 },
      { x: 2, y: 0 },
    ];
    expect(reduceFreehandPoints(points, 0)).toEqual(points);
  });

  it('drops consecutive duplicate samples even at smoothing=0', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 0, y: 0 },
      { x: 0.001, y: 0.001 },
      { x: 5, y: 5 },
    ];
    expect(reduceFreehandPoints(points, 0)).toEqual([
      { x: 0, y: 0 },
      { x: 5, y: 5 },
    ]);
  });

  it('reduces the point count more as smoothing increases, on a jittery input', () => {
    const points = jitteryLine();
    const raw = reduceFreehandPoints(points, 0);
    const light = reduceFreehandPoints(points, 0.3);
    const heavy = reduceFreehandPoints(points, 1);
    expect(raw.length).toBe(points.length);
    expect(light.length).toBeLessThan(raw.length);
    expect(heavy.length).toBeLessThanOrEqual(light.length);
    // The real corner (around x=40) must survive even heavy simplification —
    // smoothing should not flatten the stroke into a single straight line.
    expect(heavy.length).toBeGreaterThanOrEqual(3);
  });

  it('is deterministic: same input and smoothing always reduces to the same output', () => {
    const points = jitteryLine();
    expect(reduceFreehandPoints(points, 0.5)).toEqual(reduceFreehandPoints(points, 0.5));
  });

  it('clamps out-of-range smoothing into [0, 1]', () => {
    const points = jitteryLine();
    expect(reduceFreehandPoints(points, 5)).toEqual(reduceFreehandPoints(points, 1));
    expect(reduceFreehandPoints(points, -3)).toEqual(reduceFreehandPoints(points, 0));
  });

  it('handles empty and single-point input safely', () => {
    expect(reduceFreehandPoints([], 1)).toEqual([]);
    expect(reduceFreehandPoints([{ x: 1, y: 2 }], 1)).toEqual([{ x: 1, y: 2 }]);
  });
});

describe('pointsToPathData', () => {
  it('returns an empty string for no points', () => {
    expect(pointsToPathData([])).toBe('');
  });

  it('builds a degenerate M/L segment for a single point', () => {
    expect(pointsToPathData([{ x: 3, y: 4 }])).toBe('M 3 4 L 3 4');
  });

  it('builds a straight M/L segment for two points', () => {
    expect(
      pointsToPathData([
        { x: 0, y: 0 },
        { x: 10, y: 0 },
      ])
    ).toBe('M 0 0 L 10 0');
  });

  it('builds quadratic-through-midpoint curve commands for 3+ points', () => {
    const d = pointsToPathData([
      { x: 0, y: 0 },
      { x: 10, y: 10 },
      { x: 20, y: 0 },
    ]);
    expect(d).toBe('M 0 0 Q 10 10 15 5 L 20 0');
  });

  it('only ever emits finite numbers', () => {
    const points = jitteryLine();
    const d = pointsToPathData(points);
    const numbers = d.match(/-?\d+(\.\d+)?/g).map(Number);
    expect(numbers.every((n) => Number.isFinite(n))).toBe(true);
  });
});

describe('buildFreehandPath', () => {
  it('combines reduction and path-building, and is deterministic', () => {
    const points = jitteryLine();
    const a = buildFreehandPath(points, 0.4);
    const b = buildFreehandPath(points, 0.4);
    expect(a).toEqual(b);
    expect(a.d.startsWith('M ')).toBe(true);
    expect(a.points.length).toBeLessThan(points.length);
  });

  it('keeps the minimal raw path at smoothing=0', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 5, y: 5 },
      { x: 10, y: 0 },
    ];
    const { points: reduced, d } = buildFreehandPath(points, 0);
    expect(reduced).toEqual(points);
    expect(d).toBe('M 0 0 Q 5 5 7.5 2.5 L 10 0');
  });
});

describe('hasPressureData', () => {
  it('is false for points with no pressure sample at all', () => {
    expect(
      hasPressureData([
        { x: 0, y: 0 },
        { x: 1, y: 1 },
      ])
    ).toBe(false);
  });

  it('is true when at least one point carries a finite pressure', () => {
    expect(
      hasPressureData([
        { x: 0, y: 0 },
        { x: 1, y: 1, pressure: 0.6 },
      ])
    ).toBe(true);
  });

  it('is false for empty or non-array input', () => {
    expect(hasPressureData([])).toBe(false);
    expect(hasPressureData(null)).toBe(false);
    expect(hasPressureData(undefined)).toBe(false);
  });
});

describe('buildPressureSegments', () => {
  it('returns one fewer segment than reduced points, each a straight M/L pair', () => {
    const points = [
      { x: 0, y: 0, pressure: 0.2 },
      { x: 10, y: 0, pressure: 0.8 },
      { x: 20, y: 0, pressure: 0.5 },
    ];
    const segments = buildPressureSegments(points, 0, 2);
    expect(segments).toHaveLength(2);
    expect(segments[0].d).toBe('M 0 0 L 10 0');
    expect(segments[1].d).toBe('M 10 0 L 20 0');
  });

  it('scales width up for higher pressure and down for lower pressure, around baseWidth', () => {
    const light = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 0.05 },
        { x: 10, y: 0, pressure: 0.05 },
      ],
      0,
      2
    );
    const heavy = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 0.95 },
        { x: 10, y: 0, pressure: 0.95 },
      ],
      0,
      2
    );
    expect(light[0].width).toBeLessThan(2);
    expect(heavy[0].width).toBeGreaterThan(2);
    expect(light[0].width).toBeGreaterThan(0);
  });

  it('averages the two endpoints of a segment when both carry pressure', () => {
    const segments = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 0 },
        { x: 10, y: 0, pressure: 1 },
      ],
      0,
      2
    );
    // pressure 0 endpoint alone would scale toward the min factor, pressure 1
    // alone toward the max factor — the segment's own width must sit between
    // what either endpoint would produce on its own.
    const soleZero = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 0 },
        { x: 10, y: 0, pressure: 0 },
      ],
      0,
      2
    )[0].width;
    const soleOne = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 1 },
        { x: 10, y: 0, pressure: 1 },
      ],
      0,
      2
    )[0].width;
    expect(segments[0].width).toBeGreaterThan(soleZero);
    expect(segments[0].width).toBeLessThan(soleOne);
  });

  it('falls back to the single point that has pressure when only one endpoint carries a sample', () => {
    const segments = buildPressureSegments(
      [
        { x: 0, y: 0, pressure: 0.9 },
        { x: 10, y: 0 },
      ],
      0,
      2
    );
    expect(segments[0].width).toBeGreaterThan(2);
  });

  it('falls back to baseWidth for a segment with no pressure data at all', () => {
    const segments = buildPressureSegments(
      [
        { x: 0, y: 0 },
        { x: 10, y: 0 },
      ],
      0,
      3
    );
    expect(segments[0].width).toBe(3);
  });

  it('handles a single-point stroke as one zero-length dot segment', () => {
    const segments = buildPressureSegments([{ x: 5, y: 5, pressure: 0.5 }], 0, 2);
    expect(segments).toEqual([{ d: 'M 5 5 L 5 5', width: expect.any(Number) }]);
  });

  it('returns an empty array for no points', () => {
    expect(buildPressureSegments([], 0, 2)).toEqual([]);
  });

  it('is deterministic', () => {
    const points = [
      { x: 0, y: 0, pressure: 0.3 },
      { x: 4, y: 6, pressure: 0.7 },
      { x: 9, y: 1, pressure: 0.4 },
    ];
    expect(buildPressureSegments(points, 0.4, 2)).toEqual(buildPressureSegments(points, 0.4, 2));
  });
});
