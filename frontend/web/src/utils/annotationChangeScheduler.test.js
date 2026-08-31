import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createAnnotationChangeScheduler } from './annotationChangeScheduler';

describe('createAnnotationChangeScheduler (accepted operation timing)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each(['create', 'delete', 'style', 'geometry'])(
    'publishes immediately for a %s change, with no pending timer',
    (kind) => {
      const publish = vi.fn();
      const { schedule } = createAnnotationChangeScheduler({ publish });
      schedule(kind);
      expect(publish).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(10_000);
      expect(publish).toHaveBeenCalledTimes(1);
    }
  );

  it('publishes immediately when no kind is given (unclassified call sites default to immediate)', () => {
    const publish = vi.fn();
    const { schedule } = createAnnotationChangeScheduler({ publish });
    schedule();
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it('debounces a text change: no publish before the window elapses', () => {
    const publish = vi.fn();
    const { schedule } = createAnnotationChangeScheduler({ publish, debounceMs: 300 });
    schedule('text');
    expect(publish).not.toHaveBeenCalled();
    vi.advanceTimersByTime(299);
    expect(publish).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it('coalesces a burst of text changes into one publish (a keystroke storm is not one op per character)', () => {
    const publish = vi.fn();
    const { schedule } = createAnnotationChangeScheduler({ publish, debounceMs: 300 });
    for (let i = 0; i < 20; i++) {
      schedule('text');
      vi.advanceTimersByTime(50); // well inside the window, so each call re-arms it
    }
    expect(publish).not.toHaveBeenCalled();
    vi.advanceTimersByTime(300);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it('an immediate-kind change flushes a pending text debounce rather than losing it or double-publishing', () => {
    const publish = vi.fn();
    const { schedule } = createAnnotationChangeScheduler({ publish, debounceMs: 300 });
    schedule('text');
    vi.advanceTimersByTime(100);
    expect(publish).not.toHaveBeenCalled();
    // e.g. the same annotation is deleted 100ms into a text edit's debounce
    // window: the delete must publish now, and the stale text timer must not
    // also fire later and publish a second, redundant time.
    schedule('delete');
    expect(publish).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(publish).toHaveBeenCalledTimes(1);
  });

  it('clearPending cancels a pending text debounce without publishing', () => {
    const publish = vi.fn();
    const { schedule, clearPending } = createAnnotationChangeScheduler({
      publish,
      debounceMs: 300,
    });
    schedule('text');
    clearPending();
    vi.advanceTimersByTime(1000);
    expect(publish).not.toHaveBeenCalled();
  });

  it('clearPending is a safe no-op when nothing is pending', () => {
    const publish = vi.fn();
    const { clearPending } = createAnnotationChangeScheduler({ publish });
    expect(() => clearPending()).not.toThrow();
  });

  it('uses the injected timer functions rather than the global ones', () => {
    const publish = vi.fn();
    const calls = [];
    const setTimeoutFn = (fn, ms) => {
      calls.push(ms);
      return setTimeout(fn, ms);
    };
    const { schedule } = createAnnotationChangeScheduler({
      publish,
      debounceMs: 123,
      setTimeoutFn,
    });
    schedule('text');
    expect(calls).toEqual([123]);
  });
});
