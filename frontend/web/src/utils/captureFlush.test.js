// covers: frontend/web/src/utils/captureFlush.js
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createCaptureFlushQueue, flushCanvasForCapture } from './captureFlush';
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
function setup() {
  const sync = { _renderHolds: 0, _closed: false };
  const callbacks = [];
  const options = {
    getSync: () => sync,
    isCurrent: () => true,
    requestSnapshot: (callback) => {
      callbacks.push(callback);
      return () => callbacks.splice(callbacks.indexOf(callback), 1);
    },
    timeoutMs: 100,
  };
  return { sync, callbacks, options, draw: () => callbacks.shift()() };
}
it('waits through holds, including one that begins during a snapshot', async () => {
  const h = setup();
  h.sync._renderHolds = 1;
  const result = flushCanvasForCapture(h.options);
  expect(h.callbacks).toHaveLength(0);
  h.sync._renderHolds = 0;
  await vi.advanceTimersByTimeAsync(25);
  h.sync._renderHolds = 1;
  h.draw();
  expect(h.callbacks).toHaveLength(0);
  h.sync._renderHolds = 0;
  await vi.advanceTimersByTimeAsync(25);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('requests a snapshot immediately without a hold', async () => {
  const h = setup();
  const result = flushCanvasForCapture(h.options);
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('requests a fresh snapshot for each concurrent capture', async () => {
  const h = setup();
  const queue = createCaptureFlushQueue();
  let state = 1;
  const snapshots = [];
  const requestSnapshot = h.options.requestSnapshot;
  h.options.requestSnapshot = (callback) =>
    requestSnapshot(() => {
      snapshots.push(state);
      callback();
    });
  const first = queue(h.options);
  const second = queue(h.options);
  await Promise.resolve();
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await first;
  state = 2;
  await Promise.resolve();
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await second;
  expect(snapshots).toEqual([1, 2]);
});
it.each(['timeout', 'held', 'closed'])('fails and removes callbacks on %s', async (reason) => {
  const h = setup();
  if (reason === 'held') h.sync._renderHolds = 1;
  const result = flushCanvasForCapture(h.options);
  const failure = expect(result).rejects.toThrow();
  if (reason === 'closed') {
    h.sync._closed = true;
    h.draw();
  } else {
    await vi.advanceTimersByTimeAsync(100);
  }
  await failure;
  expect(h.callbacks).toHaveLength(0);
});
