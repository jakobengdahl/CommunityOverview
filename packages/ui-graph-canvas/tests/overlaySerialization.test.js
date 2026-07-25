import { describe, it, expect } from 'vitest';
import {
  overlayToFlowNode,
  flowNodeToOverlay,
  isManualNode,
  isArrowAnchored,
  isArrowHeld,
  nodeCenter,
  findSnapTarget,
  resolveAnchoredArrow,
} from '../src/utils/annotations';

// GraphCanvas maps between the host's canvas-shape overlay descriptors and
// ReactFlow nodes. These translators must be exact inverses so an annotation
// survives the save-view round-trip (onSaveView → PUT → annotationsToRestore).
describe('overlay serialization', () => {
  it('round-trips a note (size <-> style, font size)', () => {
    const overlay = {
      id: 'note-1',
      kind: 'note',
      position: { x: 1, y: 2 },
      text: 'hi',
      color: '#FEF08A',
      fontSize: 18,
      size: { w: 200, h: 140 },
    };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      id: 'note-1',
      type: 'note',
      position: { x: 1, y: 2 },
      style: { width: 200, height: 140 },
      data: { text: 'hi', color: '#FEF08A', fontSize: 18 },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('defaults a note to 200x140 when size is missing', () => {
    const node = overlayToFlowNode({ id: 'n', kind: 'note', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({ width: 200, height: 140 });
  });

  it('round-trips a label with a font size', () => {
    const overlay = {
      id: 'label-1',
      kind: 'label',
      position: { x: 3, y: 4 },
      text: 'L',
      color: '#fff',
      fontSize: 28,
    };
    expect(flowNodeToOverlay(overlayToFlowNode(overlay))).toEqual(overlay);
  });

  it('round-trips an arrow, preserving dx of 0 and default heads', () => {
    const overlay = {
      id: 'arrow-1',
      kind: 'arrow',
      position: { x: 5, y: 6 },
      dx: 0,
      dy: 40,
      color: '#fff',
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data).toEqual({ dx: 0, dy: 40, color: '#fff', startArrow: false, endArrow: true });
    // Defaults are materialised on the way back so the model is explicit.
    expect(flowNodeToOverlay(node)).toEqual({ ...overlay, startArrow: false, endArrow: true });
  });

  it('round-trips a double-headed line with anchors', () => {
    const overlay = {
      id: 'arrow-2',
      kind: 'arrow',
      position: { x: 0, y: 0 },
      dx: 100,
      dy: 0,
      color: '#fff',
      startArrow: true,
      endArrow: false,
      startAnchor: 'node-a',
      endAnchor: 'node-b',
    };
    const node = overlayToFlowNode(overlay);
    expect(node.draggable).toBe(false);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('leaves a free (unanchored) arrow draggable', () => {
    const node = overlayToFlowNode({
      id: 'arrow-3',
      kind: 'arrow',
      position: { x: 0, y: 0 },
      dx: 10,
      dy: 10,
    });
    expect(node.draggable).toBe(true);
  });

  it('recognises overlays and groups as manual (preserved) nodes', () => {
    expect(isManualNode({ id: 'note-1', type: 'note' })).toBe(true);
    expect(isManualNode({ id: 'arrow-1', type: 'arrow' })).toBe(true);
    expect(isManualNode({ id: 'group-1', type: 'group' })).toBe(true);
    expect(isManualNode({ id: 'x', type: 'custom' })).toBe(false);
  });
});

describe('isArrowAnchored', () => {
  it('is true when either endpoint has an anchor', () => {
    expect(isArrowAnchored({ startAnchor: 'a' })).toBe(true);
    expect(isArrowAnchored({ endAnchor: 'b' })).toBe(true);
    expect(isArrowAnchored({})).toBe(false);
    expect(isArrowAnchored(undefined)).toBe(false);
  });
});

describe('nodeCenter', () => {
  it('uses measured size when available', () => {
    expect(nodeCenter({ position: { x: 0, y: 0 }, width: 100, height: 40 })).toEqual({
      x: 50,
      y: 20,
    });
  });

  it('prefers positionAbsolute over position', () => {
    expect(
      nodeCenter({
        position: { x: 0, y: 0 },
        positionAbsolute: { x: 10, y: 10 },
        width: 20,
        height: 20,
      })
    ).toEqual({ x: 20, y: 20 });
  });

  it('returns null without a position', () => {
    expect(nodeCenter({})).toBeNull();
  });
});

describe('findSnapTarget', () => {
  const nodes = [
    { id: 'a', type: 'custom', position: { x: 100, y: 100 }, width: 0, height: 0 },
    { id: 'b', type: 'note', position: { x: 300, y: 300 }, width: 0, height: 0 },
    { id: 'self', type: 'arrow', position: { x: 100, y: 100 }, width: 0, height: 0 },
  ];

  it('snaps to the nearest node within the radius', () => {
    expect(findSnapTarget({ x: 110, y: 110 }, nodes, { excludeId: 'self' })).toBe('a');
  });

  it('returns null when nothing is close enough', () => {
    expect(findSnapTarget({ x: 1000, y: 1000 }, nodes, { excludeId: 'self' })).toBeNull();
  });

  it('never snaps to another arrow or to itself', () => {
    // Point sits exactly on the arrow "self" centre, yet arrows are skipped.
    expect(findSnapTarget({ x: 100, y: 100 }, nodes, { excludeId: 'self' })).toBe('a');
    const onlyArrows = [{ id: 'x', type: 'arrow', position: { x: 0, y: 0 }, width: 0, height: 0 }];
    expect(findSnapTarget({ x: 0, y: 0 }, onlyArrows, { excludeId: 'self' })).toBeNull();
  });
});

describe('resolveAnchoredArrow', () => {
  it('returns null when the arrow has no anchors', () => {
    const arrow = { position: { x: 0, y: 0 }, data: { dx: 10, dy: 10 } };
    expect(resolveAnchoredArrow(arrow, new Map())).toBeNull();
  });

  it('glues the start endpoint to its target centre and keeps the far end put', () => {
    const arrow = { position: { x: 0, y: 0 }, data: { dx: 100, dy: 0, startAnchor: 'a' } };
    const centers = new Map([['a', { x: 20, y: 5 }]]);
    // Old end was (100, 0); start moves to (20, 5) so dx/dy compensate.
    expect(resolveAnchoredArrow(arrow, centers)).toEqual({
      position: { x: 20, y: 5 },
      dx: 80,
      dy: -5,
    });
  });

  it('glues the end endpoint to its target centre', () => {
    const arrow = { position: { x: 0, y: 0 }, data: { dx: 100, dy: 0, endAnchor: 'b' } };
    const centers = new Map([['b', { x: 60, y: 40 }]]);
    expect(resolveAnchoredArrow(arrow, centers)).toEqual({
      position: { x: 0, y: 0 },
      dx: 60,
      dy: 40,
    });
  });

  it('returns null when geometry already matches (idempotent, loop-safe)', () => {
    const arrow = { position: { x: 0, y: 0 }, data: { dx: 60, dy: 40, endAnchor: 'b' } };
    const centers = new Map([['b', { x: 60, y: 40 }]]);
    expect(resolveAnchoredArrow(arrow, centers)).toBeNull();
  });

  it('leaves an endpoint put when its anchor target is gone', () => {
    const arrow = { position: { x: 0, y: 0 }, data: { dx: 100, dy: 0, endAnchor: 'gone' } };
    expect(resolveAnchoredArrow(arrow, new Map())).toBeNull();
  });
});

describe('isArrowHeld', () => {
  it('is false when the arrow has no anchors', () => {
    expect(isArrowHeld({ dx: 1, dy: 1 }, new Set())).toBe(false);
  });

  it('is held while at least one anchor target is present', () => {
    expect(isArrowHeld({ startAnchor: 'a', endAnchor: 'b' }, new Set(['b']))).toBe(true);
  });

  it('is not held when every anchor target is absent from the view', () => {
    // Anchors stay in the data (they re-glue if the target returns) but a
    // filtered/collapsed/deleted target must not hold the arrow non-draggable.
    expect(isArrowHeld({ startAnchor: 'a', endAnchor: 'b' }, new Set(['c']))).toBe(false);
  });
});
