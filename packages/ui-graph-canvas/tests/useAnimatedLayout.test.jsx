import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useAnimatedLayout } from '../src/hooks/useAnimatedLayout';

// Deterministic requestAnimationFrame: frames are queued and flushed by the test
// at a controlled `performance.now()`, so a tween can be stepped exactly.
let rafQueue = [];
let rafSeq = 0;
function installRaf() {
  rafQueue = [];
  rafSeq = 0;
  global.requestAnimationFrame = (cb) => {
    rafSeq += 1;
    rafQueue.push([rafSeq, cb]);
    return rafSeq;
  };
  global.cancelAnimationFrame = (id) => {
    rafQueue = rafQueue.filter(([i]) => i !== id);
  };
}
function flushFrame(nowValue) {
  vi.spyOn(performance, 'now').mockReturnValue(nowValue);
  const pending = rafQueue;
  rafQueue = [];
  pending.forEach(([, cb]) => cb(nowValue));
}

// A stateful stand-in for useNodesState: setNodes updaters are applied
// immediately against the current array, and getNodes reads it back — so the
// hook sees live positions frame to frame, exactly as in the real canvas.
function nodeStore(initial) {
  let nodes = initial;
  const setNodes = vi.fn((u) => {
    nodes = typeof u === 'function' ? u(nodes) : u;
  });
  return {
    setNodes,
    getNodes: () => nodes,
    byId: () => Object.fromEntries(nodes.map((n) => [n.id, n])),
  };
}

const anim = (over = {}) => ({ animate: true, duration_ms: 400, easing: 'ease-in-out', ...over });

