import { useCallback } from 'react';
import * as api from '../services/api';
import {
  annotationsToGroups,
  groupsToAnnotations,
  annotationsToOverlays,
  overlaysToAnnotations,
} from '../utils/sessionAnnotations';

// Convert a server session state into the exact baseline shape the local
// snapshot round-trip produces, so the sync client's diff sees no change for
// content it just applied (echo-safety). node_refs come from the *resolved*
// node ids actually on the canvas — refs that no longer resolve in the graph
// must not look like local removals on the next snapshot.
export function serverStateToMirror(state, resolvedNodeIds) {
  const s = state || {};
  const { groups, parentIds } = annotationsToGroups(s.annotations);
  const overlays = annotationsToOverlays(s.annotations);
  const intensity = Number(s.edge_intensity);
  return {
    node_refs: resolvedNodeIds || s.node_refs || [],
    positions: s.positions || {},
    hidden_node_ids: s.hidden_node_ids || [],
    hidden_edge_ids: s.hidden_edge_ids || [],
    dimmed_node_ids: s.dimmed_node_ids || [],
    dimmed_edge_ids: s.dimmed_edge_ids || [],
    edge_intensity: Number.isFinite(intensity) ? Math.max(0, Math.min(1, intensity)) : 1.0,
    annotations: [...groupsToAnnotations(groups, parentIds), ...overlaysToAnnotations(overlays)],
  };
}

// Normalize a resolved node/edge list before it reaches a mutating call:
// nullish → [], a proper array → as-is, and a *truthy non-array* (a malformed
// server payload) → throw. serverStateToMirror already fails on malformed
// annotations before applyServerSession runs, but it never inspects
// resolved.edges/nodes; without this guard a bad edge list would only blow up
// inside addNodesToVisualization — after clearVisualization() — leaving the
// canvas half-cleared while the switch is reported as failed. Throwing here
// keeps the failure atomic (canvas untouched), mirroring the annotations path.
function assertArrayField(value, field) {
  if (value == null) return [];
  if (!Array.isArray(value)) {
    throw new Error(`Malformed session payload: resolved.${field} is not an array`);
  }
  return value;
}

// Shared-session lifecycle: load a server-backed session's canvas content and
// seed the realtime sync baseline. Extracted from App.jsx (STRUCTURE_REVIEW B1
// slice 1) as a behaviour-preserving hook. Dependencies (store actions, the
// sync-client accessors) are injected so the logic stays testable in isolation.
export function useSharedSession({
  clearVisualization,
  addNodesToVisualization,
  setHiddenNodeIds,
  setHiddenEdgeIds,
  setDimmedNodeIds,
  setDimmedEdgeIds,
  setEdgeIntensity,
  setPendingGroups,
  setPendingAnnotations,
  ensureSyncConnected,
  syncRef,
  // Reset session-scoped UI state (assistant history, experts, node overlays,
  // selection) once a *different* session's content is loaded. Injected so the
  // hook stays UI-agnostic; optional so callers that don't need it can omit it.
  // Deliberately NOT invoked from applyServerSession — that path is reused by
  // same-session resync, which must keep the current session's assistant state.
  resetSessionScopedState,
}) {
  // Load a session's canvas content from the server (resolved node refs +
  // layout + group annotations) onto the store.
  const applyServerSession = useCallback(
    (payload) => {
      const state = payload?.state || {};
      const resolved = payload?.resolved || {};
      // Validate the resolved shape before the first mutating call so a malformed
      // payload fails atomically (see assertArrayField).
      const loadedNodes = assertArrayField(resolved.nodes, 'nodes');
      const resolvedEdges = assertArrayField(resolved.edges, 'edges');
      clearVisualization();
      if (loadedNodes.length) {
        const positioned = loadedNodes.map((n) =>
          state.positions?.[n.id] ? { ...n, _savedPosition: state.positions[n.id] } : n
        );
        addNodesToVisualization(positioned, resolvedEdges);
      }
      if (state.hidden_node_ids?.length) setHiddenNodeIds(state.hidden_node_ids);
      if (state.hidden_edge_ids?.length) setHiddenEdgeIds(state.hidden_edge_ids);
      if (state.dimmed_node_ids?.length) setDimmedNodeIds(state.dimmed_node_ids);
      if (state.dimmed_edge_ids?.length) setDimmedEdgeIds(state.dimmed_edge_ids);
      if (typeof state.edge_intensity === 'number') setEdgeIntensity(state.edge_intensity);
      const { groups, parentIds } = annotationsToGroups(state.annotations);
      if (groups.length) setPendingGroups({ groups, parentIds });
      const overlays = annotationsToOverlays(state.annotations);
      if (overlays.length) setPendingAnnotations(overlays);
    },
    [
      clearVisualization,
      addNodesToVisualization,
      setHiddenNodeIds,
      setHiddenEdgeIds,
      setDimmedNodeIds,
      setDimmedEdgeIds,
      setEdgeIntensity,
      setPendingGroups,
      setPendingAnnotations,
    ]
  );

  const loadSessionFromServer = useCallback(
    async (targetId, { eagerConnect = false, onMissing = null } = {}) => {
      try {
        const payload = await api.getSession(targetId, { resolve: true });
        // Compute the sync baseline before touching the canvas: it runs the same
        // annotation-transform logic applyServerSession does internally, and
        // applyServerSession itself validates the resolved shape before its first
        // mutating call, so if malformed server data would throw, it throws here —
        // before clearVisualization() — leaving the current canvas untouched and
        // this as a clean "switch failed" rather than a half-applied one.
        const resolvedIds = (payload?.resolved?.nodes || []).map((n) => n.id);
        const baselineMirror = serverStateToMirror(payload?.state, resolvedIds);
        applyServerSession(payload);
        // The target session's canvas is now loaded — drop any UI state carried
        // over from the previous session before wiring up its realtime stream.
        resetSessionScopedState?.();
        // Connect the realtime stream for this existing session and seed the sync
        // baseline from its state so later edits diff against what the server holds.
        // Best-effort from here on: the canvas above already loaded correctly, so a
        // sync-connect failure must not be reported as a failed switch.
        try {
          ensureSyncConnected(targetId)?.setBaseline(baselineMirror);
        } catch (syncError) {
          console.error('Error connecting sync client:', syncError);
        }
      } catch (error) {
        // Session does not exist server-side yet — new / not-yet-saved share URL.
        if (error?.status === 404) {
          clearVisualization();
          // Switching into a brand-new / empty session is still a session switch:
          // reset the carried-over UI state just as the loaded-content path does.
          resetSessionScopedState?.();
          if (eagerConnect) {
            ensureSyncConnected(targetId)?.setBaseline({});
          } else if (syncRef.current && syncRef.current.sessionId === targetId) {
            syncRef.current.setBaseline({});
          }
          // A 404 is ambiguous: a brand-new session materialises on first save,
          // but a *deep link* to a session that was deleted or never existed
          // should not silently present an empty canvas as if it were valid
          // (contract §5.3). The caller opts in to that notice via onMissing;
          // the empty-session fallback above still runs so nothing breaks.
          onMissing?.(targetId);
        } else {
          console.error('Error loading session:', error);
          throw error;
        }
      }
    },
    [applyServerSession, clearVisualization, ensureSyncConnected, syncRef, resetSessionScopedState]
  );

  return { applyServerSession, loadSessionFromServer };
}
