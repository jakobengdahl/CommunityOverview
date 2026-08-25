import { describe, it, expect } from 'vitest';
import {
  reduceFreehandPoints,
  pointsToPathData,
  buildFreehandPath,
  hasPressureData,
  buildPressureSegments,
  smoothAnchors,
  segmentsFromCurvePoints,
} from '../src/utils/freehandPath';

// A "jittery" hand-drawn line: an L-shaped stroke (roughly horizontal, then
// roughly vertical) with small perpendicular wobble at every sample — the
// shape a curve algorithm needs to render smoothly without losing the elbow.
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

// A long stroke that crosses the decimation size threshold, to exercise the
// payload-size safety net independently of smoothing.
function longStraightLine(count) {
  const points = [];
  for (let i = 0; i < count; i++) {
    points.push({ x: i * 0.5, y: 0 });
  }
  return points;
}

describe('reduceFreehandPoints', () => {
  it('keeps every distinct point for an ordinary-length stroke', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 1, y: 1 },
      { x: 2, y: 0 },
    ];
    expect(reduceFreehandPoints(points)).toEqual(points);
  });

  it('drops consecutive duplicate samples', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 0, y: 0 },
      { x: 0.001, y: 0.001 },
      { x: 5, y: 5 },
    ];
    expect(reduceFreehandPoints(points)).toEqual([
      { x: 0, y: 0 },
      { x: 5, y: 5 },
    ]);
  });

  it('does not simplify a jittery stroke of ordinary length at any smoothing level', () => {
    // Decimation's degree is a fixed, payload-size-driven control, not a
    // smoothing-scaled one: an ordinary-sized stroke keeps every sample no
    // matter what smoothing the caller later applies when building a path.
    const points = jitteryLine();
    expect(reduceFreehandPoints(points, 0)).toEqual(points);
    expect(reduceFreehandPoints(points, 0.3)).toEqual(points);
    expect(reduceFreehandPoints(points, 1)).toEqual(points);
  });

  it('never simplifies at smoothing=0, no matter how long the stroke', () => {
    // "No smoothing" has always meant "every sampled point" — the payload
    // safety net below only ever engages once smoothing is requested at all.
    const long = longStraightLine(1000);
    expect(reduceFreehandPoints(long, 0)).toEqual(long);
  });

  it('simplifies a long stroke only once smoothing is requested, still gated by the payload-size threshold', () => {
    const short = longStraightLine(100);
    const long = longStraightLine(1000);
    expect(reduceFreehandPoints(short, 0.5)).toEqual(short);
    expect(reduceFreehandPoints(long, 0.5).length).toBeLessThan(long.length);
  });

  it('simplifies a long stroke the same amount at every smoothing level above 0 (degree is fixed, not scaled)', () => {
    const long = longStraightLine(1000);
    expect(reduceFreehandPoints(long, 0.1)).toEqual(reduceFreehandPoints(long, 1));
  });

  it('is deterministic: same input always reduces to the same output', () => {
    const points = longStraightLine(1000);
    expect(reduceFreehandPoints(points, 0.5)).toEqual(reduceFreehandPoints(points, 0.5));
  });

  it('handles empty and single-point input safely', () => {
    expect(reduceFreehandPoints([])).toEqual([]);
    expect(reduceFreehandPoints([{ x: 1, y: 2 }])).toEqual([{ x: 1, y: 2 }]);
  });

  it('ignores non-array input', () => {
    expect(reduceFreehandPoints(null)).toEqual([]);
    expect(reduceFreehandPoints(undefined)).toEqual([]);
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
  it('is deterministic', () => {
    const points = jitteryLine();
    const a = buildFreehandPath(points, 0.4);
    const b = buildFreehandPath(points, 0.4);
    expect(a).toEqual(b);
    expect(a.d.startsWith('M ')).toBe(true);
  });

  it('keeps the exact pre-existing quadratic-midpoint path at smoothing=0', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 5, y: 5 },
      { x: 10, y: 0 },
    ];
    const { points: reduced, d } = buildFreehandPath(points, 0);
    expect(reduced).toEqual(points);
    expect(d).toBe('M 0 0 Q 5 5 7.5 2.5 L 10 0');
  });

  it('does NOT reduce point retention as smoothing rises (the bug this fixes)', () => {
    // Before this fix, raising smoothing fed straight into RDP simplification
    // and threw points away — the opposite of "smoother". `points` in the
    // return (the retained-anchor count) must now stay the same across every
    // smoothing level for a stroke well under the decimation threshold.
    const points = jitteryLine();
    const none = buildFreehandPath(points, 0);
    const light = buildFreehandPath(points, 0.3);
    const heavy = buildFreehandPath(points, 1);
    expect(light.points.length).toBe(none.points.length);
    expect(heavy.points.length).toBe(none.points.length);
  });

  it('produces a visibly different, curve-fit `d` as smoothing rises, without touching smoothing=0 rendering', () => {
    const points = jitteryLine();
    const none = buildFreehandPath(points, 0);
    const light = buildFreehandPath(points, 0.3);
    const heavy = buildFreehandPath(points, 1);
    expect(light.d).not.toBe(none.d);
    expect(heavy.d).not.toBe(light.d);
    // Higher smoothing inserts more curve-fit samples, so the `d` string
    // (one or more path commands per sample) grows longer.
    expect(heavy.d.length).toBeGreaterThan(light.d.length);
    expect(light.d.length).toBeGreaterThan(none.d.length);
  });

  it('never drops or reorders the underlying anchors when curve-fitting a straight line', () => {
    // A perfectly straight input has no curvature for Catmull-Rom to bend,
    // so smoothing should not visibly distort it off the line.
    const points = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 20, y: 0 },
      { x: 30, y: 0 },
    ];
    const { d } = buildFreehandPath(points, 1);
    const ys = d.match(/-?\d+(\.\d+)?/g).map(Number);
    // Every other number in the M/Q/L stream is a y-coordinate; on a straight
    // horizontal line they must all be (numerically) zero.
    for (let i = 1; i < ys.length; i += 2) {
      expect(Math.abs(ys[i])).toBeLessThan(1e-9);
    }
  });

  it('handles empty and short input at any smoothing level', () => {
    expect(buildFreehandPath([], 1)).toEqual({ points: [], d: '' });
    const single = buildFreehandPath([{ x: 1, y: 2 }], 1);
    expect(single.points).toEqual([{ x: 1, y: 2 }]);
    expect(single.d).toBe('M 1 2 L 1 2');
  });
});

