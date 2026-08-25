import { describe, it, expect } from 'vitest';
import { LAYER_BACK, LAYER_FRONT, resolveLayerZ } from '../src/utils/annotationLayers';
import { flowNodeToOverlay, overlayToFlowNode } from '../src/utils/annotations';

// `z` is the annotation envelope's layer field, carried on a ReactFlow node
// as `zIndex` (utils/annotations.js) and persisted by the host's session
// translators. See docs/ANNOTATION_CONTRACT.md's "Layer order".
const note = (id, zIndex) => ({ id, type: 'note', zIndex });
const graphNode = (id, zIndex) => ({ id, type: 'custom', zIndex });

describe('resolveLayerZ', () => {
  it('puts an annotation in front of every other annotation', () => {
    const nodes = [note('a', 0), note('b', 3), note('c', 7)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FRONT)).toBe(8);
  });

  it('puts an annotation behind every other annotation', () => {
    const nodes = [note('a', 5), note('b', 3), note('c', 7)];
    expect(resolveLayerZ(nodes, 'a', LAYER_BACK)).toBe(2);
  });

  it('breaks a tie with the current front rather than calling it a no-op', () => {
    // Every annotation is created at z = 0, so ties are the common case and
    // are exactly what the click exists to resolve.
    const nodes = [note('a', 0), note('b', 0), note('c', 0)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FRONT)).toBe(1);
    expect(resolveLayerZ(nodes, 'a', LAYER_BACK)).toBe(-1);
  });

  it('reports a no-op when already alone at the front or at the back', () => {
    const nodes = [note('a', 4), note('b', 1)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FRONT)).toBeNull();
    expect(resolveLayerZ(nodes, 'b', LAYER_BACK)).toBeNull();
  });

  it('reports a no-op when the annotation is the only one on the canvas', () => {
    const nodes = [note('a', 0)];
    expect(resolveLayerZ(nodes, 'a', LAYER_FRONT)).toBeNull();
    expect(resolveLayerZ(nodes, 'a', LAYER_BACK)).toBeNull();
  });

  // A browser rejects `z-index: 0.5` outright and the element keeps whatever
  // it had, so a fractional result would publish an op and move nothing on
  // screen. An agent may set any float over MCP, so fractional neighbours
  // are reachable. jsdom's CSSOM accepts the value a real browser discards,
  // so nothing downstream of this can catch a regression here.
  it('returns an integer strictly past a fractional neighbour', () => {
    const front = resolveLayerZ([note('a', 0), note('b', 2.5)], 'a', LAYER_FRONT);
    expect(Number.isInteger(front)).toBe(true);
    expect(front).toBeGreaterThan(2.5);

    const back = resolveLayerZ([note('a', 0), note('b', -2.5)], 'a', LAYER_BACK);
    expect(Number.isInteger(back)).toBe(true);
    expect(back).toBeLessThan(-2.5);
  });

  it('returns an integer even when the annotation itself sits on a fractional layer', () => {
    const z = resolveLayerZ([note('a', 0.5), note('b', 0.5)], 'a', LAYER_FRONT);
    expect(Number.isInteger(z)).toBe(true);
    expect(z).toBeGreaterThan(0.5);
  });

  it('never returns NaN or a non-finite layer', () => {
    const nodes = [note('a', Number.NaN), note('b', Number.POSITIVE_INFINITY), note('c', 2)];
    for (const direction of [LAYER_FRONT, LAYER_BACK]) {
      const z = resolveLayerZ(nodes, 'a', direction);
      expect(Number.isFinite(z)).toBe(true);
    }
  });

  it('orders against annotations only, never against a graph node', () => {
    // Graph nodes share the ReactFlow z-space but are not part of the
    // annotation layer model; ordering against one would silently change how
    // the graph itself paints.
    expect(resolveLayerZ([note('a', 0), graphNode('n1', 9)], 'a', LAYER_FRONT)).toBeNull();
    expect(resolveLayerZ([note('a', 0), graphNode('n1', 9), note('b', 0)], 'a', LAYER_FRONT)).toBe(
      1
    );
  });

  it('treats a node with no zIndex as layer 0', () => {
    expect(resolveLayerZ([{ id: 'a', type: 'note' }, note('b', 4)], 'a', LAYER_FRONT)).toBe(5);
  });

  it('returns null for an id that is not on the canvas', () => {
    expect(resolveLayerZ([note('a', 0)], 'missing', LAYER_FRONT)).toBeNull();
  });

  it('is reversible: front then back puts the annotation behind the others again', () => {
    const nodes = [note('a', 0), note('b', 0), note('c', 0)];
    const front = resolveLayerZ(nodes, 'a', LAYER_FRONT);
    const moved = [{ ...nodes[0], zIndex: front }, nodes[1], nodes[2]];
    expect(resolveLayerZ(moved, 'a', LAYER_BACK)).toBeLessThan(0);
  });
});

