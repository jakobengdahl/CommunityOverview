import { describe, it, expect } from 'vitest';
import {
  ANNOTATION_SCHEMA_VERSION,
  applyAnnotationOperation,
  createAnnotation,
  normalizeAnnotationDocument,
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

  it('fails invalid operations without mutating the document', () => {
    const doc = normalizeAnnotationDocument([{ id: 'note-1', type: 'note', text: 'safe' }]);
    expect(() =>
      applyAnnotationOperation(doc, { type: 'update', id: 'missing', patch: { text: 'x' } })
    ).toThrow(/Annotation not found/);
    expect(doc.annotations[0].text).toBe('safe');
  });
});
