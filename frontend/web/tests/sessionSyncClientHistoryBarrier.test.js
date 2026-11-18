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

describe('appearance ordering', () => {
  it('folds removal, acknowledgment and remote appearance in canvas queue order', async () => {
    const deliveries = [];
    const acknowledged = {
      op: 'node_shape_set',
      node_id: 'a',
      selection: { builtin: 'ack' },
      revisions: { 'node:a': 2 },
    };
    const { client } = makeClient({
      deferRemoteOps: true,
      fetchImpl: makeFetch([{ ok: true, json: async () => ({ seq: 2, applied: [acknowledged] }) }]),
      handlers: { onRemoteOps: (ops, meta) => deliveries.push({ op: ops[0], ...meta }) },
    });
    client.setBaseline({ node_refs: ['a'], shape_overrides: { a: { builtin: true } } });
    client._handleEvent({
      type: 'op',
      seq: 1,
      client_id: 'other',
      op: { op: 'nodes_removed', node_ids: ['a'] },
    });
    client.sendOps([acknowledged]);
    await client.flush();
    client._handleEvent({
      type: 'op',
      seq: 3,
      client_id: 'other',
      op: {
        op: 'type_shape_set',
        type: 'Thing',
        selection: { builtin: true },
        revisions: { 'type:Thing': 1 },
      },
    });
    expect(client.isMember('a')).toBe(true);
    expect(deliveries.map((entry) => entry.op.op)).toEqual([
      'nodes_removed',
      'node_shape_set',
      'type_shape_set',
    ]);
    expect(deliveries[0].applyBaseline()).toBe(true);
    expect(client.isMember('a')).toBe(false);
    expect(deliveries[1].applyBaseline()).toBe(false);
    expect(client._baseline.shape_overrides).toEqual({ a: { builtin: 'ack' } });
    expect(deliveries[2].applyBaseline()).toBe(true);
    expect(client._baseline).toMatchObject({
      shape_overrides: { a: { builtin: 'ack' } },
      type_shapes: { Thing: { builtin: true } },
      shape_revisions: { 'node:a': 2, 'type:Thing': 1 },
    });
    client._handleEvent({
      type: 'op',
      seq: 4,
      client_id: 'other',
      op: {
        op: 'node_shape_set',
        node_id: 'a',
        selection: { builtin: 'new' },
        revisions: { 'node:a': 3 },
      },
    });
    expect(deliveries[3].applyBaseline()).toBe(true);
    expect(client._baseline.shape_overrides.a).toEqual({ builtin: 'new' });
    client.close();
  });

  // The own write is acknowledged as `[seq, revision]`; its POST response lands
  // before stream event `ackAt`, and 'own' marks that write's echo.
  it.each([
    [
      'after a later removal',
      [1, 1],
      [
        { seq: 1, client_id: 'client-me', op: 'own' },
        { seq: 2, client_id: 'other', op: { op: 'nodes_removed', node_ids: ['a'] } },
      ],
      2,
      { overrides: {}, revision: 2, order: ['node_shape_set', 'nodes_removed'] },
    ],
    [
      'before an earlier queued removal',
      [3, 2],
      [
        { seq: 1, client_id: 'other', op: { op: 'nodes_removed', node_ids: ['a'] } },
        { seq: 2, client_id: 'other', op: { op: 'nodes_added', node_ids: ['a'] } },
        { seq: 3, client_id: 'client-me', op: 'own' },
      ],
      0,
      {
        overrides: { a: { builtin: 'mine' } },
        revision: 2,
        order: ['nodes_removed', 'nodes_added', 'node_shape_set'],
      },
    ],
  ])(
    'folds an appearance acknowledgment at its stream seq when it arrives %s',
    async (_label, [ackSeq, ackRevision], events, ackAt, expected) => {
      const own = { op: 'node_shape_set', node_id: 'a', selection: { builtin: 'mine' } };
      const deliveries = [];
      let respond;
      const fetchImpl = vi.fn(() => new Promise((resolve) => (respond = resolve)));
      const { client } = makeClient({
        deferRemoteOps: true,
        fetchImpl,
        handlers: { onRemoteOps: (ops, meta) => deliveries.push({ op: ops[0], ...meta }) },
      });
      client.setBaseline({ node_refs: ['a'], shape_overrides: { a: { builtin: 'old' } } });
      client.sendOps([own]);
      const flushed = client.flush();
      const applied = { ...own, seq: ackSeq, revisions: { 'node:a': ackRevision } };
      const ack = async () => {
        respond({ ok: true, json: async () => ({ seq: ackSeq, applied: [applied] }) });
        await flushed;
      };
      for (const [index, event] of events.entries()) {
        if (index === ackAt) await ack();
        client._handleEvent({ type: 'op', ...event, op: event.op === 'own' ? applied : event.op });
      }
      if (ackAt >= events.length) await ack();
      expect(deliveries.map((entry) => entry.op.op)).toEqual(expected.order);
      for (const entry of deliveries) entry.applyBaseline();
      expect(client._baseline.shape_overrides).toEqual(expected.overrides);
      expect(client._baseline.shape_revisions['node:a']).toBe(expected.revision);
      client.close();
    }
  );

  it.each([
    [3, 1, { Thing: { builtin: 'new' } }, { a: { builtin: 'later' } }],
    [1, 3, { Thing: { builtin: 'later' } }, {}],
    [1, 1, { Thing: { builtin: 'new' } }, {}],
  ])(
    'applies each current target independently (node %s, type %s)',
    (nodeRev, typeRev, types, overrides) => {
      const deliveries = [];
      const { client } = makeClient({
        deferRemoteOps: true,
        handlers: { onRemoteOps: (ops, meta) => deliveries.push({ op: ops[0], ...meta }) },
      });
      client.setBaseline({
        shape_overrides: { a: { builtin: 'later' } },
        type_shapes: { Thing: { builtin: 'later' } },
        shape_revisions: { 'node:a': nodeRev, 'type:Thing': typeRev },
      });
      client._handleEvent({
        type: 'op',
        seq: 1,
        client_id: 'other',
        op: {
          op: 'type_shape_set',
          type: 'Thing',
          selection: { builtin: 'new' },
          clear_node_id: 'a',
          revisions: { 'type:Thing': 2, 'node:a': 2 },
        },
      });
      expect(deliveries[0].applyBaseline()).toBe(true);
      expect(client._baseline.type_shapes).toEqual(types);
      expect(client._baseline.shape_overrides).toEqual(overrides);
      expect(deliveries[0].op.op).toBe(typeRev > 2 ? 'node_shape_set' : 'type_shape_set');
      client.close();
    }
  );

  it('holds autosave and resync until a local edit completes', async () => {
    const onHeldSaveReleased = vi.fn();
    const { client } = makeClient({ handlers: { onHeldSaveReleased } });
    const release = client.holdLocalEdit();
    client.syncState({ node_refs: ['a'], positions: { a: { x: 1, y: 2 } } });
    let settled = false;
    const waiting = client.waitForLocalEdits().then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(client.getPendingOps()).toEqual([]);
    release();
    await waiting;
    expect(onHeldSaveReleased).toHaveBeenCalledOnce();
    expect(settled).toBe(true);
    client.close();
  });

  it('retains a newer revision when an older write conflicts', async () => {
    const onDropped = vi.fn();
    const fetchImpl = makeFetch([
      {
        ok: false,
        status: 409,
        json: async () => ({
          detail: { error: 'shape_revision_conflict', target: 'node:a', revision: 3 },
        }),
      },
    ]);
    const { client } = makeClient({ fetchImpl, handlers: { onDropped } });
    client.setBaseline({ node_refs: ['a'], shape_revisions: { 'node:a': 5 } });
    client.sendOps([
      {
        op: 'node_shape_set',
        node_id: 'a',
        version: 1,
        operation_id: 'one',
        base_revision: 2,
        selection: { builtin: true },
      },
    ]);
    await client.flush();
    expect(client._baseline.shape_revisions['node:a']).toBe(5);
    expect(client.getPendingOps()).toEqual([]);
    expect(onDropped).toHaveBeenCalledOnce();
    expect(fetchImpl.calls[0].body.appearance_version).toBe(1);
    client.close();
  });
});
