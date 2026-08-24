/**
 * Helpers for the Session tab of the Activity drawer: turning a raw session
 * activity record (backend/core/session_activity.py's build_activity_record
 * shape — op, actor, affected, before/after, inverse_op, undone) into a
 * human-readable description, and deciding undo eligibility for the current
 * actor.
 *
 * Pure functions (no React, no i18n) so the description logic is unit-testable
 * without a translation layer — the caller renders `describeActivity`'s
 * `{key, params}` via `t(key, params)`, the same contract utils/history.js
 * uses for relativeTime.
 */

const ANNOTATION_TYPE_KEYS = new Set([
  'note',
  'text',
  'label',
  'line',
  'frame',
  'group',
  'shape',
  'icon',
  'vote_dot',
  'image',
  'freehand',
]);

/** i18n key for an annotation's "a sticky note" / "a shape" phrase. */
function annotationTypeKey(annotation) {
  const type = annotation && (annotation.type || annotation.kind);
  return ANNOTATION_TYPE_KEYS.has(type)
    ? `history.annotation_type.${type}`
    : 'history.annotation_type.unknown';
}

function geometryOf(annotation) {
  return (annotation && (annotation.geometry || annotation.position)) || {};
}

function numbersDiffer(a, b) {
  return a !== b;
}

/**
 * Classify what an `annotation_updated` record actually changed, from the
 * updated top-level fields (`affected.fields`, the incoming update's own
 * keys — see session_store.py) plus a before/after geometry comparison, so
 * "moved" / "resized" / "rotated" read distinctly instead of a blanket
 * "updated". Order matters: the most specific, most visible change wins when
 * several plausibly apply (e.g. a drag-resize touches both size and position).
 */
function classifyAnnotationUpdate(record) {
  const fields = new Set((record.affected && record.affected.fields) || []);
  const before = geometryOf(record.before);
  const after = geometryOf(record.after);

  if (fields.has('shape')) return 'shape';
  if (fields.has('locked')) return record.after && record.after.locked ? 'locked' : 'unlocked';
  if (fields.has('attachment')) {
    const attached = Boolean(record.after && record.after.attachment);
    return attached ? 'attached' : 'detached';
  }
  if (fields.has('geometry') || fields.has('position')) {
    if (numbersDiffer(before.rotation, after.rotation)) return 'rotated';
    if (numbersDiffer(before.w, after.w) || numbersDiffer(before.h, after.h)) return 'resized';
    return 'moved';
  }
  if (fields.has('style')) return 'style';
  if (fields.has('text') || fields.has('label') || fields.has('value')) return 'text';
  return 'generic';
}

/**
 * Describe one activity record as an i18n key + interpolation params, ready
 * for `t(key, params)`. Every UNDOABLE_OPS kind (session_activity.py) is
 * covered; an op this build doesn't recognise falls back to a generic
 * "changed something" description rather than a raw dump.
 *
 * @param {Object} record - one entry from GET /sessions/{id}/activity
 * @param {{nodeName?: (id: string) => string|undefined}} [opts] - resolve a
 *   node id to its display name for node_moved (falls back to the id).
 * @returns {{key: string, params: Object}}
 */
export function describeActivity(record, opts = {}) {
  const resolveNodeName = opts.nodeName || (() => undefined);
  const op = record && record.op;

  switch (op) {
    case 'annotation_created':
      return {
        key: 'history.desc.annotation_created',
        params: { type: annotationTypeKey(record.after) },
      };
    case 'annotation_deleted':
      return {
        key: 'history.desc.annotation_deleted',
        params: { type: annotationTypeKey(record.before) },
      };
    case 'annotation_updated': {
      const kind = classifyAnnotationUpdate(record);
      return {
        key: `history.desc.annotation_updated_${kind}`,
        params: { type: annotationTypeKey(record.after) },
      };
    }
    case 'node_moved': {
      const nodeId = record.affected && record.affected.id;
      return {
        key: 'history.desc.node_moved',
        params: { name: resolveNodeName(nodeId) || nodeId || '' },
      };
    }
    case 'layout_applied': {
      const count =
        (record.affected && record.affected.node_ids && record.affected.node_ids.length) || 0;
      return { key: 'history.desc.layout_applied', params: { count } };
    }
    case 'nodes_hidden': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.nodes_hidden', params: { count } };
    }
    case 'nodes_shown': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.nodes_shown', params: { count } };
    }
    default:
      return { key: 'history.desc.unknown', params: {} };
  }
}

/**
 * Whether `record` is the kind of thing undo could ever apply to (not
 * whether it currently would succeed — a stale conflict is only knowable
 * server-side, see session_activity.undo_conflict_reason).
 */
export function isUndoableRecord(record) {
  return Boolean(record && !record.undone && record.inverse_op);
}

/**
 * The single record the "Undo" affordance targets for `actorId`: the newest
 * not-yet-undone record with an inverse op, by that actor. Mirrors
 * session_activity.find_latest_undoable exactly, scanning `records` (assumed
 * newest-first, as GET .../activity returns them) for the first match rather
 * than reversing — so it agrees with the backend on which record undo will
 * actually revert.
 *
 * @param {Array} records - activity records, newest first
 * @param {string} actorId
 * @returns {Object|null}
 */
export function findLatestUndoable(records, actorId) {
  if (!Array.isArray(records) || !actorId) return null;
  return records.find((r) => r.actor === actorId && isUndoableRecord(r)) || null;
}

/**
 * Undo error classification, from api.undoSessionAction's rejected error
 * (`status` + `message`, the latter read from the backend's HTTPException
 * `detail` — see rest_api.py's get_session_activity/undo_session_action and
 * session_manager.py's undo_last_action). Drives which
 * `history.session_undo_<reason>` i18n key the caller shows, so the message
 * is always translated rather than surfacing the backend's English-only text.
 *
 * @param {{status?: number, message?: string}} err
 * @returns {'rate_limited'|'unavailable'|'busy'|'conflict'|'failed'}
 */
export function classifyUndoError(err) {
  const status = err?.status;
  if (status === 429) return 'rate_limited';
  if (status === 404) return 'unavailable';
  if (status === 409) {
    // The only 409 the UI ever triggers other than a real conflict: the
    // session is mid-write (LayoutBusy, session_manager.py) — literal string
    // match against that one backend message so a transient lock reads as
    // "busy, retry" rather than the (non-retryable) conflict message. A
    // future wording change there just falls back to the conflict message,
    // which is still a safe, true description of a 409.
    if (err?.message === 'session busy, retry') return 'busy';
    return 'conflict';
  }
  return 'failed';
}
