import { useEffect, useRef } from 'react';

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

function clampDuration(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.min(n, MAX_DURATION_MS);
}

/**
 * Tween an MCP-initiated batch layout (`layout_applied` op, contract §9–§10) from
 * each node's current position to the agent's target, so a correlated re-layout
 * reads as one coherent motion instead of node-by-node jumps.
 *
 * `animatedLayout` is a per-op command object `{ positions, animation, seq }`
 * where `positions` is the normalised **absolute** target map from the broadcast
 * and `animation` is the carried hint `{ animate, duration_ms, easing }`. A new
 * command supersedes any in-flight tween: the effect's cleanup cancels the frame
 * loop and the next run re-starts from wherever the nodes currently are
 * (contract §9 cancellation/replacement).
 *
 * Positions are applied in ReactFlow's own coordinate space (`n.position`),
 * exactly as `useRemotePositions` does, so grouped/child nodes are not double
 * offset. Nodes the user is actively dragging (`n.dragging`) are left untouched
 * every frame, so an agent layout never fights a concurrent drag.
 */
export function useAnimatedLayout({
  animatedLayout,
  onAnimatedLayoutApplied,
  onAgentArrangingChange,
  setNodes,
  getNodes,
}) {
  const rafRef = useRef(null);
  // Callbacks reach the frame loop through refs so a parent re-render (a new
  // inline callback identity) never re-triggers the effect and restarts an
  // in-flight tween. The effect depends only on the command object.
  const appliedRef = useRef(onAnimatedLayoutApplied);
  const arrangingRef = useRef(onAgentArrangingChange);
  appliedRef.current = onAnimatedLayoutApplied;
  arrangingRef.current = onAgentArrangingChange;

  useEffect(() => {
    if (!animatedLayout) return undefined;
    const { positions, animation } = animatedLayout;
    const targets = positions || {};
    const ids = Object.keys(targets);
    if (ids.length === 0) {
      appliedRef.current?.();
      return undefined;
    }

    // Supersede any tween still running for the previous command.
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }

    // Snap the moved nodes to their targets, never disturbing a dragged node.
    const snap = () => {
      setNodes((nds) =>
        nds.map((n) => {
          const t = targets[n.id];
          if (!t || n.dragging) return n;
          return { ...n, position: { x: t.x, y: t.y } };
        })
      );
    };

    const wantsAnimation = !!(animation && animation.animate === true);
    const duration = wantsAnimation
      ? clampDuration(animation.duration_ms ?? DEFAULT_DURATION_MS)
      : 0;

    if (!wantsAnimation || duration === 0 || prefersReducedMotion()) {
      snap();
      appliedRef.current?.();
      return undefined;
    }

    const ease = EASINGS[animation.easing] || EASINGS['ease-in-out'];
    // Capture start positions once from the live nodes; a target with no live
    // start (node not yet mounted) begins at its target, i.e. no visible move.
    const starts = {};
    const live = typeof getNodes === 'function' ? getNodes() : [];
    live.forEach((n) => {
      if (targets[n.id] && n.position) starts[n.id] = { x: n.position.x, y: n.position.y };
    });

    const startTime = typeof performance !== 'undefined' ? performance.now() : Date.now();
    arrangingRef.current?.(true);

    const step = () => {
      const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
      const raw = Math.min(1, (now - startTime) / duration);
      const k = ease(raw);
      setNodes((nds) =>
        nds.map((n) => {
          const target = targets[n.id];
          if (!target || n.dragging) return n;
          const s = starts[n.id] || target;
          return {
            ...n,
            position: { x: s.x + (target.x - s.x) * k, y: s.y + (target.y - s.y) * k },
          };
        })
      );
      if (raw < 1) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        rafRef.current = null;
        arrangingRef.current?.(false);
        appliedRef.current?.();
      }
    };
    rafRef.current = requestAnimationFrame(step);

    return () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
        // A superseded/unmounted tween must not leave the indicator stuck on.
        arrangingRef.current?.(false);
      }
    };
  }, [animatedLayout, setNodes, getNodes]);
}
