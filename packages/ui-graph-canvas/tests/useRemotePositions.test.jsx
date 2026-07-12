import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useRemotePositions } from '../src/hooks/useRemotePositions';

// Apply each captured setNodes updater to a fresh copy of `seed` independently
// and return the first result satisfying `predicate`, isolating the updater
// under test from the others.
function findResult(setNodes, seed, predicate) {
  for (const call of setNodes.mock.calls) {
    if (typeof call[0] !== 'function') continue;
    let result;
    try {
      result = call[0](seed.map((n) => ({ ...n })));
    } catch {
      continue;
    }
    if (Array.isArray(result) && predicate(result)) return result;
  }
  return null;
}

describe('useRemotePositions', () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.useRealTimers());

  it('applies a remote position to a matching mounted node', () => {
    const setNodes = vi.fn();
    const onApplied = vi.fn();
    renderHook(() =>
      useRemotePositions({
        remotePositions: { 'node-a': { x: 42, y: 99 } },
        onRemotePositionsApplied: onApplied,
        nodes: [{ id: 'node-a' }],
        setNodes,
      })
    );
    const seed = [{ id: 'node-a', position: { x: 0, y: 0 } }];
    const result = findResult(setNodes, seed, (r) => r[0].position.x === 42);
    expect(result[0].position).toEqual({ x: 42, y: 99 });
    expect(onApplied).toHaveBeenCalled();
  });

  it('holds a position for a not-yet-mounted node and applies it once it mounts', () => {
    const setNodes = vi.fn();
    const { rerender } = renderHook((props) => useRemotePositions(props), {
      initialProps: {
        remotePositions: { late: { x: 33, y: 44 } },
        onRemotePositionsApplied: vi.fn(),
        nodes: [],
        setNodes,
      },
    });
    // Node arrives; parent has since cleared remotePositions. The node-list
    // change alone must be enough to apply the position that arrived too early.
    setNodes.mockClear();
    rerender({
      remotePositions: null,
      onRemotePositionsApplied: vi.fn(),
      nodes: [{ id: 'late' }],
      setNodes,
    });
    const seed = [{ id: 'late', position: { x: 0, y: 0 } }];
    const result = findResult(setNodes, seed, (r) => r[0].position.x === 33);
    expect(result[0].position).toEqual({ x: 33, y: 44 });
  });

  it('prunes a pending entry whose node never mounts once the TTL elapses', () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const setNodes = vi.fn();
    const { rerender } = renderHook((props) => useRemotePositions(props), {
      initialProps: {
        remotePositions: { ghost: { x: 5, y: 6 } },
        onRemotePositionsApplied: vi.fn(),
        nodes: [],
        setNodes,
      },
    });
    // TTL elapses before the node ever mounts; a subsequent node-list change
    // must drop the stale entry rather than apply it to an unrelated node that
    // later reuses the id.
    vi.setSystemTime(31_000);
    setNodes.mockClear();
    rerender({
      remotePositions: null,
      onRemotePositionsApplied: vi.fn(),
      nodes: [{ id: 'ghost' }],
      setNodes,
    });
    const seed = [{ id: 'ghost', position: { x: 0, y: 0 } }];
    // No updater should move 'ghost' to the pruned position.
    const applied = findResult(setNodes, seed, (r) => r[0].position.x === 5);
    expect(applied).toBeNull();
  });

  it('still applies a pending position within the TTL window', () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const setNodes = vi.fn();
    const { rerender } = renderHook((props) => useRemotePositions(props), {
      initialProps: {
        remotePositions: { soon: { x: 7, y: 8 } },
        onRemotePositionsApplied: vi.fn(),
        nodes: [],
        setNodes,
      },
    });
    vi.setSystemTime(10_000);
    setNodes.mockClear();
    rerender({
      remotePositions: null,
      onRemotePositionsApplied: vi.fn(),
      nodes: [{ id: 'soon' }],
      setNodes,
    });
    const seed = [{ id: 'soon', position: { x: 0, y: 0 } }];
    const result = findResult(setNodes, seed, (r) => r[0].position.x === 7);
    expect(result[0].position).toEqual({ x: 7, y: 8 });
  });

  it('returns the same array reference when a node-list change matches nothing', () => {
    const setNodes = vi.fn();
    const { rerender } = renderHook((props) => useRemotePositions(props), {
      initialProps: {
        remotePositions: { held: { x: 1, y: 2 } },
        onRemotePositionsApplied: vi.fn(),
        nodes: [],
        setNodes,
      },
    });
    setNodes.mockClear();
    // A node-list change that does not include the pending id must not create a
    // new array reference (the effect depends on `nodes` — a new ref loops).
    rerender({
      remotePositions: null,
      onRemotePositionsApplied: vi.fn(),
      nodes: [{ id: 'other' }],
      setNodes,
    });
    const seed = [{ id: 'other', position: { x: 0, y: 0 } }];
    for (const call of setNodes.mock.calls) {
      if (typeof call[0] !== 'function') continue;
      const result = call[0](seed);
      expect(result).toBe(seed);
    }
  });
});
