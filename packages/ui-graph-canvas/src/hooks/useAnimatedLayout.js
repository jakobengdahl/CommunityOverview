import { useCallback, useEffect, useRef } from 'react';

// Defaults mirror the MCP layout contract (§9): a write that omits animation
// fields still arrives with these, and a malformed hint falls back to them.
const DEFAULT_DURATION_MS = 400;
// Cap the tween so a hostile or fat-fingered `duration_ms` cannot freeze the
// canvas mid-motion for minutes; beyond this the move is effectively a snap from
// the user's point of view anyway.
const MAX_DURATION_MS = 4000;

const EASINGS = {
  linear: (t) => t,
  'ease-in': (t) => t * t,
  'ease-out': (t) => t * (2 - t),
  'ease-in-out': (t) => (t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t),
};

// prefers-reduced-motion is a *client* decision (contract §9): when the viewer
// asked for reduced motion the canvas snaps to the final positions regardless of
// the agent's `animate` hint. Guarded for non-browser (test) environments.
function prefersReducedMotion() {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

function now() {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

function clampDuration(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.min(n, MAX_DURATION_MS);
}

/**
 * Tween MCP-initiated batch layouts (`layout_applied` ops, contract §9–§10) from
 * each node's current position to the agent's target, so a correlated re-layout
 * reads as one coherent motion instead of node-by-node jumps.
 *
 * `animatedLayout` is a per-op command object `{ positions, animation, seq }`
 * where `positions` is the normalised **absolute** target map from the broadcast
 * and `animation` is the carried hint `{ animate, duration_ms, easing }`.
 *
 * Supersede is **per node** (contract §9: a later op supersedes an in-flight
 * transition *for the same nodes*). Each incoming batch is folded into a single
 * long-lived tween keyed by node id: a node named again restarts from its current
 * position toward the new target, while nodes from an earlier batch that this one
 * does not mention keep animating to their own targets. This matters because the
 * documented workflow splits a large arrange across successive animated writes —
 * replacing the whole tween would strand the earlier, disjoint nodes.
 *
 * Positions are applied in ReactFlow's own coordinate space (`n.position`),
 * exactly as `useRemotePositions` does, so grouped/child nodes are not double
 * offset. A node the user is dragging (`n.dragging`) is left untouched every
 * frame, so an agent layout never fights a concurrent drag.
 */
export function useAnimatedLayout({
  animatedLayout,
  onAnimatedLayoutApplied,
  onAgentArrangingChange,
  setNodes,
  getNodes,
}) {
  const rafRef = useRef(null);
  // nodeId -> { sx, sy, tx, ty, start, duration, ease }. Persists across commands
  // so overlapping batches merge instead of cancelling one another.
  const activeRef = useRef(new Map());
  // Callbacks reach the frame loop through refs so a parent re-render (a new
  // inline callback identity) never re-triggers the ingest effect.
  const appliedRef = useRef(onAnimatedLayoutApplied);
  const arrangingRef = useRef(onAgentArrangingChange);
  appliedRef.current = onAnimatedLayoutApplied;
  arrangingRef.current = onAgentArrangingChange;

  const step = useCallback(() => {
    const active = activeRef.current;
    const t = now();
    setNodes((nds) =>
      nds.map((n) => {
        const a = active.get(n.id);
        if (!a || n.dragging) return n;
        const raw = a.duration === 0 ? 1 : Math.min(1, (t - a.start) / a.duration);
        const k = a.ease(raw);
        return {
          ...n,
          position: { x: a.sx + (a.tx - a.sx) * k, y: a.sy + (a.ty - a.sy) * k },
        };
      })
    );
    for (const [id, a] of active) {
      const raw = a.duration === 0 ? 1 : Math.min(1, (t - a.start) / a.duration);
      if (raw >= 1) active.delete(id);
    }
    if (active.size > 0) {
      rafRef.current = requestAnimationFrame(step);
    } else {
      rafRef.current = null;
      arrangingRef.current?.(false);
    }
  }, [setNodes]);

  // Cancel the frame loop on unmount (a superseded node is handled by merging,
  // not by tearing the loop down).
  useEffect(
    () => () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    },
    []
  );

  useEffect(() => {
    if (!animatedLayout) return;
    const { positions, animation } = animatedLayout;
    const targets = positions || {};
    const ids = Object.keys(targets);
    if (ids.length === 0) {
      appliedRef.current?.();
      return;
    }

    const wantsAnimation = !!(animation && animation.animate === true);
    const duration = wantsAnimation
      ? clampDuration(animation.duration_ms ?? DEFAULT_DURATION_MS)
      : 0;

    if (!wantsAnimation || duration === 0 || prefersReducedMotion()) {
      // Snap: drop any in-flight tween for these nodes and jump to the target,
      // never disturbing a node the user is dragging.
      ids.forEach((id) => activeRef.current.delete(id));
      setNodes((nds) =>
        nds.map((n) => {
          const target = targets[n.id];
          if (!target || n.dragging) return n;
          return { ...n, position: { x: target.x, y: target.y } };
        })
      );
      if (activeRef.current.size === 0) arrangingRef.current?.(false);
      appliedRef.current?.();
      return;
    }

    // Fold this batch into the running tween: each named node restarts from its
    // current live position toward the new target (per-node supersede).
    const live = typeof getNodes === 'function' ? getNodes() : [];
    const livePos = new Map(live.map((n) => [n.id, n.position]));
    const ease = EASINGS[animation.easing] || EASINGS['ease-in-out'];
    const startTime = now();
    ids.forEach((id) => {
      const start = livePos.get(id) || targets[id];
      activeRef.current.set(id, {
        sx: start.x,
        sy: start.y,
        tx: targets[id].x,
        ty: targets[id].y,
        start: startTime,
        duration,
        ease,
      });
    });
    arrangingRef.current?.(true);
    if (rafRef.current == null) rafRef.current = requestAnimationFrame(step);
    // The command is consumed the moment its targets are registered; the tween
    // now lives in the ref, so the parent can clear the channel for the next op.
    appliedRef.current?.();
  }, [animatedLayout, setNodes, getNodes, step]);
}
