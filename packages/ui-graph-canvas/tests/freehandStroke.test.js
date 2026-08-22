import { describe, it, expect, vi } from 'vitest';
import { createFreehandStrokeCapture } from '../src/utils/freehandStroke';

// toModelPoint stands in for the host's screen->flow coordinate conversion;
// tests use an identity mapping unless noted otherwise.
const identity = (event) => ({ x: event.clientX, y: event.clientY });

function down(pointerId, x, y, extra = {}) {
  return { pointerId, clientX: x, clientY: y, pointerType: 'mouse', ...extra };
}

describe('createFreehandStrokeCapture', () => {
  it('accumulates points from pointerdown through pointermove and completes on pointerup', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerDown(down(1, 0, 0), identity);
    expect(capture.isActive()).toBe(true);
    capture.onPointerMove(down(1, 5, 5), identity);
    capture.onPointerMove(down(1, 10, 2), identity);
    capture.onPointerUp(down(1, 10, 2));

    expect(capture.isActive()).toBe(false);
    expect(onStrokeComplete).toHaveBeenCalledTimes(1);
    const [{ points, pointerType }] = onStrokeComplete.mock.calls[0];
    expect(points).toEqual([
      { x: 0, y: 0 },
      { x: 5, y: 5 },
      { x: 10, y: 2 },
    ]);
    expect(pointerType).toBe('mouse');
  });

  it('discards a stroke shorter than minPoints without firing the callback', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete, minPoints: 3 });

    capture.onPointerDown(down(1, 0, 0), identity);
    capture.onPointerUp(down(1, 0, 0)); // only 1 sample, below minPoints

    expect(onStrokeComplete).not.toHaveBeenCalled();
    expect(capture.isActive()).toBe(false);
  });

  it('ignores a second pointer going down mid-stroke (no second stroke, no interruption)', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerDown(down(1, 0, 0), identity);
    capture.onPointerDown(down(2, 100, 100), identity); // ignored: pointer 1 already active
    capture.onPointerMove(down(2, 200, 200), identity); // ignored: not the primary pointer
    capture.onPointerMove(down(1, 5, 5), identity);
    capture.onPointerUp(down(1, 5, 5));

    expect(onStrokeComplete).toHaveBeenCalledTimes(1);
    const [{ points }] = onStrokeComplete.mock.calls[0];
    expect(points).toEqual([
      { x: 0, y: 0 },
      { x: 5, y: 5 },
    ]);
  });

  it('is a no-op for pointerup/pointermove from a pointer id that never went down', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerMove(down(7, 1, 1), identity);
    capture.onPointerUp(down(7, 1, 1));

    expect(onStrokeComplete).not.toHaveBeenCalled();
    expect(capture.isActive()).toBe(false);
  });

  it('discards the stroke on pointercancel without firing the callback', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerDown(down(1, 0, 0), identity);
    capture.onPointerMove(down(1, 5, 5), identity);
    capture.onPointerCancel(down(1, 5, 5));

    expect(onStrokeComplete).not.toHaveBeenCalled();
    expect(capture.isActive()).toBe(false);

    // A fresh stroke afterwards behaves normally — cancel does not wedge state.
    capture.onPointerDown(down(1, 1, 1), identity);
    capture.onPointerMove(down(1, 2, 2), identity);
    capture.onPointerUp(down(1, 2, 2));
    expect(onStrokeComplete).toHaveBeenCalledTimes(1);
  });

  it('includes pressure only when the device reports a finite, positive value', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerDown(down(1, 0, 0, { pressure: 0.7 }), identity);
    capture.onPointerMove(down(1, 1, 1, { pressure: 0 }), identity); // mouse: reports 0
    capture.onPointerMove(down(1, 2, 2, { pressure: NaN }), identity);
    capture.onPointerUp(down(1, 2, 2));

    const [{ points }] = onStrokeComplete.mock.calls[0];
    expect(points[0]).toEqual({ x: 0, y: 0, pressure: 0.7 });
    expect(points[1]).not.toHaveProperty('pressure');
    expect(points[2]).not.toHaveProperty('pressure');
  });

  it('reset() forcibly abandons an in-flight stroke', () => {
    const onStrokeComplete = vi.fn();
    const capture = createFreehandStrokeCapture({ onStrokeComplete });

    capture.onPointerDown(down(1, 0, 0), identity);
    capture.reset();
    expect(capture.isActive()).toBe(false);
    capture.onPointerUp(down(1, 0, 0));
    expect(onStrokeComplete).not.toHaveBeenCalled();
  });
});
