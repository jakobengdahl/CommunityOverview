import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useViewportMode } from './useViewportMode';

// A minimal fake MediaQueryList whose listeners are observable and whose
// `matches` can be flipped from a test via `emit`.
function makeMql(query, initialMatches) {
  const listeners = new Set();
  return {
    media: query,
    matches: initialMatches,
    addEventListener: vi.fn((event, handler) => {
      if (event === 'change') listeners.add(handler);
    }),
    removeEventListener: vi.fn((event, handler) => {
      if (event === 'change') listeners.delete(handler);
    }),
    emit(matches) {
      this.matches = matches;
      listeners.forEach((handler) => handler({ matches }));
    },
    listenerCount() {
      return listeners.size;
    },
  };
}

describe('useViewportMode', () => {
  let originalMatchMedia;
  let mqls;

  beforeEach(() => {
    originalMatchMedia = window.matchMedia;
    mqls = new Map();
    window.matchMedia = vi.fn((query) => {
      if (!mqls.has(query)) mqls.set(query, makeMql(query, false));
      return mqls.get(query);
    });
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('falls back to both flags false when window.matchMedia is undefined (jsdom-safe)', () => {
    // Simulates the default jsdom environment, which has no matchMedia at all.
    window.matchMedia = undefined;

    const { result } = renderHook(() => useViewportMode());

    expect(result.current.isMobile).toBe(false);
    expect(result.current.isCoarsePointer).toBe(false);
  });

  it('reads the initial state of each media query independently', () => {
    mqls.set('(max-width: 768px)', makeMql('(max-width: 768px)', true));
    mqls.set('(pointer: coarse)', makeMql('(pointer: coarse)', false));

    const { result } = renderHook(() => useViewportMode());

    expect(result.current.isMobile).toBe(true);
    expect(result.current.isCoarsePointer).toBe(false);
  });

  it('subscribes to each query via addEventListener and updates on change', () => {
    const mobileMql = makeMql('(max-width: 768px)', false);
    const coarseMql = makeMql('(pointer: coarse)', false);
    mqls.set('(max-width: 768px)', mobileMql);
    mqls.set('(pointer: coarse)', coarseMql);

    const { result } = renderHook(() => useViewportMode());

    expect(mobileMql.addEventListener).toHaveBeenCalledWith('change', expect.any(Function));
    expect(coarseMql.addEventListener).toHaveBeenCalledWith('change', expect.any(Function));
    expect(result.current.isMobile).toBe(false);
    expect(result.current.isCoarsePointer).toBe(false);

    act(() => {
      mobileMql.emit(true);
    });
    expect(result.current.isMobile).toBe(true);
    expect(result.current.isCoarsePointer).toBe(false);

    act(() => {
      coarseMql.emit(true);
    });
    expect(result.current.isMobile).toBe(true);
    expect(result.current.isCoarsePointer).toBe(true);
  });

  it('removes both change listeners on unmount', () => {
    const mobileMql = makeMql('(max-width: 768px)', false);
    const coarseMql = makeMql('(pointer: coarse)', false);
    mqls.set('(max-width: 768px)', mobileMql);
    mqls.set('(pointer: coarse)', coarseMql);

    const { unmount } = renderHook(() => useViewportMode());

    expect(mobileMql.listenerCount()).toBe(1);
    expect(coarseMql.listenerCount()).toBe(1);

    unmount();

    expect(mobileMql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
    expect(coarseMql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
    expect(mobileMql.listenerCount()).toBe(0);
    expect(coarseMql.listenerCount()).toBe(0);
  });

  it('exposes width from window.innerWidth', () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });

    const { result } = renderHook(() => useViewportMode());

    expect(result.current.width).toBe(1024);

    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
  });
});
