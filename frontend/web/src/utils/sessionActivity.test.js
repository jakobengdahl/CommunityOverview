// covers: frontend/web/src/utils/sessionActivity.js
import { describe, it, expect, vi } from 'vitest';
import { createAnnotation, ANNOTATION_TYPES } from '@community-graph/ui-graph-canvas';
import {
  annotationsToGroups,
  annotationsToOverlays,
  groupsToAnnotations,
  overlaysToAnnotations,
} from './sessionAnnotations';
import en from '../i18n/en.json';
import sv from '../i18n/sv.json';
import {
  describeActivity,
  isUndoableRecord,
  findLatestUndoable,
  classifyUndoError,
} from './sessionActivity';

function record(overrides = {}) {
  return {
    id: 'act-1',
    op: 'annotation_created',
    actor: 'client-a',
    affected: { kind: 'annotation', id: 'note-1' },
    before: null,
    after: { id: 'note-1', type: 'note' },
    inverse_op: { op: 'annotation_deleted', annotation_id: 'note-1' },
    undone: false,
    ...overrides,
  };
}

describe('describeActivity', () => {
  it('describes a created annotation by its type', () => {
    const r = record({ op: 'annotation_created', after: { type: 'note' } });
    expect(describeActivity(r)).toEqual({
      key: 'history.desc.annotation_created',
      params: { type: 'history.annotation_type.note' },
    });
  });

  it('describes a deleted annotation from the before snapshot', () => {
    const r = record({
      op: 'annotation_deleted',
      before: { type: 'shape' },
      after: null,
    });
    expect(describeActivity(r)).toEqual({
      key: 'history.desc.annotation_deleted',
      params: { type: 'history.annotation_type.shape' },
    });
  });

  it('falls back to the unknown type key for an unrecognised annotation type', () => {
    const r = record({ op: 'annotation_created', after: { type: 'not_a_real_type' } });
    expect(describeActivity(r).params.type).toBe('history.annotation_type.unknown');
  });
  it.each(ANNOTATION_TYPES)('names the %s kind rather than falling back to unknown', (type) => {
    const r = record({ op: 'annotation_created', after: { type } });
    expect(describeActivity(r).params.type).toBe(`history.annotation_type.${type}`);
  });

  it.each(ANNOTATION_TYPES)('has an en and sv activity string for %s', (type) => {
    expect(en.history.annotation_type[type]).toBeTruthy();
    expect(sv.history.annotation_type[type]).toBeTruthy();
  });

  describe('annotation_updated classification', () => {
    it.each([
      [
        { type: 'shape', shape: 'rectangle', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        { type: 'shape', shape: 'ellipse', geometry: { x: 5, y: 5, w: 10, h: 10, rotation: 0 } },
        'shape',
      ],
      [
        { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 15 } },
        'rotated',
      ],
      [
        { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        { type: 'note', geometry: { x: 0, y: 0, w: 40, h: 10, rotation: 0 } },
        'resized',
      ],
      [
        { type: 'note', position: { x: 0, y: 0 } },
        { type: 'note', position: { x: 50, y: 0 } },
        'moved',
      ],
      [
        { type: 'note', style: { color: 'blue' } },
        { type: 'note', style: { color: 'red' } },
        'style',
      ],
      [{ type: 'note', text: 'old' }, { type: 'note', text: 'new' }, 'text'],
      [{ type: 'note', locked: false }, { type: 'note', locked: true }, 'locked'],
      [{ type: 'note', locked: true }, { type: 'note', locked: false }, 'unlocked'],
      [{ type: 'label' }, { type: 'label', attachment: { target_id: 'node-1' } }, 'attached'],
      [{ type: 'label', attachment: { target_id: 'node-1' } }, { type: 'label' }, 'detached'],
      [{ type: 'note' }, { type: 'note', some_future_field: 'x' }, 'generic'],
    ])('classifies an annotation update from %j to %j as %s', (before, after, expected) => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: before.type === 'shape' ? 'shape-1' : 'a1' },
        before,
        after,
      });
      expect(describeActivity(r).key).toBe(`history.desc.annotation_updated_${expected}`);
    });

    it('falls back to "generic" rather than guessing when there is no before snapshot', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['locked', 'z', 'geometry'] },
        before: null,
        after: { type: 'note', locked: false, z: 0 },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
    });
  });
  describe('annotation_updated classification, browser-shaped full payloads', () => {
    function browserEdit(base, changes) {
      const incoming = createAnnotation({ ...base, ...changes });
      const before = { ...createAnnotation(base), updated_at: '2026-08-26T09:00:00Z' };
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: base.id, fields: Object.keys(incoming).sort() },
        before,
        after: { ...before, ...incoming, updated_at: '2026-08-26T09:00:01Z' },
      });
    }
    const KINDS = [
      { id: 'n1', type: 'note', text: 'hello', position: { x: 0, y: 0 }, z: 0 },
      { id: 'l1', type: 'label', text: 'a label', position: { x: 0, y: 0 }, z: 0 },
      { id: 'i1', type: 'icon', icon: 'circle', position: { x: 0, y: 0 }, z: 0 },
      { id: 's1', type: 'shape', shape: 'rectangle', position: { x: 0, y: 0 }, z: 0 },
    ];

    it.each([
      ...KINDS.flatMap((base) => [
        { name: `${base.type} raised`, base, changes: { z: 5 }, expected: 'raised' },
        { name: `${base.type} lowered`, base, changes: { z: -1 }, expected: 'lowered' },
        {
          name: `${base.type} moved`,
          base,
          changes: { position: { x: 300, y: 200 } },
          expected: 'moved',
        },
        { name: `${base.type} recoloured`, base, changes: { color: 'crimson' }, expected: 'style' },
        { name: `${base.type} locked`, base, changes: { locked: true }, expected: 'locked' },
        {
          name: `${base.type} unlocked`,
          base: { ...base, locked: true },
          changes: { locked: false },
          expected: 'unlocked',
        },
      ]),
      {
        name: 'resized',
        base: KINDS[0],
        changes: { size: { w: 400, h: 300 } },
        expected: 'resized',
      },
      { name: 'rotated', base: KINDS[0], changes: { rotation: 30 }, expected: 'rotated' },
      { name: 'text edited', base: KINDS[0], changes: { text: 'rewritten' }, expected: 'text' },
      { name: 'unchanged', base: KINDS[0], changes: {}, expected: 'generic' },
    ])('classifies a full payload: $name', ({ base, changes, expected }) => {
      expect(describeActivity(browserEdit(base, changes)).key).toBe(
        `history.desc.annotation_updated_${expected}`
      );
    });

    it('does not read the browser materialising envelope defaults as a change', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1' },
        before: { id: 'a1', type: 'note', position: { x: 0, y: 0 } },
        after: { id: 'a1', type: 'note', position: { x: 40, y: 0 }, z: 0, locked: false, text: '' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });
  });
  describe('annotation_updated classification, server-created annotations', () => {
    function serverAnnotation(type, { x = 10, y = 20, ...content } = {}) {
      return {
        type,
        kind: type,
        id: `srv-${type}`,
        geometry: { x, y, w: 0, h: 0, rotation: 0 },
        position: { x, y },
        z: 0,
        locked: false,
        ...content,
      };
    }
    function browserMove(stored, dx = 250) {
      const overlays = annotationsToOverlays([stored]);
      const moved = overlays.map((o) => ({
        ...o,
        position: { x: (o.position?.x ?? 0) + dx, y: o.position?.y ?? 0 },
      }));
      const incoming = JSON.parse(JSON.stringify(overlaysToAnnotations(moved)[0]));
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: stored.id },
        before: stored,
        after: { ...stored, ...incoming, updated_at: '2026-08-26T09:00:01Z' },
      });
    }

    const MOVABLE = [
      { name: 'shape with no shape field at all', ann: serverAnnotation('shape') },
      {
        name: 'shape whose shape is a free-text spelling',
        ann: serverAnnotation('shape', { shape: 'Process Arrow' }),
      },
      { name: 'line', ann: serverAnnotation('line', { to: { x: 100, y: 60 } }) },
      { name: 'label', ann: serverAnnotation('label', { text: 'hi' }) },
      { name: 'text', ann: serverAnnotation('text', { text: 'hi' }) },
      { name: 'icon', ann: serverAnnotation('icon', { icon: 'star' }) },
      { name: 'vote_dot', ann: serverAnnotation('vote_dot') },
      { name: 'note', ann: serverAnnotation('note', { text: 'hi' }) },
    ];

    it.each(MOVABLE)('reads a drag of an agent-created $name as "moved"', ({ ann }) => {
      expect(describeActivity(browserMove(ann)).key).toBe('history.desc.annotation_updated_moved');
    });

    it('does not read the browser normalising a free-text shape name as a shape change', () => {
      const r = browserMove(serverAnnotation('shape', { shape: 'Process Arrow' }));
      expect(r.before.shape).toBe('Process Arrow');
      expect(r.after.shape).toBe('process_arrow');
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });

    it('no longer materializes a default size on an unsized label (smallfix-browser-clobbers-unsized-annotation-geometry)', () => {
      const r = browserMove(serverAnnotation('label', { text: 'hi' }));
      expect(r.before.geometry.w).toBe(0);
      expect(r.after.geometry.w).toBe(0);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });

    it('still reports a genuine shape change on a server-created shape', () => {
      const before = serverAnnotation('shape', { shape: 'rectangle' });
      expect(
        describeActivity(
          record({ op: 'annotation_updated', before, after: { ...before, shape: 'ellipse' } })
        ).key
      ).toBe('history.desc.annotation_updated_shape');
    });

    it('still reports a genuine resize once the annotation has a real size', () => {
      const before = serverAnnotation('shape', { shape: 'rectangle' });
      before.geometry = { x: 10, y: 20, w: 160, h: 96, rotation: 0 };
      const after = { ...before, geometry: { x: 10, y: 20, w: 400, h: 96, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });

    it.each([
      {
        name: 'line whose stored position disagrees with its from-endpoint',
        ann: () =>
          serverAnnotation('line', {
            x: 0,
            y: 0,
            from: { x: 100, y: 100 },
            to: { x: 300, y: 100 },
          }),
      },
    ])('does not read the browser rebuilding $name as a move', ({ ann }) => {
      const stored = ann();
      const r = browserMove(stored, 0);
      expect(r.before.position).not.toEqual(r.after.position);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
    });

    it('does not rewrite a freehand position that disagrees with its first point', () => {
      const stored = serverAnnotation('freehand', {
        x: 0,
        y: 0,
        points: [
          { x: 120, y: 340 },
          { x: 130, y: 350 },
        ],
      });
      const r = browserMove(stored, 0);
      expect(r.after.position).toEqual(r.before.position);
      expect(r.after.geometry).toMatchObject({
        x: r.before.geometry.x,
        y: r.before.geometry.y,
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
    });

    it.each([
      {
        name: 'freehand',
        ann: () => serverAnnotation('freehand', { x: 120, y: 340, points: [{ x: 120, y: 340 }] }),
      },
      {
        name: 'line',
        ann: () =>
          serverAnnotation('line', {
            x: 100,
            y: 100,
            from: { x: 100, y: 100 },
            to: { x: 300, y: 100 },
          }),
      },
    ])('still reads a genuine drag of a $name as a move', ({ ann }) => {
      expect(describeActivity(browserMove(ann(), 250)).key).toBe(
        'history.desc.annotation_updated_moved'
      );
    });

    it('still reports a genuine resize of an annotation the server left unsized', () => {
      const before = serverAnnotation('label', { text: 'hi' });
      const after = {
        ...before,
        geometry: { x: 10, y: 20, w: 400, h: 220, rotation: 0 },
      };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });

    it('still reports an agent resizing a dimension down to zero', () => {
      const before = serverAnnotation('shape');
      before.geometry = { x: 10, y: 20, w: 800, h: 400, rotation: 0 };
      const after = { ...before, geometry: { x: 10, y: 20, w: 0, h: 400, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });

    it('reads a move onto and off the origin as a move', () => {
      const at = (x, y) => ({
        type: 'note',
        geometry: { x, y, w: 160, h: 96, rotation: 0 },
        position: { x, y },
      });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(250, 80), after: at(0, 0) }))
          .key
      ).toBe('history.desc.annotation_updated_moved');
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(0, 0), after: at(250, 80) }))
          .key
      ).toBe('history.desc.annotation_updated_moved');
    });

    it('does not claim a resize when only the style changed on an unsized label', () => {
      const before = serverAnnotation('label', { text: 'hi' });
      const after = {
        ...before,
        style: { color: 'crimson' },
      };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_style'
      );
    });

    it('now reports a genuine resize to exactly 160x96 on a previously-unsized label', () => {
      const before = serverAnnotation('label', { text: 'hi' });
      const after = { ...before, geometry: { x: 10, y: 20, w: 160, h: 96, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });
  });
  describe('annotation_updated classification, groups', () => {
    function serverGroup({ x = 10, y = 20, w = 320, h = 200, ...rest } = {}) {
      const { color, ...fields } = rest;
      return {
        type: 'group',
        kind: 'group',
        id: 'srv-group',
        position: { x, y },
        geometry: { x, y, w, h, rotation: 0 },
        size: { w, h },
        label: '',
        description: '',
        z: 0,
        locked: false,
        ...(color ? { color, style: { color } } : {}),
        ...fields,
      };
    }
    function groupEdit(stored, mutate = (g) => g) {
      const { groups, parentIds } = annotationsToGroups([stored]);
      const edited = groups.map((g) => mutate({ ...g }));
      const incoming = JSON.parse(JSON.stringify(groupsToAnnotations(edited, parentIds)[0]));
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: stored.id },
        before: stored,
        after: { ...stored, ...incoming, updated_at: '2026-08-26T09:00:01Z' },
      });
    }

    it('does not report renaming a LOCKED group as an unlock', () => {
      const r = groupEdit(serverGroup({ label: 'Team', locked: true }), (g) => ({
        ...g,
        label: 'Team B',
      }));
      expect(r.before.locked).toBe(true);
      expect(r.after.locked).toBe(true);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it.each([
      {
        what: 'dragging',
        mutate: (g) => ({ ...g, position: { x: 300, y: 20 } }),
        expected: 'moved',
      },
      { what: 'recolouring', mutate: (g) => ({ ...g, color: 'crimson' }), expected: 'style' },
    ])('does not report $what a locked group as an unlock', ({ mutate, expected }) => {
      const r = groupEdit(serverGroup({ label: 'Team', locked: true }), mutate);
      expect(describeActivity(r).key).toBe(`history.desc.annotation_updated_${expected}`);
    });

    it('does not report renaming a group carrying a z as a layer change', () => {
      const r = groupEdit(serverGroup({ label: 'Team', z: 4 }), (g) => ({ ...g, label: 'B' }));
      expect(r.before.z).toBe(4);
      expect(r.after.z).toBe(4);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it.each([
      { edit: 'locking', field: 'locked', from: false, to: true, expected: 'locked' },
      { edit: 'unlocking', field: 'locked', from: true, to: false, expected: 'unlocked' },
      { edit: 'raising', field: 'z', from: 0, to: 5, expected: 'raised' },
      { edit: 'lowering to zero', field: 'z', from: 5, to: 0, expected: 'lowered' },
    ])('reports an agent $edit a group in both directions', (c) => {
      const at = (v) => serverGroup({ [c.field]: v });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(c.from), after: at(c.to) }))
          .key
      ).toBe(`history.desc.annotation_updated_${c.expected}`);
    });

    it('under-reports naming an unlabelled group literally "Group"', () => {
      const at = (v) => serverGroup({ label: v });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(''), after: at('Group') }))
          .key
      ).toBe('history.desc.annotation_updated_generic');
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(''), after: at('Team') }))
          .key
      ).toBe('history.desc.annotation_updated_text');
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at('Group'), after: at('') }))
          .key
      ).toBe('history.desc.annotation_updated_text');
    });

    it('reports rotating and unrotating a group once the translators preserve rotation', () => {
      const at = (v) => serverGroup({ geometry: { x: 10, y: 20, w: 320, h: 200, rotation: v } });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(0), after: at(45) })).key
      ).toBe('history.desc.annotation_updated_rotated');
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(45), after: at(0) })).key
      ).toBe('history.desc.annotation_updated_rotated');
    });

    it('does not report renaming a rotated group as a rotation change', () => {
      const rotated = serverGroup({ label: 'Team' });
      rotated.geometry = { ...rotated.geometry, rotation: 45 };
      expect(describeActivity(groupEdit(rotated, (g) => ({ ...g, label: 'B' }))).key).toBe(
        'history.desc.annotation_updated_text'
      );
    });
  });
  describe('annotation_updated classification, other producer rewrites', () => {
    function serverAnn(type, content = {}) {
      return {
        type,
        kind: type,
        id: `srv-${type}`,
        geometry: { x: 10, y: 20, w: 0, h: 0, rotation: 0 },
        position: { x: 10, y: 20 },
        z: 0,
        locked: false,
        ...content,
      };
    }

    function browserRoundTrip(stored, mutate = (o) => o) {
      const overlays = annotationsToOverlays([stored]).map((o) => mutate({ ...o }));
      const incoming = JSON.parse(JSON.stringify(overlaysToAnnotations(overlays)[0]));
      return record({
        op: 'annotation_updated',
        before: stored,
        after: { ...stored, ...incoming, updated_at: '2026-08-26T09:00:01Z' },
      });
    }

    it.each(['text', 'label', 'icon'])(
      'does not report an attachment gaining target_type on a dragged %s as "attached"',
      (type) => {
        const stored = serverAnn(type, { attachment: { target_id: 'n2' }, text: 'hi' });
        const r = browserRoundTrip(stored, (o) => ({
          ...o,
          position: { x: 300, y: 20 },
        }));
        expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
      }
    );

    it('does not report normalising freehand point metadata as a move', () => {
      const stored = serverAnn('freehand', {
        points: [
          { x: 120, y: 340, pressure: 5 },
          { x: 130, y: 350, t: 17 },
        ],
      });
      expect(describeActivity(browserRoundTrip(stored)).key).toBe(
        'history.desc.annotation_updated_generic'
      );
    });

    it('does not report normalising a line endpoint as a move', () => {
      const stored = serverAnn('line', {
        from: { x: 100, y: 100, anchor: 'left' },
        to: { x: 300, y: 100 },
      });
      expect(describeActivity(browserRoundTrip(stored)).key).toBe(
        'history.desc.annotation_updated_generic'
      );
    });

    it('reports a text edit as text even when the write-back drops a style key', () => {
      const stored = serverAnn('text', { text: 'before', style: { color: 'red', opacity: 0.5 } });
      const r = browserRoundTrip(stored, (o) => ({ ...o, text: 'after' }));
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it('still reports an agent resizing a kind whose overlay carries its size', () => {
      const before = serverAnn('shape');
      const after = { ...before, geometry: { x: 10, y: 20, w: 160, h: 96, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });
  });

  describe('cost of classifying an image record', () => {
    function imagePayload(counter) {
      const payload = { url: `data:image/webp;base64,${'A'.repeat(64 * 1024)}` };
      Object.defineProperty(payload, 'toJSON', {
        enumerable: false,
        value() {
          counter.serialised += 1;
          return { url: payload.url };
        },
      });
      return payload;
    }

    function imageRecord(counter, overrides = {}) {
      const before = {
        id: 'img-1',
        type: 'image',
        kind: 'image',
        geometry: { x: 0, y: 0, w: 200, h: 100, rotation: 0 },
        position: { x: 0, y: 0 },
        z: 0,
        locked: false,
        image: imagePayload(counter),
        alt: '',
      };
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: before.id },
        before,
        after: {
          ...before,
          geometry: { ...before.geometry, x: 300 },
          position: { x: 300, y: 0 },
          image: imagePayload(counter),
          ...overrides,
        },
      });
    }

    it('never serialises the image payload', () => {
      const counter = { serialised: 0 };
      const r = imageRecord(counter);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
      expect(counter.serialised).toBe(0);
    });

    it('does not re-run the write-back when the same record is classified again', () => {
      let typeReads = 0;
      const before = {
        id: 'memo-1',
        geometry: { x: 0, y: 0, w: 160, h: 96, rotation: 0 },
        position: { x: 0, y: 0 },
        z: 0,
        locked: false,
        text: 'hi',
      };
      Object.defineProperty(before, 'type', {
        enumerable: true,
        get() {
          typeReads += 1;
          return 'note';
        },
      });
      const r = record({
        op: 'annotation_updated',
        before,
        after: { ...before, type: 'note', position: { x: 300, y: 0 } },
      });

      describeActivity(r);
      const afterFirst = typeReads;
      expect(afterFirst).toBeGreaterThan(0);
      describeActivity(r);
      describeActivity(r);
      expect(typeReads).toBe(afterFirst);
    });

    it('classifies the same whether or not the payload also changed', () => {
      const counter = { serialised: 0 };
      const samePayload = describeActivity(imageRecord(counter)).key;
      const swappedPayload = describeActivity(
        imageRecord(counter, { image: { url: 'data:image/webp;base64,DIFFERENT' } })
      ).key;
      expect(swappedPayload).toBe(samePayload);
      expect(samePayload).toBe('history.desc.annotation_updated_moved');
      expect(counter.serialised).toBe(0);
    });

    it('reports a payload-only change as a plain update', () => {
      const swapOnly = record({
        op: 'annotation_updated',
        before: { type: 'image', image: { url: 'data:image/webp;base64,AAAA' } },
        after: { type: 'image', image: { url: 'data:image/webp;base64,BBBB' } },
      });
      expect(describeActivity(swapOnly).key).toBe('history.desc.annotation_updated_generic');
    });
  });

  describe('label/line style keys beyond the ones the canvas controls', () => {
    const label = {
      id: 'l1',
      type: 'label',
      kind: 'label',
      position: { x: 0, y: 0 },
      geometry: { x: 0, y: 0, w: 0, h: 0, rotation: 0 },
      text: 'hi',
      z: 0,
      locked: false,
      style: { color: 'red', fontSize: 18, opacity: 0.5, dash: 'dotted' },
    };
    const line = {
      id: 'a1',
      type: 'line',
      kind: 'line',
      from: { x: 0, y: 0 },
      to: { x: 160, y: 0 },
      startArrow: false,
      endArrow: true,
      z: 0,
      locked: false,
      style: { color: 'red', opacity: 0.5, dash: 'dotted' },
    };

    function browserEdit(stored, edit) {
      const overlays = annotationsToOverlays([stored]).map(edit);
      const incoming = JSON.parse(JSON.stringify(overlaysToAnnotations(overlays)[0]));
      return record({
        op: 'annotation_updated',
        before: stored,
        after: { ...stored, ...incoming },
      });
    }

    it('reports a text edit from this build as a text edit', () => {
      const r = browserEdit(label, (o) => ({ ...o, text: 'bye' }));
      expect(r.after.style).toEqual(label.style);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it('still reports a restyle from this build as a restyle', () => {
      const r = browserEdit(label, (o) => ({ ...o, color: 'blue' }));
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_style');
      const lineRestyle = browserEdit(line, (o) => ({ ...o, opacity: 0.9 }));
      expect(describeActivity(lineRestyle).key).toBe('history.desc.annotation_updated_style');
    });

    const labelWithExtraWidth = { ...label, style: { ...label.style, width: 2 } };
    const lineWithFontSize = { ...line, style: { color: 'red', opacity: 0.5, fontSize: 18 } };
    it.each([
      {
        name: 'older text edit with dropped key',
        before: label,
        after: { ...label, text: 'bye', style: { color: 'red', fontSize: 18, opacity: 0.5 } },
        expected: 'text',
      },
      {
        name: 'older arrowhead edit with dropped key',
        before: line,
        after: { ...line, endArrow: false, style: { color: 'red', opacity: 0.5 } },
        expected: 'generic',
      },
      {
        name: 'older restyle',
        before: label,
        after: { ...label, style: { color: 'blue', fontSize: 18, opacity: 0.5 } },
        expected: 'style',
      },
      ...['note', 'shape'].map((type) => {
        const before = {
          id: 's1',
          type,
          position: { x: 0, y: 0 },
          z: 0,
          locked: false,
          style: { color: 'red', opacity: 0.5, dash: 'dotted' },
        };
        return {
          name: `${type} style key drop`,
          before,
          after: { ...before, style: { color: 'red', opacity: 0.5 } },
          expected: 'style',
        };
      }),
      {
        name: 'label named key drop',
        before: label,
        after: { ...label, style: { color: 'red', opacity: 0.5 } },
        expected: 'style',
      },
      {
        name: 'uncontrolled key change',
        before: label,
        after: { ...label, style: { ...label.style, dash: 'dashed' } },
        expected: 'style',
      },
      {
        name: 'partial uncontrolled key drop',
        before: labelWithExtraWidth,
        after: { ...labelWithExtraWidth, style: label.style },
        expected: 'style',
      },
      {
        name: 'line fontSize drop',
        before: lineWithFontSize,
        after: { ...lineWithFontSize, endArrow: false, style: { color: 'red', opacity: 0.5 } },
        expected: 'generic',
      },
    ])('classifies style changes: $name', ({ before, after, expected }) => {
      const r = record({ op: 'annotation_updated', before, after });
      expect(describeActivity(r).key).toBe(`history.desc.annotation_updated_${expected}`);
    });

    it("recognises an older build's drop on records that carry only type", () => {
      const { kind: _kind, ...labelByType } = label;
      const olderText = record({
        op: 'annotation_updated',
        before: labelByType,
        after: { ...labelByType, text: 'bye', style: { color: 'red', fontSize: 18, opacity: 0.5 } },
      });
      expect(describeActivity(olderText).key).toBe('history.desc.annotation_updated_text');
    });

    it("recognises an older build's drop on records that carry only kind", () => {
      const { type: _labelType, ...labelByKind } = label;
      const { type: _lineType, ...lineByKind } = line;
      const olderText = record({
        op: 'annotation_updated',
        before: labelByKind,
        after: { ...labelByKind, text: 'bye', style: { color: 'red', fontSize: 18, opacity: 0.5 } },
      });
      expect(describeActivity(olderText).key).toBe('history.desc.annotation_updated_text');
      const olderArrowhead = record({
        op: 'annotation_updated',
        before: lineByKind,
        after: { ...lineByKind, endArrow: false, style: { color: 'red', opacity: 0.5 } },
      });
      expect(describeActivity(olderArrowhead).key).toBe('history.desc.annotation_updated_generic');
    });
  });

  describe('shape spelling', () => {
    it('does not report an agent respelling a shape it already is as a shape change', () => {
      for (const [before, after] of [
        ['rectangle', 'Rectangle'],
        ['process_arrow', 'Process Arrow'],
        ['process_arrow', 'process-arrow'],
      ]) {
        const r = record({
          op: 'annotation_updated',
          before: { type: 'shape', shape: before },
          after: { type: 'shape', shape: after },
        });
        expect(describeActivity(r).key, `${before} -> ${after}`).toBe(
          'history.desc.annotation_updated_generic'
        );
      }
    });

    it('still reports a genuine shape change, including to an out-of-set name', () => {
      const change = (before, after) =>
        describeActivity(
          record({
            op: 'annotation_updated',
            before: { type: 'shape', shape: before },
            after: { type: 'shape', shape: after },
          })
        ).key;
      expect(change('circle', 'triangle')).toBe('history.desc.annotation_updated_shape');
      expect(change('rectangle', 'blob')).toBe('history.desc.annotation_updated_shape');
    });
  });

  describe('an annotation kind this build cannot read', () => {
    const stored = {
      id: 's1',
      type: 'sticker',
      kind: 'sticker',
      locked: true,
      text: 'hi',
      z: 3,
      position: { x: 0, y: 0 },
    };

    it('never claims an unlock for a whole-annotation rewrite it cannot verify', () => {
      const wholeWrite = {
        ...stored,
        locked: false,
        z: 0,
        text: 'bye',
      };
      expect(
        describeActivity(record({ op: 'annotation_updated', before: stored, after: wholeWrite }))
          .key
      ).toBe('history.desc.annotation_updated_generic');
    });

    it('reports a plain update for a sparse agent patch too, rather than guessing', () => {
      const patched = { ...stored, text: 'bye' };
      expect(
        describeActivity(record({ op: 'annotation_updated', before: stored, after: patched })).key
      ).toBe('history.desc.annotation_updated_generic');
    });

    it('leaves the stored image on the record it classifies', () => {
      const image = { url: 'data:image/webp;base64,AAAA' };
      const r = record({
        op: 'annotation_updated',
        before: { type: 'image', image },
        after: { type: 'image', image, z: 2 },
      });
      describeActivity(r);
      expect(r.before.image).toBe(image);
    });

    it('logs the skipped annotation without an image field it never had', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      try {
        describeActivity(
          record({ op: 'annotation_updated', before: stored, after: { ...stored, z: 9 } })
        );
        const logged = warn.mock.calls
          .filter(([message]) => String(message).startsWith('Skipping an annotation'))
          .map(([, annotation]) => annotation);
        expect(logged.length).toBeGreaterThan(0);
        for (const annotation of logged) {
          expect(Object.prototype.hasOwnProperty.call(annotation, 'image')).toBe(false);
        }
      } finally {
        warn.mockRestore();
      }
    });

    it('still describes the record rather than failing', () => {
      const r = record({ op: 'annotation_updated', before: stored, after: { ...stored, z: 9 } });
      expect(describeActivity(r)).toEqual({
        key: 'history.desc.annotation_updated_generic',
        params: { type: 'history.annotation_type.unknown' },
      });
    });
  });

  describe('sameValue equivalences', () => {
    it('treats every unset spelling as one value', () => {
      const unset = [undefined, null, false, '', 0, {}, []];
      for (const a of unset) {
        for (const b of unset) {
          const r = record({
            op: 'annotation_updated',
            before: { type: 'note', z: 1, style: a },
            after: { type: 'note', z: 1, style: b },
          });
          expect(describeActivity(r).key, `${JSON.stringify(a)} vs ${JSON.stringify(b)}`).toBe(
            'history.desc.annotation_updated_generic'
          );
        }
      }
    });

    it('still sees a change between an unset value and a real one', () => {
      const r = record({
        op: 'annotation_updated',
        before: { type: 'note', style: {} },
        after: { type: 'note', style: { color: 'red' } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_style');
    });

    it.each([
      { name: '0 to null', before: { value: 0 }, after: { value: null } },
      { name: '0 to cleared', before: { value: 0 }, after: {} },
      { name: 'null to 0', before: { value: null }, after: { value: 0 } },
      { name: 'cleared to 0', before: {}, after: { value: 0 } },
    ])('reports a vote_dot value change from $name', ({ before, after }) => {
      expect(
        describeActivity(
          record({
            op: 'annotation_updated',
            before: { type: 'vote_dot', ...before },
            after: { type: 'vote_dot', ...after },
          })
        ).key
      ).toBe('history.desc.annotation_updated_text');
    });

    it.each([
      { name: '5 to 3', before: { intensity: 5 }, after: { intensity: 3 } },
      { name: '5 to 0', before: { intensity: 5 }, after: { intensity: 0 } },
      { name: 'unset to 0', before: {}, after: { intensity: 0 } },
      { name: '0 to 10', before: { intensity: 0 }, after: { intensity: 10 } },
    ])('reports a heat-map intensity change from $name', ({ before, after }) => {
      const base = { id: 'h1', geometry: { x: 0, y: 0, w: 160, h: 160, rotation: 0 } };
      expect(
        describeActivity(
          record({
            op: 'annotation_updated',
            before: { ...base, type: 'heatmap', ...before },
            after: { ...base, type: 'heatmap', ...after },
          })
        ).key
      ).toBe('history.desc.annotation_updated_intensity');
    });

    it('does not report the browser clamping a stored out-of-range intensity to 0 as an edit', () => {
      const base = { id: 'h1', geometry: { x: 0, y: 0, w: 160, h: 160, rotation: 0 } };
      const r = record({
        op: 'annotation_updated',
        before: { ...base, type: 'heatmap', intensity: -3 },
        after: { ...base, type: 'heatmap', intensity: 0 },
      });
      expect(describeActivity(r).key).not.toBe('history.desc.annotation_updated_intensity');
    });

    it('does not report the browser filling in a heat-map default intensity as an edit', () => {
      const base = { id: 'h1', geometry: { x: 0, y: 0, w: 160, h: 160, rotation: 0 } };
      const r = record({
        op: 'annotation_updated',
        before: { ...base, type: 'heatmap' },
        after: { ...base, type: 'heatmap', intensity: 5 },
      });
      expect(describeActivity(r).key).not.toBe('history.desc.annotation_updated_intensity');
    });

    it('names a heat-map circle in a created record', () => {
      const r = record({ op: 'annotation_created', after: { id: 'h1', type: 'heatmap' } });
      expect(describeActivity(r).params.type).toBe('history.annotation_type.heatmap');
    });

    it('keeps 0 equivalent to unset for non-vote_dot materialized defaults', () => {
      const r = record({
        op: 'annotation_updated',
        before: { type: 'note', z: 1, style: { opacity: null } },
        after: { type: 'note', z: 1, style: { opacity: 0 } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
    });

    it('compares nested objects and arrays by value, not identity', () => {
      const same = record({
        op: 'annotation_updated',
        before: { type: 'freehand', points: [{ x: 1, y: 2 }], style: { color: 'red' } },
        after: { type: 'freehand', points: [{ x: 1, y: 2 }], style: { color: 'red' } },
      });
      expect(describeActivity(same).key).toBe('history.desc.annotation_updated_generic');

      const differs = record({
        op: 'annotation_updated',
        before: { type: 'freehand', points: [{ x: 1, y: 2 }], style: { color: 'red' } },
        after: { type: 'freehand', points: [{ x: 1, y: 2 }], style: { color: 'blue' } },
      });
      expect(describeActivity(differs).key).toBe('history.desc.annotation_updated_style');
    });
  });

  describe('annotation_updated classification, browser-shaped full payloads (continued)', () => {
    it('reports an agent-only sparse patch from the same before/after diff', () => {
      const before = createAnnotation({ id: 'n1', type: 'note', text: 'before' });
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'n1' },
        before,
        after: { ...before, text: 'after' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });
  });

  it('describes node_moved, resolving the node name when a resolver is given', () => {
    const r = record({
      op: 'node_moved',
      affected: { kind: 'node_position', id: 'node-42' },
    });
    expect(describeActivity(r, { nodeName: () => 'Acme Corp' })).toEqual({
      key: 'history.desc.node_moved',
      params: { name: 'Acme Corp' },
    });
    expect(describeActivity(r).params.name).toBe('node-42');
  });

  it.each([
    ['layout_applied', { kind: 'layout', node_ids: ['a', 'b', 'c'] }, 3],
    ['nodes_hidden', { kind: 'node_visibility', ids: ['a', 'b'] }, 2],
    ['nodes_shown', { kind: 'node_visibility', ids: ['a'] }, 1],
    ['nodes_dimmed', { kind: 'node_dim', ids: ['a', 'b'] }, 2],
    ['nodes_undimmed', { kind: 'node_dim', ids: ['a'] }, 1],
    ['edges_dimmed', { kind: 'edge_dim', ids: ['e1'] }, 1],
    ['edges_undimmed', { kind: 'edge_dim', ids: ['e1', 'e2'] }, 2],
  ])('describes %s by affected count', (op, affected, count) => {
    expect(describeActivity(record({ op, affected }))).toEqual({
      key: `history.desc.${op}`,
      params: { count },
    });
  });

  it('describes edge_intensity_set without needing an id or count', () => {
    const r = record({ op: 'edge_intensity_set', affected: { kind: 'edge_intensity' } });
    expect(describeActivity(r)).toEqual({ key: 'history.desc.edge_intensity_set', params: {} });
  });

  it('falls back to a generic description for an unrecognised op instead of dumping raw JSON', () => {
    const r = record({ op: 'some_future_op' });
    expect(describeActivity(r)).toEqual({ key: 'history.desc.unknown', params: {} });
  });
});

describe('isUndoableRecord', () => {
  it('is true only for a not-undone record with an inverse op', () => {
    expect(isUndoableRecord(record())).toBe(true);
    expect(isUndoableRecord(record({ undone: true }))).toBe(false);
    expect(isUndoableRecord(record({ inverse_op: null }))).toBe(false);
    expect(isUndoableRecord(null)).toBe(false);
  });
});

describe('findLatestUndoable', () => {
  it('picks the newest not-undone record for the actor, ignoring others', () => {
    const records = [
      record({ id: 'r3', actor: 'client-a', undone: false }), // newest
      record({ id: 'r2', actor: 'client-b', undone: false }),
      record({ id: 'r1', actor: 'client-a', undone: false }), // older, same actor
    ];
    expect(findLatestUndoable(records, 'client-a').id).toBe('r3');
  });

  it('skips an already-undone record even if it is the newest for that actor', () => {
    const records = [
      record({ id: 'r2', actor: 'client-a', undone: true }),
      record({ id: 'r1', actor: 'client-a', undone: false }),
    ];
    expect(findLatestUndoable(records, 'client-a').id).toBe('r1');
  });

  it('returns null when the actor has nothing undoable', () => {
    const records = [record({ actor: 'client-b' })];
    expect(findLatestUndoable(records, 'client-a')).toBeNull();
  });

  it('returns null for an empty or missing list, or a missing actor', () => {
    expect(findLatestUndoable([], 'client-a')).toBeNull();
    expect(findLatestUndoable(undefined, 'client-a')).toBeNull();
    expect(findLatestUndoable([record()], '')).toBeNull();
  });
});

describe('classifyUndoError', () => {
  it('maps 429 to rate_limited', () => {
    expect(classifyUndoError({ status: 429 })).toBe('rate_limited');
  });

  it('maps 404 to unavailable, regardless of whether it was "no undoable action" or "session not found"', () => {
    expect(classifyUndoError({ status: 404, message: 'no undoable action' })).toBe('unavailable');
    expect(classifyUndoError({ status: 404, message: 'session not found' })).toBe('unavailable');
  });

  it('maps the exact LayoutBusy 409 message to busy (retryable)', () => {
    expect(classifyUndoError({ status: 409, message: 'session busy, retry' })).toBe('busy');
  });

  it('maps the LeaseConflict 409 to claimed (retryable), not to conflict', () => {
    expect(
      classifyUndoError({
        status: 409,
        message:
          "annotation 'note-1' is being edited by another client ('client-b'); " +
          'wait for the lease to release or expire before editing it',
      })
    ).toBe('claimed');
    expect(
      classifyUndoError({
        status: 409,
        message:
          "annotation 'sticky-42' is being edited by another client ('someone-else'); " +
          'wait for the lease to release or expire before editing it',
      })
    ).toBe('claimed');
  });

  it('maps every other 409 to conflict (not retryable)', () => {
    expect(
      classifyUndoError({ status: 409, message: 'affected state changed since this action' })
    ).toBe('conflict');
    expect(classifyUndoError({ status: 409, message: 'action could not be reverted' })).toBe(
      'conflict'
    );
    expect(classifyUndoError({ status: 409, message: 'something else entirely' })).toBe('conflict');
  });

  it('maps anything else (500, network error, missing status) to failed', () => {
    expect(classifyUndoError({ status: 500 })).toBe('failed');
    expect(classifyUndoError({})).toBe('failed');
    expect(classifyUndoError(undefined)).toBe('failed');
  });
});

describe('describeActivity × i18n', () => {
  const cases = [
    { before: { type: 'note', shape: 'rectangle' }, after: { type: 'note', shape: 'ellipse' } },
    { before: { type: 'note', locked: false }, after: { type: 'note', locked: true } },
    { before: { type: 'note', locked: true }, after: { type: 'note', locked: false } },
    { before: { type: 'label' }, after: { type: 'label', attachment: { target_id: 'n1' } } },
    { before: { type: 'label', attachment: { target_id: 'n1' } }, after: { type: 'label' } },
    {
      before: { type: 'note', geometry: { x: 0, y: 0, w: 1, h: 1, rotation: 0 } },
      after: { type: 'note', geometry: { x: 0, y: 0, w: 1, h: 1, rotation: 9 } },
    },
    {
      before: { type: 'note', geometry: { x: 0, y: 0, w: 1, h: 1, rotation: 0 } },
      after: { type: 'note', geometry: { x: 0, y: 0, w: 9, h: 1, rotation: 0 } },
    },
    {
      before: { type: 'note', position: { x: 0, y: 0 } },
      after: { type: 'note', position: { x: 9, y: 0 } },
    },
    { before: { type: 'note', z: 0 }, after: { type: 'note', z: 5 } },
    { before: { type: 'note', z: 0 }, after: { type: 'note', z: -5 } },
    {
      before: { type: 'note', style: { color: 'a' } },
      after: { type: 'note', style: { color: 'b' } },
    },
    { before: { type: 'note', text: 'a' }, after: { type: 'note', text: 'b' } },
    { before: { type: 'note' }, after: { type: 'note', future_field: 'x' } },
  ];

  it('has an en and sv message for every annotation_updated kind these inputs classify to', () => {
    const keys = cases.map((c) => describeActivity(record({ op: 'annotation_updated', ...c })).key);
    const unique = [...new Set(keys)];
    expect(unique).toHaveLength(cases.length);
    for (const key of unique) {
      const name = key.replace('history.desc.', '');
      expect(en.history.desc[name], `en: ${name}`).toBeTruthy();
      expect(sv.history.desc[name], `sv: ${name}`).toBeTruthy();
    }
  });
});

describe('classifyUndoError × i18n', () => {
  const errors = [
    { status: 429 },
    { status: 404, message: 'no undoable action' },
    { status: 409, message: 'session busy, retry' },
    { status: 409, message: "annotation 'note-1' is being edited by another client ('c2'); wait" },
    { status: 409, message: 'affected state changed since this action' },
    { status: 500 },
  ];

  it('has an en and sv message for every reason these inputs classify to', () => {
    const reasons = [...new Set(errors.map(classifyUndoError))];
    expect(reasons).toHaveLength(errors.length);
    for (const reason of reasons) {
      expect(en.history[`session_undo_${reason}`], `en: ${reason}`).toBeTruthy();
      expect(sv.history[`session_undo_${reason}`], `sv: ${reason}`).toBeTruthy();
    }
  });
});
