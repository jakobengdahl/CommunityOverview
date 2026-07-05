import { describe, it, expect } from 'vitest';
import {
  overlayToFlowNode,
  flowNodeToOverlay,
  isManualNode,
} from '../src/utils/annotations';

// GraphCanvas maps between the host's canvas-shape overlay descriptors and
// ReactFlow nodes. These translators must be exact inverses so an annotation
// survives the save-view round-trip (onSaveView → PUT → annotationsToRestore).
describe('overlay serialization', () => {
  it('round-trips a note (size <-> style)', () => {
    const overlay = { id: 'note-1', kind: 'note', position: { x: 1, y: 2 }, text: 'hi', color: '#FEF08A', size: { w: 200, h: 140 } };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      id: 'note-1', type: 'note', position: { x: 1, y: 2 },
      style: { width: 200, height: 140 }, data: { text: 'hi', color: '#FEF08A' },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('defaults a note to 200x140 when size is missing', () => {
    const node = overlayToFlowNode({ id: 'n', kind: 'note', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({ width: 200, height: 140 });
  });

  it('round-trips a label', () => {
    const overlay = { id: 'label-1', kind: 'label', position: { x: 3, y: 4 }, text: 'L', color: '#fff' };
    expect(flowNodeToOverlay(overlayToFlowNode(overlay))).toEqual(overlay);
  });

  it('round-trips an arrow, preserving a legitimate dx of 0', () => {
    const overlay = { id: 'arrow-1', kind: 'arrow', position: { x: 5, y: 6 }, dx: 0, dy: 40, color: '#fff' };
    const node = overlayToFlowNode(overlay);
    expect(node.data).toEqual({ dx: 0, dy: 40, color: '#fff' });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('recognises overlays and groups as manual (preserved) nodes', () => {
    expect(isManualNode({ id: 'note-1', type: 'note' })).toBe(true);
    expect(isManualNode({ id: 'arrow-1', type: 'arrow' })).toBe(true);
    expect(isManualNode({ id: 'group-1', type: 'group' })).toBe(true);
    expect(isManualNode({ id: 'x', type: 'custom' })).toBe(false);
  });
});
