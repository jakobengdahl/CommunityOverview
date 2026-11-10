// covers: frontend/web/src/services/sessionSyncClient.js
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SessionSyncClient } from '../src/services/sessionSyncClient';

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    this.readyState = 1; // OPEN — stream connected successfully
    FakeEventSource.instances.push(this);
  }
  emit(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  // terminal=false simulates a transient drop of an open stream (native reconnect);
  // terminal=true simulates a handshake-level failure (e.g. 429, readyState CLOSED).
  error(terminal = false) {
    if (terminal) this.readyState = 2;
    this.onerror?.();
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
}
FakeEventSource.instances = [];

function makeFetch(responses = []) {
  const calls = [];
  const queue = [...responses];
  const fn = vi.fn(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    const next = queue.shift();
    if (next instanceof Error) throw next;
    return next || { ok: true, status: 200, json: async () => ({ applied: [], seq: 1 }) };
  });
  fn.calls = calls;
  return fn;
}

function makeClient(overrides = {}) {
  const handlers = {};
  const fetchImpl = overrides.fetchImpl || makeFetch();
  const client = new SessionSyncClient({
    sessionId: '1234-5678',
    clientId: 'client-me',
    streamUrl: '/api/sessions/1234-5678/stream',
    opsUrl: '/api/sessions/1234-5678/ops',
    handlers,
    flushIntervalMs: 1,
    fetchImpl,
    EventSourceImpl: FakeEventSource,
    ...overrides,
  });
  return { client, fetchImpl, handlers };
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

describe('history synchronization barrier', () => {
  it('waits for an in-flight write and its stream delivery', async () => {
    let ack;
    const fetchImpl = vi.fn(
      () =>
        new Promise((resolve) => {
          ack = resolve;
        })
    );
    const { client } = makeClient({ fetchImpl });
    client.connect();
    const stream = FakeEventSource.instances.at(-1);
    stream.emit({ type: 'snapshot', seq: 0 });
    client.sendOps([{ op: 'node_moved', node_id: 'a', position: { x: 3, y: 4 } }]);
    const sending = client.flush();
    let settled = false;
    const pending = client.flushPending().then(() => {
      settled = true;
    });
    ack({ ok: true, json: async () => ({ seq: 1, applied: [] }) });
    await sending;
    expect(settled).toBe(false);
    stream.emit({ type: 'op', seq: 1, client_id: 'client-me', op: { op: 'node_moved' } });
    await pending;
    expect(client.getPendingOps()).toEqual([]);
    client.close();
  });

  it('rejects a history barrier when a write is not confirmed', async () => {
    const { client } = makeClient({ fetchImpl: makeFetch([{ ok: false, status: 503 }]) });
    client.connect();
    FakeEventSource.instances.at(-1).emit({ type: 'snapshot', seq: 0 });
    client.sendOps([{ op: 'node_moved', node_id: 'a', position: { x: 3, y: 4 } }]);
    await expect(client.flushPending()).rejects.toBeInstanceOf(Error);
    expect(client.getPendingOps()).toHaveLength(1);
    client.close();
  });
});
