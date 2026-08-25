import { describe, it, expect } from 'vitest';
import { LAYER_BACKWARD, LAYER_FORWARD, resolveLayerZ } from '../src/utils/annotationLayers';

// `z` is the annotation envelope's layer field, carried on a ReactFlow node
// as `zIndex` (utils/annotations.js) and persisted by the host's session
// translators. These cases pin what one forward/back click must produce:
// docs/ANNOTATION_CONTRACT.md's "semantic default layers plus manual
// forward/back".
const note = (id, zIndex) => ({ id, type: 'note', zIndex });
const graphNode = (id, zIndex) => ({ id, type: 'custom', zIndex });

describe('resolveLayerZ', () => {
  it('steps past exactly one neighbouring layer rather than to the front', () => {
    const nodes = [note('a', 0), note('b', 1), note('c', 5)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeGreaterThan(1);
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeLessThan(5);
  });

  it('lands strictly above the layer it passed, never level with it', () => {
    // Landing *on* a neighbour's z leaves paint order to ReactFlow's DOM
    // order, so the click could visibly do nothing.
    const nodes = [note('a', 0), note('b', 1)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeGreaterThan(1);
  });

  it('breaks a tie upward when another annotation shares the same layer', () => {
    const nodes = [note('a', 3), note('b', 3), note('c', 9)];
    const z = resolveLayerZ(nodes, 'a', LAYER_FORWARD);
    expect(z).toBeGreaterThan(3);
    expect(z).toBeLessThan(9);
  });

  it('breaks a tie downward on a backward step', () => {
    const nodes = [note('a', 3), note('b', 3), note('c', -4)];
    const z = resolveLayerZ(nodes, 'a', LAYER_BACKWARD);
    expect(z).toBeLessThan(3);
    expect(z).toBeGreaterThan(-4);
  });

  it('fits between two adjacent integer layers instead of renumbering them', () => {
    const nodes = [note('a', 0), note('b', 5), note('c', 6)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBe(5.5);
  });

  it('reports a no-op when already at the front or at the back', () => {
    const nodes = [note('a', 4), note('b', 1)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeNull();
    expect(resolveLayerZ(nodes, 'b', LAYER_BACKWARD)).toBeNull();
  });

  it('reports a no-op when the annotation is the only one on the canvas', () => {
    const nodes = [note('a', 0)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeNull();
    expect(resolveLayerZ(nodes, 'a', LAYER_BACKWARD)).toBeNull();
  });

  it('never steps an annotation past a graph node', () => {
    // Graph nodes share the ReactFlow z-space but are not part of the
    // annotation layer model; reordering against one would silently change
    // how the graph itself paints.
    const nodes = [note('a', 0), graphNode('n1', 2)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBeNull();
  });

  it('treats a node with no zIndex as layer 0', () => {
    const nodes = [{ id: 'a', type: 'note' }, note('b', 0)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FORWARD)).toBe(1);
  });

  it('returns null for an id that is not on the canvas', () => {
    expect(resolveLayerZ([note('a', 0)], 'missing', LAYER_FORWARD)).toBeNull();
  });

  it('is reversible: forward then backward puts the annotation back below the layer it passed', () => {
    const nodes = [note('a', 0), note('b', 1), note('c', 5)];
    const forward = resolveLayerZ(nodes, 'a', LAYER_FORWARD);
    const moved = [{ ...nodes[0], zIndex: forward }, nodes[1], nodes[2]];
    const back = resolveLayerZ(moved, 'a', LAYER_BACKWARD);
    expect(back).toBeLessThan(1);
  });
});
