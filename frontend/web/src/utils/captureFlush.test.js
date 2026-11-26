// covers: frontend/web/src/utils/captureFlush.js
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createCaptureFlushQueue, flushCanvasForCapture } from './captureFlush';
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
function setup() {
  const sync = { _renderHolds: 0, _closed: false, flushPending: vi.fn(async () => {}) };
  const callbacks = [];
  const options = {
    getSync: () => sync, isCurrent: () => true,
    requestSnapshot: (callback) => {
      callbacks.push(callback); return () => callbacks.splice(callbacks.indexOf(callback), 1);
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
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  h.sync._renderHolds = 1;
  h.draw();
  expect(h.callbacks).toHaveLength(0);
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('proceeds immediately without a hold or a snapshot round trip', async () => {
  const h = setup();
  await expect(flushCanvasForCapture(h.options)).resolves.toBeUndefined();
  expect(h.callbacks).toHaveLength(0);
  expect(h.sync.flushPending).not.toHaveBeenCalled();
});
it('requests a fresh snapshot for each concurrent capture', async () => {
  const h = setup();
  const queue = createCaptureFlushQueue();
  h.sync._renderHolds = 1;
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
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  expect(h.callbacks).toHaveLength(1);
  h.sync._renderHolds = 1;
  h.draw();
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  h.draw();
  await first;
  state = 2;
  h.sync._renderHolds = 1;
  await Promise.resolve();
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await second;
  expect(snapshots).toEqual([1, 1, 2]);
});
it.each(['timeout', 'held', 'closed'])('fails and removes callbacks on %s', async (reason) => {
  const h = setup();
  h.sync._renderHolds = 1;
  const result = flushCanvasForCapture(h.options);
  if (reason !== 'held') {
    h.sync._renderHolds = 0;
    await vi.advanceTimersByTimeAsync(25);
  }
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

it.each(['success', 'failure'])('waits for publication and propagates %s', async (outcome) => {
  const h = setup();
  let complete;
  h.sync.flushPending.mockReturnValue(new Promise((resolve, reject) => { complete = outcome === 'success' ? resolve : reject; }));
  h.sync._renderHolds = 1;
  const result = flushCanvasForCapture(h.options);
  const assertion = outcome === 'success' ? expect(result).resolves.toBeUndefined() : expect(result).rejects.toThrow('publication failed');
  h.sync._renderHolds = 0; await vi.advanceTimersByTimeAsync(25);
  h.draw();
  expect(h.sync.flushPending).toHaveBeenCalledTimes(1);
  let settled = false;
  result.finally(() => { settled = true; }).catch(() => {});
  await Promise.resolve();
  expect(settled).toBe(false);
  complete(new Error('publication failed'));
  await assertion;
});
