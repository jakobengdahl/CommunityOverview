// The sync client only while it still belongs to `sessionId`: until a switch
// to a session with no client yet is cleaned up, the previous client remains.
export function syncForSession(sync, sessionId) {
  return sync?.sessionId === sessionId ? sync : null;
}

// The holds as they stand when a capture is requested, read before any queue
// or await so a hold released before the capture runs is still accounted for.
function readHolds(getSync) {
  const sync = getSync();
  return {
    sync,
    active: sync?._renderHolds > 0,
    generation: sync?._renderHoldGeneration ?? 0,
  };
}

// Wait for a complete canvas snapshot: one is taken when a hold was active at
// the request or has begun since, and again until none begins while it is drawn.
export function flushCanvasForCapture({
  getSync,
  sessionId,
  isCurrent,
  requestSnapshot,
  onReady,
  timeoutMs = 10000,
  requested = readHolds(getSync),
}) {
  return new Promise((resolve, reject) => {
    const initialSync = requested.sync;
    let cancelSnapshot = () => {};
    let pollTimer;
    let finished = false;
    const finish = (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      clearTimeout(pollTimer);
      cancelSnapshot();
      if (error) reject(error);
      else {
        try {
          resolve(onReady?.());
        } catch (cause) {
          reject(cause);
        }
      }
    };
    const deadline = setTimeout(() => finish(new Error('Canvas capture timed out')), timeoutMs);
    // No client at all (a session never connected) captures as before; only a
    // client left over from another session must not be captured through.
    const unavailable = (sync) =>
      !isCurrent() ||
      (sync && sessionId !== undefined && sync.sessionId !== sessionId) ||
      (initialSync && sync !== initialSync) ||
      sync?._closed;
    let snapshotNeeded = requested.active;
    let seenGeneration = requested.generation;
    const holdsSince = (sync) =>
      sync?._renderHolds > 0 || (sync?._renderHoldGeneration ?? 0) !== seenGeneration;
    const attempt = () => {
      if (finished) return;
      const sync = getSync();
      if (unavailable(sync)) {
        finish(new Error('Session is no longer available'));
        return;
      }
      if (sync?._renderHolds > 0) {
        pollTimer = setTimeout(attempt, 25);
        return;
      }
      if (!snapshotNeeded && !holdsSince(sync)) {
        finish();
        return;
      }
      snapshotNeeded = false;
      seenGeneration = sync?._renderHoldGeneration ?? 0;
      cancelSnapshot = requestSnapshot(async () => {
        cancelSnapshot = () => {};
        const current = getSync();
        if (unavailable(current)) {
          finish(new Error('Session is no longer available'));
          return;
        }
        if (holdsSince(current)) {
          attempt();
          return;
        }
        try {
          await current?.flushPending();
        } catch (error) {
          finish(error);
          return;
        }
        attempt();
      });
    };
    attempt();
  });
}

// Serialize held captures so each reads a fresh snapshot; a capture with no
// hold and nothing ahead of it does not wait on the queue.
export function createCaptureFlushQueue() {
  let tail = Promise.resolve();
  let inFlight = 0;
  return (options) => {
    const requested = readHolds(options.getSync);
    if (!requested.active && inFlight === 0) {
      return flushCanvasForCapture({ ...options, requested });
    }
    inFlight += 1;
    const result = tail.then(() => flushCanvasForCapture({ ...options, requested }));
    tail = result
      .catch(() => {})
      .finally(() => {
        inFlight -= 1;
      });
    return result;
  };
}