// The arithmetic above is only useful if the value it produces survives to
// the server. This ties it to the translators that carry it: the layer the
// control writes onto the flow node's `zIndex` must come back out of
// `flowNodeToOverlay` as the annotation's `z`, or the change would be
// computed, published as a no-op diff, and lost on reload.
describe('resolveLayerZ round-trips through the overlay translators', () => {
  it('lands the new layer on the annotation envelope as z', () => {
    const overlay = { id: 'a', kind: 'note', position: { x: 0, y: 0 }, z: 0, text: 'hi' };
    const flowNode = overlayToFlowNode(overlay);
    expect(flowNode.zIndex).toBe(0);

    const canvas = [flowNode, { id: 'b', type: 'note', zIndex: 3 }];
    const z = resolveLayerZ(canvas, 'a', LAYER_FRONT);
    const moved = { ...flowNode, zIndex: z };

    expect(flowNodeToOverlay(moved).z).toBe(4);
  });

  it('keeps a layer set by an agent when the annotation is only rehydrated', () => {
    const overlay = { id: 'a', kind: 'shape', position: { x: 0, y: 0 }, z: 7, shape: 'circle' };
    expect(flowNodeToOverlay(overlayToFlowNode(overlay)).z).toBe(7);
  });
});

describe('resolveLayerZ guards', () => {
  const nodes = [note('a', 0), note('b', 4)];

  it('does nothing for a direction it does not recognise', () => {
    // A stale constant left behind by a rename must no-op rather than pick
    // one of the two real directions and move the annotation the wrong way.
    for (const bogus of ['forward', 'backward', undefined, null, '']) {
      expect(resolveLayerZ(nodes, 'a', bogus)).toBeNull();
    }
  });

  it('refuses rather than tie when the neighbour is already at the CSS bound', () => {
    // An agent may set any z over MCP (Date.now() is a common bring-to-front
    // idiom). CSS clamps z-index to int32, so clamping the step back down to
    // the bound would land level with the neighbour it is meant to pass —
    // the exact tie this module exists to break, published as an op that
    // changes nothing on screen. Nowhere further to go is a no-op.
    expect(resolveLayerZ([note('a', 0), note('b', 2147483647)], 'a', LAYER_FRONT)).toBeNull();
    expect(resolveLayerZ([note('a', 0), note('b', -2147483648)], 'a', LAYER_BACK)).toBeNull();
  });

  it('refuses when a neighbour past the bound would drag the result out the other side', () => {
    // The dangerous direction: an annotation parked above Z_MAX (an agent
    // writing Date.now() as a bring-to-front) makes send-to-back compute a
    // layer just under it — still far above Z_MAX, which the browser clamps
    // back down to Z_MAX and paints at the very front. That is the opposite
    // of what was asked for, not a no-op, so both bounds are checked in both
    // directions.
    expect(resolveLayerZ([note('a', 4e9), note('b', 3e9)], 'a', LAYER_BACK)).toBeNull();
    expect(resolveLayerZ([note('a', -4e9), note('b', -3e9)], 'a', LAYER_FRONT)).toBeNull();
    expect(
      resolveLayerZ([note('a', 1787640438290), note('b', 1787640438282)], 'a', LAYER_BACK)
    ).toBeNull();
  });

  it('refuses past the range where an integer step survives float precision', () => {
    // Beyond 2^53, Math.floor(max) + 1 === max, so the "strictly past"
    // guarantee cannot be met at all.
    expect(resolveLayerZ([note('a', 0), note('b', 2 ** 60)], 'a', LAYER_FRONT)).toBeNull();
    expect(resolveLayerZ([note('a', 0), note('b', -(2 ** 60))], 'a', LAYER_BACK)).toBeNull();
  });

  it('still moves for a large layer that is comfortably inside the bound', () => {
    expect(resolveLayerZ([note('a', 0), note('b', 1000000)], 'a', LAYER_FRONT)).toBe(1000001);
  });
});
