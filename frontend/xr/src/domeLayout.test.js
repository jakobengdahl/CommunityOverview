import { describe, it, expect } from 'vitest';
import {
  DEFAULT_DOME,
  zoomToRadius,
  sphericalToCartesian,
  domePosition,
  layoutBounds,
} from './domeLayout.js';

describe('zoomToRadius', () => {
  it('returns the base radius at neutral zoom', () => {
    expect(zoomToRadius(1)).toBeCloseTo(DEFAULT_DOME.baseRadius);
  });

  it('shrinks the radius when zooming in (shell comes closer)', () => {
    expect(zoomToRadius(2)).toBeLessThan(zoomToRadius(1));
  });

  it('grows the radius when zooming out (shell recedes)', () => {
    expect(zoomToRadius(0.5)).toBeGreaterThan(zoomToRadius(1));
  });

  it('clamps to the configured min and max', () => {
    expect(zoomToRadius(1000)).toBe(DEFAULT_DOME.minRadius);
    expect(zoomToRadius(0.0001)).toBe(DEFAULT_DOME.maxRadius);
  });

  it('falls back to the base radius for non-positive zoom', () => {
    expect(zoomToRadius(0)).toBe(DEFAULT_DOME.baseRadius);
    expect(zoomToRadius(-3)).toBe(DEFAULT_DOME.baseRadius);
  });

  // A dropped or malformed zoom takes the same guard as 0 and -3, and is the
  // input that actually shows up in practice.
  it('falls back to the base radius for a missing or non-numeric zoom', () => {
    expect(zoomToRadius(NaN)).toBe(DEFAULT_DOME.baseRadius);
    expect(zoomToRadius(undefined)).toBe(DEFAULT_DOME.baseRadius);
  });

  // Without these, reversing the opts merge — silently ignoring every caller
  // override — would leave the whole suite green.
  it('honours caller overrides of the radius range', () => {
    expect(zoomToRadius(1000, { minRadius: 4 })).toBe(4);
    expect(zoomToRadius(0.0001, { maxRadius: 9 })).toBe(9);
    expect(zoomToRadius(1, { baseRadius: 11 })).toBeCloseTo(11);
  });

  // Every other return is inside [minRadius, maxRadius]; the fallback must be
  // too, or callers cannot rely on the range at all.
  it('keeps the non-positive-zoom fallback inside the configured range', () => {
    expect(zoomToRadius(0, { minRadius: 8 })).toBe(8);
    expect(zoomToRadius(-1, { maxRadius: 4 })).toBe(4);
  });
});

describe('sphericalToCartesian', () => {
  it('places the straight-ahead point on -Z', () => {
    const p = sphericalToCartesian(5, 0, 0);
    expect(p.x).toBeCloseTo(0);
    expect(p.y).toBeCloseTo(0);
    expect(p.z).toBeCloseTo(-5);
  });

  it('keeps every point on the sphere of the given radius', () => {
    const p = sphericalToCartesian(7, 0.6, -0.4);
    const r = Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z);
    expect(r).toBeCloseTo(7);
  });

  it('puts positive azimuth on +X and positive elevation on +Y', () => {
    expect(sphericalToCartesian(5, 0.5, 0).x).toBeGreaterThan(0);
    expect(sphericalToCartesian(5, 0, 0.5).y).toBeGreaterThan(0);
  });
});

