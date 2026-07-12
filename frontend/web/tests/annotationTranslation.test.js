import { describe, it, expect } from 'vitest';
import { annotationsToOverlays, overlaysToAnnotations } from '../src/utils/sessionAnnotations';

// The canvas emits overlay descriptors (note/label/arrow) via onSaveView; App
// stores them in the server annotation model and restores them on load. These
// tests lock the two translations as exact inverses so a session round-trips.
describe('annotation overlay translation', () => {
  it('round-trips a note through the server model', () => {
    const overlays = [
      {
        id: 'note-1',
        kind: 'note',
        position: { x: 10, y: 20 },
        text: 'hello',
        color: '#FEF08A',
        size: { w: 200, h: 140 },
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0]).toMatchObject({
      id: 'note-1',
      kind: 'note',
      text: 'hello',
      color: '#FEF08A',
    });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a label (colour stored under style)', () => {
    const overlays = [
      { id: 'label-1', kind: 'label', position: { x: 3, y: 4 }, text: 'Region', color: '#fff' },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toEqual({ color: '#fff' });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips an arrow via absolute from/to points', () => {
    const overlays = [
      {
        id: 'arrow-1',
        kind: 'arrow',
        position: { x: 5, y: 6 },
        dx: 100,
        dy: 40,
        color: '#fff',
        startArrow: false,
        endArrow: true,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].from).toEqual({ x: 5, y: 6 });
    expect(server[0].to).toEqual({ x: 105, y: 46 });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a double-headed line with anchors and no colour', () => {
    const overlays = [
      {
        id: 'arrow-2',
        kind: 'arrow',
        position: { x: 0, y: 0 },
        dx: 80,
        dy: 0,
        color: undefined,
        startArrow: true,
        endArrow: false,
        startAnchor: 'node-a',
        endAnchor: 'node-b',
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].startArrow).toBe(true);
    expect(server[0].endArrow).toBe(false);
    expect(server[0].startAnchor).toBe('node-a');
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips note and label text sizes', () => {
    const overlays = [
      {
        id: 'note-1',
        kind: 'note',
        position: { x: 0, y: 0 },
        text: 'n',
        color: '#FEF08A',
        fontSize: 24,
        size: { w: 200, h: 140 },
      },
      {
        id: 'label-1',
        kind: 'label',
        position: { x: 1, y: 1 },
        text: 'l',
        color: '#fff',
        fontSize: 28,
      },
    ];
    expect(annotationsToOverlays(overlaysToAnnotations(overlays))).toEqual(overlays);
  });

  it('ignores group annotations (handled separately)', () => {
    const server = [
      { id: 'g1', kind: 'group', position: { x: 0, y: 0 }, member_node_ids: ['n1'] },
      { id: 'n1', kind: 'note', position: { x: 1, y: 1 }, text: 'x' },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays).toHaveLength(1);
    expect(overlays[0].kind).toBe('note');
  });
});
