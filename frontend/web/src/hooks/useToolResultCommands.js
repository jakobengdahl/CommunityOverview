import { useCallback, useEffect, useRef } from 'react';
import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import useGraphStore from '../store/graphStore';

// MCP tool-result "command" application (STRUCTURE_REVIEW B1 slice 4), extracted
// from App.jsx as a behaviour-preserving hook. Owns the bounded recent-command-id
// dedup history, the apply-to-canvas logic shared by both delivery channels, and
// the legacy single-consumer SSE push stream. The op-protocol stream's broadcast
// `command` events stay wired in App.jsx via `syncHandlersRef` (useSyncConnection's
// domain) and route through the `applyToolResultCommand` this hook returns, so an
// AI agent's pushes look identical regardless of which channel delivered them.
export function useToolResultCommands({ sessionId, opStreamReady, latestViewport }) {
  // Bounded recent-command-id history for MCP push dedup (R5) — a small LRU
  // rather than a single last-applied slot, and keyed by the server-assigned
  // command_id rather than payload content, so a later *legitimately* repeated
  // command (e.g. an agent re-adds a node a user just removed) is never
  // mistaken for the same broadcast delivered twice.
  const appliedCommandIdsRef = useRef([]);

  // Apply an MCP tool-result command to the canvas. Shared by the legacy push
  // stream and the op-protocol stream's `command` events (design §3.8, R5) so
  // an AI agent's pushes look identical regardless of which channel delivered
  // them. Deduped by command_id against recently applied ids: during the
  // handover from the legacy stream to the op stream (or a brief overlap
  // window) the same MCP push can be broadcast on both.
  const applyToolResultCommand = useCallback(
    (toolResult, commandId) => {
      if (!toolResult) return;
      if (commandId) {
        if (appliedCommandIdsRef.current.includes(commandId)) return;
        appliedCommandIdsRef.current.push(commandId);
        if (appliedCommandIdsRef.current.length > 20) appliedCommandIdsRef.current.shift();
      }

      const {
        nodes: currentNodes,
        edges: currentEdges,
        addNodesToVisualization: addNodes,
        updateVisualization: updateViz,
        clearVisualization: clearViz,
      } = useGraphStore.getState();

      const filtered = (toolResult.nodes || []).filter(
        (n) => n.type !== 'Community' && n.data?.type !== 'Community'
      );

      if (toolResult.action === 'add_to_visualization') {
        if (filtered.length > 0) {
          const allEdges = [...currentEdges, ...(toolResult.edges || [])];
          const vp = latestViewport.current;
          const viewportCenter = vp
            ? {
                x: (window.innerWidth / 2 - vp.x) / vp.zoom,
                y: (window.innerHeight / 2 - vp.y) / vp.zoom,
              }
            : null;
          const positioned = positionNewNodes(filtered, currentNodes, allEdges, { viewportCenter });
          addNodes(positioned, toolResult.edges || []);
        }
      } else if (
        toolResult.action === 'load_visualization' ||
        toolResult.action === 'clear_visualization'
      ) {
        clearViz();
        if (filtered.length > 0) {
          updateViz(filtered, toolResult.edges || []);
        }
      } else if (filtered.length > 0) {
        updateViz(filtered, toolResult.edges || []);
      }
    },
    [latestViewport]
  );

  // Apply a `node_pulse` command (external pulse-trigger URLs): play a transient
  // visual pulse on the targeted node. Shares the same command-id dedup as
  // tool-result commands so a pulse that arrives on both the legacy stream and
  // the op stream during handover only fires once.
  const applyPulseCommand = useCallback((command, commandId) => {
    if (!command?.node_id) return;
    if (commandId) {
      if (appliedCommandIdsRef.current.includes(commandId)) return;
      appliedCommandIdsRef.current.push(commandId);
      if (appliedCommandIdsRef.current.length > 20) appliedCommandIdsRef.current.shift();
    }
    const pulse = command.pulse || {};
    useGraphStore.getState().pulseNode(command.node_id, {
      style: pulse.style,
      color: pulse.color,
      durationMs: pulse.duration_ms,
    });
  }, []);

  // ── Visualization session: legacy SSE connection ────────────────────────
  // Opens the single-consumer push stream so external AI clients can push
  // visualization commands to this browser window via MCP, until the
  // op-protocol stream takes over. Once the op stream has connected for this
  // session (`opStreamReady`), its broadcast `command` events replace this
  // channel — reaching every collaborator, not just whichever browser wins the
  // legacy stream's single queue (design §3.8, R5) — so this stream is closed
  // and not reopened while that holds.
  useEffect(() => {
    if (opStreamReady) return undefined;
    const evtSource = new EventSource(api.getVisualizationStreamUrl(sessionId));
    evtSource.onmessage = (e) => {
      try {
        const cmd = JSON.parse(e.data);
        if (cmd.type === 'ping' || cmd.type === 'connected') return;
        if (cmd.type === 'node_pulse') {
          applyPulseCommand(cmd, cmd.command_id);
          return;
        }
        if (cmd.type !== 'tool_result' || !cmd.result) return;
        applyToolResultCommand(cmd.result, cmd.command_id);
      } catch (err) {
        console.error('[Session SSE] parse error:', err);
      }
    };
    evtSource.onerror = () => {
      // Browser auto-reconnects on SSE errors; no manual retry needed.
    };
    return () => evtSource.close();
  }, [sessionId, opStreamReady, applyToolResultCommand, applyPulseCommand]);

  return { applyToolResultCommand, applyPulseCommand };
}
