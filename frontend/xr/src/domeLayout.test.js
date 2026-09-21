import { describe, it, expect } from 'vitest';
import {
  DEFAULT_DOME,
  domeAnglesFromRay,
  domeView,
  zoomToRadius,
  zoomToDensity,
  sphericalToCartesian,
  domePosition,
  layoutPositionFromDomePoint,
  layoutPositionFromRay,
  layoutBounds,
  panDomeView,
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

describe('zoomToDensity', () => {
  it('uses neutral density at neutral zoom', () => {
    expect(zoomToDensity(1)).toBe(1);
  });

  it('increases density when zooming in', () => {
    expect(zoomToDensity(2)).toBe(2);
  });

  it('clamps density to the configured range', () => {
    expect(zoomToDensity(1000)).toBe(DEFAULT_DOME.maxDensity);
    expect(zoomToDensity(0.01)).toBe(DEFAULT_DOME.minDensity);
  });

  it('falls back to neutral density for malformed zoom', () => {
    expect(zoomToDensity(NaN)).toBe(1);
    expect(zoomToDensity(undefined)).toBe(1);
  });
});

describe('domeView', () => {
  const bounds = { minX: 0, maxX: 100, minY: 0, maxY: 80 };

  it('shrinks the visible layout window as density increases', () => {
    expect(domeView(bounds, { density: 2 })).toMatchObject({
      density: 2,
      centerX: 50,
      centerY: 40,
      visibleWidth: 50,
      visibleHeight: 40,
    });
  });

  it('wraps the horizontal centre by the actual layout width', () => {
    expect(domeView(bounds, { centerX: 125 }).centerX).toBe(25);
    expect(domeView(bounds, { centerX: -25 }).centerX).toBe(75);
  });

  it('clamps the vertical centre and reports edge indicators', () => {
    const top = domeView(bounds, { density: 2, centerY: -100 });
    expect(top.centerY).toBe(20);
    expect(top.atTop).toBe(true);
    expect(top.atBottom).toBe(false);

    const bottom = domeView(bounds, { density: 2, centerY: 100 });
    expect(bottom.centerY).toBe(60);
    expect(bottom.atTop).toBe(false);
    expect(bottom.atBottom).toBe(true);
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

  it('spreads layout points across a wider angular span as density increases', () => {
    const neutral = domePosition(100, 50, bounds);
    const dense = domePosition(100, 50, bounds, { density: 2 });
    expect(Math.atan2(dense.x, -dense.z)).toBeCloseTo(
      Math.atan2(neutral.x, -neutral.z) * 2
    );
  });

  it('uses the wrapped horizontal centre when panning', () => {
    const centred = domePosition(75, 50, bounds, { centerX: -25 });
    expect(Math.atan2(centred.x, -centred.z)).toBeCloseTo(0);
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
  // node would otherwise be indistinguishable from a poisoned scene. Merely
  // being finite is not enough: the node has to land dead ahead, not in a
  // corner, or a bad coordinate reads as a real position.
  it('centres a non-finite coordinate instead of producing NaN', () => {
    for (const bad of [NaN, undefined, Infinity]) {
      const p = domePosition(bad, bad, bounds);
      expect(p.x).toBeCloseTo(0);
      expect(p.y).toBeCloseTo(0);
      expect(p.z).toBeCloseTo(-DEFAULT_DOME.baseRadius);
    }
  });

  // Inverted bounds are the only case the max <= min guard still handles on its
  // own: the division stays finite but mirrors the axis, so the non-finite
  // fallback never sees it.
  it('centres a point given inverted bounds rather than mirroring the axis', () => {
    const inverted = { minX: 100, maxX: 0, minY: 100, maxY: 0 };
    const p = domePosition(25, 25, inverted);
    expect(p.x).toBeCloseTo(0);
    expect(p.y).toBeCloseTo(0);
    expect(p.z).toBeCloseTo(-DEFAULT_DOME.baseRadius);
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

describe('layoutPositionFromDomePoint', () => {
  const bounds = { minX: 0, maxX: 100, minY: 0, maxY: 100 };

  it('round-trips a dome point back into the graph layout coordinate space', () => {
    const source = { x: 80, y: 25 };
    const point = domePosition(source.x, source.y, bounds);
    const layout = layoutPositionFromDomePoint(point, bounds);
    expect(layout.x).toBeCloseTo(source.x);
    expect(layout.y).toBeCloseTo(source.y);
  });

  it('wraps azimuth outside the comfortable view back onto the layout width', () => {
    const layout = layoutPositionFromDomePoint({ x: 100, y: 100, z: 0 }, bounds);
    expect(layout.x).toBeCloseTo(25);
    expect(layout.y).toBeCloseTo(0, 2);
  });

  it('round-trips through a dense panned view', () => {
    const source = { x: 10, y: 25 };
    const opts = { density: 2, centerX: 90, centerY: 35 };
    const point = domePosition(source.x, source.y, bounds, opts);
    const layout = layoutPositionFromDomePoint(point, bounds, opts);
    expect(layout.x).toBeCloseTo(source.x);
    expect(layout.y).toBeCloseTo(source.y);
  });

  it('uses the degenerate coordinate for a single-node extent', () => {
    const bounds = { minX: 42, maxX: 42, minY: 7, maxY: 7 };
    expect(layoutPositionFromDomePoint({ x: 0, y: 0, z: -6 }, bounds)).toEqual({
      x: 42,
      y: 7,
    });
  });
});

describe('layoutPositionFromRay', () => {
  const bounds = { minX: 0, maxX: 100, minY: 0, maxY: 100 };

  it('projects a controller ray onto the dome and returns session coordinates', () => {
    const target = domePosition(75, 40, bounds);
    const layout = layoutPositionFromRay(
      { x: 0, y: 1.5, z: 0 },
      { x: target.x, y: target.y, z: target.z },
      bounds,
      { eyeHeight: 1.5 }
    );
    expect(layout.x).toBeCloseTo(75);
    expect(layout.y).toBeCloseTo(40);
  });

  it('returns dome angles for empty-background pan gestures', () => {
    const target = domePosition(75, 40, bounds);
    const angles = domeAnglesFromRay(
      { x: 0, y: 1.5, z: 0 },
      { x: target.x, y: target.y, z: target.z },
      { eyeHeight: 1.5 }
    );
    expect(angles.azimuth).toBeCloseTo(DEFAULT_DOME.hFovRad / 4);
    expect(angles.elevation).toBeCloseTo(DEFAULT_DOME.vFovRad / 10);
  });

  it('projects rays through density and pan before returning session coordinates', () => {
    const opts = { density: 2, centerX: 75, centerY: 60, eyeHeight: 1.5 };
    const target = domePosition(10, 70, bounds, opts);
    const layout = layoutPositionFromRay(
      { x: 0, y: 1.5, z: 0 },
      { x: target.x, y: target.y, z: target.z },
      bounds,
      opts
    );
    expect(layout.x).toBeCloseTo(10);
    expect(layout.y).toBeCloseTo(70);
  });

  it('returns null for a zero-length ray direction', () => {
    expect(
      layoutPositionFromRay({ x: 0, y: 1.5, z: 0 }, { x: 0, y: 0, z: 0 }, bounds, {
        eyeHeight: 1.5,
      })
    ).toBeNull();
  });
});

describe('panDomeView', () => {
  const bounds = { minX: 0, maxX: 100, minY: 0, maxY: 100 };

  it('converts two-axis angular grab movement into wrapped layout pan', () => {
    const view = domeView(bounds, { density: 2, centerX: 95, centerY: 50 });
    const next = panDomeView(
      view,
      { azimuth: -DEFAULT_DOME.hFovRad / 5, elevation: DEFAULT_DOME.vFovRad / 5 },
      bounds
    );
    expect(next.centerX).toBeCloseTo(5);
    expect(next.centerY).toBeCloseTo(60);
  });

  it('clamps vertical pan at the layout edge', () => {
    const view = domeView(bounds, { density: 2 });
    const next = panDomeView(view, { elevation: -DEFAULT_DOME.vFovRad * 10 }, bounds);
    expect(next.centerY).toBe(25);
    expect(next.atTop).toBe(true);
  });
});
