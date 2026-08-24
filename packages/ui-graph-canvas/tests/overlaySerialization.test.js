import { describe, it, expect } from 'vitest';
import {
  overlayToFlowNode,
  flowNodeToOverlay,
  isManualNode,
  isArrowAnchored,
  isArrowHeld,
  isRemoteLocked,
  isAnnotationDraggable,
  nodeCenter,
  findSnapTarget,
  resolveAnchoredArrow,
  computeDroppedAttachment,
  resolveAttachedPosition,
  ATTACHABLE_OVERLAY_KINDS,
  ATTACH_SNAP_RADIUS,
  OVERLAY_TYPES,
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
      z: 0,
      locked: false,
      rotation: 0,
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

  // z (layer order), locked (the canvas UI's own edit-lock convention, set
  // via the generic MCP annotation tools) and rotation are envelope fields on
  // every v1 annotation type. A translator that dropped them would, on the
  // host's next autosave, diff the annotation back to its
  // z=0/locked=false/rotation=0 defaults and overwrite a collaborator's or
  // agent's change.
  it('round-trips a non-zero z, a locked flag and a rotation through the flow node', () => {
    const overlay = {
      id: 'note-1',
      kind: 'note',
      position: { x: 1, y: 2 },
      text: 'hi',
      color: '#FEF08A',
      fontSize: 18,
      size: { w: 200, h: 140 },
      z: 5,
      locked: true,
      rotation: 30,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.zIndex).toBe(5);
    expect(node.draggable).toBe(false);
    expect(node.data.locked).toBe(true);
    expect(node.data.rotation).toBe(30);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('defaults z, locked and rotation when absent from the overlay (a freshly created annotation)', () => {
    const node = overlayToFlowNode({ id: 'n', kind: 'note', position: { x: 0, y: 0 } });
    expect(node.zIndex).toBe(0);
    expect(node.draggable).toBe(true);
    expect(flowNodeToOverlay(node)).toMatchObject({ z: 0, locked: false, rotation: 0 });
  });

  it('defaults z, locked and rotation when absent from a bare flow node (never synced yet)', () => {
    const node = { id: 'n', type: 'note', position: { x: 0, y: 0 }, data: {} };
    expect(flowNodeToOverlay(node)).toMatchObject({ z: 0, locked: false, rotation: 0 });
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
      z: 0,
      locked: false,
      rotation: 0,
    };
    expect(flowNodeToOverlay(overlayToFlowNode(overlay))).toEqual(overlay);
  });

  it('round-trips a label with an attachment', () => {
    const overlay = {
      id: 'label-2',
      kind: 'label',
      position: { x: 3, y: 4 },
      text: 'L',
      color: '#fff',
      fontSize: 28,
      attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 5, y: 5 } },
      z: 0,
      locked: false,
      rotation: 0,
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
    expect(node.data).toEqual({
      dx: 0,
      dy: 40,
      color: '#fff',
      startArrow: false,
      endArrow: true,
      locked: false,
      rotation: 0,
    });
    // Defaults are materialised on the way back so the model is explicit.
    expect(flowNodeToOverlay(node)).toEqual({
      ...overlay,
      startArrow: false,
      endArrow: true,
      z: 0,
      locked: false,
      rotation: 0,
    });
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
      z: 0,
      locked: false,
      rotation: 0,
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

  it('keeps a locked arrow non-draggable even without anchors', () => {
    const node = overlayToFlowNode({
      id: 'arrow-4',
      kind: 'arrow',
      position: { x: 0, y: 0 },
      dx: 10,
      dy: 10,
      locked: true,
    });
    expect(node.draggable).toBe(false);
  });

  it('recognises overlays and groups as manual (preserved) nodes', () => {
    expect(isManualNode({ id: 'note-1', type: 'note' })).toBe(true);
    expect(isManualNode({ id: 'arrow-1', type: 'arrow' })).toBe(true);
    expect(isManualNode({ id: 'group-1', type: 'group' })).toBe(true);
    expect(isManualNode({ id: 'x', type: 'custom' })).toBe(false);
  });
});

// The v1 annotation model (docs/ANNOTATION_CONTRACT.md) also defines text,
// frame, shape, icon, vote_dot and image. These were previously store/MCP-only:
// the canvas normalized them (annotationModel.js) but silently dropped them
// here, so an MCP-created shape or image never appeared on screen. These
// round-trips lock in a simple, generic visual representation for each.
describe('generic annotation overlay serialization', () => {
  it('registers text/frame/shape/icon/vote_dot/image as overlay types', () => {
    for (const kind of ['text', 'frame', 'shape', 'icon', 'vote_dot', 'image']) {
      expect(OVERLAY_TYPES.has(kind)).toBe(true);
    }
  });

  it('round-trips a text overlay (colour/font size)', () => {
    const overlay = {
      id: 'text-1',
      kind: 'text',
      position: { x: 1, y: 2 },
      text: 'Hi',
      color: '#fff',
      fontSize: 20,
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      id: 'text-1',
      type: 'text',
      position: { x: 1, y: 2 },
      data: { text: 'Hi', color: '#fff', fontSize: 20 },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips a frame overlay with an explicit size', () => {
    const overlay = {
      id: 'frame-1',
      kind: 'frame',
      position: { x: 0, y: 0 },
      color: '#4ADE80',
      size: { w: 300, h: 200 },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      id: 'frame-1',
      type: 'frame',
      style: { width: 300, height: 200 },
      data: { color: '#4ADE80' },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('defaults a frame to 160x96 when size is missing', () => {
    const node = overlayToFlowNode({ id: 'f', kind: 'frame', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({ width: 160, height: 96 });
  });

  it('round-trips a shape overlay (shape name, colour, size)', () => {
    const overlay = {
      id: 'shape-1',
      kind: 'shape',
      position: { x: 5, y: 5 },
      shape: 'circle',
      color: '#60A5FA',
      size: { w: 120, h: 120 },
      z: 2,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data).toEqual({ shape: 'circle', color: '#60A5FA', locked: false, rotation: 0 });
    expect(node.zIndex).toBe(2);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips a non-zero rotation on a generic overlay', () => {
    const overlay = {
      id: 'shape-2',
      kind: 'shape',
      position: { x: 0, y: 0 },
      shape: 'process_arrow',
      color: '#60A5FA',
      size: { w: 160, h: 96 },
      z: 0,
      locked: false,
      rotation: 135,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data.rotation).toBe(135);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips a locked icon overlay', () => {
    const overlay = {
      id: 'icon-1',
      kind: 'icon',
      position: { x: 8, y: 8 },
      icon: 'flag',
      color: '#F472B6',
      z: 0,
      locked: true,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data).toEqual({ icon: 'flag', color: '#F472B6', locked: true, rotation: 0 });
    expect(node.draggable).toBe(false);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips a vote_dot overlay, preserving a value of 0', () => {
    const overlay = {
      id: 'vote-1',
      kind: 'vote_dot',
      position: { x: 9, y: 9 },
      value: 0,
      color: '#FB923C',
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data).toEqual({ value: 0, color: '#FB923C', locked: false, rotation: 0 });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips an image overlay (URL content, alt, and colour)', () => {
    const overlay = {
      id: 'image-1',
      kind: 'image',
      position: { x: 2, y: 3 },
      image: { url: 'https://example.com/a.png' },
      alt: 'diagram',
      color: '#38BDF8',
      size: { w: 240, h: 180 },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      type: 'image',
      style: { width: 240, height: 180 },
      data: { image: { url: 'https://example.com/a.png' }, alt: 'diagram', color: '#38BDF8' },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips a freehand overlay (node-relative points, pressure, stroke style)', () => {
    const overlay = {
      id: 'freehand-1',
      kind: 'freehand',
      position: { x: 10, y: 10 },
      points: [
        { x: 0, y: 0, pressure: 0.4 },
        { x: 5, y: 3 },
        { x: 12, y: 1 },
      ],
      color: '#F472B6',
      strokeWidth: 3,
      smoothing: 0.5,
      pointerType: 'pen',
      pressureSource: 'device',
      z: 1,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node).toMatchObject({
      id: 'freehand-1',
      type: 'freehand',
      position: { x: 10, y: 10 },
      data: {
        points: overlay.points,
        color: '#F472B6',
        strokeWidth: 3,
        smoothing: 0.5,
        pointerType: 'pen',
        pressureSource: 'device',
      },
    });
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips an attachment on a text overlay', () => {
    const overlay = {
      id: 'text-2',
      kind: 'text',
      position: { x: 40, y: 40 },
      text: 'attached',
      color: '#fff',
      fontSize: 16,
      attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 10, y: -5 } },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.data.attachment).toEqual(overlay.attachment);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips an attachment on an icon overlay', () => {
    const overlay = {
      id: 'icon-2',
      kind: 'icon',
      position: { x: 1, y: 1 },
      icon: 'flag',
      color: '#fff',
      attachment: { target_id: 'node-2', target_type: 'annotation', offset: { x: 0, y: 20 } },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  it('round-trips an attachment on a vote_dot overlay', () => {
    const overlay = {
      id: 'vote-2',
      kind: 'vote_dot',
      position: { x: 1, y: 1 },
      value: 5,
      color: '#fff',
      attachment: { target_id: 'node-3', target_type: 'node', offset: { x: -8, y: -8 } },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(flowNodeToOverlay(node)).toEqual(overlay);
  });

  // 61d5cc7b / smallfix-annotation-unsized-generic-geometry-clobber: icon,
  // vote_dot and text carry no `style` box (RESIZABLE_KINDS in
  // GenericAnnotationNode.jsx excludes them), so before this fix
  // overlayToFlowNode had nowhere to put their geometry.w/h and
  // flowNodeToOverlay always dropped it. sessionAnnotations.js now carries
  // that geometry unconditionally on the server-facing side; this pins the
  // matching fix on the live-canvas side (`data.size`, mirroring how
  // `data.rotation` survives independent of ROTATABLE_OVERLAY_KINDS).
  it.each(['text', 'icon', 'vote_dot'])(
    'round-trips geometry.w/h for a %s through a data-only slot, not a style box',
    (kind) => {
      const overlay = {
        id: `${kind}-sized`,
        kind,
        position: { x: 1, y: 1 },
        text: kind === 'text' ? 'hi' : undefined,
        icon: kind === 'icon' ? 'flag' : undefined,
        value: kind === 'vote_dot' ? 3 : undefined,
        color: '#fff',
        size: { w: 32, h: 32 },
        z: 0,
        locked: false,
        rotation: 0,
      };
      const node = overlayToFlowNode(overlay);
      expect(node.data.size).toEqual({ w: 32, h: 32 });
      // Not exposed as a resizable box — no `style` at all for these kinds.
      expect(node.style).toBeUndefined();
      expect(flowNodeToOverlay(node)).toEqual(overlay);
    }
  );

  it('drops a stale style box on a non-sized kind rather than treating it as geometry', () => {
    // A node that somehow carries both a `style` box (e.g. leftover from a
    // prior render) and a `data.size` must still read geometry from
    // `data.size` for a non-sized kind — style is never consulted for these.
    const node = {
      id: 'icon-1',
      type: 'icon',
      position: { x: 0, y: 0 },
      style: { width: 999, height: 999 },
      data: { icon: 'flag', locked: false, rotation: 0, size: { w: 32, h: 32 } },
    };
    expect(flowNodeToOverlay(node).size).toEqual({ w: 32, h: 32 });
  });

  it('carries no size on a freshly hydrated icon/vote_dot/text overlay with no size given', () => {
    const node = overlayToFlowNode({ id: 't', kind: 'text', position: { x: 0, y: 0 } });
    expect(node.data.size).toBeUndefined();
    expect(flowNodeToOverlay(node).size).toBeUndefined();
  });

  it('leaves attachment absent by default on generic overlays', () => {
    const node = overlayToFlowNode({ id: 't', kind: 'text', position: { x: 0, y: 0 } });
    expect(node.data.attachment).toBeUndefined();
  });

  it('drags a freehand node without rewriting its point coordinates', () => {
    // The node-relative convention: moving `position` alone (a plain
    // ReactFlow drag) must be enough to move the whole stroke — `data.points`
    // never needs to change.
    const overlay = {
      id: 'freehand-1',
      kind: 'freehand',
      position: { x: 0, y: 0 },
      points: [
        { x: 0, y: 0 },
        { x: 10, y: 10 },
      ],
      color: '#fff',
      z: 0,
      locked: false,
    };
    const node = overlayToFlowNode(overlay);
    const dragged = { ...node, position: { x: 100, y: 200 } };
    const draggedOverlay = flowNodeToOverlay(dragged);
    expect(draggedOverlay.position).toEqual({ x: 100, y: 200 });
    expect(draggedOverlay.points).toEqual(overlay.points);
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

describe('ATTACHABLE_OVERLAY_KINDS', () => {
  it('names exactly the node-attachable generic kinds', () => {
    expect(ATTACHABLE_OVERLAY_KINDS).toEqual(new Set(['text', 'label', 'icon', 'vote_dot']));
  });
});

describe('computeDroppedAttachment', () => {
  const nodes = [
    { id: 'target', type: 'custom', position: { x: 100, y: 100 }, width: 0, height: 0 },
    { id: 'self', type: 'label', position: { x: 100, y: 100 }, width: 0, height: 0 },
  ];

  it('attaches to a node within the snap radius, storing the drop offset', () => {
    const dropped = { x: 130, y: 90 };
    expect(computeDroppedAttachment(dropped, nodes, 'self')).toEqual({
      target_id: 'target',
      target_type: 'node',
      offset: { x: 30, y: -10 },
    });
  });

  it('reports target_type "annotation" when the target is an annotation overlay', () => {
    const overlayNodes = [
      { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, width: 0, height: 0 },
      { id: 'self', type: 'label', position: { x: 0, y: 0 }, width: 0, height: 0 },
    ];
    expect(computeDroppedAttachment({ x: 5, y: 5 }, overlayNodes, 'self')).toMatchObject({
      target_id: 'note-1',
      target_type: 'annotation',
    });
  });

  // Contract: "frame and group are containment/visual constructs, not
  // attachment targets" — dropping near one must not attach to it, even when
  // it is the nearest candidate.
  it('never attaches to a frame or a group, even when nothing else is nearby', () => {
    const withFrameAndGroup = [
      { id: 'frame-1', type: 'frame', position: { x: 100, y: 100 }, width: 0, height: 0 },
      { id: 'group-1', type: 'group', position: { x: 100, y: 100 }, width: 0, height: 0 },
      { id: 'self', type: 'label', position: { x: 100, y: 100 }, width: 0, height: 0 },
    ];
    expect(computeDroppedAttachment({ x: 100, y: 100 }, withFrameAndGroup, 'self')).toBeNull();
  });

  it('skips a nearby frame to attach to a real node within the same radius', () => {
    const mixed = [
      { id: 'frame-1', type: 'frame', position: { x: 100, y: 100 }, width: 0, height: 0 },
      { id: 'node-1', type: 'custom', position: { x: 105, y: 105 }, width: 0, height: 0 },
      { id: 'self', type: 'label', position: { x: 100, y: 100 }, width: 0, height: 0 },
    ];
    expect(computeDroppedAttachment({ x: 100, y: 100 }, mixed, 'self')).toMatchObject({
      target_id: 'node-1',
    });
  });

  it('returns null outside the snap radius (detach)', () => {
    expect(
      computeDroppedAttachment({ x: 100 + ATTACH_SNAP_RADIUS + 1, y: 100 }, nodes, 'self')
    ).toBeNull();
  });

  it('never attaches to itself, even at distance 0 from its own centre', () => {
    const onlySelf = [{ id: 'self', type: 'label', position: { x: 0, y: 0 }, width: 0, height: 0 }];
    expect(computeDroppedAttachment({ x: 0, y: 0 }, onlySelf, 'self')).toBeNull();
  });
});

describe('resolveAttachedPosition', () => {
  it('returns null when the node carries no attachment', () => {
    const node = { position: { x: 0, y: 0 }, data: {} };
    expect(resolveAttachedPosition(node, new Map())).toBeNull();
  });

  it('recomputes the position from the target centre plus the stored offset', () => {
    const node = {
      position: { x: 0, y: 0 },
      data: { attachment: { target_id: 'a', offset: { x: 10, y: -5 } } },
    };
    const centers = new Map([['a', { x: 50, y: 50 }]]);
    expect(resolveAttachedPosition(node, centers)).toEqual({ x: 60, y: 45 });
  });

  it('returns null (freezes position) when the target is absent', () => {
    const node = {
      position: { x: 12, y: 34 },
      data: { attachment: { target_id: 'gone', offset: { x: 0, y: 0 } } },
    };
    expect(resolveAttachedPosition(node, new Map())).toBeNull();
  });

  it('is idempotent when the position already matches (loop-safe)', () => {
    const node = {
      position: { x: 60, y: 45 },
      data: { attachment: { target_id: 'a', offset: { x: 10, y: -5 } } },
    };
    const centers = new Map([['a', { x: 50, y: 50 }]]);
    expect(resolveAttachedPosition(node, centers)).toBeNull();
  });
});

// task-annotation-shared-session-realtime: another live client's selection
// claim makes an annotation's edit lease exclusive rather than merely
// advisory. isRemoteLocked/isAnnotationDraggable are the single source of
// truth every annotation component and GraphCanvas's remote-claim effect
// reads to enforce it.
describe('isRemoteLocked / isAnnotationDraggable (exclusive annotation leases)', () => {
  const CLAIM = { clientId: 'c2', color: '#e6194b', displayName: 'Ada' };

  it('isRemoteLocked is false with no remoteSelection and true once one is set', () => {
    expect(isRemoteLocked(undefined)).toBe(false);
    expect(isRemoteLocked({})).toBe(false);
    expect(isRemoteLocked({ remoteSelection: null })).toBe(false);
    expect(isRemoteLocked({ remoteSelection: CLAIM })).toBe(true);
  });

  it('a plain unlocked, unclaimed annotation is draggable', () => {
    expect(isAnnotationDraggable({ type: 'note', data: {} })).toBe(true);
  });

  it('a persisted `locked` flag blocks dragging, independent of any claim', () => {
    expect(isAnnotationDraggable({ type: 'note', data: { locked: true } })).toBe(false);
  });

  it('a live remote claim blocks dragging even when unlocked', () => {
    expect(
      isAnnotationDraggable({ type: 'note', data: { locked: false, remoteSelection: CLAIM } })
    ).toBe(false);
  });

  it('an anchored arrow stays non-draggable regardless of claim state', () => {
    expect(isAnnotationDraggable({ type: 'arrow', data: { startAnchor: 'node-a' } })).toBe(false);
  });

  it('an unanchored, unlocked, unclaimed arrow is draggable', () => {
    expect(isAnnotationDraggable({ type: 'arrow', data: {} })).toBe(true);
  });

  it('a group box is subject to the same claim-based exclusivity as any other annotation', () => {
    expect(isAnnotationDraggable({ type: 'group', data: {} })).toBe(true);
    expect(isAnnotationDraggable({ type: 'group', data: { remoteSelection: CLAIM } })).toBe(false);
  });
});
