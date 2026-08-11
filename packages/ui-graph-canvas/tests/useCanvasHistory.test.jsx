import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useCanvasHistory } from '../src/hooks/useCanvasHistory';

const move = (id, from, to) => ({ id, from, to });

describe('useCanvasHistory', () => {
  it('starts empty', () => {
    const { result } = renderHook(() => useCanvasHistory());
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(false);
    expect(result.current.undo()).toBe(null);
    expect(result.current.redo()).toBe(null);
  });

  it('undo returns the prior positions and redo returns the new positions', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([move('a', { x: 0, y: 0 }, { x: 100, y: 50 })]);
    });
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);

    let undone;
    act(() => {
      undone = result.current.undo();
    });
    expect(undone).toEqual([{ id: 'a', position: { x: 0, y: 0 } }]);
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(true);

    let redone;
    act(() => {
      redone = result.current.redo();
    });
    expect(redone).toEqual([{ id: 'a', position: { x: 100, y: 50 } }]);
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(false);
  });

  it('undo then redo restores a multi-node batch as one action', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([
        move('a', { x: 0, y: 0 }, { x: 10, y: 10 }),
        move('b', { x: 5, y: 5 }, { x: 25, y: 25 }),
      ]);
    });
    let undone;
    act(() => {
      undone = result.current.undo();
    });
    expect(undone).toEqual([
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 5, y: 5 } },
    ]);
    let redone;
    act(() => {
      redone = result.current.redo();
    });
    expect(redone).toEqual([
      { id: 'a', position: { x: 10, y: 10 } },
      { id: 'b', position: { x: 25, y: 25 } },
    ]);
  });

  it('round-trips parentId so undo/redo can restore group membership', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      // Node dragged from free space (no parent) into group g1 (parent-relative).
      result.current.record([
        {
          id: 'a',
          from: { x: 500, y: 500, parentId: undefined },
          to: { x: 40, y: 40, parentId: 'g1' },
        },
      ]);
    });
    let undone;
    act(() => {
      undone = result.current.undo();
    });
    expect(undone).toEqual([{ id: 'a', position: { x: 500, y: 500 }, parentId: undefined }]);
    let redone;
    act(() => {
      redone = result.current.redo();
    });
    expect(redone).toEqual([{ id: 'a', position: { x: 40, y: 40 }, parentId: 'g1' }]);
  });

  it('records a move where only the parent changed (position identical)', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([
        {
          id: 'a',
          from: { x: 10, y: 10, parentId: undefined },
          to: { x: 10, y: 10, parentId: 'g1' },
        },
      ]);
    });
    expect(result.current.canUndo).toBe(true);
    let undone;
    act(() => {
      undone = result.current.undo();
    });
    expect(undone).toEqual([{ id: 'a', position: { x: 10, y: 10 }, parentId: undefined }]);
  });

  it('recording a new action clears the redo stack', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([move('a', { x: 0, y: 0 }, { x: 1, y: 1 })]);
    });
    act(() => {
      result.current.undo();
    });
    expect(result.current.canRedo).toBe(true);
    act(() => {
      result.current.record([move('b', { x: 0, y: 0 }, { x: 2, y: 2 })]);
    });
    expect(result.current.canRedo).toBe(false);
  });

  it('ignores no-op and malformed moves', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([move('a', { x: 3, y: 4 }, { x: 3, y: 4 })]); // unchanged
      result.current.record([]); // empty
      result.current.record(null); // malformed
      result.current.record([{ id: 'x' }]); // missing positions
    });
    expect(result.current.canUndo).toBe(false);
  });

  it('bounds the undo stack to the configured limit, dropping oldest', () => {
    const { result } = renderHook(() => useCanvasHistory({ limit: 2 }));
    act(() => {
      result.current.record([move('a', { x: 0, y: 0 }, { x: 1, y: 0 })]);
      result.current.record([move('b', { x: 0, y: 0 }, { x: 2, y: 0 })]);
      result.current.record([move('c', { x: 0, y: 0 }, { x: 3, y: 0 })]);
    });
    // Three undos available? No — capped at 2, oldest ('a') dropped.
    let first;
    act(() => {
      first = result.current.undo();
    });
    expect(first).toEqual([{ id: 'c', position: { x: 0, y: 0 } }]);
    let second;
    act(() => {
      second = result.current.undo();
    });
    expect(second).toEqual([{ id: 'b', position: { x: 0, y: 0 } }]);
    expect(result.current.canUndo).toBe(false); // 'a' was dropped
  });

  it('clear empties both stacks', () => {
    const { result } = renderHook(() => useCanvasHistory());
    act(() => {
      result.current.record([move('a', { x: 0, y: 0 }, { x: 1, y: 1 })]);
      result.current.undo();
    });
    expect(result.current.canRedo).toBe(true);
    act(() => {
      result.current.clear();
    });
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(false);
  });

  it('does not mutate the recorded entry when the caller reuses position objects', () => {
    const { result } = renderHook(() => useCanvasHistory());
    const from = { x: 0, y: 0 };
    const to = { x: 9, y: 9 };
    act(() => {
      result.current.record([move('a', from, to)]);
    });
    // Mutate the caller's objects after recording.
    from.x = 999;
    to.y = 999;
    let undone;
    act(() => {
      undone = result.current.undo();
    });
    expect(undone).toEqual([{ id: 'a', position: { x: 0, y: 0 } }]);
  });
});
