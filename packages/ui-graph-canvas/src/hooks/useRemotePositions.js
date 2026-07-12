import { useEffect, useRef } from 'react';

// A remote position may arrive before its node is mounted (the paired
// nodes_added is still awaiting its async node-details fetch when the paired
// node_moved arrives). Such a position is held here, keyed by node id, until the
// node appears — instead of being dropped. Each entry carries the arrival
// timestamp so an entry that is never consumed (its node was removed, or the add
// was rejected and it never mounts) can be pruned instead of leaking for the
// lifetime of the session.
const PENDING_POSITION_TTL_MS = 30_000;

/**
 * Apply node positions arriving from another client (design step 6) to the
 * ReactFlow node list, holding positions for not-yet-mounted nodes until they
 * appear. Owns the pending-positions ref and both effects; behaviour matches the
 * previous inline implementation, with bounded pruning of stale pending entries.
 *
 * Positions are stored and emitted in ReactFlow's own coordinate space
 * (`n.position`) — absolute for a free node, relative to the parent for a grouped
 * node — and the load path restores them the same way, so they are applied
 * directly. (Subtracting a parent offset here would double-count it for grouped
 * nodes and corrupt them.)
 */
export function useRemotePositions({ remotePositions, onRemotePositionsApplied, nodes, setNodes }) {
  const pendingRef = useRef({});

  useEffect(() => {
    if (!remotePositions) return;
    const ids = Object.keys(remotePositions);
    const now = Date.now();
    ids.forEach((id) => {
      const pos = remotePositions[id];
      pendingRef.current[id] = { x: pos.x, y: pos.y, ts: now };
    });
    if (ids.length > 0) {
      setNodes((nds) =>
        nds.map((n) => {
          const pos = pendingRef.current[n.id];
          if (!pos) return n;
          delete pendingRef.current[n.id];
          return { ...n, position: { x: pos.x, y: pos.y } };
        })
      );
    }
    onRemotePositionsApplied?.();
  }, [remotePositions, setNodes, onRemotePositionsApplied]);

  // Catch up a newly-mounted node on a remote position that arrived before it
  // existed. Runs whenever the node list changes, e.g. once nodes_added's async
  // node-details fetch resolves. Also prunes entries that were never consumed
  // within the TTL (their node was removed or never mounted) so the pending map
  // stays bounded. Must return the *same* array reference when nothing matched —
  // this effect depends on `nodes`, so a new reference every run would loop.
  useEffect(() => {
    const pending = pendingRef.current;
    if (Object.keys(pending).length === 0) return;
    const cutoff = Date.now() - PENDING_POSITION_TTL_MS;
    for (const id of Object.keys(pending)) {
      if (pending[id].ts < cutoff) delete pending[id];
    }
    if (Object.keys(pending).length === 0) return;
    setNodes((nds) => {
      let changed = false;
      const next = nds.map((n) => {
        const pos = pending[n.id];
        if (!pos) return n;
        changed = true;
        delete pending[n.id];
        return { ...n, position: { x: pos.x, y: pos.y } };
      });
      return changed ? next : nds;
    });
  }, [nodes, setNodes]);

  return pendingRef;
}
