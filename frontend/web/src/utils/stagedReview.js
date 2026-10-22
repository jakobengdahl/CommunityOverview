/**
 * Pure helpers for reviewing, merging and discarding a staged session's
 * changes (docs/SESSION_OVERLAY_CONTRACT.md §10–§12). The diff items are the
 * ones GET /api/sessions/{id}/staged answers; nothing here talks to the
 * server, so the rules the review dialog follows can be tested on their own.
 */

export const STATUS_CLEAN = 'clean';

/** A stable key for one layer entry. */
export function entryKey(kind, entityId) {
  return `${kind}:${entityId}`;
}

export function itemKey(item) {
  return entryKey(item.kind, item.entity_id);
}

function keyOfRef(ref) {
  return entryKey(ref.kind, ref.entity_id);
}

/**
 * How each staged entity shows on the canvas, from the diff: `created`,
 * `updated`, `orphaned` (an update of an entity the graph no longer holds,
 * §8.1) or `deleted` (a tombstone).
 * @param {Array} items
 * @returns {{nodes: Object<string, string>, edges: Object<string, string>}}
 */
export function stagedStatesFromDiff(items) {
  const out = { nodes: {}, edges: {} };
  for (const item of items || []) {
    let state;
    if (item.action === 'create') state = 'created';
    else if (item.action === 'delete') state = 'deleted';
    else state = item.status === 'orphaned' ? 'orphaned' : 'updated';
    out[item.kind === 'edge' ? 'edges' : 'nodes'][item.entity_id] = state;
  }
  return out;
}

/**
 * `selected` grown by every entry it depends on, transitively (§11.4), so a
 * selection the user ticks is always one the server accepts.
 * @param {Array} items
 * @param {Iterable<string>} selected - entry keys
 * @returns {Set<string>}
 */
export function dependencyClosure(items, selected) {
  const byKey = new Map((items || []).map((item) => [itemKey(item), item]));
  const out = new Set();
  const stack = [...selected];
  while (stack.length) {
    const key = stack.pop();
    if (out.has(key)) continue;
    out.add(key);
    for (const dep of byKey.get(key)?.depends_on || []) stack.push(keyOfRef(dep));
  }
  return out;
}

/**
 * The entries that depend on `key`, transitively: unticking `key` must untick
 * them too, or the selection stops being closed.
 * @param {Array} items
 * @param {string} key
 * @returns {Set<string>}
 */
export function dependants(items, key) {
  const out = new Set();
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of items || []) {
      const k = itemKey(item);
      if (out.has(k)) continue;
      if ((item.depends_on || []).some((d) => keyOfRef(d) === key || out.has(keyOfRef(d)))) {
        out.add(k);
        grew = true;
      }
    }
  }
  return out;
}

/** Field names of an item whose values conflict with the main graph. */
export function conflictingFields(item) {
  return Object.entries(item?.fields || {})
    .filter(([, diff]) => diff && diff.conflict)
    .map(([name]) => name);
}

/**
 * Whether a dangling edge is the dependant of a node create the user drops
 * with keep_main: only then may it be kept, attached to the graph's node that
 * holds the id (§11.2).
 */
export function heldByDroppedCreate(item, items, resolutions) {
  const creates = new Set(
    (items || [])
      .filter((i) => i.kind === 'node' && i.action === 'create')
      .map(itemKey)
      .filter((key) => resolutions?.[key]?.choice === 'keep_main')
  );
  return (item?.depends_on || []).some((dep) => creates.has(keyOfRef(dep)));
}

/**
 * The resolutions §11.2 allows for an item, in the order the dialog offers
 * them. A clean item needs none. A dangling one can only be dropped, unless
 * it depends on a create dropped with keep_main (`heldByDropped`).
 * @param {Object} item
 * @param {boolean} [heldByDropped]
 * @returns {string[]}
 */
export function allowedChoices(item, heldByDropped = false) {
  if (!item || item.status === STATUS_CLEAN) return [];
  if (item.status === 'dangling')
    return heldByDropped ? ['take_session', 'keep_main'] : ['keep_main'];
  const choices = ['take_session', 'keep_main'];
  const nodeNotDelete = item.kind === 'node' && item.action !== 'delete';
  if (nodeNotDelete && ['conflict', 'orphaned', 'id_taken'].includes(item.status)) {
    choices.push('as_new');
  }
  if (item.status === 'conflict' && item.action === 'update') choices.push('manual');
  return choices;
}

/**
 * Whether a resolution covers an item: a non-clean item needs one of its
 * allowed choices, and `manual` a final value for every conflicting field.
 */
export function isResolved(item, resolution, heldByDropped = false) {
  if (!item || item.status === STATUS_CLEAN) return true;
  const choice = resolution?.choice;
  if (!allowedChoices(item, heldByDropped).includes(choice)) return false;
  if (choice !== 'manual') return true;
  const values = resolution.values || {};
  return conflictingFields(item).every((field) => Object.hasOwn(values, field));
}

/**
 * The selected items that still need a resolution before a merge can go.
 * @param {Array} items
 * @param {Set<string>} selected
 * @param {Object<string, Object>} resolutions - by entry key
 * @returns {string[]} entry keys
 */
export function unresolvedKeys(items, selected, resolutions) {
  return (items || [])
    .filter((item) => selected.has(itemKey(item)))
    .filter(
      (item) =>
        !isResolved(item, resolutions[itemKey(item)], heldByDroppedCreate(item, items, resolutions))
    )
    .map(itemKey);
}

