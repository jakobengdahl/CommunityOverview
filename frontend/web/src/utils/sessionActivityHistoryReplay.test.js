// covers: frontend/web/src/utils/sessionActivity.js
import { describe, it, expect, vi } from 'vitest';
import { replayCanvasHistory, createHistoryReplayQueue } from './sessionActivity';

describe('replayCanvasHistory', () => {
  it('publishes and confirms pending canvas edits before replay, then waits for the stream', async () => {
    const calls = [];
    const sync = {
      sessionId: '1234-5678',
      flushPending: vi.fn(async () => calls.push('flush')),
      waitForRevision: vi.fn(async () => calls.push('stream')),
    };
    const replay = vi.fn(async () => {
      calls.push('replay');
      return { revision: 3 };
    });
    await replayCanvasHistory({
      sessionId: sync.sessionId,
      clientId: 'me',
      getSync: () => sync,
      replay,
      snapshot: async () => calls.push('snapshot'),
    });
    expect(calls).toEqual(['snapshot', 'flush', 'replay', 'stream']);
    expect(replay).toHaveBeenCalledWith(sync.sessionId, 'me');
    expect(sync.waitForRevision).toHaveBeenCalledWith(3);
  });

  it('does not replay when pending writes fail or the session switches', async () => {
    const replay = vi.fn();
    const sync = { sessionId: '1234-5678', flushPending: vi.fn().mockRejectedValue(new Error()) };
    const args = {
      sessionId: sync.sessionId,
      clientId: 'me',
      getSync: () => sync,
      replay,
      snapshot: async () => {},
    };
    await expect(replayCanvasHistory(args)).rejects.toBeInstanceOf(Error);
    await replayCanvasHistory({ ...args, getSync: () => ({ sessionId: '8765-4321' }) });
    expect(replay).not.toHaveBeenCalled();
  });
});

describe('createHistoryReplayQueue', () => {
  it('queues keyboard undo, redo and drawer undo until each replay stream completes', async () => {
    const enqueue = createHistoryReplayQueue();
    const releases = [];
    const calls = [];
    const sync = {
      sessionId: '1234-5678',
      flushPending: async () => {},
      waitForRevision: (revision) =>
        new Promise((resolve) => {
          calls.push(`stream:${revision}`);
          releases.push(resolve);
        }),
    };
    const run = (action, revision) =>
      enqueue(() =>
        replayCanvasHistory({
          sessionId: sync.sessionId,
          clientId: 'me',
          getSync: () => sync,
          snapshot: async () => calls.push(`snapshot:${action}`),
          replay: async () => {
            calls.push(action);
            return { revision };
          },
        })
      );
    const undo = run('undo', 1);
    const redo = run('redo', 2);
    let drawerSettled = false;
    const drawer = run('drawer', 3).then(() => {
      drawerSettled = true;
    });
    await vi.waitFor(() => expect(releases).toHaveLength(1));
    expect(calls).toEqual(['snapshot:undo', 'undo', 'stream:1']);
    expect(drawerSettled).toBe(false);
    releases[0]();
    await undo;
    await vi.waitFor(() => expect(releases).toHaveLength(2));
    expect(calls).toEqual([
      'snapshot:undo',
      'undo',
      'stream:1',
      'snapshot:redo',
      'redo',
      'stream:2',
    ]);
    expect(drawerSettled).toBe(false);
    releases[1]();
    await redo;
    await vi.waitFor(() => expect(releases).toHaveLength(3));
    expect(calls.slice(-3)).toEqual(['snapshot:drawer', 'drawer', 'stream:3']);
    expect(drawerSettled).toBe(false);
    releases[2]();
    await drawer;
    expect(drawerSettled).toBe(true);
  });

  it('rejects the failing caller and continues processing queued requests', async () => {
    const enqueue = createHistoryReplayQueue();
    const error = new Error('replay failed');
    const failed = enqueue(async () => {
      throw error;
    });
    const next = vi.fn(async () => 'undone');
    const queued = enqueue(next);
    await expect(failed).rejects.toBe(error);
    await expect(queued).resolves.toBe('undone');
    expect(next).toHaveBeenCalledOnce();
  });
});
