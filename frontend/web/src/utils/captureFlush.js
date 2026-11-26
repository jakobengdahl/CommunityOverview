// Wait for a complete canvas snapshot, retrying if a layout starts while it is drawn.
export function flushCanvasForCapture({ getSync, isCurrent, requestSnapshot, timeoutMs = 10000 }) {
  return new Promise((resolve, reject) => {
    const initialSync = getSync();
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
      else resolve();
    };
    const deadline = setTimeout(() => finish(new Error('Canvas capture timed out')), timeoutMs);
    const attempt = () => {
      if (finished) return;
      const sync = getSync();
      if (!isCurrent() || (initialSync && sync !== initialSync) || sync?._closed) {
        finish(new Error('Session is no longer available'));
        return;
      }
      if (sync?._renderHolds > 0) {
        pollTimer = setTimeout(attempt, 25);
        return;
      }
      cancelSnapshot = requestSnapshot(() => {
        cancelSnapshot = () => {};
        const current = getSync();
        if (!isCurrent() || (initialSync && current !== initialSync) || current?._closed) {
          finish(new Error('Session is no longer available'));
        } else if (current?._renderHolds > 0) {
          attempt();
        } else {
          finish();
        }
      });
    };
    attempt();
  });
}

// Each capture needs a new canvas round trip, even when requests overlap.
export function createCaptureFlushQueue() {
  let tail = Promise.resolve();
  return (options) => {
    const result = tail.then(() => flushCanvasForCapture(options));
    tail = result.catch(() => {});
    return result;
  };
}