describe('useAnimatedLayout', () => {
  beforeEach(() => {
    installRaf();
    vi.spyOn(performance, 'now').mockReturnValue(0);
    // Default: motion allowed. A single test overrides this to reduce-motion.
    window.matchMedia = vi.fn().mockReturnValue({ matches: false });
  });
  afterEach(() => vi.restoreAllMocks());

  it('snaps immediately (no frames) when animate is false', () => {
    const store = nodeStore([{ id: 'a', position: { x: 0, y: 0 } }]);
    const onApplied = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: { a: { x: 10, y: 20 } }, animation: anim({ animate: false }) },
        onAnimatedLayoutApplied: onApplied,
        onAgentArrangingChange: vi.fn(),
        setNodes: store.setNodes,
        getNodes: store.getNodes,
      })
    );
    expect(store.byId().a.position).toEqual({ x: 10, y: 20 });
    expect(onApplied).toHaveBeenCalled();
    expect(rafQueue.length).toBe(0);
  });

  it('snaps when prefers-reduced-motion is set, ignoring the animate hint', () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: true });
    const store = nodeStore([{ id: 'a', position: { x: 0, y: 0 } }]);
    const arranging = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: { a: { x: 5, y: 5 } }, animation: anim() },
        onAnimatedLayoutApplied: vi.fn(),
        onAgentArrangingChange: arranging,
        setNodes: store.setNodes,
        getNodes: store.getNodes,
      })
    );
    expect(store.byId().a.position).toEqual({ x: 5, y: 5 });
    expect(rafQueue.length).toBe(0);
    // No tween ran, so the arranging indicator was never turned on.
    expect(arranging).not.toHaveBeenCalledWith(true);
  });

  it('tweens from live start to target and lands exactly on the target', () => {
    const store = nodeStore([{ id: 'a', position: { x: 0, y: 0 } }]);
    const onApplied = vi.fn();
    const arranging = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: {
          positions: { a: { x: 100, y: 0 } },
          animation: anim({ easing: 'linear' }),
        },
        onAnimatedLayoutApplied: onApplied,
        onAgentArrangingChange: arranging,
        setNodes: store.setNodes,
        getNodes: store.getNodes,
      })
    );
    // The command is consumed on ingest so the channel is free for the next op.
    expect(onApplied).toHaveBeenCalledTimes(1);
    expect(arranging).toHaveBeenCalledWith(true);

    flushFrame(200); // halfway (linear): x ~= 50
    expect(store.byId().a.position.x).toBeCloseTo(50, 1);

    flushFrame(400); // end of the tween: exact target, indicator off
    expect(store.byId().a.position).toEqual({ x: 100, y: 0 });
    expect(arranging).toHaveBeenLastCalledWith(false);
  });

  it('never moves a node the user is dragging', () => {
    const store = nodeStore([
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 0, y: 0 }, dragging: true },
    ]);
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: {
          positions: { a: { x: 100, y: 100 }, b: { x: 200, y: 200 } },
          animation: anim({ easing: 'linear' }),
        },
        onAnimatedLayoutApplied: vi.fn(),
        onAgentArrangingChange: vi.fn(),
        setNodes: store.setNodes,
        getNodes: store.getNodes,
      })
    );
    flushFrame(400);
    expect(store.byId().a.position).toEqual({ x: 100, y: 100 });
    // The dragged node keeps its live position — the tween left it alone.
    expect(store.byId().b.position).toEqual({ x: 0, y: 0 });
  });

  it('restarts an already-tweening node toward the newest target (same-node supersede)', () => {
    const store = nodeStore([{ id: 'a', position: { x: 0, y: 0 } }]);
    const base = {
      onAnimatedLayoutApplied: vi.fn(),
      onAgentArrangingChange: vi.fn(),
      setNodes: store.setNodes,
      getNodes: store.getNodes,
    };
    const { rerender } = renderHook((props) => useAnimatedLayout(props), {
      initialProps: {
        ...base,
        animatedLayout: {
          positions: { a: { x: 100, y: 0 } },
          animation: anim({ easing: 'linear' }),
          seq: 1,
        },
      },
    });
    flushFrame(200); // a partway to (100,0), now at ~50
    rerender({
      ...base,
      animatedLayout: {
        positions: { a: { x: 0, y: 300 } },
        animation: anim({ easing: 'linear' }),
        seq: 2,
      },
    });
    // The new command restarts a's tween at the supersede time (200); it lands on
    // the newest target one duration later.
    flushFrame(600);
    expect(store.byId().a.position).toEqual({ x: 0, y: 300 });
  });

  it('keeps a disjoint earlier batch animating to its target when a new batch arrives', () => {
    // Regression (contract §9: supersede is per-node): a split arrange — two
    // successive animated writes for different node sets — must not strand the
    // first set. `a` from batch 1 still reaches its target after `b` from batch 2
    // replaces the command object.
    const store = nodeStore([
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 0, y: 0 } },
    ]);
    const base = {
      onAnimatedLayoutApplied: vi.fn(),
      onAgentArrangingChange: vi.fn(),
      setNodes: store.setNodes,
      getNodes: store.getNodes,
    };
    const { rerender } = renderHook((props) => useAnimatedLayout(props), {
      initialProps: {
        ...base,
        animatedLayout: {
          positions: { a: { x: 100, y: 0 } },
          animation: anim({ easing: 'linear' }),
          seq: 1,
        },
      },
    });
    flushFrame(200); // a mid-flight at ~50
    rerender({
      ...base,
      animatedLayout: {
        positions: { b: { x: 0, y: 300 } },
        animation: anim({ easing: 'linear' }),
        seq: 2,
      },
    });
    flushFrame(600); // a completes (started at 0); b completes (started at 200)
    expect(store.byId().a.position).toEqual({ x: 100, y: 0 }); // not stranded at ~50
    expect(store.byId().b.position).toEqual({ x: 0, y: 300 });
  });

  it('applies nothing and reports done for an empty batch', () => {
    const store = nodeStore([]);
    const onApplied = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: {}, animation: anim() },
        onAnimatedLayoutApplied: onApplied,
        onAgentArrangingChange: vi.fn(),
        setNodes: store.setNodes,
        getNodes: store.getNodes,
      })
    );
    expect(store.setNodes).not.toHaveBeenCalled();
    expect(onApplied).toHaveBeenCalled();
  });
});
