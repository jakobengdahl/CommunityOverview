import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../services/api';
import { SessionSyncClient } from '../services/sessionSyncClient';

// Realtime sync-client lifecycle (STRUCTURE_REVIEW B1 slice 2), extracted from
// App.jsx as a behaviour-preserving hook. Owns the per-session SessionSyncClient
// (the op-protocol stream + op emission), the connection-scoped state that is
// reset whenever the client is torn down (remote positions/annotations, the
// presence roster, remote selections, op-stream readiness) and the create /
// connect / teardown flow. Op-application handlers stay in App.jsx and are
// routed through `syncHandlersRef` so this long-lived client always calls the
// latest closures.
export function useSyncConnection(sessionId) {
  const syncRef = useRef(null);
  const syncHandlersRef = useRef({});
  const [remotePositions, setRemotePositions] = useState(null);
  // A single MCP-initiated batch layout to animate (contract §9–§10), kept apart
  // from remotePositions so an agent's arrange tweens while ordinary remote drags
  // still apply instantly.
  const [animatedLayout, setAnimatedLayout] = useState(null);
  const [remoteAnnotationOps, setRemoteAnnotationOps] = useState(null);
  // Presence roster + remote selection markers for the active session (step 7).
  const [roster, setRoster] = useState([]);
  const [remoteSelections, setRemoteSelections] = useState({});
  // Remote edit-lease markers (task-annotation-exclusive-edit-leases) — kept
  // apart from remoteSelections above: selection stays a purely cosmetic
  // presence marker, this is the exclusive one GraphCanvas actually gates
  // editing on.
  const [remoteLeases, setRemoteLeases] = useState({});
  // Whether the op-protocol stream has connected at least once for the active
  // session (first snapshot delivered). Once true, MCP commands arrive via the
  // op stream's broadcast `command` events, so the single-consumer legacy push
  // stream is no longer opened for this session (design §3.8, R5).
  const [opStreamReady, setOpStreamReady] = useState(false);

  // Clear everything scoped to a single connected client, so a stale roster /
  // remote positions from the previous session never bleed into the next one.
  const resetConnectionState = useCallback(() => {
    setRemotePositions(null);
    setAnimatedLayout(null);
    setRemoteAnnotationOps(null);
    setRoster([]);
    setRemoteSelections({});
    setRemoteLeases({});
    setOpStreamReady(false);
  }, []);

  // Lazily create + connect the sync client for a session. Called on the first
  // non-empty save and when loading an existing session — never eagerly on load,
  // so an empty never-edited session is never materialised server-side (the
  // step-4 lazy-materialisation behaviour). Handlers delegate through a ref so
  // this long-lived client always runs the latest closures.
  const ensureSyncConnected = useCallback(
    (targetId) => {
      const existing = syncRef.current;
      if (existing && existing.sessionId === targetId) {
        existing.connect();
        return existing;
      }
      if (existing) {
        existing.flush();
        existing.close();
        resetConnectionState();
      }
      let client = null;
      const isCurrentClient = () => syncRef.current === client;
      const callIfCurrent = (handlerName, ...args) => {
        if (!isCurrentClient()) return;
        syncHandlersRef.current[handlerName]?.(...args);
      };
      client = new SessionSyncClient({
        sessionId: targetId,
        clientId: api.getClientId(),
        displayName: api.getDisplayName(),
        streamUrl: api.getSessionStreamUrl(targetId),
        opsUrl: api.getSessionOpsUrl(targetId),
        handlers: {
          onReady: (...a) => {
            if (!isCurrentClient()) return;
            setOpStreamReady(true);
            syncHandlersRef.current.onReady?.(...a);
          },
          onResync: (...a) => callIfCurrent('onResync', ...a),
          onRemoteOps: (...a) => callIfCurrent('onRemoteOps', ...a),
          onPresence: (...a) => callIfCurrent('onPresence', ...a),
          onSelections: (...a) => callIfCurrent('onSelections', ...a),
          onLeases: (...a) => callIfCurrent('onLeases', ...a),
          onSessionRenamed: (...a) => callIfCurrent('onSessionRenamed', ...a),
          onSessionDeleted: (...a) => callIfCurrent('onSessionDeleted', ...a),
          onCommand: (...a) => callIfCurrent('onCommand', ...a),
          onDropped: (...a) => callIfCurrent('onDropped', ...a),
        },
      });
      // Connect before installing: if connect() throws (e.g. new EventSource on a
      // malformed stream URL), a half-connected client must not be left in
      // syncRef.current — the same-session fast path above would otherwise keep
      // retrying that dead client forever, and the un-guarded auto-save call site
      // would throw. On failure return null so callers' optional-chained calls
      // no-op and the next auto-save/load builds a fresh client.
      try {
        client.connect();
      } catch (err) {
        console.error('Error connecting sync client:', err);
        return null;
      }
      syncRef.current = client;
      return client;
    },
    [resetConnectionState]
  );

  // Tear down the client when the session changes or the app unmounts; the next
  // save/load lazily reconnects for the new id.
  useEffect(() => {
    const currentSessionId = sessionId;
    return () => {
      const client = syncRef.current;
      if (client && client.sessionId === currentSessionId) {
        syncRef.current = null;
        // Flush last-moment ops before closing; the POST outlives the stream teardown.
        client.flush();
        client.close();
        resetConnectionState();
      }
    };
  }, [sessionId, resetConnectionState]);

  return {
    syncRef,
    syncHandlersRef,
    ensureSyncConnected,
    remotePositions,
    setRemotePositions,
    animatedLayout,
    setAnimatedLayout,
    remoteAnnotationOps,
    setRemoteAnnotationOps,
    roster,
    setRoster,
    remoteSelections,
    setRemoteSelections,
    remoteLeases,
    setRemoteLeases,
    opStreamReady,
    setOpStreamReady,
  };
}
