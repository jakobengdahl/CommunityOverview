import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  annotationsToOverlays,
  overlaysToAnnotations,
  annotationsToGroups,
  annotationDocumentToLegacyMetadata,
  legacyMetadataToAnnotationDocument,
} from '../src/utils/sessionAnnotations';
// Reaches past the package's public entry point (which does not export these)
// straight at the ReactFlow-node translators, so this test can build the same
// GraphCanvas-shaped snapshot useSharedSession.js/App.jsx actually produce —
// see the cross-layer test below.
import {
  overlayToFlowNode,
  flowNodeToOverlay,
} from '../../../packages/ui-graph-canvas/src/utils/annotations';

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
        z: 0,
        locked: false,
        rotation: 0,
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
      {
        id: 'label-1',
        kind: 'label',
        position: { x: 3, y: 4 },
        text: 'Region',
        color: '#fff',
        z: 0,
        locked: false,
        rotation: 0,
      },
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
        z: 0,
        locked: false,
        rotation: 0,
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
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].startArrow).toBe(true);
    expect(server[0].endArrow).toBe(false);
    expect(server[0].startAnchor).toBe('node-a');
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // smallfix-line-endpoint-attachment-dropped-by-translator: docs/ANNOTATION_
  // CONTRACT.md makes a line's start/end endpoint attachment (attach to a
  // node or another annotation) a first-class v1 field, validated server-side
  // (backend/core/session_annotations.py's _line_endpoint_error). This
  // translator used to read only from/to/startAnchor/endAnchor and never
  // start/end, so the attachment was rebuilt as a bare point on the very
  // first browser round trip, silently discarding what an MCP agent set.
  it("preserves a line endpoint's attachment across the full round trip", () => {
    const server = [
      {
        id: 'line-attached',
        type: 'line',
        position: { x: 0, y: 0 },
        from: { x: 0, y: 0 },
        to: { x: 160, y: 0 },
        start: {
          point: { x: 0, y: 0 },
          attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 5, y: -5 } },
        },
        end: { point: { x: 160, y: 0 } },
        startArrow: false,
        endArrow: true,
      },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays[0].start.attachment).toMatchObject({
      target_id: 'node-1',
      target_type: 'node',
      offset: { x: 5, y: -5 },
    });
    // The unattached end endpoint stays a bare point and must not spuriously
    // pick up an `end` field on the overlay.
    expect(overlays[0].end).toBeUndefined();

    const roundTripped = overlaysToAnnotations(overlays);
    expect(roundTripped[0].start.attachment).toMatchObject({
      target_id: 'node-1',
      target_type: 'node',
      offset: { x: 5, y: -5 },
    });
    expect(roundTripped[0].end.attachment).toBeUndefined();
  });

  // A line with no attachment on either endpoint must round-trip identically
  // to before this fix — the overlay must not grow a spurious start/end
  // field just because createAnnotation always produces at least `{point}`
  // internally.
  it('does not add a start/end field to a plain, unattached line overlay', () => {
    const overlays = [
      {
        id: 'arrow-plain',
        kind: 'arrow',
        position: { x: 0, y: 0 },
        dx: 100,
        dy: 0,
        color: '#fff',
        startArrow: false,
        endArrow: true,
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].start.attachment).toBeUndefined();
    expect(server[0].end.attachment).toBeUndefined();
    const roundTripped = annotationsToOverlays(server);
    expect(roundTripped[0].start).toBeUndefined();
    expect(roundTripped[0].end).toBeUndefined();
    expect(roundTripped).toEqual(overlays);
  });

  // Regression for the review finding on smallfix-line-endpoint-attachment-
  // dropped-by-translator: this translator fix alone was not enough. The
  // realtime sync baseline (useSharedSession.js's serverStateToMirror) runs
  // this file's round trip directly, but the live snapshot actually synced
  // (App.jsx's persistSessionSnapshot, fed by GraphCanvas's onSaveView) goes
  // through packages/ui-graph-canvas's overlayToFlowNode/flowNodeToOverlay
  // first. Before that layer also carried start/end through, the baseline had
  // the attachment and the first real browser snapshot did not — a mismatch
  // sessionSyncClient.js's whole-object comparison read as a real edit,
  // producing a full-object annotation_updated op that
  // backend/core/session_store.py's shallow `target.update(incoming)` merge
  // used to silently delete the attachment on the very first time the
  // session opened in a browser. Both translators must agree, or autosave
  // ships this regression again.
  it('produces the same annotation for an attached line whether built via the sync baseline or a live GraphCanvas round trip', () => {
    const server = [
      {
        id: 'line-attached',
        type: 'line',
        position: { x: 0, y: 0 },
        from: { x: 0, y: 0 },
        to: { x: 160, y: 0 },
        start: {
          point: { x: 0, y: 0 },
          attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 5, y: -5 } },
        },
        end: { point: { x: 160, y: 0 } },
        startArrow: false,
        endArrow: true,
      },
    ];

    // Baseline mirror: exactly useSharedSession.js's serverStateToMirror.
    const baselineMirror = overlaysToAnnotations(annotationsToOverlays(server));

    // GraphCanvas-shaped snapshot: hydrate to overlays (as on session load),
    // build a ReactFlow node per overlay (GraphCanvas hydration), serialize
    // each back to an overlay (GraphCanvas's onSaveView), then run the same
    // overlaysToAnnotations leg persistSessionSnapshot runs on viewData.annotations.
    const hydratedOverlays = annotationsToOverlays(server);
    const canvasOverlays = hydratedOverlays.map((o) => flowNodeToOverlay(overlayToFlowNode(o)));
    const graphCanvasSnapshot = overlaysToAnnotations(canvasOverlays);

    expect(graphCanvasSnapshot).toEqual(baselineMirror);
    expect(graphCanvasSnapshot[0].start.attachment).toMatchObject({
      target_id: 'node-1',
      target_type: 'node',
      offset: { x: 5, y: -5 },
    });
  });

  it('round-trips a freehand stroke via absolute model-space points', () => {
    const overlays = [
      {
        id: 'freehand-1',
        kind: 'freehand',
        position: { x: 5, y: 6 },
        points: [
          { x: 0, y: 0, pressure: 0.6 },
          { x: 10, y: 4 },
          { x: 18, y: -2 },
        ],
        color: '#F472B6',
        strokeWidth: 3,
        smoothing: 0.4,
        pointerType: 'pen',
        pressureSource: 'device',
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    // The server document stores absolute model-space points (anchor + each
    // node-relative offset), unlike the overlay's node-relative points.
    expect(server[0].points).toEqual([
      { x: 5, y: 6, pressure: 0.6 },
      { x: 15, y: 10 },
      { x: 23, y: 4 },
    ]);
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('defaults strokeWidth/smoothing for a freehand stroke drawn at the origin with no style given', () => {
    const overlays = [
      {
        id: 'freehand-2',
        kind: 'freehand',
        position: { x: 0, y: 0 },
        points: [
          { x: 0, y: 0 },
          { x: 3, y: 3 },
        ],
        color: undefined,
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].points).toEqual([
      { x: 0, y: 0 },
      { x: 3, y: 3 },
    ]);
    // strokeWidth/smoothing always resolve to a concrete value in the
    // canonical model (annotationModel.js's withTypePayload), unlike colour
    // which stays undefined when not given — so these do not survive as
    // `undefined` the way the "no colour" line case does.
    expect(server[0].strokeWidth).toBeGreaterThan(0);
    expect(server[0].smoothing).toBe(0);
    const roundTripped = annotationsToOverlays(server);
    expect(roundTripped[0]).toMatchObject({
      id: 'freehand-2',
      kind: 'freehand',
      position: { x: 0, y: 0 },
      points: overlays[0].points,
    });
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
        z: 0,
        locked: false,
        rotation: 0,
      },
      {
        id: 'label-1',
        kind: 'label',
        position: { x: 1, y: 1 },
        text: 'l',
        color: '#fff',
        fontSize: 28,
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    expect(annotationsToOverlays(overlaysToAnnotations(overlays))).toEqual(overlays);
  });

  // z (layer order), locked (the canvas UI's own edit-lock convention set by
  // the generic MCP annotation tools, docs/ANNOTATION_CONTRACT.md) and
  // rotation are envelope fields on every v1 annotation type. Before this fix,
  // annotationsToOverlays
  // silently dropped them, so a browser's next autosave would diff the
  // annotation back to z=0/locked=false and overwrite a collaborator's or
  // agent's `reorder_annotation`/`set_annotation_lock` call — a realtime-sync
  // data-loss bug, not just a rendering gap.
  it('preserves a non-default z and locked flag across the full round trip', () => {
    const overlays = [
      {
        id: 'note-1',
        kind: 'note',
        position: { x: 0, y: 0 },
        text: 'n',
        color: '#FEF08A',
        size: { w: 200, h: 140 },
        z: 3,
        locked: true,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0]).toMatchObject({ z: 3, locked: true });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // Rotation is the third envelope field with the same failure mode: the
  // model, the backend and the MCP tools all carried geometry.rotation, but
  // this translation dropped it, so a rotation set by an agent survived until
  // the browser's next autosave diffed it back to 0.
  it('preserves a rotation set on the server annotation across the full round trip', () => {
    const server = [
      {
        id: 'shape-1',
        type: 'shape',
        position: { x: 0, y: 0 },
        geometry: { x: 0, y: 0, w: 160, h: 96, rotation: 45 },
        shape: 'triangle',
      },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays[0].rotation).toBe(45);
    expect(overlaysToAnnotations(overlays)[0].geometry.rotation).toBe(45);
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
// shape, icon, vote_dot and image, created via the generic MCP
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
        size: { w: 220, h: 60 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toEqual({ color: '#fff', fontSize: 20 });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // task-annotation-drag-to-draw: mirroring is set by the direction the shape
  // was drawn in and is the ONLY way to aim a directional variant — there is
  // no flip control anywhere in the UI. Dropping it on the write path meant a
  // triangle came back from a reload pointing the other way, and a
  // collaborator never saw the aiming at all. Both directions are pinned here
  // because the host keeps its own field whitelist, independent of the canvas
  // package's GENERIC_OVERLAY_FIELDS.
  it("round-trips a shape's mirror flags", () => {
    const overlays = [
      {
        id: 'shape-1',
        kind: 'shape',
        position: { x: 5, y: 6 },
        shape: 'triangle',
        text: '',
        flipX: true,
        flipY: true,
        size: { w: 120, h: 104 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style.flipX).toBe(true);
    expect(server[0].style.flipY).toBe(true);
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('leaves an unmirrored shape with no flip fields at all', () => {
    // Absent-when-false: an existing shape must not gain new fields, and the
    // round trip must not turn `undefined` into `false`.
    const overlays = [
      {
        id: 'shape-2',
        kind: 'shape',
        position: { x: 0, y: 0 },
        shape: 'rectangle',
        text: '',
        size: { w: 160, h: 96 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style.flipX).toBeUndefined();
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // task-annotation-text-alignment-and-font: alignment and font family are
  // new fields, carried under `style` alongside `fontSize` — same
  // "unsized-geometry clobber" risk as any other new content field, so this
  // pins that both directions carry all three, not just fontSize.
  it("round-trips a text annotation's alignment and font family", () => {
    const overlays = [
      {
        id: 'text-2b',
        kind: 'text',
        position: { x: 1, y: 2 },
        text: 'Region',
        color: '#fff',
        fontSize: 20,
        textAlign: 'middle-center',
        font: 'serif',
        size: { w: 220, h: 60 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toEqual({
      color: '#fff',
      fontSize: 20,
      textAlign: 'middle-center',
      font: 'serif',
    });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it("round-trips a shape's caption alignment, font size and font family", () => {
    const overlays = [
      {
        id: 'shape-2b',
        kind: 'shape',
        position: { x: 0, y: 0 },
        shape: 'hexagon',
        text: 'Step 2',
        fill: '#60A5FA',
        border: 'transparent',
        fontSize: 18,
        textAlign: 'top-left',
        font: 'monospace',
        size: { w: 160, h: 96 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toEqual({
      fill: '#60A5FA',
      border: 'transparent',
      fontSize: 18,
      textAlign: 'top-left',
      font: 'monospace',
    });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // task-annotation-merge-frame-into-shape-rectangle: `frame` is retired,
  // folded into `shape` — a transparent fill with a coloured border is what
  // `frame` used to draw. See [Fill and border](docs/ANNOTATION_CONTRACT.md).
  it('round-trips a shape with a transparent fill and a coloured border (size lives in geometry, not a payload field)', () => {
    const overlays = [
      {
        id: 'shape-frame-like',
        kind: 'shape',
        position: { x: 0, y: 0 },
        shape: 'rectangle',
        text: '',
        fill: 'transparent',
        border: '#4ADE80',
        size: { w: 300, h: 200 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].geometry).toMatchObject({ w: 300, h: 200 });
    expect(server[0].style).toMatchObject({ fill: 'transparent', border: '#4ADE80' });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a shape annotation with a non-default layer order', () => {
    const overlays = [
      {
        id: 'shape-1',
        kind: 'shape',
        position: { x: 5, y: 5 },
        shape: 'circle',
        // task-annotation-doubleclick-to-edit-text: shape now carries an
        // optional caption field, defaulting to '' like text/label's own.
        text: '',
        fill: '#60A5FA',
        border: 'transparent',
        size: { w: 120, h: 120 },
        z: 4,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].shape).toBe('circle');
    expect(server[0].z).toBe(4);
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('round-trips a locked icon annotation', () => {
    const overlays = [
      {
        id: 'icon-1',
        kind: 'icon',
        position: { x: 8, y: 8 },
        icon: 'flag',
        color: '#F472B6',
        size: { w: 32, h: 32 },
        z: 0,
        locked: true,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].icon).toBe('flag');
    expect(server[0].locked).toBe(true);
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // task-annotation-vote-dot-simplify: a vote dot is a plain coloured dot —
  // `color` is the only annotation-specific field either direction still
  // carries for it (`value` no longer exists).
  it('round-trips a vote_dot annotation (colour only, no value)', () => {
    const overlays = [
      {
        id: 'vote-1',
        kind: 'vote_dot',
        position: { x: 9, y: 9 },
        color: '#FB923C',
        size: { w: 24, h: 24 },
        z: 0,
        locked: false,
        rotation: 0,
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].style).toMatchObject({ color: '#FB923C' });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  // Explicit regression: a vote_dot stored before this change may still
  // carry `value` and/or `attachment` (no migration was written). Neither
  // leg of this translation may resurrect either field on a live overlay.
  it('drops a stale value and attachment on a stored vote_dot annotation', () => {
    const server = [
      {
        id: 'vote-legacy',
        type: 'vote_dot',
        position: { x: 9, y: 9 },
        value: 3,
        attachment: { target_id: 'node-1', target_type: 'node' },
        style: { color: '#FB923C' },
      },
    ];
    const [overlay] = annotationsToOverlays(server);
    expect(overlay.value).toBeUndefined();
    expect(overlay.attachment).toBeUndefined();
    expect(overlay.color).toBe('#FB923C');
  });

  it('round-trips an image annotation (URL content, alt, and colour)', () => {
    const overlays = [
      {
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
      },
    ];
    const server = overlaysToAnnotations(overlays);
    expect(server[0].image).toEqual({ url: 'https://example.com/a.png' });
    expect(server[0].style).toMatchObject({ color: '#38BDF8' });
    expect(annotationsToOverlays(server)).toEqual(overlays);
  });

  it('no longer drops the generic v1 types that used to be silently ignored', () => {
    const server = [
      { id: 't1', type: 'text', position: { x: 0, y: 0 }, text: 'hi' },
      { id: 's1', type: 'shape', position: { x: 0, y: 0 }, shape: 'rectangle' },
      { id: 'i1', type: 'icon', position: { x: 0, y: 0 }, icon: 'flag' },
      { id: 'v1', type: 'vote_dot', position: { x: 0, y: 0 }, value: 1 },
      { id: 'im1', type: 'image', position: { x: 0, y: 0 }, image: { url: 'https://x/y.png' } },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays.map((o) => o.kind).sort()).toEqual([
      'icon',
      'image',
      'shape',
      'text',
      'vote_dot',
    ]);
  });

  // task-annotation-merge-frame-into-shape-rectangle: `frame` is retired and
  // no longer a member of GENERIC_OVERLAY_TYPES — a stored `frame` (from
  // before this task) must be quietly dropped here, not resurrected or
  // thrown on, the same guarantee task-annotation-tolerate-unexpected-data
  // built for any other unrecognised kind.
  it('quietly drops a stored `frame` annotation (retired into `shape`) rather than throwing', () => {
    const server = [
      { id: 't1', type: 'text', position: { x: 0, y: 0 }, text: 'hi' },
      {
        id: 'f1',
        type: 'frame',
        position: { x: 0, y: 0 },
        geometry: { x: 0, y: 0, w: 160, h: 96 },
      },
    ];
    const overlays = annotationsToOverlays(server);
    expect(overlays.map((o) => o.id)).toEqual(['t1']);
  });
});

describe('a stored annotation this version cannot read', () => {
  // The failure this prevents is much worse than the one it looks like. The
  // normaliser rejects an unknown kind by throwing, and that throw used to
  // escape the whole document — so a single stored annotation of a kind that
  // no longer exists made the session fail to load. The user lost the entire
  // session, not one decoration, and in the deep-link path it surfaced as a
  // session that silently never opened.
  //
  // Annotation kinds may change without migrating what is stored, so meeting
  // an unknown one is expected input, not a corrupt document.
  let warn;
  afterEach(() => warn?.mockRestore());

  const mixed = [
    { id: 'good-1', kind: 'note', position: { x: 0, y: 0 }, text: 'survives' },
    { id: 'gone-1', kind: 'wormhole', position: { x: 10, y: 10 } },
    { id: 'good-2', kind: 'label', position: { x: 5, y: 5 }, text: 'also survives' },
    null,
    { id: 'gone-2', position: { x: 0, y: 0 } },
    'not an annotation at all',
  ];

  it('does not take the rest of the document down with it', () => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const overlays = annotationsToOverlays(mixed);
    expect(overlays.map((o) => o.id)).toEqual(['good-1', 'good-2']);
    expect(overlays.find((o) => o.id === 'good-1').text).toBe('survives');
  });

  it('reports what it dropped instead of losing it silently', () => {
    // A silent drop looks to the user like their annotation was deleted, and
    // leaves nobody a way to find out which stored shape stopped working.
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    annotationsToOverlays(mixed);
    expect(warn).toHaveBeenCalled();
    expect(warn.mock.calls.length).toBe(4);
  });

  // Each entry point into the normaliser has to pass the reporter, and each is
  // asserted separately. Wiring it at one call site and forgetting another is
  // invisible otherwise: the session still loads, so nothing fails — the drop
  // just becomes silent, which is the outcome this reporting exists to prevent.
  it.each([
    ['annotationsToOverlays (bare list)', () => annotationsToOverlays(mixed)],
    [
      'annotationsToOverlays (versioned document)',
      () => annotationsToOverlays({ schema_version: 1, annotations: mixed }),
    ],
    [
      'legacyMetadataToAnnotationDocument (groups + overlays)',
      // The branch that composes a document out of the older group/overlay
      // shapes. Only groups are malformed here: the overlay serialiser is a
      // write path and is deliberately still strict.
      () =>
        legacyMetadataToAnnotationDocument({
          groups: [{ id: 'g-ok', label: 'fine', position: { x: 0, y: 0 } }, null],
          parentIds: {},
        }),
    ],
    ['annotationsToGroups', () => annotationsToGroups(mixed)],
    [
      'annotationDocumentToLegacyMetadata',
      () => annotationDocumentToLegacyMetadata({ schema_version: 1, annotations: mixed }),
    ],
    [
      'legacyMetadataToAnnotationDocument (document)',
      () => legacyMetadataToAnnotationDocument({ annotation_document: mixed }),
    ],
    [
      'legacyMetadataToAnnotationDocument (versioned list)',
      () =>
        legacyMetadataToAnnotationDocument({ annotation_schema_version: 1, annotations: mixed }),
    ],
  ])('%s survives it and reports the drop', (_name, run) => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(run).not.toThrow();
    expect(warn).toHaveBeenCalled();
  });

  it('refuses a primitive group entry instead of fabricating a phantom group', () => {
    // A string or a number does NOT throw on `g.id` — it yields undefined — so
    // without an explicit refusal it becomes a group annotation with a
    // generated id, an empty label and no members: an unexplained box on the
    // canvas, and nothing anywhere reporting it.
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const groups = annotationsToGroups([]).groups;
    expect(groups).toEqual([]);
    const doc = legacyMetadataToAnnotationDocument({
      groups: [{ id: 'g-ok', label: 'fine', position: { x: 0, y: 0 } }, 'a string', 7, true],
      parentIds: {},
    });
    expect(doc.annotations.map((a) => a.id)).toEqual(['g-ok']);
    expect(warn).toHaveBeenCalledTimes(3);
  });

  it('the legacy overlay path keeps the annotations it can read', () => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(
      legacyMetadataToAnnotationDocument({ annotation_document: mixed }).annotations.map(
        (a) => a.id
      )
    ).toEqual(['good-1', 'good-2']);
  });

  it('still refuses a payload whose annotation slot is not a list', () => {
    // Deliberately still fatal: that is a malformed session, not an
    // annotation this version cannot read.
    expect(() => annotationsToOverlays({ annotations: 'nope' })).toThrow(/not an array/);
  });
});

describe('malformed overlay data on the write path', () => {
  let warn;
  afterEach(() => warn?.mockRestore());

  it('skips a freehand overlay with malformed points and keeps the rest', () => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const annotations = overlaysToAnnotations([
      {
        id: 'bad-freehand',
        kind: 'freehand',
        position: { x: 10, y: 20 },
        points: [null],
      },
      {
        id: 'good-note',
        kind: 'note',
        position: { x: 1, y: 2 },
        text: 'survives',
      },
    ]);

    expect(annotations.map((a) => a.id)).toEqual(['good-note']);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it('skips null and malformed overlay entries without blocking valid overlays', () => {
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const annotations = overlaysToAnnotations([
      null,
      'not an overlay',
      { id: 'bad-kind', kind: 'wormhole', position: { x: 0, y: 0 } },
      { id: 'good-label', kind: 'label', position: { x: 5, y: 6 }, text: 'ok' },
    ]);

    expect(annotations.map((a) => a.id)).toEqual(['good-label']);
    expect(warn).toHaveBeenCalledTimes(3);
  });
});
