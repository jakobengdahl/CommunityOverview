import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useVisualViewportInset } from './useVisualViewportInset';

// A minimal fake VisualViewport whose listeners are observable and whose
// height/offsetTop can be driven from a test via `emit`.
function makeVisualViewport({ height, offsetTop = 0 }) {
  const listeners = { resize: new Set(), scroll: new Set() };
  return {
    height,
    offsetTop,
    addEventListener: vi.fn((event, handler) => {
      listeners[event]?.add(handler);
    }),
    removeEventListener: vi.fn((event, handler) => {
      listeners[event]?.delete(handler);
    }),
    emit(event, { height: newHeight, offsetTop: newOffsetTop = 0 } = {}) {
      if (newHeight !== undefined) this.height = newHeight;
      this.offsetTop = newOffsetTop;
      listeners[event]?.forEach((handler) => handler());
    },
    listenerCount(event) {
      return listeners[event]?.size ?? 0;
    },
  };
}

describe('useVisualViewportInset', () => {
  let originalVisualViewport;
  let originalInnerHeight;

  beforeEach(() => {
    originalVisualViewport = window.visualViewport;
    originalInnerHeight = window.innerHeight;
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
  });

  afterEach(() => {
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: originalVisualViewport,
    });
    Object.defineProperty(window, 'innerHeight', {
      configurable: true,
      value: originalInnerHeight,
    });
  });

  it('is a graceful no-op (returns 0) when visualViewport is unavailable (jsdom-safe)', () => {
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined });

    const { result } = renderHook(() => useVisualViewportInset());

    expect(result.current).toBe(0);
  });

  it('reads the initial inset as the gap between innerHeight and visualViewport height', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result } = renderHook(() => useVisualViewportInset());

    expect(result.current).toBe(0);
  });

  it('reports a positive inset once the keyboard shrinks the visual viewport', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result } = renderHook(() => useVisualViewportInset());
    expect(result.current).toBe(0);

    act(() => {
      vv.emit('resize', { height: 480 });
    });
    expect(result.current).toBe(320);
  });

  it('accounts for offsetTop so a scrolled visual viewport does not overstate the inset', () => {
    const vv = makeVisualViewport({ height: 780, offsetTop: 20 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result } = renderHook(() => useVisualViewportInset());

    // 800 - 780 - 20 == 0: the missing 20px is the top offset, not a keyboard.
    expect(result.current).toBe(0);
  });

  it('never reports a negative inset', () => {
    const vv = makeVisualViewport({ height: 820 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result } = renderHook(() => useVisualViewportInset());

    expect(result.current).toBe(0);
  });

  it('subscribes to resize and scroll, and unsubscribes on unmount', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { unmount } = renderHook(() => useVisualViewportInset());

    expect(vv.addEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(vv.addEventListener).toHaveBeenCalledWith('scroll', expect.any(Function));
    expect(vv.listenerCount('resize')).toBe(1);
    expect(vv.listenerCount('scroll')).toBe(1);

    unmount();

    expect(vv.removeEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(vv.removeEventListener).toHaveBeenCalledWith('scroll', expect.any(Function));
    expect(vv.listenerCount('resize')).toBe(0);
    expect(vv.listenerCount('scroll')).toBe(0);
  });

  it('does not subscribe at all when enabled=false, so a caller that never reads the value pays no listener cost', () => {
    const vv = makeVisualViewport({ height: 480 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result } = renderHook(() => useVisualViewportInset(false));

    expect(result.current).toBe(0);
    expect(vv.addEventListener).not.toHaveBeenCalled();
  });

  it('unsubscribes and resets to 0 when enabled flips from true to false', () => {
    const vv = makeVisualViewport({ height: 480 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { result, rerender } = renderHook(({ enabled }) => useVisualViewportInset(enabled), {
      initialProps: { enabled: true },
    });
    expect(result.current).toBe(320);
    expect(vv.listenerCount('resize')).toBe(1);

    rerender({ enabled: false });

    expect(result.current).toBe(0);
    expect(vv.listenerCount('resize')).toBe(0);
  });
});
