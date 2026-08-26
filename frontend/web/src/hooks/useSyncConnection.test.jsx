import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import * as api from '../services/api';
import { SessionSyncClient } from '../services/sessionSyncClient';
import { useSyncConnection } from './useSyncConnection';

vi.mock('../services/api', () => ({
  getClientId: vi.fn(() => 'client-1'),
  getDisplayName: vi.fn(() => 'Tester'),
  getSessionStreamUrl: vi.fn((id) => `/stream/${id}`),
  getSessionOpsUrl: vi.fn((id) => `/ops/${id}`),
}));

// A minimal fake client whose connect/flush/close are observable. `connectImpl`
// lets a test make connect() throw to exercise the half-connected guard.
class FakeClient {
  constructor(opts) {
    this.opts = opts;
    this.sessionId = opts.sessionId;
    this.handlers = opts.handlers;
    this.connected = false;
    this.connectCalls = 0;
    this.flushCalls = 0;
    this.closeCalls = 0;
    FakeClient.instances.push(this);
  }
  connect() {
    this.connectCalls += 1;
    if (this.connectImpl) this.connectImpl();
    this.connected = true;
  }
  flush() {
    this.flushCalls += 1;
  }
  close() {
    this.closeCalls += 1;
    this.connected = false;
  }
}
FakeClient.instances = [];

vi.mock('../services/sessionSyncClient', () => ({
  SessionSyncClient: vi.fn((opts) => new FakeClient(opts)),
}));

beforeEach(() => {
  vi.clearAllMocks();
  FakeClient.instances = [];
});

describe('useSyncConnection.ensureSyncConnected', () => {
  it('creates and connects a client for a new session and stores it in syncRef', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));

    let client;
    act(() => {
      client = result.current.ensureSyncConnected('1111-2222');
    });

    expect(SessionSyncClient).toHaveBeenCalledTimes(1);
    expect(client.connectCalls).toBe(1);
    expect(result.current.syncRef.current).toBe(client);
    expect(api.getSessionStreamUrl).toHaveBeenCalledWith('1111-2222');
  });

  it('reuses (reconnects) the existing client on the same-session fast path', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));

    let first;
    act(() => {
      first = result.current.ensureSyncConnected('1111-2222');
    });
    let second;
    act(() => {
      second = result.current.ensureSyncConnected('1111-2222');
    });

    expect(second).toBe(first);
    expect(SessionSyncClient).toHaveBeenCalledTimes(1);
    expect(first.connectCalls).toBe(2); // initial + fast-path reconnect
  });

  it('tears down the previous client and resets connection state on a session change', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));

    let first;
    act(() => {
      first = result.current.ensureSyncConnected('1111-2222');
      // Seed some connection-scoped state that must be cleared on switch.
      result.current.setRoster([{ client_id: 'x' }]);
      result.current.setOpStreamReady(true);
    });
    expect(result.current.roster).toHaveLength(1);

    let second;
    act(() => {
      second = result.current.ensureSyncConnected('3333-4444');
    });

    expect(second).not.toBe(first);
    expect(first.flushCalls).toBe(1);
    expect(first.closeCalls).toBe(1);
    expect(result.current.roster).toEqual([]);
    expect(result.current.opStreamReady).toBe(false);
  });

  // Regression (carried from App.jsx): a client whose connect() throws must not
  // be left in syncRef.current, or the same-session fast path retries it forever
  // and the un-guarded auto-save call site throws.
  it('returns null and leaves syncRef empty when connect throws', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));
    SessionSyncClient.mockImplementationOnce((opts) => {
      const c = new FakeClient(opts);
      c.connectImpl = () => {
        throw new Error('bad stream url');
      };
      return c;
    });

    let ret;
    act(() => {
      ret = result.current.ensureSyncConnected('1111-2222');
    });

    expect(ret).toBeNull();
    expect(result.current.syncRef.current).toBeNull();
  });

  it('drives the op-stream-ready flag through the wrapped onReady handler', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));
    const appOnReady = vi.fn();
    result.current.syncHandlersRef.current = { onReady: appOnReady };

    let client;
    act(() => {
      client = result.current.ensureSyncConnected('1111-2222');
    });
    act(() => {
      client.handlers.onReady('payload');
    });

    expect(result.current.opStreamReady).toBe(true);
    expect(appOnReady).toHaveBeenCalledWith('payload');
  });

  it('delegates onDropped to the latest handler in syncHandlersRef (R9)', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));
    const appOnDropped = vi.fn();
    result.current.syncHandlersRef.current = { onDropped: appOnDropped };

    let client;
    act(() => {
      client = result.current.ensureSyncConnected('1111-2222');
    });
    act(() => {
      client.handlers.onDropped([{ op: 'annotation_created' }], 400);
    });

    expect(appOnDropped).toHaveBeenCalledWith([{ op: 'annotation_created' }], 400);
  });

  it('drops remote ops delivered by a stale session client after switching sessions', () => {
    const { result } = renderHook(() => useSyncConnection('1111-2222'));
    const appOnRemoteOps = vi.fn();
    result.current.syncHandlersRef.current = { onRemoteOps: appOnRemoteOps };

    let staleClient;
    act(() => {
      staleClient = result.current.ensureSyncConnected('1111-2222');
    });
    act(() => {
      result.current.ensureSyncConnected('3333-4444');
    });
    act(() => {
      staleClient.handlers.onRemoteOps([{ op: 'nodes_added', node_ids: ['stale-node'] }], {
        clientId: 'client-other',
      });
    });

    expect(appOnRemoteOps).not.toHaveBeenCalled();
  });
});

describe('useSyncConnection teardown', () => {
  it('flushes and closes the active client when the session id changes', () => {
    const { result, rerender } = renderHook(({ id }) => useSyncConnection(id), {
      initialProps: { id: '1111-2222' },
    });

    let client;
    act(() => {
      client = result.current.ensureSyncConnected('1111-2222');
    });

    rerender({ id: '3333-4444' });

    expect(client.flushCalls).toBe(1);
    expect(client.closeCalls).toBe(1);
    expect(result.current.syncRef.current).toBeNull();
  });

  it('does not tear down a client that belongs to a different session', () => {
    const { result, rerender } = renderHook(({ id }) => useSyncConnection(id), {
      initialProps: { id: '1111-2222' },
    });

    // Client for a *different* id than the effect's captured session id.
    let client;
    act(() => {
      client = result.current.ensureSyncConnected('9999-0000');
    });

    rerender({ id: '3333-4444' });

    expect(client.closeCalls).toBe(0);
    expect(result.current.syncRef.current).toBe(client);
  });
});
