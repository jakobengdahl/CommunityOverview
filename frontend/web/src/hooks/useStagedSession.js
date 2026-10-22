import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { stagedStatesFromDiff } from '../utils/stagedReview';

export const STAGED_OPS = new Set([
  'graph_change_staged',
  'graph_changes_discarded',
  'graph_changes_merged',
  'write_mode_changed',
]);

const EMPTY_STATES = { nodes: {}, edges: {} };
const REFRESH_DEBOUNCE_MS = 250;

/**
 * The active session's write mode and its staged changes
 * (docs/SESSION_OVERLAY_CONTRACT.md §5, §10, §15, §17).
 *
 * The write mode comes from the server only: a session payload, a create, a
 * `write_mode_changed` op. Learning that a session is staged marks it in
 * api.js at once, so the next graph request already carries the session
 * header. Staged is final (§5.2), so nothing here turns a staged session back
 * into a direct one; only forgetSession (after a delete) unmarks it.
 *
 * The diff, the pending count and the canvas markers all come from
 * GET /api/sessions/{id}/staged, which asks the authorization hook. The
 * session stream's ops are used only as a cue to re-read it: their payloads
 * reach anyone holding the session id, so nothing from them is shown.
 * Everything is scoped to the session it was read for, and dropped on a
 * switch, so one session's staged work never shows in another.
 *
 * @param {string} sessionId - the active session
 */
export function useStagedSession(sessionId) {
  const [modes, setModes] = useState({});
  const [diff, setDiff] = useState({ sessionId: null, items: [], error: null });
  const activeRef = useRef(sessionId);
  activeRef.current = sessionId;
  const modesRef = useRef(modes);
  modesRef.current = modes;
  const timerRef = useRef(null);
  const requestSeqRef = useRef(0);

  // 'unknown' until the server has said how a session that may exist there
  // writes (a deep link before its load lands, or after it failed); graph
  // writes from it are held meanwhile (api.setSessionModeUnknown).
  const writeMode =
    modes[sessionId] === 'staged'
      ? 'staged'
      : modes[sessionId] === 'unknown'
        ? 'unknown'
        : 'direct';

  const adoptWriteMode = useCallback((id, mode) => {
    if (!id) return;
    if (mode === 'staged') {
      api.setSessionStaged(id, true);
      setModes((prev) => (prev[id] === 'staged' ? prev : { ...prev, [id]: 'staged' }));
      return;
    }
    if (mode !== 'direct') return;
    // Staged is final: a direct answer only settles a session not known staged.
    if (modesRef.current[id] === 'staged') return;
    api.setSessionModeDirect(id);
    setModes((prev) => {
      if (!(id in prev) || prev[id] === 'staged') return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  // A session marked staged below the UI (api.js) shows as staged at once.
  useEffect(
    () =>
      api.subscribeStagedMarks((id) =>
        setModes((prev) => (prev[id] === 'staged' ? prev : { ...prev, [id]: 'staged' }))
      ),
    []
  );

  const markModeUnknown = useCallback((id) => {
    if (!id || modesRef.current[id] === 'staged') return;
    api.setSessionModeUnknown(id);
    setModes((prev) => ({ ...prev, [id]: 'unknown' }));
  }, []);

  // A session payload (GET/POST/PATCH /api/sessions...) names its mode; null
  // means the server holds no such session, which is then browser-only and
  // direct (§5.1).
  const adoptSessionPayload = useCallback(
    (id, payload) => {
      if (payload) api.setSessionOnServer(id);
      adoptWriteMode(id, payload ? payload.write_mode : 'direct');
    },
    [adoptWriteMode]
  );

  const forgetSession = useCallback((id) => {
    api.setSessionStaged(id, false);
    setModes((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const refreshDiff = useCallback(async () => {
    const target = activeRef.current;
    if (modesRef.current[target] !== 'staged') return null;
    const seq = ++requestSeqRef.current;
    try {
      const result = await api.getStagedChanges(target);
      if (seq !== requestSeqRef.current || activeRef.current !== target) return null;
      const items = Array.isArray(result?.items) ? result.items : [];
      setDiff({ sessionId: target, items, error: null });
      return items;
    } catch (error) {
      if (seq !== requestSeqRef.current || activeRef.current !== target) return null;
      // Keep no stale items: a refused read must not leave the last
      // session's (or a wider view's) changes on screen.
      setDiff({ sessionId: target, items: [], error });
      return null;
    }
  }, []);

  const scheduleRefresh = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      refreshDiff();
    }, REFRESH_DEBOUNCE_MS);
  }, [refreshDiff]);

  // A switch orphans any read still in flight for the previous session; its
  // diff is already hidden, since `items` below shows only the active one's.
  useEffect(() => {
    requestSeqRef.current += 1;
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [sessionId]);

  // Read the diff whenever the active session is (or becomes) staged.
  useEffect(() => {
    if (writeMode === 'staged') refreshDiff();
  }, [sessionId, writeMode, refreshDiff]);

  /**
   * Route one session op. Returns whether it was a staged-change op.
   * @param {Object} op
   */
  const handleSessionOp = useCallback(
    (op) => {
      if (!op || !STAGED_OPS.has(op.op)) return false;
      if (op.op === 'write_mode_changed') {
        adoptWriteMode(activeRef.current, op.write_mode);
      } else {
        scheduleRefresh();
      }
      return true;
    },
    [adoptWriteMode, scheduleRefresh]
  );

  const items = useMemo(() => (diff.sessionId === sessionId ? diff.items : []), [diff, sessionId]);
  const stagedStates = useMemo(
    () => (items.length ? stagedStatesFromDiff(items) : EMPTY_STATES),
    [items]
  );

  return {
    writeMode,
    items,
    pendingCount: items.length,
    diffError: diff.sessionId === sessionId ? diff.error : null,
    stagedStates,
    adoptWriteMode,
    adoptSessionPayload,
    markModeUnknown,
    forgetSession,
    refreshDiff,
    handleSessionOp,
  };
}
