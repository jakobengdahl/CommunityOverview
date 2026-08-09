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
function flushFrame(now) {
  vi.spyOn(performance, 'now').mockReturnValue(now);
  const pending = rafQueue;
  rafQueue = [];
  pending.forEach(([, cb]) => cb(now));
}

// Apply the last captured setNodes updater to a fresh copy of `seed`.
function applyLast(setNodes, seed) {
  const fns = setNodes.mock.calls.map((c) => c[0]).filter((f) => typeof f === 'function');
  if (fns.length === 0) return null;
  return fns[fns.length - 1](seed.map((n) => ({ ...n, position: { ...n.position } })));
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
    const setNodes = vi.fn();
    const onApplied = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: { a: { x: 10, y: 20 } }, animation: anim({ animate: false }) },
        onAnimatedLayoutApplied: onApplied,
        onAgentArrangingChange: vi.fn(),
        setNodes,
        getNodes: () => [{ id: 'a', position: { x: 0, y: 0 } }],
      })
    );
    const result = applyLast(setNodes, [{ id: 'a', position: { x: 0, y: 0 } }]);
    expect(result[0].position).toEqual({ x: 10, y: 20 });
    expect(onApplied).toHaveBeenCalled();
    expect(rafQueue.length).toBe(0);
  });

  it('snaps when prefers-reduced-motion is set, ignoring the animate hint', () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: true });
    const setNodes = vi.fn();
    const arranging = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: { a: { x: 5, y: 5 } }, animation: anim() },
        onAnimatedLayoutApplied: vi.fn(),
        onAgentArrangingChange: arranging,
        setNodes,
        getNodes: () => [{ id: 'a', position: { x: 0, y: 0 } }],
      })
    );
    const result = applyLast(setNodes, [{ id: 'a', position: { x: 0, y: 0 } }]);
    expect(result[0].position).toEqual({ x: 5, y: 5 });
    expect(rafQueue.length).toBe(0);
    // No tween ran, so the arranging indicator was never turned on.
    expect(arranging).not.toHaveBeenCalledWith(true);
  });

  it('tweens from live start to target and lands exactly on the target', () => {
    const setNodes = vi.fn();
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
        setNodes,
        getNodes: () => [{ id: 'a', position: { x: 0, y: 0 } }],
      })
    );
    expect(arranging).toHaveBeenCalledWith(true);

    // Halfway (linear easing): x ~= 50.
    flushFrame(200);
    let result = applyLast(setNodes, [{ id: 'a', position: { x: 0, y: 0 } }]);
    expect(result[0].position.x).toBeCloseTo(50, 1);
    expect(onApplied).not.toHaveBeenCalled();

    // End of the tween: exact target, indicator off, applied fired once.
    flushFrame(400);
    result = applyLast(setNodes, [{ id: 'a', position: { x: 0, y: 0 } }]);
    expect(result[0].position).toEqual({ x: 100, y: 0 });
    expect(arranging).toHaveBeenLastCalledWith(false);
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  it('never moves a node the user is dragging', () => {
    const setNodes = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: {
          positions: { a: { x: 100, y: 100 }, b: { x: 200, y: 200 } },
          animation: anim({ easing: 'linear' }),
        },
        onAnimatedLayoutApplied: vi.fn(),
        onAgentArrangingChange: vi.fn(),
        setNodes,
        getNodes: () => [
          { id: 'a', position: { x: 0, y: 0 } },
          { id: 'b', position: { x: 0, y: 0 }, dragging: true },
        ],
      })
    );
    flushFrame(400);
    const seed = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 0, y: 0 }, dragging: true },
    ];
    const result = applyLast(setNodes, seed);
    const byId = Object.fromEntries(result.map((n) => [n.id, n]));
    expect(byId.a.position).toEqual({ x: 100, y: 100 });
    // The dragged node keeps its live position — the tween left it alone.
    expect(byId.b.position).toEqual({ x: 0, y: 0 });
  });

  it('supersedes an in-flight tween when a new layout arrives', () => {
    const cancelSpy = vi.spyOn(global, 'cancelAnimationFrame');
    const setNodes = vi.fn();
    const { rerender } = renderHook((props) => useAnimatedLayout(props), {
      initialProps: {
        animatedLayout: {
          positions: { a: { x: 100, y: 0 } },
          animation: anim({ easing: 'linear' }),
          seq: 1,
        },
        onAnimatedLayoutApplied: vi.fn(),
        onAgentArrangingChange: vi.fn(),
        setNodes,
        getNodes: () => [{ id: 'a', position: { x: 0, y: 0 } }],
      },
    });
    flushFrame(200); // partway through the first tween
    rerender({
      animatedLayout: {
        positions: { a: { x: 0, y: 300 } },
        animation: anim({ easing: 'linear' }),
        seq: 2,
      },
      onAnimatedLayoutApplied: vi.fn(),
      onAgentArrangingChange: vi.fn(),
      setNodes,
      getNodes: () => [{ id: 'a', position: { x: 50, y: 0 } }],
    });
    expect(cancelSpy).toHaveBeenCalled();
    // The new tween starts at the supersede time (200) and lands on the newest
    // target one full duration later.
    flushFrame(600);
    const result = applyLast(setNodes, [{ id: 'a', position: { x: 50, y: 0 } }]);
    expect(result[0].position).toEqual({ x: 0, y: 300 });
  });

  it('applies nothing and reports done for an empty batch', () => {
    const setNodes = vi.fn();
    const onApplied = vi.fn();
    renderHook(() =>
      useAnimatedLayout({
        animatedLayout: { positions: {}, animation: anim() },
        onAnimatedLayoutApplied: onApplied,
        onAgentArrangingChange: vi.fn(),
        setNodes,
        getNodes: () => [],
      })
    );
    expect(setNodes).not.toHaveBeenCalled();
    expect(onApplied).toHaveBeenCalled();
  });
});
