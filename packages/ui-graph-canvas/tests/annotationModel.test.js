import { describe, it, expect, vi } from 'vitest';
import {
  ANNOTATION_SCHEMA_VERSION,
  ANNOTATION_SHAPES,
  applyAnnotationOperation,
  createAnnotation,
  normalizeAnnotationDocument,
  normalizeShapeName,
} from '../src/utils/annotationModel';

describe('annotationModel contract v1', () => {
  it('normalizes legacy arrow annotations into v1 line annotations', () => {
    const annotation = createAnnotation({
      id: 'arrow-1',
      kind: 'arrow',
      position: { x: 10, y: 20 },
      dx: 30,
      dy: 40,
      color: '#111',
      startAnchor: 'node-a',
      endAnchor: 'node-b',
    });

    expect(annotation).toMatchObject({
      id: 'arrow-1',
      type: 'line',
      kind: 'line',
      position: { x: 10, y: 20 },
      from: { x: 10, y: 20 },
      to: { x: 40, y: 60 },
      style: { color: '#111' },
      startArrow: false,
      endArrow: true,
      startAnchor: 'node-a',
      endAnchor: 'node-b',
    });
  });

  it('creates a versioned document from a legacy array of notes labels arrows and groups', () => {
    const doc = normalizeAnnotationDocument([
      { id: 'note-1', kind: 'note', text: 'n', position: { x: 1, y: 2 } },
      { id: 'label-1', kind: 'label', text: 'l', position: { x: 3, y: 4 } },
      { id: 'arrow-1', kind: 'arrow', position: { x: 5, y: 6 }, dx: 7, dy: 8 },
      { id: 'group-1', kind: 'group', label: 'g', member_node_ids: ['n1'] },
    ]);

    expect(doc.schema_version).toBe(ANNOTATION_SCHEMA_VERSION);
    expect(doc.annotations.map((a) => a.type)).toEqual(['note', 'label', 'line', 'group']);
    expect(doc.annotations[3].member_node_ids).toEqual(['n1']);
  });

  it('preserves group size and color through normalization', () => {
    const doc = normalizeAnnotationDocument([
      {
        id: 'group-1',
        type: 'group',
        label: 'Styled group',
        position: { x: 10, y: 20 },
        size: { w: 320, h: 180 },
        color: '#f5a623',
      },
    ]);

    expect(doc.annotations[0]).toMatchObject({
      id: 'group-1',
      type: 'group',
      color: '#f5a623',
      size: { w: 320, h: 180 },
      geometry: { x: 10, y: 20, w: 320, h: 180 },
    });
  });

  it('applies create update transform reorder lock duplicate delete and inverse operations', () => {
    let result = applyAnnotationOperation(
      { annotations: [] },
      {
        type: 'create',
        annotation: { id: 'note-1', type: 'note', text: 'first' },
      }
    );
    expect(result.document.annotations[0].text).toBe('first');
    expect(result.inverse).toMatchObject({ type: 'delete', id: 'note-1' });

    result = applyAnnotationOperation(result.document, {
      type: 'update',
      id: 'note-1',
      patch: { text: 'second' },
    });
    expect(result.document.annotations[0].text).toBe('second');
    expect(result.inverse.patch.text).toBe('first');

    result = applyAnnotationOperation(result.document, {
      type: 'transform',
      id: 'note-1',
      geometry: { x: 20, y: 30, w: 200, h: 100 },
    });
    expect(result.document.annotations[0].geometry).toMatchObject({ x: 20, y: 30, w: 200, h: 100 });

    result = applyAnnotationOperation(result.document, { type: 'reorder', id: 'note-1', z: 9 });
    expect(result.document.annotations[0].z).toBe(9);

    result = applyAnnotationOperation(result.document, {
      type: 'lock',
      id: 'note-1',
      locked: true,
    });
    expect(result.document.annotations[0].locked).toBe(true);

    result = applyAnnotationOperation(result.document, {
      type: 'duplicate',
      id: 'note-1',
      new_id: 'note-2',
    });
    expect(result.document.annotations.map((a) => a.id)).toEqual(['note-1', 'note-2']);

    result = applyAnnotationOperation(result.document, { type: 'delete', id: 'note-2' });
    expect(result.document.annotations.map((a) => a.id)).toEqual(['note-1']);
    expect(result.inverse.annotation.id).toBe('note-2');
  });

  it('normalizes a line endpoint attachment the same way whether nested or given as target_id shorthand', () => {
    const nested = createAnnotation({
      id: 'line-1',
      type: 'line',
      from: { x: 0, y: 0 },
      to: { x: 100, y: 0 },
      start: { attachment: { target_id: 5, anchor: 'left', offset: { x: 1, y: 2 } } },
    });
    const shorthand = createAnnotation({
      id: 'line-2',
      type: 'line',
      from: { x: 0, y: 0 },
      to: { x: 100, y: 0 },
      start: { target_id: 5, anchor: 'left', offset: { x: 1, y: 2 } },
    });

    const expected = {
      target_id: '5',
      target_type: 'node',
      anchor: 'left',
      offset: { x: 1, y: 2 },
    };
    expect(nested.start.attachment).toEqual(expected);
    expect(shorthand.start.attachment).toEqual(expected);
  });

  it.each(['text', 'label', 'icon'])(
    'normalizes a %s attachment to a node with target_id/target_type/anchor/offset',
    (type) => {
      const annotation = createAnnotation({
        id: `${type}-1`,
        type,
        attachment: { target_id: 'node-9', anchor: 'bottom', offset: { x: 4, y: -2 } },
      });
      expect(annotation.attachment).toEqual({
        target_id: 'node-9',
        target_type: 'node',
        anchor: 'bottom',
        offset: { x: 4, y: -2 },
      });
    }
  );

  it.each(['text', 'label', 'icon'])(
    'drops a %s attachment with no target id rather than storing a dangling reference',
    (type) => {
      const annotation = createAnnotation({ id: `${type}-2`, type, attachment: { anchor: 'top' } });
      expect(annotation.attachment).toBeUndefined();
    }
  );

  // task-annotation-vote-dot-simplify: unlike text/label/icon above,
  // `vote_dot` no longer normalizes an `attachment` at all — its
  // withTypePayload returns no payload fields beyond the shared envelope, so
  // a well-formed attachment is dropped exactly the same as a malformed one
  // (a stale field from before this change, since nobody used the
  // annotation feature yet — no migration was written for it).
  it('drops a vote_dot attachment even when well-formed, since it is no longer an attachable kind', () => {
    const annotation = createAnnotation({
      id: 'vote-dot-1',
      type: 'vote_dot',
      attachment: { target_id: 'node-9', anchor: 'bottom', offset: { x: 4, y: -2 } },
    });
    expect(annotation.attachment).toBeUndefined();
  });

  it('drops a vote_dot value, and still carries its colour, through a full normalize round trip', () => {
    const doc = normalizeAnnotationDocument({
      annotations: [
        {
          id: 'vote-dot-2',
          type: 'vote_dot',
          position: { x: 1, y: 2 },
          value: 7,
          style: { color: '#22c55e' },
        },
      ],
    });
    const [annotation] = doc.annotations;
    expect(annotation.value).toBeUndefined();
    expect(annotation.style.color).toBe('#22c55e');
  });

  it('accepts targetId/nodeId/target_type as attachment aliases for label/icon', () => {
    expect(
      createAnnotation({ id: 'label-alias', type: 'label', attachment: { nodeId: 42 } }).attachment
        .target_id
    ).toBe('42');
    expect(
      createAnnotation({
        id: 'icon-alias',
        type: 'icon',
        attachment: { targetId: 'n1', targetType: 'annotation' },
      }).attachment
    ).toMatchObject({ target_id: 'n1', target_type: 'annotation' });
  });

  it('normalizes both line endpoint attachments independently (start to a node, end to an annotation)', () => {
    const annotation = createAnnotation({
      id: 'line-endpoints',
      type: 'line',
      from: { x: 0, y: 0 },
      to: { x: 100, y: 0 },
      start: { attachment: { target_id: 'node-a' } },
      end: { attachment: { target_id: 'label-b', target_type: 'annotation' } },
    });
    expect(annotation.start.attachment).toMatchObject({ target_id: 'node-a', target_type: 'node' });
    expect(annotation.end.attachment).toMatchObject({
      target_id: 'label-b',
      target_type: 'annotation',
    });
  });

  it('drops a line endpoint attachment when the shorthand carries no target id', () => {
    const annotation = createAnnotation({
      id: 'line-3',
      type: 'line',
      from: { x: 0, y: 0 },
      to: { x: 100, y: 0 },
      start: { x: 0, y: 0 },
    });
    expect(annotation.start.attachment).toBeUndefined();
  });

  it('normalizes a freehand annotation: points, pressure clamp, smoothing clamp, stroke width', () => {
    const annotation = createAnnotation({
      id: 'freehand-1',
      type: 'freehand',
      points: [
        { x: 0, y: 0, pressure: 0.5 },
        { x: 10, y: 5, pressure: 2 }, // out of range, clamps to 1
        { x: 20, y: 0, pressure: -1 }, // out of range, clamps to 0
        { x: 30, y: 5 }, // no pressure sample
      ],
      smoothing: 5, // out of range, clamps to 1
      strokeWidth: 4,
      pointerType: 'pen',
      pressureSource: 'device',
    });

    expect(annotation).toMatchObject({
      id: 'freehand-1',
      type: 'freehand',
      kind: 'freehand',
      position: { x: 0, y: 0 },
      points: [
        { x: 0, y: 0, pressure: 0.5 },
        { x: 10, y: 5, pressure: 1 },
        { x: 20, y: 0, pressure: 0 },
        { x: 30, y: 5 },
      ],
      smoothing: 1,
      strokeWidth: 4,
      pointerType: 'pen',
      pressureSource: 'device',
    });
    expect(annotation.points[3]).not.toHaveProperty('pressure');
  });

  it('defaults a freehand annotation with no points to a single point at its position', () => {
    const annotation = createAnnotation({
      id: 'freehand-empty',
      type: 'freehand',
      position: { x: 7, y: 9 },
    });
    expect(annotation.points).toEqual([{ x: 7, y: 9 }]);
    expect(annotation.smoothing).toBe(0);
    expect(annotation.strokeWidth).toBeGreaterThan(0);
  });

  it('translates a freehand stroke via the transform operation like any other annotation geometry', () => {
    const doc = normalizeAnnotationDocument([
      {
        id: 'freehand-1',
        type: 'freehand',
        points: [
          { x: 0, y: 0 },
          { x: 10, y: 10 },
        ],
      },
    ]);
    const result = applyAnnotationOperation(doc, {
      type: 'transform',
      id: 'freehand-1',
      geometry: { x: 5, y: 5 },
    });
    // Geometry (the envelope anchor) moves; the raw sampled points are a
    // separate content field the frontend's overlay layer keeps in sync with
    // it (see sessionAnnotations.js) — this operation alone does not rewrite
    // them, mirroring how `transform` treats a line's from/to.
    expect(result.document.annotations[0].geometry).toMatchObject({ x: 5, y: 5 });
    expect(result.document.annotations[0].points).toEqual([
      { x: 0, y: 0 },
      { x: 10, y: 10 },
    ]);
  });

  it('keeps every accepted shape variant as its canonical name', () => {
    for (const shape of ANNOTATION_SHAPES) {
      expect(createAnnotation({ type: 'shape', shape }).shape).toBe(shape);
    }
  });

  it('resolves the spellings of a shape name that mean the same variant', () => {
    for (const spelling of ['process_arrow', 'process-arrow', 'Process Arrow', 'processArrow']) {
      expect(normalizeShapeName(spelling)).toBe('process_arrow');
    }
    expect(normalizeShapeName(undefined)).toBe('rectangle');
  });

  it('keeps an unrecognised shape name rather than rewriting it to a rectangle', () => {
    expect(createAnnotation({ type: 'shape', shape: 'star' }).shape).toBe('star');
  });

  // task-annotation-doubleclick-to-edit-text: a shape can now carry an
  // inline-edited caption, the same optional `text` field `text`/`label`
  // already carry — an omitted one defaults to '', not undefined, so a
  // shape created before this change keeps rendering identically.
  it("keeps a shape's optional caption text", () => {
    expect(createAnnotation({ type: 'shape', shape: 'circle', text: 'Step 1' }).text).toBe(
      'Step 1'
    );
    expect(createAnnotation({ type: 'shape', shape: 'circle' }).text).toBe('');
  });

  // task-annotation-text-alignment-and-font: fontSize/font/textAlign live
  // under `style`, which createAnnotation already clones and carries through
  // generically (withTypePayload has no special case for either type) — this
  // pins that the generic passthrough actually covers the new fields, not
  // just `color`/`fontSize` (text) or nothing at all (shape, before this task).
  it('carries text/shape typography through the generic style passthrough', () => {
    const text = createAnnotation({
      type: 'text',
      style: { color: '#fff', fontSize: 24, font: 'serif', textAlign: 'middle-center' },
    });
    expect(text.style).toEqual({
      color: '#fff',
      fontSize: 24,
      font: 'serif',
      textAlign: 'middle-center',
    });

    const shape = createAnnotation({
      type: 'shape',
      shape: 'hexagon',
      style: { fontSize: 18, font: 'monospace', textAlign: 'top-left' },
    });
    expect(shape.style).toEqual({ fontSize: 18, font: 'monospace', textAlign: 'top-left' });
  });

  // Rotation is accepted for text, labels, notes, images, icons, dots and
  // shapes; it must survive normalization and the transform op the same way
  // x/y/w/h do, or a rotated annotation snaps back on the next round-trip.
  it('round-trips rotation through geometry, whether given inline or in geometry', () => {
    expect(createAnnotation({ type: 'shape', rotation: 45 }).geometry.rotation).toBe(45);
    expect(
      createAnnotation({ type: 'shape', geometry: { x: 0, y: 0, rotation: -90 } }).geometry.rotation
    ).toBe(-90);
    expect(createAnnotation({ type: 'shape' }).geometry.rotation).toBe(0);
  });

  it('transforms rotation and inverts back to the previous rotation', () => {
    const doc = normalizeAnnotationDocument([{ id: 'shape-1', type: 'shape', rotation: 15 }]);
    const result = applyAnnotationOperation(doc, {
      type: 'transform',
      id: 'shape-1',
      geometry: { rotation: 60 },
    });
    expect(result.document.annotations[0].geometry.rotation).toBe(60);
    const reverted = applyAnnotationOperation(result.document, result.inverse);
    expect(reverted.document.annotations[0].geometry.rotation).toBe(15);
  });

  it('fails invalid operations without mutating the document', () => {
    const doc = normalizeAnnotationDocument([{ id: 'note-1', type: 'note', text: 'safe' }]);
    expect(() =>
      applyAnnotationOperation(doc, { type: 'update', id: 'missing', patch: { text: 'x' } })
    ).toThrow(/Annotation not found/);
    expect(doc.annotations[0].text).toBe('safe');
  });

  // task-annotation-merge-frame-into-shape-rectangle: `frame` was a real,
  // recognised type until this task retired it in favour of `shape` with a
  // transparent fill. A session written before this task can still hold a
  // stored `frame`-kind annotation, and it must degrade quietly — skipped,
  // not thrown — the same guarantee task-annotation-tolerate-unexpected-data
  // built for any other kind this version does not recognise. Explicit
  // rather than assumed: `createAnnotation` throwing for an unknown type is
  // exactly what makes this work, so this pins that `frame` actually takes
  // that path rather than having quietly been left in TYPE_SET.
  it('skips a stored `frame` annotation (retired into `shape`) rather than throwing', () => {
    expect(() => createAnnotation({ id: 'f-1', type: 'frame' })).toThrow(
      /Unsupported annotation type: frame/
    );
    const onSkipped = vi.fn();
    const doc = normalizeAnnotationDocument(
      [
        { id: 'f-1', type: 'frame', position: { x: 0, y: 0 } },
        { id: 'note-1', type: 'note', text: 'survives' },
      ],
      { onSkipped }
    );
    expect(doc.annotations.map((a) => a.id)).toEqual(['note-1']);
    expect(onSkipped).toHaveBeenCalledTimes(1);
    expect(onSkipped.mock.calls[0][0]).toMatchObject({ id: 'f-1', type: 'frame' });
  });
});
