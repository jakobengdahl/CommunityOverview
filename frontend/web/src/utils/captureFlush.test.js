// covers: frontend/web/src/utils/captureFlush.js
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createCaptureFlushQueue, flushCanvasForCapture } from './captureFlush';
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
function setup() {
  const sync = { _renderHolds: 0, _renderHoldGeneration: 0, flushPending: vi.fn(async () => {}) };
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
  const hold = () => {
    sync._renderHolds += 1;
    sync._renderHoldGeneration += 1;
  };
  const release = () => (sync._renderHolds -= 1);
  return { sync, callbacks, options, hold, release, draw: () => callbacks.shift()() };
}
it('waits through holds, including one that begins during a snapshot', async () => {
  const h = setup();
  h.hold();
  const result = flushCanvasForCapture(h.options);
  expect(h.callbacks).toHaveLength(0);
  h.release();
  await vi.advanceTimersByTimeAsync(25);
  h.hold();
  h.draw();
  expect(h.callbacks).toHaveLength(0);
  h.release();
  await vi.advanceTimersByTimeAsync(25);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('snapshots for a hold active at the request even if released before the queue runs', async () => {
  const h = setup();
  h.hold();
  const result = createCaptureFlushQueue()(h.options);
  h.release();
  await vi.advanceTimersByTimeAsync(0);
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('takes a fresh snapshot for each capture requested during a hold', async () => {
  const h = setup();
  const queue = createCaptureFlushQueue();
  h.hold();
  const first = queue(h.options);
  const second = queue(h.options);
  h.release();
  await vi.advanceTimersByTimeAsync(0);
  h.draw();
  await first;
  await vi.advanceTimersByTimeAsync(0);
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await second;
  expect(h.sync.flushPending).toHaveBeenCalledTimes(2);
});
it('snapshots again when a hold begins and ends during publication', async () => {
  const h = setup();
  let complete;
  h.sync.flushPending.mockReturnValueOnce(
    new Promise((resolve) => {
      complete = resolve;
    })
  );
  h.hold();
  const result = flushCanvasForCapture(h.options);
  h.release();
  await vi.advanceTimersByTimeAsync(25);
  h.draw();
  h.hold();
  h.release();
  complete();
  await vi.advanceTimersByTimeAsync(0);
  expect(h.callbacks).toHaveLength(1);
  h.draw();
  await expect(result).resolves.toBeUndefined();
});
it('queues an unheld capture behind one in flight and skips the queue once it is empty', async () => {
  const h = setup();
  const queue = createCaptureFlushQueue();
  h.hold();
  const first = queue(h.options);
  h.release();
  let settled = false;
  const second = queue(h.options).then(() => (settled = true));
  await vi.advanceTimersByTimeAsync(0);
  expect(settled).toBe(false);
  h.draw();
  await Promise.all([first, second]);
  settled = false;
  let ready = false;
  queue({
    ...h.options,
    onReady: () => {
      ready = true;
    },
  }).then(() => (settled = true));
  expect(ready).toBe(true);
  await Promise.resolve();
  expect(settled).toBe(true);
  expect(h.sync.flushPending).toHaveBeenCalledTimes(1);
});
it.each(['timeout', 'held', 'closed'])('fails and removes callbacks on %s', async (reason) => {
  const h = setup();
  h.hold();
  const result = flushCanvasForCapture(h.options);
  if (reason !== 'held') {
    h.release();
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
  h.sync.flushPending.mockReturnValue(
    new Promise((resolve, reject) => {
      complete = outcome === 'success' ? resolve : reject;
    })
  );
  h.hold();
  const result = flushCanvasForCapture(h.options);
  const assertion =
    outcome === 'success'
      ? expect(result).resolves.toBeUndefined()
      : expect(result).rejects.toThrow('publication failed');
  h.release();
  await vi.advanceTimersByTimeAsync(25);
  h.draw();
  expect(h.sync.flushPending).toHaveBeenCalledTimes(1);
  let settled = false;
  result
    .finally(() => {
      settled = true;
    })
    .catch(() => {});
  await Promise.resolve();
  expect(settled).toBe(false);
  complete(new Error('publication failed'));
  await assertion;
});
