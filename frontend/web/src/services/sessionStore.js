/**
 * Local recents index for shared sessions, backed by localStorage.
 *
 * Session *data* (node references, layout, annotations) now lives on the
 * server (see `docs/MULTI_USER_SESSIONS_DESIGN.md`, step 4). localStorage keeps
 * only a personal, best-effort list of recently visited sessions so the drawer
 * can offer quick navigation — never any canvas content (decision D6).
 *
 * Storage layout:
 *   graph_sessions_index → [{id, name, updatedAt}]
 *
 * Names in this list are a cached hint; the drawer refreshes them from the
 * server when it lists sessions.
 */

const INDEX_KEY = 'graph_sessions_index';
const LEGACY_SNAPSHOT_PREFIX = 'graph_session_snapshot_';

// Cap the personal recents list so it cannot grow unbounded. Oldest first.
const MAX_SESSIONS = 50;

// Four grouped-digit form DDDD-DDDD-DDDD-DDDD (~10^16 space); the two-group
// legacy form DDDD-DDDD stays valid so previously-shared URLs keep resolving.
export const SESSION_ID_PATTERN = /^\d{4}-\d{4}(?:-\d{4}-\d{4})?$/;

export function isValidSessionId(id) {
  return typeof id === 'string' && SESSION_ID_PATTERN.test(id);
}

function readIndex() {
  try {
    const raw = window.localStorage.getItem(INDEX_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((e) => e && e.id) : [];
  } catch {
    return [];
  }
}

function writeIndex(index) {
  try {
    window.localStorage.setItem(INDEX_KEY, JSON.stringify(index));
  } catch {
    // ignore storage errors — the recents list is best-effort
  }
}

/**
 * Remove legacy per-session snapshot blobs left by the pre-server storage
 * model (decision D10 — no import; the feature was never rolled out). The
 * recents index itself is kept; names refresh from the server. Runs once on
 * module load so upgrades reclaim the space without any user action.
 */
export function purgeLegacySnapshots() {
  try {
    const toRemove = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (key && key.startsWith(LEGACY_SNAPSHOT_PREFIX)) toRemove.push(key);
    }
    toRemove.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // ignore — purging is best-effort cleanup
  }
}

/**
 * List known sessions, most recently updated first.
 * @returns {Array<{id: string, name: string|null, updatedAt: number}>}
 */
export function listSessions() {
  return readIndex().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

/**
 * Whether the session is present in the recents index.
 */
export function hasSession(id) {
  return readIndex().some((e) => e.id === id);
}

function upsertEntry(index, id, patch) {
  const existing = index.find((e) => e.id === id);
  if (existing) {
    Object.assign(existing, patch);
    return index;
  }
  return [...index, { id, name: null, updatedAt: Date.now(), ...patch }];
}

/**
 * Record a visit to a session (create or bump its recents entry).
 * @param {string} id
 * @param {{name?: string|null}} [patch]
 */
export function touchSession(id, patch = {}) {
  if (!id) return;
  let index = upsertEntry(readIndex(), id, { updatedAt: Date.now(), ...patch });
  // Evict the oldest entries beyond the cap.
  while (index.length > MAX_SESSIONS) {
    const victim = [...index].sort((a, b) => (a.updatedAt || 0) - (b.updatedAt || 0))[0];
    if (!victim) break;
    index = index.filter((e) => e.id !== victim.id);
  }
  writeIndex(index);
}

/**
 * Give a session a user-visible name in the recents list (empty clears it).
 * Only updates an entry that already exists — never resurrects a removed one.
 */
export function renameSession(id, name) {
  const index = readIndex();
  const entry = index.find((e) => e.id === id);
  if (!entry) return;
  entry.name = name?.trim() || null;
  writeIndex(index);
}

/**
 * Remove a session from the recents list (e.g. after it is deleted).
 */
export function removeSession(id) {
  writeIndex(readIndex().filter((e) => e.id !== id));
}

purgeLegacySnapshots();
