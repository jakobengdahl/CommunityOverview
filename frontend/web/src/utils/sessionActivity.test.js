import { describe, it, expect } from 'vitest';
import { createAnnotation } from '@community-graph/ui-graph-canvas';
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
    affected: { kind: 'annotation', id: 'note-1', fields: null },
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

  describe('annotation_updated classification', () => {
    it('reads a shape-subtype change as "shape" even when geometry also moved', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'shape-1', fields: ['shape', 'geometry'] },
        before: {
          type: 'shape',
          shape: 'rectangle',
          geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 },
        },
        after: {
          type: 'shape',
          shape: 'ellipse',
          geometry: { x: 5, y: 5, w: 10, h: 10, rotation: 0 },
        },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_shape');
    });

    it('reads a rotation-only geometry change as "rotated"', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['geometry'] },
        before: { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        after: { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 15 } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_rotated');
    });

    it('reads a size change as "resized"', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['geometry'] },
        before: { type: 'note', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        after: { type: 'note', geometry: { x: 0, y: 0, w: 40, h: 10, rotation: 0 } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_resized');
    });

    it('reads a plain position change as "moved"', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['position'] },
        before: { type: 'note', position: { x: 0, y: 0 } },
        after: { type: 'note', position: { x: 50, y: 0 } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });

    it('reads a style-only change as "style"', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['style'] },
        before: { type: 'note', style: { color: 'blue' } },
        after: { type: 'note', style: { color: 'red' } },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_style');
    });

    it('reads a text field change as "text"', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['text'] },
        before: { type: 'note', text: 'old' },
        after: { type: 'note', text: 'new' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it('reads a locked flip as "locked" / "unlocked"', () => {
      const locking = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['locked'] },
        before: { type: 'note', locked: false },
        after: { type: 'note', locked: true },
      });
      expect(describeActivity(locking).key).toBe('history.desc.annotation_updated_locked');

      const unlocking = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['locked'] },
        before: { type: 'note', locked: true },
        after: { type: 'note', locked: false },
      });
      expect(describeActivity(unlocking).key).toBe('history.desc.annotation_updated_unlocked');
    });

    it('reads an attachment field change as "attached" / "detached"', () => {
      const attaching = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['attachment'] },
        before: { type: 'label' },
        after: { type: 'label', attachment: { target_id: 'node-1' } },
      });
      expect(describeActivity(attaching).key).toBe('history.desc.annotation_updated_attached');

      const detaching = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['attachment'] },
        before: { type: 'label', attachment: { target_id: 'node-1' } },
        after: { type: 'label' },
      });
      expect(describeActivity(detaching).key).toBe('history.desc.annotation_updated_detached');
    });

    it('falls back to "generic" for an unrecognised field set', () => {
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['some_future_field'] },
        before: { type: 'note' },
        after: { type: 'note', some_future_field: 'x' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
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

  // The regression suite for the defect these records are shaped after: the
  // browser's computeOps (services/sessionSyncClient.js) puts the WHOLE
  // annotation in every annotation_updated op, so `affected.fields` — which
  // session_store.py fills with the incoming payload's key set — is the
  // annotation's entire key set no matter what the user actually did. Every
  // annotation carries `locked` and `z` as mandatory envelope fields
  // (createAnnotation), so a classifier reading `fields` as a change set
  // announced "Unlocked" for a note/label/icon that had merely been moved or
  // relayered, and "Changed the shape of" for every edit of a shape.
  //
  // The pre-existing cases above all feed SPARSE fields, which is the shape
  // the MCP patch path produces — the producer the classifier happened to be
  // correct for. These feed the browser's shape, built through the real
  // createAnnotation so the payload is the one the canvas actually ships.
  describe('annotation_updated classification, browser-shaped full payloads', () => {
    function browserEdit(base, changes) {
      // What the store records for a browser-originated edit: `before` is the
      // stored annotation, `after` is the whole incoming annotation merged
      // over it, and `fields` is that payload's entire key set.
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
      { id: 'n1', type: 'note', text: 'hello', position: { x: 0, y: 0 } },
      { id: 'l1', type: 'label', text: 'a label', position: { x: 0, y: 0 } },
      { id: 'i1', type: 'icon', icon: 'circle', position: { x: 0, y: 0 } },
      { id: 's1', type: 'shape', shape: 'rectangle', position: { x: 0, y: 0 } },
    ];

    it.each(KINDS)('classifies a bring-to-front on $type as "raised", not "unlocked"', (base) => {
      expect(describeActivity(browserEdit(base, { z: 5 })).key).toBe(
        'history.desc.annotation_updated_raised'
      );
    });

    it.each(KINDS)('classifies a send-to-back on $type as "lowered", not "unlocked"', (base) => {
      expect(describeActivity(browserEdit(base, { z: -1 })).key).toBe(
        'history.desc.annotation_updated_lowered'
      );
    });

    it.each(KINDS)('classifies a plain move of $type as "moved", not "unlocked"', (base) => {
      expect(describeActivity(browserEdit(base, { position: { x: 300, y: 200 } })).key).toBe(
        'history.desc.annotation_updated_moved'
      );
    });

    it.each(KINDS)('classifies a recolour of $type as "style", not "unlocked"', (base) => {
      expect(describeActivity(browserEdit(base, { color: 'crimson' })).key).toBe(
        'history.desc.annotation_updated_style'
      );
    });

    it.each(KINDS)('still reports a genuine lock and unlock of $type as itself', (base) => {
      expect(describeActivity(browserEdit(base, { locked: true })).key).toBe(
        'history.desc.annotation_updated_locked'
      );
      const locked = { ...base, locked: true };
      expect(describeActivity(browserEdit(locked, { locked: false })).key).toBe(
        'history.desc.annotation_updated_unlocked'
      );
    });

    it('reads a resize as "resized" and a rotation as "rotated" through a full payload', () => {
      const base = KINDS[0];
      expect(describeActivity(browserEdit(base, { size: { w: 400, h: 300 } })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
      expect(describeActivity(browserEdit(base, { rotation: 30 })).key).toBe(
        'history.desc.annotation_updated_rotated'
      );
    });

    it('reads a text edit through a full payload as "text"', () => {
      expect(describeActivity(browserEdit(KINDS[0], { text: 'rewritten' })).key).toBe(
        'history.desc.annotation_updated_text'
      );
    });

    it('reports a re-sent but unchanged annotation as "generic", not as an unlock', () => {
      // computeOps only emits an op when something differs, but a retried
      // batch or an undo replay can re-apply an identical payload.
      expect(describeActivity(browserEdit(KINDS[0], {})).key).toBe(
        'history.desc.annotation_updated_generic'
      );
    });

    it('does not read the browser materialising envelope defaults as a change', () => {
      // The first browser touch of an agent-created annotation spells out the
      // envelope fields the agent left off (`z`, `locked`, `text`). Counting
      // those as changes is the same false-unlock defect by another route, so
      // the move is what gets reported.
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'a1', fields: ['id', 'locked', 'position', 'z'] },
        before: { id: 'a1', type: 'note', position: { x: 0, y: 0 } },
        after: { id: 'a1', type: 'note', position: { x: 40, y: 0 }, z: 0, locked: false, text: '' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });
  });

  // The suite above builds `before` with createAnnotation too, so both sides
  // are already browser-normalised — which models a browser-created
  // annotation edited by the browser, the case that was never broken. The
  // producer divergence that caused the original defect lives in the other
  // pairing: a SERVER-stored `before` (backend/core/session_annotations.py's
  // build_annotation — no materialised defaults, `w`/`h` at 0, `content`
  // merged verbatim) against a browser-normalised `after`. These drive the
  // real overlay translators over server-shaped annotations, which is what
  // the classifier actually sees the first time a user touches anything an
  // agent created.
  describe('annotation_updated classification, server-created annotations', () => {
    // build_annotation's output shape, hand-written so the test states the
    // contract rather than importing it across the language boundary:
    // required x/y, w/h/rotation defaulted to 0, `content` merged verbatim.
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

    // The round trip a user's drag actually takes: load through the overlay
    // translators, move it, write it back, over the wire as JSON.
    function browserMove(stored, dx = 250) {
      const overlays = annotationsToOverlays([stored]);
      const moved = overlays.map((o) => ({
        ...o,
        position: { x: (o.position?.x ?? 0) + dx, y: o.position?.y ?? 0 },
      }));
      const incoming = JSON.parse(JSON.stringify(overlaysToAnnotations(moved)[0]));
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: stored.id, fields: Object.keys(incoming).sort() },
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
      { name: 'frame', ann: serverAnnotation('frame') },
      { name: 'note', ann: serverAnnotation('note', { text: 'hi' }) },
    ];

    it.each(MOVABLE)('reads a drag of an agent-created $name as "moved"', ({ ann }) => {
      expect(describeActivity(browserMove(ann)).key).toBe('history.desc.annotation_updated_moved');
    });

    it('does not read the browser normalising a free-text shape name as a shape change', () => {
      // "Process Arrow" comes back as "process_arrow" purely from
      // normalizeShapeName; the server stores content.shape verbatim.
      const r = browserMove(serverAnnotation('shape', { shape: 'Process Arrow' }));
      expect(r.before.shape).toBe('Process Arrow');
      expect(r.after.shape).toBe('process_arrow');
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
    });

    it('does not read the browser filling in a default size as a resize', () => {
      // label/line/freehand carry no size through the overlay translators, so
      // normalizeGeometry fills 160x96 over the server's 0.
      const r = browserMove(serverAnnotation('label', { text: 'hi' }));
      expect(r.before.geometry.w).toBe(0);
      expect(r.after.geometry.w).toBeGreaterThan(0);
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
        name: 'freehand whose stored position disagrees with its first point',
        ann: () =>
          serverAnnotation('freehand', {
            x: 0,
            y: 0,
            points: [
              { x: 120, y: 340 },
              { x: 130, y: 350 },
            ],
          }),
      },
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
      // The translators derive these kinds' position from their own content,
      // so an untouched round trip rewrites position without the user having
      // dragged anything. Nothing else changed either, so there is nothing to
      // report.
      const stored = ann();
      const r = browserMove(stored, 0);
      expect(r.before.position).not.toEqual(r.after.position);
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
      // A real drag shifts the derived content too, which is what separates it
      // from the rewrite above.
      expect(describeActivity(browserMove(ann(), 250)).key).toBe(
        'history.desc.annotation_updated_moved'
      );
    });

    it('still reports a genuine resize of an annotation the server left unsized', () => {
      // The materialised-default guard must key on the default itself, not on
      // "either side is 0", or a first-touch resize disappears.
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
      // build_annotation_patch accepts w=0; only MCP can reach this, since the
      // canvas resizer has a minimum.
      const before = serverAnnotation('frame');
      before.geometry = { x: 10, y: 20, w: 800, h: 400, rotation: 0 };
      const after = { ...before, geometry: { x: 10, y: 20, w: 0, h: 400, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });

    it('reads a move onto and off the origin as a move', () => {
      // Pins the asymmetry the geometry branch rests on: `x`/`y` are compared
      // plainly while `w`/`h` are guarded, so a coordinate of 0 stays a real
      // position. Collapsing the two onto one rule would pass every other test
      // here while silently dropping these.
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

    it('does not claim a move when only the size was materialised', () => {
      // A recolour of an agent-created label: geometry differs only by the
      // default size the browser filled in, so the recolour is what happened.
      const before = serverAnnotation('label', { text: 'hi' });
      const after = {
        ...before,
        geometry: { x: 10, y: 20, w: 160, h: 96, rotation: 0 },
        style: { color: 'crimson' },
      };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_style'
      );
    });
  });

  // Groups are the kind two rounds of per-field fixes never reached, because
  // they are not overlays: `useSharedSession` mirrors them through
  // annotationsToGroups/groupsToAnnotations, which carry neither `z` nor
  // `locked` nor rotation. So the browser's write-back of an agent-created
  // group dropped all three, and renaming a locked group announced
  // "Unlocked" — the literal string this whole change exists to eliminate.
  describe('annotation_updated classification, groups', () => {
    // build_group_annotation's output shape (backend/core/session_annotations.py).
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

    // The round trip a group edit actually takes.
    function groupEdit(stored, mutate = (g) => g) {
      const { groups, parentIds } = annotationsToGroups([stored]);
      const edited = groups.map((g) => mutate({ ...g }));
      const incoming = JSON.parse(JSON.stringify(groupsToAnnotations(edited, parentIds)[0]));
      return record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: stored.id, fields: Object.keys(incoming).sort() },
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
      expect(r.after.locked).toBe(false); // the translators really do drop it
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

    it('does not report a group z that the translators drop as a layer change', () => {
      // The group pair carries no `z`, so the write-back always yields 0.
      const r = groupEdit(serverGroup({ label: 'Team', z: 4 }), (g) => ({ ...g, label: 'B' }));
      expect(r.before.z).toBe(4);
      expect(r.after.z).toBe(0);
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it.each([
      { edit: 'locking', field: 'locked', from: false, to: true, expected: 'locked' },
      { edit: 'raising', field: 'z', from: 0, to: 5, expected: 'raised' },
      { edit: 'rotating', field: 'rotation', from: 0, to: 45, expected: 'rotated' },
    ])('reports an agent $edit a group, since it moves AWAY from the dropped default', (c) => {
      // The asymmetry the browserWriteBack docstring describes, pinned so the
      // comment cannot drift back to the easier and wrong "these fields are
      // ignored for groups". The write-back always states the default, so a
      // change away from it is visible...
      const at = (v) =>
        c.field === 'rotation'
          ? serverGroup({ geometry: { x: 10, y: 20, w: 320, h: 200, rotation: v } })
          : serverGroup({ [c.field]: v });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(c.from), after: at(c.to) }))
          .key
      ).toBe(`history.desc.annotation_updated_${c.expected}`);
    });

    it.each([
      { edit: 'unlocking', field: 'locked', from: true, to: false },
      { edit: 'lowering to zero', field: 'z', from: 5, to: 0 },
      { edit: 'unrotating', field: 'rotation', from: 45, to: 0 },
    ])('under-reports an agent $edit a group, which it cannot distinguish', (c) => {
      // ...and a change TO the default is indistinguishable from the
      // translators dropping the field, so it is not reported. Unlocking is
      // the one that matters: this classifier can say "Locked" truly but can
      // never say "Unlocked" truly for a group. Under-reporting is the safe
      // direction; the real fix is in the group translators, logged
      // separately.
      const at = (v) =>
        c.field === 'rotation'
          ? serverGroup({ geometry: { x: 10, y: 20, w: 320, h: 200, rotation: v } })
          : serverGroup({ [c.field]: v });
      expect(
        describeActivity(record({ op: 'annotation_updated', before: at(c.from), after: at(c.to) }))
          .key
      ).toBe('history.desc.annotation_updated_generic');
    });

    it('does not report renaming a group as a layer or rotation change', () => {
      // `z` and rotation are dropped by the group translators the same way.
      const withZ = groupEdit(serverGroup({ label: 'Team', z: 3 }), (g) => ({
        ...g,
        label: 'Team B',
      }));
      expect(describeActivity(withZ).key).toBe('history.desc.annotation_updated_text');

      const rotated = serverGroup({ label: 'Team' });
      rotated.geometry = { ...rotated.geometry, rotation: 45 };
      expect(describeActivity(groupEdit(rotated, (g) => ({ ...g, label: 'B' }))).key).toBe(
        'history.desc.annotation_updated_text'
      );
    });
  });

  // Round 3's remaining findings: every one is a producer rewriting a field
  // the user never touched, on a path the per-field guards did not cover.
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

    it.each(['text', 'label', 'icon', 'vote_dot'])(
      'does not report an attachment gaining target_type on a dragged %s as "attached"',
      (type) => {
        // normalizeAttachment fills target_type:'node'; the backend makes it
        // optional, so an agent may legally omit it.
        const stored = serverAnn(type, { attachment: { target_id: 'n2' }, text: 'hi' });
        const r = browserRoundTrip(stored, (o) => ({
          ...o,
          position: { x: 300, y: 20 },
        }));
        expect(describeActivity(r).key).toBe('history.desc.annotation_updated_moved');
      }
    );

    it('does not report normalising freehand point metadata as a move', () => {
      // normalizeFreehandPoint clamps pressure and drops unknown keys; the
      // backend stores points verbatim.
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
      // The translators carry a fixed style key list, so style.opacity is
      // dropped on the way out. That must not outrank what the user did.
      const stored = serverAnn('text', { text: 'before', style: { color: 'red', opacity: 0.5 } });
      const r = browserRoundTrip(stored, (o) => ({ ...o, text: 'after' }));
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_text');
    });

    it('still reports an agent resizing a kind whose overlay carries its size', () => {
      // A frame's overlay preserves w/h, so 0 -> 160x96 there can only be an
      // agent resize. A guard keyed on the value rather than the round trip
      // swallowed exactly this.
      const before = serverAnn('frame');
      const after = { ...before, geometry: { x: 10, y: 20, w: 160, h: 96, rotation: 0 } };
      expect(describeActivity(record({ op: 'annotation_updated', before, after })).key).toBe(
        'history.desc.annotation_updated_resized'
      );
    });
  });

  describe('cost of classifying an image record', () => {
    // Reconstructing the write-back runs the annotation through
    // createAnnotation, which deep-clones an image's payload through JSON —
    // and that payload is an embedded data URI of up to 2 MB. Done twice per
    // record from SessionActivityList's render body, on a drawer holding up
    // to 500 records that re-renders on every node change, it cost 1838 ms of
    // synchronous main-thread time for 100 records against 1 ms before.
    //
    // Guarded structurally rather than on the clock: a wall-clock bound tight
    // enough to catch the regression is close enough to the honest cost to go
    // flaky on a slow runner. `toJSON` is called if and only if something
    // JSON-serialises the payload, which is exactly the regression.
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
      // SessionActivityList calls describeActivity from its render body, so
      // without the memo every re-render pays the full reconstruction.
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
      // The invariant that makes holding `image` out of the write-back safe:
      // the payload never perturbs how the rest of the annotation is read.
      //
      // Deliberately not asserted as "a swap still registers as a change" —
      // that reads as a guard but cannot fail, because no branch keys on
      // `image` and the change set is only asked whether it holds a named
      // field, so whether `image` is in it is unobservable. Removing the
      // image comparison outright leaves such an assertion passing. This one
      // fails if the payload ever starts influencing the outcome.
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
      // Documented behaviour rather than a guard: there is no `image` kind to
      // report, so a swap with nothing else changed has no more specific
      // description available.
      const swapOnly = record({
        op: 'annotation_updated',
        before: { type: 'image', image: { url: 'data:image/webp;base64,AAAA' } },
        after: { type: 'image', image: { url: 'data:image/webp;base64,BBBB' } },
      });
      expect(describeActivity(swapOnly).key).toBe('history.desc.annotation_updated_generic');
    });
  });

  describe('shape spelling', () => {
    it('does not report an agent respelling a shape it already is as a shape change', () => {
      // The server stores content.shape verbatim, so an agent may write
      // "Rectangle" over a `rectangle`. Normalising only the before-side
      // caught the browser's rewrite but not this one.
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
      // normalizeShapeName keeps an unrecognised name verbatim, so this is a
      // real change rather than a spelling of the same thing.
      expect(change('rectangle', 'blob')).toBe('history.desc.annotation_updated_shape');
    });
  });

  describe('an annotation kind this build cannot read', () => {
    // The activity log keeps 7 days, so it outlives a deploy that drops a
    // kind. `browserWriteBack` cannot reconstruct a write-back for a kind the
    // translators cannot parse, and diffing such a record raw reproduces the
    // exact defect this module exists to remove — the records the OLDER
    // build's browser wrote are whole-annotation rewrites, so every field the
    // new build would normalise reads as a user edit.
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
      // What an older build's browser wrote: everything shipped, the envelope
      // fields landing on the values that build normalised them to.
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
      // The safe direction of the trade: naming the field here would require
      // trusting a raw diff, which is what mislabels the case above. An
      // unreadable kind gets an honest "something changed" either way.
      const patched = { ...stored, text: 'bye' };
      expect(
        describeActivity(record({ op: 'annotation_updated', before: stored, after: patched })).key
      ).toBe('history.desc.annotation_updated_generic');
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
    // classifyAnnotationUpdate's whole correctness rests on this table, and
    // nothing else pins it: a later edit here changes what the activity log
    // asserts, so it gets its own test rather than only being reached through
    // geometry/style.
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
      // The MCP path sends a sparse patch, but the store still snapshots the
      // whole annotation either side, so the diff serves both producers and
      // `affected.fields` is not consulted for either.
      const before = createAnnotation({ id: 'n1', type: 'note', text: 'before' });
      const r = record({
        op: 'annotation_updated',
        affected: { kind: 'annotation', id: 'n1', fields: ['id', 'text'] },
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
    // Falls back to the raw id when the node cannot be resolved (e.g. no
    // longer on the canvas).
    expect(describeActivity(r).params.name).toBe('node-42');
  });

  it('describes layout_applied / nodes_hidden / nodes_shown by affected count', () => {
    expect(
      describeActivity(
        record({ op: 'layout_applied', affected: { kind: 'layout', node_ids: ['a', 'b', 'c'] } })
      )
    ).toEqual({ key: 'history.desc.layout_applied', params: { count: 3 } });

    expect(
      describeActivity(
        record({ op: 'nodes_hidden', affected: { kind: 'node_visibility', ids: ['a', 'b'] } })
      )
    ).toEqual({ key: 'history.desc.nodes_hidden', params: { count: 2 } });

    expect(
      describeActivity(
        record({ op: 'nodes_shown', affected: { kind: 'node_visibility', ids: ['a'] } })
      )
    ).toEqual({ key: 'history.desc.nodes_shown', params: { count: 1 } });
  });

  it('describes nodes_dimmed / nodes_undimmed / edges_dimmed / edges_undimmed by affected count', () => {
    expect(
      describeActivity(
        record({ op: 'nodes_dimmed', affected: { kind: 'node_dim', ids: ['a', 'b'] } })
      )
    ).toEqual({ key: 'history.desc.nodes_dimmed', params: { count: 2 } });

    expect(
      describeActivity(record({ op: 'nodes_undimmed', affected: { kind: 'node_dim', ids: ['a'] } }))
    ).toEqual({ key: 'history.desc.nodes_undimmed', params: { count: 1 } });

    expect(
      describeActivity(record({ op: 'edges_dimmed', affected: { kind: 'edge_dim', ids: ['e1'] } }))
    ).toEqual({ key: 'history.desc.edges_dimmed', params: { count: 1 } });

    expect(
      describeActivity(
        record({ op: 'edges_undimmed', affected: { kind: 'edge_dim', ids: ['e1', 'e2'] } })
      )
    ).toEqual({ key: 'history.desc.edges_undimmed', params: { count: 2 } });
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

  it('maps the ClaimConflict 409 to claimed (retryable), not to conflict', () => {
    // Verbatim str(ClaimConflict) from backend/core/session_manager.py; the
    // ids vary, so the classifier matches the invariant middle of the string.
    expect(
      classifyUndoError({
        status: 409,
        message:
          "annotation 'note-1' is claimed by another client ('client-b'); " +
          'wait for the claim to release or expire before editing it',
      })
    ).toBe('claimed');
    expect(
      classifyUndoError({
        status: 409,
        message:
          "annotation 'sticky-42' is claimed by another client ('someone-else'); " +
          'wait for the claim to release or expire before editing it',
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
    // A future wording change to the busy message would fall through here —
    // still a safe, true "conflict" description of a 409, not a crash.
    expect(classifyUndoError({ status: 409, message: 'something else entirely' })).toBe('conflict');
  });

  it('maps anything else (500, network error, missing status) to failed', () => {
    expect(classifyUndoError({ status: 500 })).toBe('failed');
    expect(classifyUndoError({})).toBe('failed');
    expect(classifyUndoError(undefined)).toBe('failed');
  });
});

describe('describeActivity × i18n', () => {
  // ActivityDrawer.jsx renders describeActivity's key straight through t(),
  // so a classification with no key shows the user the key name. Driven
  // through the classifier for the same reason the undo-reason pairing below
  // is: a kind that changes spelling is caught here, not in the UI.
  const cases = [
    { before: { type: 'note', shape: 'rectangle' }, after: { type: 'note', shape: 'ellipse' } },
    { before: { type: 'note', locked: false }, after: { type: 'note', locked: true } },
    { before: { type: 'note', locked: true }, after: { type: 'note', locked: false } },
    // `label`, not `note`: only label/text/icon/vote_dot carry an attachment
    // through the overlay translators, so a note is never attached or
    // detached and using one here would assert a transition that cannot
    // happen.
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
    // Every case must reach a kind of its own; two collapsing onto one means
    // a branch was lost.
    expect(unique).toHaveLength(cases.length);
    for (const key of unique) {
      const name = key.replace('history.desc.', '');
      expect(en.history.desc[name], `en: ${name}`).toBeTruthy();
      expect(sv.history.desc[name], `sv: ${name}`).toBeTruthy();
    }
  });
});

describe('classifyUndoError × i18n', () => {
  // ActivityDrawer.jsx renders `history.session_undo_${reason}` straight from
  // the classifier's return value, so a reason with no key shows the user the
  // key name. Driven through the classifier rather than hardcoding the reason
  // strings, so a reason that changes spelling is caught here and not in the
  // UI. It does not police the classifier's *shape*: a new branch added here
  // without a matching input below is not caught — the toHaveLength guard
  // catches two inputs collapsing onto one reason, which is the branch
  // deletion this pairs with, not a branch addition.
  const errors = [
    { status: 429 },
    { status: 404, message: 'no undoable action' },
    { status: 409, message: 'session busy, retry' },
    { status: 409, message: "annotation 'note-1' is claimed by another client ('c2'); wait" },
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
