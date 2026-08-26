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

import { normalizeShapeName } from '@community-graph/ui-graph-canvas';

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

/**
 * Whether a value carries no information: the state an annotation is in
 * before anything has been set on that field.
 *
 * This exists because the two producers of an `annotation_updated` record
 * disagree about which absent fields they spell out. The browser ships the
 * whole annotation on every edit, and its translators materialise every
 * envelope default on the way out (`z: a.z ?? 0`, `locked: Boolean(a.locked)`,
 * `text: o.text || ''` — see utils/sessionAnnotations.js); an agent writing
 * the same annotation over MCP may simply omit them. So the first browser
 * touch of an agent-created annotation turns `z: undefined` into `z: 0` and
 * `locked: undefined` into `locked: false` without the user having done
 * anything to either. Counting those as changes would put this classifier
 * straight back to announcing an unlock that never happened, which is the
 * defect it is being fixed for — so an absent field and its own default read
 * as the same value here.
 */
function isEmptyValue(value) {
  if (value === undefined || value === null || value === false || value === '' || value === 0) {
    return true;
  }
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === 'object' && Object.keys(value).length === 0;
}

/** Deep value equality, with every "unset" spelling treated as one value. */
function sameValue(a, b) {
  if (a === b) return true;
  if (isEmptyValue(a) || isEmptyValue(b)) return isEmptyValue(a) && isEmptyValue(b);
  if (typeof a !== 'object' || typeof b !== 'object') return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    if (!sameValue(a[key], b[key])) return false;
  }
  return true;
}

/**
 * Whether a field's two spellings mean the same value.
 *
 * `shape` needs more than `sameValue`: the server stores `content.shape`
 * verbatim (`_validate_generic_content` only type-checks it, and
 * `ANNOTATION_SHAPES` is deliberately not a rejection list), while the
 * browser runs it through `normalizeShapeName` on load. So an agent-created
 * shape with no `shape` at all, or one spelled "Process Arrow", comes back
 * from the first browser touch as "rectangle" / "process_arrow" — a
 * normalisation artefact that would otherwise announce "Changed the shape
 * of" for a user who only dragged it. Compared through the browser's own
 * normaliser rather than a second copy of the rule, so the two cannot drift.
 */
function fieldsEqual(key, before, after) {
  if (key === 'shape') return sameValue(normalizeShapeName(before), normalizeShapeName(after));
  return sameValue(before, after);
}

/**
 * A change between two values that both carry information — as opposed to a
 * field being filled in for the first time. Used where a producer supplies a
 * non-zero default for a field the other producer leaves at nothing, which
 * `isEmptyValue` alone cannot absorb because the default is not empty.
 */
function realChange(before, after) {
  return !isEmptyValue(before) && !isEmptyValue(after) && !sameValue(before, after);
}

// Rewritten by the store on every write whatever the user touched
// (session_store.py sets `updated_at` on each applied op), so a diff that
// counted them would report every update as a change to them.
const BOOKKEEPING_FIELDS = new Set(['updated_at', 'updated_by', 'created_at', 'created_by']);

/**
 * The annotation fields this update actually changed, by comparing the
 * record's own before/after snapshots.
 *
 * Deliberately not `affected.fields`: that is the *incoming payload's* key
 * set, not a change set (session_store.py's `annotation_updated` branch
 * records `sorted(incoming.keys())`), and the browser's computeOps sends the
 * whole annotation in every op (services/sessionSyncClient.js). For any
 * browser-originated edit `fields` is therefore the annotation's entire key
 * set and is identical whatever the user did — reading it as a change set
 * made a move, a recolour or a text edit on a note/label/icon all render as
 * "Unlocked", asserting a security-relevant state change that never
 * happened. before/after are populated for every producer, because
 * `apply_state_op` is the single choke point both the browser batch and the
 * MCP write path go through, so the diff is authoritative for both.
 *
 * Returns null when there is no before snapshot to compare against, which is
 * the one case where nothing about the change can be asserted.
 */
function changedFields(before, after) {
  if (!before || typeof before !== 'object') return null;
  if (!after || typeof after !== 'object') return null;
  const changed = new Set();
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (BOOKKEEPING_FIELDS.has(key)) continue;
    if (!fieldsEqual(key, before[key], after[key])) changed.add(key);
  }
  return changed;
}

