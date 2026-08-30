/**
 * Helpers for the Session tab of the Activity drawer: turning a raw session
 * activity record (backend/core/session_activity.py's build_activity_record
 * shape — op, actor, affected, before/after, inverse_op, undone) into a
 * human-readable description, and deciding undo eligibility for the current
 * actor.
 *
 * No React and no i18n, so the description logic is unit-testable without a
 * translation layer — the caller renders `describeActivity`'s `{key, params}`
 * via `t(key, params)`, the same contract utils/history.js uses for
 * relativeTime. It does import the canvas package's own normalisers, so that
 * the rules deciding whether a field really changed are the ones the browser
 * applied when it wrote the field, rather than a second copy that can drift.
 */

import { normalizeShapeName } from '@community-graph/ui-graph-canvas';
import {
  annotationsToGroups,
  annotationsToOverlays,
  groupsToAnnotations,
  overlaysToAnnotations,
} from './sessionAnnotations';

const ANNOTATION_TYPE_KEYS = new Set([
  'note',
  'text',
  'label',
  'line',
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
 * Whether a value carries no information: the state a field is in before
 * anything has been set on it. Every spelling of "unset" reads as one value,
 * so a producer writing `0` where another wrote nothing is not a change.
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
 * What the browser would send back for `annotation` if the user changed
 * nothing: the annotation through the same overlay translators every browser
 * edit passes through on its way out.
 *
 * This exists because the browser and an agent writing over MCP do not spell
 * the same annotation the same way, and `computeOps` ships the WHOLE
 * annotation in every op — so the first browser touch of anything an agent
 * created rewrites every field where the two disagree, without the user
 * having done a thing to it. Those rewrites are numerous and keep being
 * found one at a time: envelope defaults (`z`, `locked`, `text`), a shape
 * name run through `normalizeShapeName`, a default 160x96 size on the kinds
 * whose overlays carry none, a `freehand`/`line` position rebuilt from its
 * own points, an attachment gaining `target_type: 'node'`, style and group
 * fields the translators simply drop. Reconstructing the no-op write-back
 * catches all of them by construction rather than one branch at a time, and
 * catches the ones nobody has thought of yet.
 *
 * Groups take the other translator pair — they are not overlays, and
 * `useSharedSession` mirrors them through `annotationsToGroups` — which is
 * exactly why they were the kind still producing the original false
 * "Unlocked" after two rounds of per-field fixes.
 *
 * Wherever that pair states a value of its own rather than the stored one —
 * by dropping a field, or by substituting a default for it — reading that
 * field becomes ASYMMETRIC: a change AWAY from the value it states is
 * visible, a change TO it is indistinguishable from the substitution and so
 * reads as a plain update. Under-reporting is the safe direction, but it is
 * not "the field is ignored", and a follow-up scoped from that misreading
 * would be scoped wrong.
 *
 * Two fields are in that position today, by the two different routes:
 * `rotation`, which the pair simply does not carry, so a group rotated off 0
 * reports it and one rotated back to 0 does not; and `label`, which
 * `annotationsToGroups` substitutes `'Group'` for when it is empty, so
 * naming an unlabelled group literally "Group" reads as a plain update.
 *
 * Holding `label` to this rule is not a wart to be fixed — but the exposure
 * is narrower than it first looks, and worth stating precisely so a
 * follow-up aims at the right place. A drag is safe either way: `shape`,
 * `locked`, `attachment`, geometry, `z` and `style` are all read before
 * `label`, so an edit that touches any of them reports itself and never
 * reaches the text branch. What the rule actually prevents is a write-back
 * of an unlabelled group in which nothing higher-priority changed being
 * reported as a rename the user never made.
 *
 * `rotation`'s asymmetry is latent rather than live: no shipped producer can
 * set a group's rotation — `build_group_annotation` hardcodes 0 and exposes
 * no parameter, and the rotation-carrying generic tools refuse group ids — so
 * it is guarded here ahead of a group rotation control existing, not because
 * something writes it today.
 *
 * `locked` and `z` were there too until the translators were fixed to carry
 * them, at which point this module started reporting a group's lock and layer
 * correctly in both directions with no change of its own. That is the point
 * of reconstructing the write-back rather than enumerating known rewrites:
 * what the round trip preserves is what gets reported, so the classifier
 * tracks the translators instead of drifting from them.
 *
 * Returns null when the annotation cannot be round-tripped — reachable by a
 * `before` snapshot written by an older build, carrying a kind this one
 * cannot read: the log keeps 7 days, so it outlives a deploy that drops a
 * kind. The caller then reports a plain update rather than diffing the
 * snapshots raw.
 *
 * That is deliberate, and the reasoning is worth keeping because two earlier
 * attempts at it were wrong. A raw diff looks safe for such a record on the
 * argument that only a sparse MCP patch can write one — this build's browser
 * drops an unreadable kind from its mirror entirely, so `computeOps` emits
 * nothing for it. But the same 7-day window also holds records the OLDER
 * build's browser wrote, back when it could read the kind, and those are
 * whole-annotation rewrites. Diffing one raw reproduces the exact defect
 * this module exists to remove — measured: a text edit on a locked
 * annotation of a since-removed kind reports "Unlocked". Nothing here can
 * reconstruct a write-back for a kind it cannot parse, so the honest answer
 * is to assert nothing about which field changed. The cost is that a sparse
 * agent patch on such a record reads as a plain update instead of naming the
 * field, which is the safe direction of the two.
 */
function browserWriteBack(annotation) {
  if (!annotation || typeof annotation !== 'object') return null;
  try {
    if ((annotation.type || annotation.kind) === 'group') {
      const { groups, parentIds } = annotationsToGroups([annotation]);
      return groupsToAnnotations(groups, parentIds)[0] || null;
    }
    // `image` is held out of the round trip. An image annotation's payload is
    // an embedded data URI of up to 2 MB (image_ingest's size cap), and
    // `createAnnotation` deep-clones it through JSON on the way past — twice
    // per write-back, which turned opening the drawer on a session full of
    // images into a multi-second main-thread stall.
    //
    // Holding it out cannot change a classification, and the reason is worth
    // stating exactly rather than hand-waving at "it is still compared": no
    // branch below keys on `image`, and the change set is only ever asked
    // whether it holds a *named* field — never its size — so whether `image`
    // lands in it is not observable in the result at all. The translators
    // also do not normalise the payload, only clone it, so there is nothing
    // about it the write-back could have told us.
    const withoutImage = { ...annotation, image: undefined };
    return overlaysToAnnotations(annotationsToOverlays([withoutImage]))[0] || null;
  } catch {
    return null;
  }
}

/**
 * Whether the user changed this field, as opposed to a producer rewriting it.
 *
 * Two conditions, and both are needed. The values must actually differ — or
 * an agent re-sending an identical annotation would look like an edit, since
 * the normal form differs from what it stored. And the new value must not be
 * the one the no-op write-back produces — that is what separates "the user
 * set this" from "the browser normalised it on the way past". A genuine edit
 * fails the second test precisely because it lands somewhere the round trip
 * would not have.
 */
function userChanged(before, after, normalised) {
  if (sameValue(before, after)) return false;
  return !sameValue(after, normalised);
}

/**
 * Whether the user changed the shape subtype.
 *
 * `shape` needs its own equality in both directions, which the write-back
 * alone does not give: that normalises `before`, so it catches the browser
 * rewriting a stored spelling, but an agent may equally write a
 * non-canonical spelling of the shape an annotation already has —
 * `update_annotation(content={"shape": "Rectangle"})` on a `rectangle` is
 * stored verbatim, since `_validate_generic_content` only type-checks it.
 * Comparing both sides through the browser's own normaliser makes that the
 * no-op it is, rather than "Changed the shape of" on a visually identical
 * annotation.
 */
function shapeChanged(before, after) {
  return !sameValue(
    normalizeShapeName(before && before.shape),
    normalizeShapeName(after && after.shape)
  );
}

// Rewritten by the store on every write whatever the user touched
// (session_store.py sets `updated_at` on each applied op), so a diff that
// counted them would report every update as a change to them.
const BOOKKEEPING_FIELDS = new Set(['updated_at', 'updated_by', 'created_at', 'created_by']);

/**
 * The annotation fields this update actually changed, from the record's own
 * before/after snapshots.
 *
 * Deliberately not `affected.fields`: that is the *incoming payload's* key
 * set, not a change set (session_store.py's `annotation_updated` branch
 * records `sorted(incoming.keys())`), and the browser sends the whole
 * annotation in every op. For any browser-originated edit `fields` is
 * therefore the annotation's entire key set and is identical whatever the
 * user did — reading it as a change set made a move, a recolour or a text
 * edit all render as "Unlocked", asserting a security-relevant state change
 * that never happened. before/after are populated for every producer, because
 * `apply_state_op` is the single choke point both the browser batch and the
 * MCP write path go through, so the diff is authoritative for both.
 *
 * Returns null when there is no before snapshot to compare against, which is
 * the one case where nothing about the change can be asserted.
 */
function changedFields(before, after, normalised) {
  if (!before || typeof before !== 'object') return null;
  if (!after || typeof after !== 'object') return null;
  const changed = new Set();
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (BOOKKEEPING_FIELDS.has(key)) continue;
    if (userChanged(before[key], after[key], normalised[key])) changed.add(key);
  }
  return changed;
}

/**
 * Classify what an `annotation_updated` record actually changed, so "moved" /
 * "resized" / "rotated" / "raised" read distinctly instead of a blanket
 * "updated" — and so a kind is only ever reported when the user genuinely did
 * that. Order matters: the most specific, most visible change wins when
 * several plausibly apply (e.g. a drag-resize touches both size and position,
 * and bringing an annotation to the front while dragging it reads as the
 * move).
 */
const classificationCache = new WeakMap();

/**
 * `classifyAnnotationUpdate`, computed once per record.
 *
 * The classification is a pure function of a record, and an activity record
 * is an immutable snapshot — but `describeActivity` is called from
 * SessionActivityList's render, the drawer holds up to 500 records, and it
 * re-renders on every node change in a shared session. Reconstructing the
 * write-back on each of those is pure waste, so it is done once per record
 * object; a refetch produces new objects and so a fresh result. This also
 * keeps the translators' own "cannot read this annotation" warning to once
 * per record rather than once per render, for the older-build `before`
 * snapshot described above.
 */
function classifyAnnotationUpdate(record) {
  const cached = classificationCache.get(record);
  if (cached !== undefined) return cached;
  const kind = computeAnnotationUpdateKind(record);
  classificationCache.set(record, kind);
  return kind;
}

function computeAnnotationUpdateKind(record) {
  const normalised = browserWriteBack(record.before);
  // No write-back means no way to tell a user's edit from a producer's
  // rewrite, so nothing about the change can be asserted — see
  // `browserWriteBack`.
  if (!normalised) return 'generic';
  const changed = changedFields(record.before, record.after, normalised);
  if (!changed) return 'generic';

  const before = geometryOf(record.before);
  const after = geometryOf(record.after);
  const normalisedGeometry = geometryOf(normalised);
  const geometryChanged = (key) => userChanged(before[key], after[key], normalisedGeometry[key]);

  if (changed.has('shape') && shapeChanged(record.before, record.after)) return 'shape';
  if (changed.has('locked')) return record.after && record.after.locked ? 'locked' : 'unlocked';
  if (changed.has('attachment')) {
    const attached = Boolean(record.after && record.after.attachment);
    return attached ? 'attached' : 'detached';
  }
  if (changed.has('geometry') || changed.has('position')) {
    if (geometryChanged('rotation')) return 'rotated';
    if (geometryChanged('w') || geometryChanged('h')) return 'resized';
    if (geometryChanged('x') || geometryChanged('y')) return 'moved';
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
    // (LayoutBusy), or another client holds a live edit lease on the
    // annotation the inverse op would touch (LeaseConflict, task-annotation-
    // exclusive-edit-leases — this used to be a mere selection claim; a lease
    // is acquired only on actual edit-start, never on selection) — the
    // latter clears on release or when the 30 s TTL expires. The 'claimed'
    // classification name is kept (not renamed to 'leased') to avoid an
    // unrelated i18n-key churn across en.json/sv.json for what is still, from
    // the UI's point of view, "someone else is holding this". The message
    // string is pinned from the backend side by
    // backend/core/tests/test_session_manager.py's
    // test_lease_conflict_message_matches_the_ui_classifier, so a wording
    // change there fails CI rather than silently degrading this into the
    // (false, permanent-sounding) conflict text. The LayoutBusy string is not
    // pinned that way — it is a literal in rest_api.py, and a drift there
    // falls back to the conflict message.
    if (err?.message === 'session busy, retry') return 'busy';
    if (
      typeof err?.message === 'string' &&
      err.message.includes('is being edited by another client')
    ) {
      return 'claimed';
    }
    return 'conflict';
  }
  return 'failed';
}