/**
 * The merge request body, without its merge id. The selection is always the
 * explicit list of entries the user reviewed, never `all`: an entry staged
 * after the review was read must not ride along unseen.
 * @param {Array} items
 * @param {Set<string>} selected
 * @param {Object<string, Object>} resolutions
 */
export function buildMergeRequest(items, selected, resolutions) {
  const chosen = (items || []).filter((item) => selected.has(itemKey(item)));
  const entries = chosen.map((item) => ({ kind: item.kind, entity_id: item.entity_id }));
  const resolved = [];
  for (const item of chosen) {
    const resolution = resolutions[itemKey(item)];
    if (!resolution?.choice || item.status === STATUS_CLEAN) continue;
    const out = { kind: item.kind, entity_id: item.entity_id, choice: resolution.choice };
    if (resolution.choice === 'manual') {
      const fields = conflictingFields(item);
      out.values = Object.fromEntries(fields.map((f) => [f, resolution.values?.[f]]));
    }
    resolved.push(out);
  }
  const request = { entries };
  if (resolved.length) request.resolutions = resolved;
  return request;
}

/**
 * What a user has seen of an entry: everything the review shows that decides
 * what merging it does. Two reads with the same fingerprint show the same.
 */
export function itemFingerprint(item) {
  return JSON.stringify([
    item.action,
    item.status,
    item.fields,
    item.depends_on,
    item.unstaged_edges || null,
    item.session_seq,
  ]);
}

/**
 * The entries whose fresh read no longer matches the fingerprint taken when
 * the user ticked them: restaged, merged or discarded meanwhile, or judged
 * differently against the main graph. A merge or discard is sent only when
 * this is empty. (The request cannot pin the version itself, so a restage
 * after this read still lands; see the dialog.)
 * @param {Map<string, string>} prints - entry key -> fingerprint when ticked
 * @param {Array} fresh
 * @returns {string[]} entry keys
 */
export function reviewDrift(prints, fresh) {
  const freshByKey = new Map((fresh || []).map((item) => [itemKey(item), item]));
  const drift = [];
  for (const [key, print] of prints) {
    const now = freshByKey.get(key);
    if (!now || itemFingerprint(now) !== print) drift.push(key);
  }
  return drift;
}

/**
 * `items` with the statuses a merge_blocked answer gave: some (a dangling
 * dependant of a dropped create) exist only within that merge, so the plain
 * diff shows them clean and would offer no resolution.
 * @param {Array} items
 * @param {Map<string, string>} statuses - entry key -> status
 */
export function withStatuses(items, statuses) {
  if (!statuses?.size) return items;
  return (items || []).map((item) => {
    const status = statuses.get(itemKey(item));
    return status ? { ...item, status } : item;
  });
}

/**
 * Whether a failed merge may have landed. Then a retry must reuse its merge id
 * so the server replays the first result instead of merging twice (I9); any
 * refusal the server answered with a code wrote nothing.
 * @param {Error} error
 */
export function mergeOutcomeUnknown(error) {
  if (!error || typeof error.status !== 'number') return true; // network failure
  return error.status >= 500;
}

/**
 * The attempt a merge request goes out as. A merge id is reused only to
 * retry the very same request whose outcome is unknown; any other request,
 * or one after a definite answer, takes a fresh id. Reusing an id for a
 * different request would make the server replay the first one's result.
 * @param {{mergeId: string, fingerprint: string, outcomeUnknown: boolean}|null} previous
 * @param {Object} request - the body without merge id
 * @param {() => string} newId
 */
export function nextMergeAttempt(previous, request, newId) {
  const fingerprint = JSON.stringify(request);
  if (previous && previous.outcomeUnknown && previous.fingerprint === fingerprint) {
    return previous;
  }
  return { mergeId: newId(), fingerprint, outcomeUnknown: false };
}

/**
 * A display name for an item: its name as staged, else as main or base holds
 * it, else the name of the entity on the canvas, else its id.
 * @param {Object} item
 * @param {(kind: string, id: string) => string|null} [canvasName]
 */
export function itemLabel(item, canvasName) {
  const name = item?.fields?.name;
  const fromDiff = name ? (name.staged ?? name.main ?? name.base) : null;
  if (typeof fromDiff === 'string' && fromDiff) return fromDiff;
  return canvasName?.(item.kind, item.entity_id) || item.entity_id;
}

/**
 * Parse what the user typed as a manual value: text for a field whose
 * staged value is text, JSON otherwise.
 * @returns {{ok: boolean, value?: any}}
 */
export function parseManualValue(text, stagedValue) {
  if (typeof stagedValue === 'string' || stagedValue === undefined) {
    return { ok: true, value: text };
  }
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}

// Refusals the staged-session routes answer with a code the UI can name in
// the user's language; any other keeps the server's (English) message.
export const KNOWN_REFUSALS = [
  'invalid_merge_request',
  'merge_id_taken',
  'session_not_staged',
  'session_not_found',
  'session_not_supported',
  'session_mismatch',
  'staged_unsupported',
  'layer_full',
  'layer_not_empty',
];

/**
 * The text to show for a failed staged-session request.
 * @param {(key: string, params?: Object) => string} t
 * @param {Error & {code?: string}} error
 * @param {string} fallbackKey - shown when the error carries no message
 */
export function refusalText(t, error, fallbackKey) {
  if (KNOWN_REFUSALS.includes(error?.code)) return t(`staged.refusal.${error.code}`);
  return error?.message || t(fallbackKey);
}

/** Render a field value for display. */
export function formatValue(value) {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}
