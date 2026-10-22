import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useViewportRequest, VIEWPORT_REQUEST_DURATION_MS } from '../src/hooks/useViewportRequest';

function mockReducedMotion(reduce) {
  window.matchMedia = vi.fn().mockReturnValue({ matches: reduce });
}

describe('useViewportRequest', () => {
  const originalMatchMedia = window.matchMedia;
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('fits model-space bounds with an animated transition', () => {
    mockReducedMotion(false);
    const fitBounds = vi.fn();
    const bounds = { x: 1, y: 2, width: 300, height: 200 };
    renderHook(() =>
      useViewportRequest({ viewportRequest: { bounds }, fitBounds, setViewport: vi.fn() })
    );
    expect(VIEWPORT_REQUEST_DURATION_MS).toBe(600);
    expect(fitBounds).toHaveBeenCalledWith(bounds, { padding: 0, duration: 600 });
  });

  it('jumps instead of animating when the viewer prefers reduced motion', () => {
    mockReducedMotion(true);
    const fitBounds = vi.fn();
    const setViewport = vi.fn();
    const { rerender } = renderHook((props) => useViewportRequest(props), {
      initialProps: {
        viewportRequest: { bounds: { x: 0, y: 0, width: 1, height: 1 } },
        fitBounds,
        setViewport,
      },
    });
    expect(fitBounds.mock.calls[0][1].duration).toBe(0);
    rerender({ viewportRequest: { viewport: { x: 5, y: 6, zoom: 1.5 } }, fitBounds, setViewport });
    expect(setViewport).toHaveBeenCalledWith({ x: 5, y: 6, zoom: 1.5 }, { duration: 0 });
  });

  it('applies each request once, not on every render', () => {
    mockReducedMotion(false);
    const fitBounds = vi.fn();
    const request = { bounds: { x: 0, y: 0, width: 1, height: 1 } };
    const { rerender } = renderHook((props) => useViewportRequest(props), {
      initialProps: { viewportRequest: request, fitBounds: vi.fn(), setViewport: vi.fn() },
    });
    rerender({ viewportRequest: request, fitBounds, setViewport: vi.fn() });
    expect(fitBounds).not.toHaveBeenCalled();
    rerender({ viewportRequest: null, fitBounds, setViewport: vi.fn() });
    expect(fitBounds).not.toHaveBeenCalled();
  });
});
