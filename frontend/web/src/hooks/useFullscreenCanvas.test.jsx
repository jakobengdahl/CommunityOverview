import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useFullscreenCanvas } from './useFullscreenCanvas';

describe('useFullscreenCanvas', () => {
  let root;
  let rootRef;

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
    rootRef = { current: root };
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      writable: true,
      value: null,
    });
    document.exitFullscreen = vi.fn(async () => {
      document.fullscreenElement = null;
      document.dispatchEvent(new Event('fullscreenchange'));
    });
  });

  afterEach(() => {
    root.remove();
    delete document.exitFullscreen;
  });

  it('enters native fullscreen and synchronizes native exit', async () => {
    root.requestFullscreen = vi.fn(async () => {
      document.fullscreenElement = root;
      document.dispatchEvent(new Event('fullscreenchange'));
    });
    const { result } = renderHook(() => useFullscreenCanvas(rootRef));

    await act(() => result.current.enterFullscreenCanvas());
    expect(result.current.fullscreenCanvasActive).toBe(true);

    act(() => {
      document.fullscreenElement = null;
      document.dispatchEvent(new Event('fullscreenchange'));
    });
    expect(result.current.fullscreenCanvasActive).toBe(false);
  });

  it('uses fallback mode when native entry is rejected', async () => {
    root.requestFullscreen = vi.fn().mockRejectedValue(new Error('denied'));
    const { result } = renderHook(() => useFullscreenCanvas(rootRef));
    await act(() => result.current.enterFullscreenCanvas());
    expect(result.current.fullscreenCanvasActive).toBe(true);
  });

  it('uses fallback mode when the API is missing and Escape exits it', async () => {
    const { result } = renderHook(() => useFullscreenCanvas(rootRef));
    await act(() => result.current.enterFullscreenCanvas());
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })));
    expect(result.current.fullscreenCanvasActive).toBe(false);
  });

  it('supports Ctrl/Cmd+Shift+F but ignores typing targets', () => {
    root.requestFullscreen = vi.fn(async () => {
      document.fullscreenElement = root;
      document.dispatchEvent(new Event('fullscreenchange'));
    });
    renderHook(() => useFullscreenCanvas(rootRef));

    act(() =>
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'F', ctrlKey: true, shiftKey: true, bubbles: true })
      )
    );
    expect(root.requestFullscreen).toHaveBeenCalledOnce();

    const input = document.createElement('input');
    document.body.appendChild(input);
    act(() =>
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'F', metaKey: true, shiftKey: true, bubbles: true })
      )
    );
    expect(document.exitFullscreen).not.toHaveBeenCalled();
    input.remove();
  });
});
