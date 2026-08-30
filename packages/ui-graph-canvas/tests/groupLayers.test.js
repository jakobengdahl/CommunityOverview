import { describe, it, expect } from 'vitest';
import { GROUP_LAYER_BACK, GROUP_LAYER_FRONT, resolveGroupOrderZ } from '../src/utils/groupLayers';

// A group's order-among-groups field lives on `data.z`, never on the
// ReactFlow `zIndex` field annotationLayers.js reads/writes for every other
// kind — see the module docstring on why the two are deliberately separate
// spaces. `shape('a', ...)` below stands in for a non-group annotation
// sharing the canvas, to pin that this module never reads it.
const group = (id, z) => ({ id, type: 'group', data: { z } });
const shape = (id, zIndex) => ({ id, type: 'shape', zIndex });

describe('resolveGroupOrderZ', () => {
  it('puts a group in front of every other group', () => {
    const nodes = [group('a', 0), group('b', 3), group('c', 7)];
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_FRONT)).toBe(8);
  });

  it('puts a group behind every other group', () => {
    const nodes = [group('a', 5), group('b', 3), group('c', 7)];
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_BACK)).toBe(2);
  });

  it('breaks a tie with the current front rather than calling it a no-op', () => {
    // Every group is created at z = 0 (build_group_annotation's default), so
    // ties are the common case, exactly what the click exists to resolve.
    const nodes = [group('a', 0), group('b', 0), group('c', 0)];
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_FRONT)).toBe(1);
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_BACK)).toBe(-1);
  });

  it('reports a no-op when already alone at the front or at the back among groups', () => {
    const nodes = [group('a', 4), group('b', 1)];
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_FRONT)).toBeNull();
    expect(resolveGroupOrderZ(nodes, 'b', GROUP_LAYER_BACK)).toBeNull();
  });

  it('reports a no-op when the group is the only one on the canvas', () => {
    expect(resolveGroupOrderZ([group('a', 0)], 'a', GROUP_LAYER_FRONT)).toBeNull();
    expect(resolveGroupOrderZ([group('a', 0)], 'a', GROUP_LAYER_BACK)).toBeNull();
  });

  it('treats a group with no data.z as layer 0', () => {
    expect(
      resolveGroupOrderZ(
        [{ id: 'a', type: 'group', data: {} }, group('b', 4)],
        'a',
        GROUP_LAYER_FRONT
      )
    ).toBe(5);
  });

  it('never returns NaN or a non-finite layer', () => {
    const nodes = [group('a', Number.NaN), group('b', Number.POSITIVE_INFINITY), group('c', 2)];
    for (const direction of [GROUP_LAYER_FRONT, GROUP_LAYER_BACK]) {
      const z = resolveGroupOrderZ(nodes, 'a', direction);
      expect(Number.isFinite(z)).toBe(true);
    }
  });

  it('returns an integer strictly past a fractional neighbour', () => {
    const front = resolveGroupOrderZ([group('a', 0), group('b', 2.5)], 'a', GROUP_LAYER_FRONT);
    expect(Number.isInteger(front)).toBe(true);
    expect(front).toBeGreaterThan(2.5);
  });

  it('does nothing for a direction it does not recognise', () => {
    const nodes = [group('a', 0), group('b', 4)];
    for (const bogus of ['forward', 'backward', undefined, null, '']) {
      expect(resolveGroupOrderZ(nodes, 'a', bogus)).toBeNull();
      expect(resolveGroupOrderZ(nodes, 'b', bogus)).toBeNull();
    }
  });

  it('returns null for an id that is not on the canvas', () => {
    expect(resolveGroupOrderZ([group('a', 0)], 'missing', GROUP_LAYER_FRONT)).toBeNull();
  });

  it('returns null for an id that exists but is not a group', () => {
    // Guards against a stray id collision (a shape and a group sharing an
    // id would never happen in practice, but the type check must be the
    // thing that refuses it, not an accident of how the arithmetic falls
    // out).
    expect(resolveGroupOrderZ([shape('a', 0), group('b', 3)], 'a', GROUP_LAYER_FRONT)).toBeNull();
  });

  it('orders against groups only, never against another annotation kind sharing the canvas', () => {
    // A shape sitting at a CSS zIndex of 9 must have zero influence here —
    // this module never reads `zIndex` and never reads a non-group node's
    // `data.z`, on purpose (see the module docstring: the two are separate
    // spaces, dec-annotation-group-background-layering only orders group
    // backgrounds against each other).
    const nodes = [group('a', 0), shape('s1', 9)];
    expect(resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_FRONT)).toBeNull();
    const withSecondGroup = [group('a', 0), shape('s1', 9), group('b', 0)];
    expect(resolveGroupOrderZ(withSecondGroup, 'a', GROUP_LAYER_FRONT)).toBe(1);
  });

  it('refuses rather than tie when the neighbour is already at the safe-integer bound', () => {
    expect(
      resolveGroupOrderZ([group('a', 0), group('b', 2147483647)], 'a', GROUP_LAYER_FRONT)
    ).toBeNull();
    expect(
      resolveGroupOrderZ([group('a', 0), group('b', -2147483648)], 'a', GROUP_LAYER_BACK)
    ).toBeNull();
  });

  it('is reversible: front then back puts the group behind the others again', () => {
    const nodes = [group('a', 0), group('b', 0), group('c', 0)];
    const front = resolveGroupOrderZ(nodes, 'a', GROUP_LAYER_FRONT);
    const moved = [{ ...nodes[0], data: { z: front } }, nodes[1], nodes[2]];
    expect(resolveGroupOrderZ(moved, 'a', GROUP_LAYER_BACK)).toBeLessThan(0);
  });
});
