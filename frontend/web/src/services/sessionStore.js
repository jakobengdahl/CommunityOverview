/**
 * Local session registry backed by localStorage.
 *
 * A "session" is the working surface identified by the visualization session
 * ID (format "DDDD-DDDD"). Each session can have an optional user-given name
 * and a canvas snapshot (nodes, edges, positions, groups, hidden ids) so it
 * can be reloaded later, similar to how chats work in ChatGPT-style apps.
 *
 * Storage layout:
 *   graph_sessions_index          → [{id, name, updatedAt, nodeCount}]
 *   graph_session_snapshot_<id>   → snapshot object
 */

const INDEX_KEY = 'graph_sessions_index';
const SNAPSHOT_PREFIX = 'graph_session_snapshot_';

// Cap the registry so snapshots (which contain full node data) cannot grow
// past localStorage quota. Oldest sessions are evicted first.
const MAX_SESSIONS = 30;

export const SESSION_ID_PATTERN = /^\d{4}-\d{4}$/;

export function isValidSessionId(id) {
  return typeof id === 'string' && SESSION_ID_PATTERN.test(id);
}

function readIndex() {
  try {
    const raw = window.localStorage.getItem(INDEX_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter(e => e && e.id) : [];
  } catch {
    return [];
  }
}

function writeIndex(index) {
  try {
    window.localStorage.setItem(INDEX_KEY, JSON.stringify(index));
  } catch {
    // ignore storage errors — the registry is best-effort
  }
}

function removeSnapshot(id) {
  try {
    window.localStorage.removeItem(SNAPSHOT_PREFIX + id);
  } catch {
    // ignore
  }
}

function evictOldest(index) {
  const sorted = [...index].sort((a, b) => (a.updatedAt || 0) - (b.updatedAt || 0));
  const victim = sorted[0];
  if (!victim) return index;
  removeSnapshot(victim.id);
  return index.filter(e => e.id !== victim.id);
}

/**
 * List known sessions, most recently updated first.
 * @returns {Array<{id: string, name: string|null, updatedAt: number, nodeCount: number}>}
 */
export function listSessions() {
  return readIndex().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

/**
 * Whether the session is present in the registry.
 */
export function hasSession(id) {
  return readIndex().some(e => e.id === id);
}

/**
 * Read a stored canvas snapshot for a session, or null.
 */
export function getSnapshot(id) {
  try {
    const raw = window.localStorage.getItem(SNAPSHOT_PREFIX + id);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function upsertEntry(index, id, patch) {
  const existing = index.find(e => e.id === id);
  if (existing) {
    Object.assign(existing, patch);
    return index;
  }
  return [...index, { id, name: null, updatedAt: Date.now(), nodeCount: 0, ...patch }];
}

// Snapshots larger than this are never worth evicting other sessions for —
// they are unlikely to fit even in an empty localStorage.
const MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024;

/**
 * Persist a canvas snapshot and update the session's index entry.
 * Evicts the oldest sessions when over capacity or when storage quota is hit.
 * If the snapshot ultimately cannot be written, the index entry is rolled
 * back so the drawer never advertises content that isn't in storage.
 *
 * @param {string} id - Session ID
 * @param {Object} snapshot - {nodes, edges, positions, parentIds, groups, hiddenNodeIds, hiddenEdgeIds, savedAt}
 * @returns {boolean} true if the snapshot was written
 */
export function saveSnapshot(id, snapshot) {
  if (!id) return false;
  const prevEntry = readIndex().find(e => e.id === id) || null;
  let index = upsertEntry(readIndex(), id, {
    updatedAt: Date.now(),
    nodeCount: snapshot?.nodes?.length || 0,
  });

  while (index.length > MAX_SESSIONS) {
    index = evictOldest(index);
  }

  const payload = JSON.stringify(snapshot);
  let written = false;
  if (payload.length <= MAX_SNAPSHOT_BYTES) {
    for (;;) {
      try {
        window.localStorage.setItem(SNAPSHOT_PREFIX + id, payload);
        written = true;
        break;
      } catch {
        // Quota exceeded — evict the oldest *other* session and retry.
        const others = index.filter(e => e.id !== id);
        if (others.length === 0) break;
        index = [...evictOldest(others), ...index.filter(e => e.id === id)];
      }
    }
  }

  if (!written) {
    // Roll back to the previous entry (which still describes whatever
    // snapshot remains in storage), or drop the entry entirely.
    index = index.filter(e => e.id !== id);
    if (prevEntry) index.push(prevEntry);
  }
  writeIndex(index);
  return written;
}

/**
 * Register a session in the index without storing a snapshot
 * (used when connecting to a session by ID).
 */
export function touchSession(id) {
  if (!id) return;
  writeIndex(upsertEntry(readIndex(), id, { updatedAt: Date.now() }));
}

/**
 * Give a session a user-visible name (or clear it with an empty value).
 */
export function renameSession(id, name) {
  const index = readIndex();
  const entry = index.find(e => e.id === id);
  if (!entry) return;
  entry.name = name?.trim() || null;
  writeIndex(index);
}
