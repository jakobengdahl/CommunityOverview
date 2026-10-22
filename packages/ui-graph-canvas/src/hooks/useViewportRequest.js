import { useEffect } from 'react';
import { prefersReducedMotion } from './useAnimatedLayout';

// Long enough to read as "the camera is travelling", short enough not to feel
// like the canvas was taken away (same order as the session-switch refit).
export const VIEWPORT_REQUEST_DURATION_MS = 600;

/**
 * Move the camera when the host asks: `viewportRequest` is a fresh object per
 * request, either `{ bounds: {x, y, width, height} }` (a model-space region to
 * fit, e.g. a session summon — every screen size frames the same region) or
 * `{ viewport: {x, y, zoom} }` (an exact camera to restore, e.g. "Return").
 *
 * The transition is animated unless the viewer asked for reduced motion, in
 * which case it jumps — a per-viewer decision, as for animated layouts.
 * `fitBounds`/`setViewport` are ReactFlow's; either may be missing in a test
 * double, in which case the request is ignored.
 */
export function useViewportRequest({ viewportRequest, fitBounds, setViewport }) {
  useEffect(() => {
    if (!viewportRequest) return;
    const duration = prefersReducedMotion() ? 0 : VIEWPORT_REQUEST_DURATION_MS;
    if (viewportRequest.bounds) {
      fitBounds?.(viewportRequest.bounds, { padding: 0, duration });
    } else if (viewportRequest.viewport) {
      setViewport?.(viewportRequest.viewport, { duration });
    }
    // Only a new request moves the camera; the ReactFlow functions may change
    // identity on any render (see the getViewportRef note in GraphCanvas).
  }, [viewportRequest]); // eslint-disable-line react-hooks/exhaustive-deps
}
