import { describe, it, expect } from 'vitest';
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
        before: { type: 'shape', geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 } },
        after: { type: 'shape', geometry: { x: 5, y: 5, w: 10, h: 10, rotation: 0 } },
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
        after: { type: 'note' },
      });
      expect(describeActivity(r).key).toBe('history.desc.annotation_updated_generic');
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

describe('classifyUndoError × i18n', () => {
  // ActivityDrawer.jsx renders `history.session_undo_${reason}` straight from
  // the classifier's return value, so a reason with no key shows the user the
  // key name. Driven through the classifier rather than hardcoding the list,
  // so a reason that changes spelling is caught here and not in the UI.
  const errors = [
    { status: 429 },
    { status: 404, message: 'no undoable action' },
    { status: 409, message: 'session busy, retry' },
    { status: 409, message: "annotation 'note-1' is claimed by another client ('c2'); wait" },
    { status: 409, message: 'affected state changed since this action' },
    { status: 500 },
  ];

  it('has an en and sv message for every reason the classifier can return', () => {
    const reasons = [...new Set(errors.map(classifyUndoError))];
    expect(reasons).toHaveLength(errors.length);
    for (const reason of reasons) {
      expect(en.history[`session_undo_${reason}`], `en: ${reason}`).toBeTruthy();
      expect(sv.history[`session_undo_${reason}`], `sv: ${reason}`).toBeTruthy();
    }
  });
});