describe('smoothAnchors + segmentsFromCurvePoints (shared reduce-and-curve-fit step)', () => {
  // FreehandAnnotationNode reduces + curve-fits once, then builds both a `d`
  // string (via pointsToPathData) and pressure segments (via
  // segmentsFromCurvePoints) from that single result, instead of paying for
  // reduceFreehandPoints + smoothAnchors twice the way calling
  // buildFreehandPath and buildPressureSegments independently would. These
  // two must agree with the all-in-one helpers for the same input.
  it('is an identity at smoothing=0, matching buildFreehandPath', () => {
    const points = jitteryLine();
    const reduced = reduceFreehandPoints(points, 0);
    expect(smoothAnchors(reduced, 0)).toEqual(reduced);
    expect(pointsToPathData(smoothAnchors(reduced, 0))).toBe(buildFreehandPath(points, 0).d);
  });

  it('produces the same `d` as buildFreehandPath when composed manually', () => {
    const points = jitteryLine();
    const reduced = reduceFreehandPoints(points, 0.6);
    const curved = smoothAnchors(reduced, 0.6);
    expect(pointsToPathData(curved)).toBe(buildFreehandPath(points, 0.6).d);
  });

  it('produces the same segments as buildPressureSegments when composed manually', () => {
    const points = [
      { x: 0, y: 0, pressure: 0.2 },
      { x: 10, y: 5, pressure: 0.8 },
      { x: 20, y: 0, pressure: 0.5 },
    ];
    const reduced = reduceFreehandPoints(points, 0.5);
    const curved = smoothAnchors(reduced, 0.5);
    expect(segmentsFromCurvePoints(curved, 3)).toEqual(buildPressureSegments(points, 0.5, 3));
  });

  it('segmentsFromCurvePoints handles empty and single-point input', () => {
    expect(segmentsFromCurvePoints([], 2)).toEqual([]);
    expect(segmentsFromCurvePoints([{ x: 5, y: 5, pressure: 0.5 }], 2)).toEqual([
      { d: 'M 5 5 L 5 5', width: expect.any(Number) },
    ]);
  });

  it('segmentsFromCurvePoints degrades gracefully on non-array input, matching pointsToPathData', () => {
    expect(segmentsFromCurvePoints(null, 2)).toEqual([]);
    expect(segmentsFromCurvePoints(undefined, 2)).toEqual([]);
  });

  it('smoothAnchors degrades gracefully on non-array input, matching its sibling helpers', () => {
    expect(smoothAnchors(null, 0.5)).toEqual([]);
    expect(smoothAnchors(undefined, 0.5)).toEqual([]);
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
  it('returns one fewer segment than reduced points, each a straight M/L pair, at smoothing=0', () => {
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

  it('produces more, shorter segments as smoothing rises, tracing the same span', () => {
    const points = [
      { x: 0, y: 0, pressure: 0.2 },
      { x: 10, y: 5, pressure: 0.8 },
      { x: 20, y: 0, pressure: 0.5 },
      { x: 30, y: 5, pressure: 0.3 },
    ];
    const none = buildPressureSegments(points, 0, 2);
    const heavy = buildPressureSegments(points, 1, 2);
    expect(heavy.length).toBeGreaterThan(none.length);
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