/**
 * Classify what an `annotation_updated` record actually changed, from a
 * before/after comparison, so "moved" / "resized" / "rotated" / "raised"
 * read distinctly instead of a blanket "updated" — and so a kind is only
 * ever reported when that field genuinely changed. Order matters: the most
 * specific, most visible change wins when several plausibly apply (e.g. a
 * drag-resize touches both size and position, and bringing an annotation to
 * the front while dragging it reads as the move).
 */
function classifyAnnotationUpdate(record) {
  const changed = changedFields(record.before, record.after);
  if (!changed) return 'generic';
  const before = geometryOf(record.before);
  const after = geometryOf(record.after);

  if (changed.has('shape')) return 'shape';
  if (changed.has('locked')) return record.after && record.after.locked ? 'locked' : 'unlocked';
  if (changed.has('attachment')) {
    const attached = Boolean(record.after && record.after.attachment);
    return attached ? 'attached' : 'detached';
  }
  if (changed.has('geometry') || changed.has('position')) {
    if (!sameValue(before.rotation, after.rotation)) return 'rotated';
    // `w`/`h` only, not `x`/`y`: an unsized annotation gains 160x96 the first
    // time the browser writes it back, because `label`, `line` and `freehand`
    // carry no size through the overlay translators and `normalizeGeometry`
    // fills DEFAULT_SIZE, while the server defaults both to 0. That is a
    // materialised default, not a resize — reporting it as one told a user
    // who dragged an agent-created line that they had resized it. `x`/`y`
    // need no such guard: both are required by `build_annotation`, so a
    // coordinate of 0 is a real position and moving off it is a real move.
    if (realChange(before.w, after.w) || realChange(before.h, after.h)) return 'resized';
    if (!sameValue(before.x, after.x) || !sameValue(before.y, after.y)) return 'moved';
    // Nothing in the geometry actually moved — fall through to whatever else
    // this update touched rather than asserting a move that did not happen.
  }
  if (changed.has('z')) {
    // Strictly the direction the layer moved, not "is now frontmost": a
    // bring-to-front click always raises and a send-to-back always lowers
    // (utils/annotationLayers.js resolveLayerZ), but an agent may set any
    // `z` over MCP, and neither producer guarantees the result is past
    // everything else on someone else's canvas.
    const raised = (record.after.z || 0) > (record.before.z || 0);
    return raised ? 'raised' : 'lowered';
  }
  if (changed.has('style')) return 'style';
  if (changed.has('text') || changed.has('label') || changed.has('value')) return 'text';
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
    case 'nodes_dimmed': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.nodes_dimmed', params: { count } };
    }
    case 'nodes_undimmed': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.nodes_undimmed', params: { count } };
    }
    case 'edges_dimmed': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.edges_dimmed', params: { count } };
    }
    case 'edges_undimmed': {
      const count = (record.affected && record.affected.ids && record.affected.ids.length) || 0;
      return { key: 'history.desc.edges_undimmed', params: { count } };
    }
    case 'edge_intensity_set':
      return { key: 'history.desc.edge_intensity_set', params: {} };
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
 * @returns {'rate_limited'|'unavailable'|'busy'|'claimed'|'conflict'|'failed'}
 */
export function classifyUndoError(err) {
  const status = err?.status;
  if (status === 429) return 'rate_limited';
  if (status === 404) return 'unavailable';
  if (status === 409) {
    // Two of the backend's 409s are transient and retryable, and the generic
    // conflict message ("can no longer be undone") is false for both, so each
    // is matched on its own backend text: the session is mid-write
    // (LayoutBusy), or another client holds a live selection claim on the
    // annotation the inverse op would touch (ClaimConflict) — the latter
    // clears on deselect or when the 30 s TTL expires. The claim string is
    // pinned from the backend side by
    // backend/core/tests/test_session_manager.py's
    // test_claim_conflict_message_matches_the_ui_classifier, so a wording
    // change there fails CI rather than silently degrading this into the
    // (false, permanent-sounding) conflict text. The LayoutBusy string is not
    // pinned that way — it is a literal in rest_api.py, and a drift there
    // falls back to the conflict message.
    if (err?.message === 'session busy, retry') return 'busy';
    if (typeof err?.message === 'string' && err.message.includes('is claimed by another client')) {
      return 'claimed';
    }
    return 'conflict';
  }
  return 'failed';
}
