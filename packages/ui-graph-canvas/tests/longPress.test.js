import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  createLongPressDetector,
  LONG_PRESS_DELAY_MS,
  LONG_PRESS_TOLERANCE_PX,
} from '../src/utils/longPress';

describe('createLongPressDetector', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fires at the delay threshold with the press payload', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).toHaveBeenCalledTimes(1);
    expect(onLongPress).toHaveBeenCalledWith({ kind: 'pane' });
  });

  it('does not fire before the delay threshold', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS - 1);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('cancels when the pointer moves past the tolerance before the delay elapses', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    detector.onPointerMove(1, 100 + LONG_PRESS_TOLERANCE_PX + 1, 100);
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('does not cancel when movement stays within the tolerance', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    detector.onPointerMove(1, 100 + LONG_PRESS_TOLERANCE_PX, 100);
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).toHaveBeenCalledTimes(1);
  });

  it('cancels on multi-touch: a second pointer going down aborts the pending press', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'node', nodeId: 'n1' });
    vi.advanceTimersByTime(100);
    detector.onPointerDown(2, 200, 200, { kind: 'node', nodeId: 'n1' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('a lone second-finger press cannot start its own long-press while the first finger is still down', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    detector.onPointerDown(2, 200, 200, { kind: 'pane' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('cancels when the pointer is released before the delay elapses (a plain tap)', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    vi.advanceTimersByTime(100);
    detector.onPointerUp(1);
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('cancels on pointercancel the same way as pointerup', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    vi.advanceTimersByTime(100);
    detector.onPointerCancel(1);
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();
  });

  it('a fresh press after a cancelled one still fires normally', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    detector.onPointerMove(1, 100 + LONG_PRESS_TOLERANCE_PX + 1, 100);
    detector.onPointerUp(1);

    detector.onPointerDown(1, 50, 50, { kind: 'node', nodeId: 'n2' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).toHaveBeenCalledTimes(1);
    expect(onLongPress).toHaveBeenCalledWith({ kind: 'node', nodeId: 'n2' });
  });

  it('an unrelated pointer moving does not cancel the primary pointer press', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    // pointer 2 was never pressed down through the detector; a stray move
    // event for it (e.g. delivered by the DOM after release) must not affect
    // pointer 1's pending press.
    detector.onPointerMove(2, 500, 500);
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).toHaveBeenCalledTimes(1);
  });

  it('respects a custom delay and tolerance', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress, delay: 300, tolerance: 2 });
    detector.onPointerDown(1, 0, 0, { kind: 'pane' });
    vi.advanceTimersByTime(299);
    expect(onLongPress).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onLongPress).toHaveBeenCalledTimes(1);
  });

  it('reset forgets in-flight pointers so a later down starts a clean press', () => {
    const onLongPress = vi.fn();
    const detector = createLongPressDetector({ onLongPress });
    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    detector.reset();
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).not.toHaveBeenCalled();

    detector.onPointerDown(1, 100, 100, { kind: 'pane' });
    vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
    expect(onLongPress).toHaveBeenCalledTimes(1);
  });
});
