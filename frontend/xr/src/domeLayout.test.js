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

  it('honours an explicit radius from zoom', () => {
    const p = domePosition(50, 50, bounds, { radius: 3 });
    expect(p.z).toBeCloseTo(-3);
  });
});

describe('layoutBounds', () => {
  it('returns a safe unit box for an empty layout', () => {
    expect(layoutBounds([])).toEqual({ minX: -1, maxX: 1, minY: -1, maxY: 1 });
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
