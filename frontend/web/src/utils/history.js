/**
 * Helpers for the read-only graph history / audit views.
 *
 * These are pure functions (no React, no i18n) so they can be unit-tested and
 * reused by both the global "Recent activity" drawer and the per-entity
 * history views. Display strings are resolved by the callers via i18n keys.
 */

/** Recognised mutation event types, in the order the backend emits them. */
export const HISTORY_EVENT_TYPES = [
  'node.create',
  'node.update',
  'node.delete',
  'edge.create',
  'edge.update',
  'edge.delete',
];

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * Describe how long ago an ISO timestamp occurred, as an i18n key + count.
 * The caller renders it with t(key, { count }). Kept data-only so the
 * unit-selection logic is testable without a translation layer.
 *
 * @param {string} iso - ISO-8601 timestamp (e.g. "2026-07-11T10:00:00Z")
 * @param {number} [nowMs] - Reference "now" in ms (defaults to Date.now())
 * @returns {{ key: string, count: number }}
 */
export function relativeTime(iso, nowMs = Date.now()) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return { key: 'history.time.unknown', count: 0 };
  }
  const diffSec = Math.max(0, Math.floor((nowMs - then) / 1000));

  if (diffSec < 45) return { key: 'history.time.just_now', count: diffSec };
  if (diffSec < HOUR) {
    return { key: 'history.time.minutes_ago', count: Math.round(diffSec / MINUTE) };
  }
  if (diffSec < DAY) {
    return { key: 'history.time.hours_ago', count: Math.round(diffSec / HOUR) };
  }
  return { key: 'history.time.days_ago', count: Math.round(diffSec / DAY) };
}

/**
 * Format an ISO timestamp as an absolute, locale-aware date-time string.
 * Falls back to the raw string when it cannot be parsed.
 *
 * @param {string} iso
 * @param {string} [locale]
 * @returns {string}
 */
export function absoluteTime(iso, locale = undefined) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso || '';
  try {
    return new Date(ms).toLocaleString(locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return new Date(ms).toISOString();
  }
}

/**
 * Best-effort human name for the entity a history entry refers to.
 * Uses the after-state for creates/updates and the before-state for deletes,
 * falling back to the entity id.
 *
 * @param {Object} entry - A history record
 * @returns {string}
 */
export function entityName(entry) {
  const state = entry?.after || entry?.before || {};
  return state.name || state.label || entry?.entity_id || '';
}

function valuesEqual(a, b) {
  if (a === b) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

/**
 * Compute a compact list of field-level changes for an update entry.
 *
 * The backend supplies a `patch` map ({ changedField: afterValue }) for node
 * updates; before-values are read from the `before` snapshot. Edge updates
 * carry no patch, so we fall back to diffing the before/after snapshots.
 *
 * @param {Object} entry - A history record
 * @returns {Array<{ field: string, before: *, after: * }>}
 */
export function computeDiff(entry) {
  if (!entry) return [];
  const before = entry.before || {};

  if (entry.patch && typeof entry.patch === 'object' && Object.keys(entry.patch).length > 0) {
    return Object.entries(entry.patch).map(([field, after]) => ({
      field,
      before: before[field],
      after,
    }));
  }

  if (entry.before && entry.after) {
    const after = entry.after;
    const fields = new Set([...Object.keys(before), ...Object.keys(after)]);
    const changes = [];
    for (const field of fields) {
      if (!valuesEqual(before[field], after[field])) {
        changes.push({ field, before: before[field], after: after[field] });
      }
    }
    return changes;
  }

  return [];
}

/**
 * Render a single field value as a short display string. Objects/arrays are
 * JSON-encoded; null/undefined become an em dash so an empty value is visible.
 *
 * @param {*} value
 * @returns {string}
 */
export function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(formatValue).join(', ');
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** Whether an entry represents an update (i.e. a diff may be meaningful). */
export function isUpdate(entry) {
  return typeof entry?.event_type === 'string' && entry.event_type.endsWith('.update');
}