describe('domePosition', () => {
  const bounds = { minX: 0, maxX: 100, minY: 0, maxY: 100 };

  it('centres the middle of the layout straight ahead', () => {
    const p = domePosition(50, 50, bounds);
    expect(p.x).toBeCloseTo(0);
    expect(p.y).toBeCloseTo(0);
    expect(p.z).toBeCloseTo(-DEFAULT_DOME.baseRadius);
  });

  it('maps larger x to the right (+X)', () => {
    expect(domePosition(90, 50, bounds).x).toBeGreaterThan(domePosition(10, 50, bounds).x);
  });

  it('maps smaller y higher on the dome (screen y grows downward)', () => {
    expect(domePosition(50, 10, bounds).y).toBeGreaterThan(domePosition(50, 90, bounds).y);
  });

  // Asserting only z at the layout centre (where x = y = 0 by construction)
  // would still pass if the radius were applied to z alone, so check the
  // distance from the origin at an off-centre point too.
  it('honours an explicit radius from zoom', () => {
    const centre = domePosition(50, 50, bounds, { radius: 3 });
    expect(centre.z).toBeCloseTo(-3);

    const corner = domePosition(0, 0, bounds, { radius: 3 });
    const r = Math.sqrt(corner.x ** 2 + corner.y ** 2 + corner.z ** 2);
    expect(r).toBeCloseTo(3);
  });

  it('honours a baseRadius override when no explicit radius is given', () => {
    expect(domePosition(50, 50, bounds, { baseRadius: 9 }).z).toBeCloseTo(-9);
  });

  it('prefers an explicit radius over baseRadius', () => {
    expect(domePosition(50, 50, bounds, { baseRadius: 9, radius: 4 }).z).toBeCloseTo(-4);
  });

  // Ordering alone would still pass if the wrap factor were wrong, so pin the
  // actual extents: the layout edges must land on exactly half the configured
  // field of view, left/right and up/down.
  it('maps the layout edges to the full configured wrap', () => {
    const r = DEFAULT_DOME.baseRadius;
    const halfH = DEFAULT_DOME.hFovRad / 2;
    const halfV = DEFAULT_DOME.vFovRad / 2;

    const left = domePosition(0, 50, bounds);
    expect(Math.atan2(left.x, -left.z)).toBeCloseTo(-halfH);

    const right = domePosition(100, 50, bounds);
    expect(Math.atan2(right.x, -right.z)).toBeCloseTo(halfH);

    const top = domePosition(50, 0, bounds);
    expect(Math.asin(top.y / r)).toBeCloseTo(halfV);

    const bottom = domePosition(50, 100, bounds);
    expect(Math.asin(bottom.y / r)).toBeCloseTo(-halfV);
  });

  it('honours explicit field-of-view overrides', () => {
    const narrow = domePosition(100, 50, bounds, { hFovRad: Math.PI / 6 });
    expect(Math.atan2(narrow.x, -narrow.z)).toBeCloseTo(Math.PI / 12);

    const tall = domePosition(50, 0, bounds, { vFovRad: Math.PI / 3 });
    expect(Math.asin(tall.y / DEFAULT_DOME.baseRadius)).toBeCloseTo(Math.PI / 6);
  });

  // A single-node graph gives minX === maxX. Without the normalize guard this
  // divides by zero and feeds NaN into the three.js matrices.
  it('centres a degenerate single-point layout instead of producing NaN', () => {
    const degenerate = { minX: 5, maxX: 5, minY: 5, maxY: 5 };
    const p = domePosition(5, 5, degenerate);
    expect(p.x).toBeCloseTo(0);
    expect(p.y).toBeCloseTo(0);
    expect(p.z).toBeCloseTo(-DEFAULT_DOME.baseRadius);
  });

  // A non-finite coordinate must not reach the three.js matrices — one poisoned
  // node would otherwise be indistinguishable from a poisoned scene.
  it('centres a non-finite coordinate instead of producing NaN', () => {
    for (const bad of [NaN, undefined, Infinity]) {
      const p = domePosition(bad, bad, bounds);
      expect(Number.isFinite(p.x)).toBe(true);
      expect(Number.isFinite(p.y)).toBe(true);
      expect(Number.isFinite(p.z)).toBe(true);
    }
  });
});

describe('layoutBounds', () => {
  const UNIT_BOX = { minX: -1, maxX: 1, minY: -1, maxY: 1 };

  it('returns a safe unit box for an empty layout', () => {
    expect(layoutBounds([])).toEqual(UNIT_BOX);
  });

  it('returns a safe unit box for a missing layout', () => {
    expect(layoutBounds(null)).toEqual(UNIT_BOX);
    expect(layoutBounds(undefined)).toEqual(UNIT_BOX);
  });

  // Skipping the bad entries must not leave the accumulators at ±Infinity.
  it('returns a safe unit box when every position is non-finite', () => {
    expect(layoutBounds([{ x: NaN, y: 0 }, { x: 1 }, {}])).toEqual(UNIT_BOX);
  });

  it('ignores non-finite positions when computing the extent', () => {
    const b = layoutBounds([
      { x: 2, y: 3 },
      { x: NaN, y: 100 },
      { x: 8, y: 9 },
    ]);
    expect(b).toEqual({ minX: 2, maxX: 8, minY: 3, maxY: 9 });
  });

  it('returns degenerate bounds for a single position', () => {
    expect(layoutBounds([{ x: 3, y: 4 }])).toEqual({ minX: 3, maxX: 3, minY: 4, maxY: 4 });
  });

  it('computes the extent of the positions', () => {
    const b = layoutBounds([
      { x: -5, y: 2 },
      { x: 7, y: -3 },
      { x: 1, y: 10 },
    ]);
    expect(b).toEqual({ minX: -5, maxX: 7, minY: -3, maxY: 10 });
  });
});
