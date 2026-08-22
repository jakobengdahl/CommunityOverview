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

// The v1 annotation model (docs/ANNOTATION_CONTRACT.md) also defines text,
// frame, shape, icon, vote_dot and image, created via the generic MCP
// annotation tools (c0edb4f). annotationsToOverlays used to only branch on
// note/label/line, so these types were silently dropped from onSaveView and
// from the annotation_created/annotation_updated live-op path in App.jsx —
// an MCP agent could create them, but they never appeared on the canvas.
describe('generic annotation overlay translation', () => {
  it('round-trips a text annotation (colour/font size via style)', () => {
    const overlays = [
      {
        id: 'text-1',
        kind: 'text',
        position: { x: 1, y: 2 },
        text: 'Region',
        color: '#fff',
        fontSize: 20,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toEqual({ color: '#fff', fontSize: 20 });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a frame annotation (size lives in geometry, not a payload field)', () => {
    const overlays = [
      {
        id: 'frame-1',
        kind: 'frame',
        position: { x: 0, y: 0 },
        color: '#4ADE80',
        size: { w: 300, h: 200 },
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].geometry).toMatchObject({ w: 300, h: 200 });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a shape annotation', () => {
    const overlays = [
      {
        id: 'shape-1',
        kind: 'shape',
        position: { x: 5, y: 5 },
        shape: 'circle',
        color: '#60A5FA',
        size: { w: 120, h: 120 },
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].shape).toBe('circle');
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips an icon annotation', () => {
    const overlays = [
      { id: 'icon-1', kind: 'icon', position: { x: 8, y: 8 }, icon: 'flag', color: '#F472B6' },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].icon).toBe('flag');
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a vote_dot annotation', () => {
    const overlays = [
      { id: 'vote-1', kind: 'vote_dot', position: { x: 9, y: 9 }, value: 3, color: '#FB923C' },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].value).toBe(3);
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips an image annotation (URL content only, no upload)', () => {
    const overlays = [
      {
        id: 'image-1',
        kind: 'image',
        position: { x: 2, y: 3 },
        image: { url: 'https://example.com/a.png' },
        alt: 'diagram',
        size: { w: 240, h: 180 },
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].image).toEqual({ url: 'https://example.com/a.png' });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('no longer drops the generic v1 types that used to be silently ignored', () => {
    const server = [
      { id: 't1', type: 'text', position: { x: 0, y: 0 }, text: 'hi' },
      {
        id: 'f1',
        type: 'frame',
        position: { x: 0, y: 0 },
        geometry: { x: 0, y: 0, w: 160, h: 96 },
      },
      { id: 's1', type: 'shape', position: { x: 0, y: 0 }, shape: 'rectangle' },
      { id: 'i1', type: 'icon', position: { x: 0, y: 0 }, icon: 'flag' },
      { id: 'v1', type: 'vote_dot', position: { x: 0, y: 0 }, value: 1 },
      { id: 'im1', type: 'image', position: { x: 0, y: 0 }, image: { url: 'https://x/y.png' } },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays.map((o) => o.kind).sort()).toEqual([
      'frame',
      'icon',
      'image',
      'shape',
      'text',
      'vote_dot',
    ]);
  });
});
