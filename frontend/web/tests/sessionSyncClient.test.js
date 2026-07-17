import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  computeOps,
  normalizeMirror,
  applyOpToMirror,
  SessionSyncClient,
} from '../src/services/sessionSyncClient';

// ── Fakes ────────────────────────────────────────────────────────────────
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

const flush = () => new Promise((r) => setTimeout(r, 0));

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

// ── computeOps ───────────────────────────────────────────────────────────
describe('computeOps', () => {
  it('emits nodes_added and nodes_removed as set ops', () => {
    const ops = computeOps({ node_refs: ['a', 'b'] }, { node_refs: ['b', 'c'] });
    expect(ops).toContainEqual({ op: 'nodes_added', node_ids: ['c'] });
    expect(ops).toContainEqual({ op: 'nodes_removed', node_ids: ['a'] });
  });

  it('emits node_moved only for positions that actually changed', () => {
    const prev = { node_refs: ['a', 'b'], positions: { a: { x: 0, y: 0 }, b: { x: 5, y: 5 } } };
    const next = { node_refs: ['a', 'b'], positions: { a: { x: 0, y: 0.2 }, b: { x: 50, y: 5 } } };
    const ops = computeOps(prev, next);
    // 'a' moved less than epsilon → ignored; 'b' moved → one op
    expect(ops.filter((o) => o.op === 'node_moved')).toEqual([
      { op: 'node_moved', node_id: 'b', position: { x: 50, y: 5 } },
    ]);
  });

  it('collapses a bulk position change into a single layout_applied op', () => {
    const node_refs = Array.from({ length: 25 }, (_, i) => `n${i}`);
    const positions = {};
    node_refs.forEach((id, i) => {
      positions[id] = { x: i, y: i };
    });
    const ops = computeOps({ node_refs, positions: {} }, { node_refs, positions });
    const layout = ops.filter((o) => o.op === 'layout_applied');
    expect(layout).toHaveLength(1);
    expect(Object.keys(layout[0].positions)).toHaveLength(25);
    expect(ops.some((o) => o.op === 'node_moved')).toBe(false);
  });

  it('emits hidden / shown set ops for nodes and edges', () => {
    const ops = computeOps(
      { hidden_node_ids: ['a'], hidden_edge_ids: ['e1'] },
      { hidden_node_ids: ['b'], hidden_edge_ids: [] }
    );
    expect(ops).toContainEqual({ op: 'nodes_hidden', node_ids: ['b'] });
    expect(ops).toContainEqual({ op: 'nodes_shown', node_ids: ['a'] });
    expect(ops).toContainEqual({ op: 'edges_shown', edge_ids: ['e1'] });
  });

  it('creates, updates and deletes annotations by id', () => {
    const prev = {
      annotations: [
        { id: 'n1', kind: 'note', text: 'hi' },
        { id: 'n2', kind: 'label', text: 'x' },
      ],
    };
    const next = {
      annotations: [
        { id: 'n1', kind: 'note', text: 'bye' },
        { id: 'n3', kind: 'note', text: 'new' },
      ],
    };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({
      op: 'annotation_updated',
      annotation: { id: 'n1', kind: 'note', text: 'bye' },
    });
    expect(ops).toContainEqual({
      op: 'annotation_created',
      annotation: { id: 'n3', kind: 'note', text: 'new' },
    });
    expect(ops).toContainEqual({ op: 'annotation_deleted', annotation_id: 'n2' });
  });

  it('routes group membership changes through group_membership_changed', () => {
    const prev = { annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a'] }] };
    const next = {
      annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a', 'b'] }],
    };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({
      op: 'group_membership_changed',
      group_id: 'g1',
      member_node_ids: ['a', 'b'],
    });
    expect(ops.some((o) => o.op === 'annotation_updated')).toBe(false);
  });

  it('emits annotation_updated for a group label change without a phantom membership op', () => {
    const prev = {
      annotations: [{ id: 'g1', kind: 'group', label: 'Old', member_node_ids: ['a'] }],
    };
    const next = {
      annotations: [{ id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] }],
    };
    const ops = computeOps(prev, next);
    expect(ops.some((o) => o.op === 'group_membership_changed')).toBe(false);
    expect(ops).toContainEqual({
      op: 'annotation_updated',
      annotation: { id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] },
    });
  });

  it('ignores updated_at churn on annotations', () => {
    const prev = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2020' }] };
    const next = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2021' }] };
    expect(computeOps(prev, next)).toEqual([]);
  });

  it('normalizeMirror dedupes and drops malformed positions', () => {
    const m = normalizeMirror({
      node_refs: ['a', 'a', 'b'],
      positions: { a: { x: 1, y: 2 }, bad: { x: 'z' } },
    });
    expect(m.node_refs).toEqual(['a', 'b']);
    expect(m.positions).toEqual({ a: { x: 1, y: 2 } });
  });
});

// ── applyOpToMirror ─────────────────────────────────────────────────────────
describe('applyOpToMirror', () => {
  it('folds set ops and moves into the mirror', () => {
    let m = { node_refs: ['a'] };
    m = applyOpToMirror(m, { op: 'nodes_added', node_ids: ['b'] });
    m = applyOpToMirror(m, { op: 'node_moved', node_id: 'b', position: { x: 3, y: 4 } });
    m = applyOpToMirror(m, { op: 'nodes_hidden', node_ids: ['a'] });
    expect(m.node_refs).toEqual(['a', 'b']);
    expect(m.positions.b).toEqual({ x: 3, y: 4 });
    expect(m.hidden_node_ids).toEqual(['a']);
  });

  it('removing a node also drops its position, hidden flag and group membership', () => {
    const m0 = {
      node_refs: ['a', 'b'],
      positions: { a: { x: 1, y: 1 } },
      hidden_node_ids: ['a'],
      annotations: [{ id: 'g', kind: 'group', member_node_ids: ['a', 'b'] }],
    };
    const m = applyOpToMirror(m0, { op: 'nodes_removed', node_ids: ['a'] });
    expect(m.node_refs).toEqual(['b']);
    expect(m.positions).toEqual({});
    expect(m.hidden_node_ids).toEqual([]);
    expect(m.annotations[0].member_node_ids).toEqual(['b']);
  });

  it('upserts and deletes annotations and updates group membership', () => {
    let m = { annotations: [] };
    m = applyOpToMirror(m, {
      op: 'annotation_created',
      annotation: { id: 'n1', kind: 'note', text: 'x' },
    });
    m = applyOpToMirror(m, {
      op: 'annotation_updated',
      annotation: { id: 'n1', kind: 'note', text: 'y' },
    });
    expect(m.annotations).toEqual([{ id: 'n1', kind: 'note', text: 'y' }]);
    m = applyOpToMirror(m, {
      op: 'annotation_created',
      annotation: { id: 'g', kind: 'group', member_node_ids: [] },
    });
    m = applyOpToMirror(m, {
      op: 'group_membership_changed',
      group_id: 'g',
      member_node_ids: ['a'],
    });
    expect(m.annotations.find((a) => a.id === 'g').member_node_ids).toEqual(['a']);
    m = applyOpToMirror(m, { op: 'annotation_deleted', annotation_id: 'n1' });
    expect(m.annotations.map((a) => a.id)).toEqual(['g']);
  });
});

// ── SessionSyncClient transport ────────────────────────────────────────────
describe('SessionSyncClient', () => {
  it('opens the stream with client_id and buffers ops until the first snapshot', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toContain('client_id=client-me');

    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0); // not ready yet

    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    await flush();
    expect(fetchImpl.calls).toHaveLength(1);
    expect(fetchImpl.calls[0].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });
    expect(fetchImpl.calls[0].body.client_id).toBe('client-me');
  });

  it('calls onReady on the first snapshot and onResync on a later one', async () => {
    const onReady = vi.fn();
    const onResync = vi.fn();
    const { client } = makeClient({ handlers: { onReady, onResync } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 3, session: { state: {} } });
    expect(onReady).toHaveBeenCalledWith(3);
    expect(onResync).not.toHaveBeenCalled();
    es.emit({ type: 'snapshot', seq: 5, session: { state: {} } });
    expect(onResync).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(5);
  });

  it('forwards command events (MCP pushes broadcast via the hub, design R5)', async () => {
    const onCommand = vi.fn();
    const { client } = makeClient({ handlers: { onCommand } });
    client.connect();
    const es = FakeEventSource.instances[0];
    const command = {
      type: 'tool_result',
      tool: 'search_graph',
      result: { action: 'add_to_visualization' },
    };
    es.emit({ type: 'command', command });
    expect(onCommand).toHaveBeenCalledWith(command);
  });

  it('forwards remote ops but suppresses echoes of its own client', async () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'op', client_id: 'client-me', op: { op: 'node_moved', node_id: 'a' }, seq: 1 });
    expect(onRemoteOps).not.toHaveBeenCalled();
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'node_moved', node_id: 'a' },
      seq: 2,
    });
    expect(onRemoteOps).toHaveBeenCalledWith([{ op: 'node_moved', node_id: 'a' }], {
      clientId: 'client-other',
    });
    expect(client.seq).toBe(2);
  });

  it('advances seq from the ops POST response', async () => {
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ applied: [], seq: 42 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(client.seq).toBe(42);
  });

  it('setBaseline suppresses ops for state already on the server', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.setBaseline({ node_refs: ['a', 'b'] });
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('requeues ops on a 500 and drops them on a 400', async () => {
    const fetchImpl = makeFetch([
      { ok: false, status: 500 },
      { ok: true, status: 200, json: async () => ({ seq: 1 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'] });
    await new Promise((r) => setTimeout(r, 30)); // let retry fire
    expect(fetchImpl.calls.length).toBeGreaterThanOrEqual(2);
    expect(fetchImpl.calls[1].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });

    const dropFetch = makeFetch([{ ok: false, status: 400 }]);
    const onDropped = vi.fn();
    const { client: c2 } = makeClient({ fetchImpl: dropFetch, handlers: { onDropped } });
    c2.connect();
    FakeEventSource.instances[1].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    c2.syncState({ node_refs: ['x'] });
    await new Promise((r) => setTimeout(r, 30));
    expect(onDropped).toHaveBeenCalled();
    expect(dropFetch.calls).toHaveLength(1); // not retried
  });

  it('folds a remote op into the baseline so re-syncing the same state emits nothing (echo-safe)', async () => {
    const onRemoteOps = vi.fn();
    const { client, fetchImpl } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.setBaseline({ node_refs: ['a'] });
    // Another client adds node 'b'; the host applies it locally.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['b'] },
      seq: 1,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    // The host's resulting snapshot now contains 'b' — but it must not echo back.
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('exposes the baseline position a node was moved to (for placing async-added nodes)', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    es.emit({ type: 'op', client_id: 'other', op: { op: 'nodes_added', node_ids: ['a'] }, seq: 1 });
    es.emit({
      type: 'op',
      client_id: 'other',
      op: { op: 'node_moved', node_id: 'a', position: { x: 7, y: 8 } },
      seq: 2,
    });
    expect(client.baselinePosition('a')).toEqual({ x: 7, y: 8 });
    expect(client.baselinePosition('missing')).toBeNull();
  });

  it('isolates a poison-pill op: a rejected multi-op batch resends singly, dropping only the bad op', async () => {
    const onDropped = vi.fn();
    // The server rejects any batch containing the annotation op; valid ops pass.
    const fetchImpl = vi.fn(async (url, opts) => {
      const ops = JSON.parse(opts.body).ops;
      if (ops.some((o) => o.op === 'annotation_created')) return { ok: false, status: 400 };
      return { ok: true, status: 200, json: async () => ({ seq: 1 }) };
    });
    fetchImpl.calls = () => fetchImpl.mock.calls.map(([, o]) => JSON.parse(o.body).ops);
    const { client } = makeClient({ fetchImpl, handlers: { onDropped } });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], annotations: [{ id: 'n1', kind: 'note', text: 'x' }] });
    await new Promise((r) => setTimeout(r, 60));
    // The valid nodes_added was delivered in its own batch; the annotation was dropped.
    const sent = fetchImpl.calls();
    expect(sent.some((ops) => ops.length === 1 && ops[0].op === 'nodes_added')).toBe(true);
    expect(onDropped).toHaveBeenCalled();
    expect(onDropped.mock.calls.at(-1)[0][0].op).toBe('annotation_created');
  });

  it('teardown flush drains every queued op in force-single mode (close() loses none)', async () => {
    // First POST (the multi-op batch) is terminally rejected, flipping the client
    // into force-single recovery with the whole batch requeued.
    const fetchImpl = makeFetch([{ ok: false, status: 400 }]);
    // Long debounce so no automatic re-flush races the explicit teardown flush.
    const { client } = makeClient({ fetchImpl, flushIntervalMs: 60_000 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], hidden_node_ids: ['h'] }); // 2 ops in one batch
    await client.flush();
    expect(fetchImpl.calls).toHaveLength(1); // rejected batch, both ops requeued

    client.flush();
    client.close();
    await new Promise((r) => setTimeout(r, 10));
    // Both requeued ops left as their own single-op batches despite close().
    const singles = fetchImpl.calls.slice(1).map((c) => c.body.ops);
    expect(singles).toEqual([
      [{ op: 'nodes_added', node_ids: ['a'] }],
      [{ op: 'nodes_hidden', node_ids: ['h'] }],
    ]);
  });

  it('teardown flush drains the remainder even while a force-single op is in flight', async () => {
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    let call = 0;
    // Call 1: the multi-op batch, terminally rejected (enters force-single).
    // Call 2: the debounced single-op resend, held in flight across teardown.
    const fetchImpl = vi.fn(async (url, opts) => {
      fetchImpl.sent.push(JSON.parse(opts.body).ops);
      call += 1;
      if (call === 1) return { ok: false, status: 400 };
      if (call === 2) await gate;
      return { ok: true, status: 200, json: async () => ({ seq: 1 }) };
    });
    fetchImpl.sent = [];
    const { client } = makeClient({ fetchImpl, flushIntervalMs: 1 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], hidden_node_ids: ['h'], hidden_edge_ids: ['e'] }); // 3 ops
    await client.flush(); // batch rejected → force-single, all 3 requeued
    await new Promise((r) => setTimeout(r, 10)); // debounce resends op 1, which now hangs
    expect(fetchImpl.sent).toHaveLength(2);

    client.flush();
    client.close();
    await new Promise((r) => setTimeout(r, 10));
    release();
    expect(fetchImpl.sent.slice(2)).toEqual([
      [{ op: 'nodes_hidden', node_ids: ['h'] }],
      [{ op: 'edges_hidden', edge_ids: ['e'] }],
    ]);
  });

  it('teardown flush sends queued ops even if the stream never became ready', async () => {
    // A client can be swapped out (session switch) before its stream ever
    // delivers a first snapshot. flush() must not drop ops queued in that
    // window — only the debounced auto-flush should wait for _ready.
    const { client, fetchImpl } = makeClient({ flushIntervalMs: 60_000 });
    client.connect();
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0); // never-ready: auto-flush stays quiet

    await client.flush();
    expect(fetchImpl.calls).toHaveLength(1);
    expect(fetchImpl.calls[0].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });
  });

  it('dispatches session lifecycle events to handlers', () => {
    const onSessionRenamed = vi.fn();
    const onSessionDeleted = vi.fn();
    const { client } = makeClient({ handlers: { onSessionRenamed, onSessionDeleted } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'session_renamed', name: 'Team map' });
    es.emit({ type: 'session_deleted', deleted_by: 'client-other' });
    expect(onSessionRenamed).toHaveBeenCalledWith('Team map');
    expect(onSessionDeleted).toHaveBeenCalledWith('client-other');
  });

  it('passes since_seq once a seq is known and stops after close', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 7, session: { state: {} } });
    client.close();
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it('schedules a backoff reconnect when the handshake is rejected (readyState CLOSED)', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      expect(FakeEventSource.instances).toHaveLength(1);

      // Simulate a 429 handshake failure: EventSource moves to readyState CLOSED.
      FakeEventSource.instances[0].error(true);

      // The dead source must be torn down immediately.
      expect(client.connected).toBe(false);
      expect(FakeEventSource.instances[0].closed).toBe(true);

      // No new connection yet — reconnect is on a backoff timer.
      expect(FakeEventSource.instances).toHaveLength(1);

      // After the backoff fires a fresh EventSource is opened.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(client.connected).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not schedule a backoff reconnect for transient errors on an open stream', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });

      // Transient drop on already-open stream — native reconnect handles it.
      es.error(false);

      // Source is still registered (browser is reconnecting natively).
      expect(client.connected).toBe(true);
      // No new EventSource is created by our code.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels the scheduled backoff reconnect when close() is called', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      FakeEventSource.instances[0].error(true); // schedules reconnect
      client.close();

      // Reconnect timer was cancelled; no new connection fires.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('proactively chunks an oversized queue against the server batch caps (R9)', async () => {
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ seq: 500 }) },
      { ok: true, status: 200, json: async () => ({ seq: 600 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });

    // 600 distinct annotation_created ops — one op per annotation id — well
    // over the server's 500-op-per-batch cap but nowhere near the byte cap.
    const annotations = Array.from({ length: 600 }, (_, i) => ({
      id: `ann-${i}`,
      kind: 'note',
      text: 'x',
      position: { x: 0, y: 0 },
    }));
    client.syncState({ annotations });

    // Let the debounced flush chain run to completion (each flush's `finally`
    // reschedules the next one while the queue is non-empty).
    await new Promise((r) => setTimeout(r, 50));

    expect(fetchImpl.calls).toHaveLength(2);
    expect(fetchImpl.calls[0].body.ops).toHaveLength(500);
    expect(fetchImpl.calls[1].body.ops).toHaveLength(100);
  });

  it('ignores a duplicate or stale sequenced op event (R15)', () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 5, session: { state: {} } });

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['a'] },
      seq: 6,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);

    // A duplicate delivery of the same seq (e.g. queued before catch-up and
    // replayed again by catch-up) must not be re-applied.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['a'] },
      seq: 6,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);

    // A stale seq (older than what's already applied) is ignored too.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['b'] },
      seq: 4,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);
  });
});

// ── Presence + selection claims (design step 7) ────────────────────────────
describe('SessionSyncClient presence + claims', () => {
  const roster = [
    { client_id: 'client-me', display_name: 'Me', color: '#111' },
    { client_id: 'client-other', display_name: 'Ada', color: '#e6194b' },
  ];

  it('seeds roster and claims from a snapshot, excluding own claims', () => {
    const onPresence = vi.fn();
    const onSelections = vi.fn();
    const { client } = makeClient({ handlers: { onPresence, onSelections } });
    client.connect();
    FakeEventSource.instances[0].emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: { 'node-a': 'client-other', 'node-b': 'client-me' },
    });
    expect(onPresence).toHaveBeenLastCalledWith(roster);
    // Own claim on node-b is excluded; the remote claim is resolved to colour+name.
    expect(client.getRemoteSelections()).toEqual({
      'node-a': { clientId: 'client-other', color: '#e6194b', displayName: 'Ada' },
    });
    expect(onSelections).toHaveBeenLastCalledWith(client.getRemoteSelections());
  });

  it('adds and removes roster members on join / leave and drops departed claims', () => {
    const onPresence = vi.fn();
    const onSelections = vi.fn();
    const { client } = makeClient({ handlers: { onPresence, onSelections } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: { 'node-a': 'client-other' },
    });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();

    es.emit({ type: 'presence_left', client_id: 'client-other' });
    expect(client.getRoster().map((m) => m.client_id)).toEqual(['client-me']);
    // The departed member's claim marker is released.
    expect(client.getRemoteSelections()).toEqual({});

    es.emit({
      type: 'presence_joined',
      member: { client_id: 'client-3', display_name: 'Zoe', color: '#3cb44b' },
    });
    expect(client.getRoster().map((m) => m.client_id)).toContain('client-3');
  });

  it('tracks remote claim / release ops but ignores its own echoes', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_claimed', element_ids: ['node-a', 'node-b'] },
    });
    expect(Object.keys(client.getRemoteSelections()).sort()).toEqual(['node-a', 'node-b']);

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_released', element_ids: ['node-a'] },
    });
    expect(Object.keys(client.getRemoteSelections())).toEqual(['node-b']);

    // Our own claim echo must not render as a remote marker.
    es.emit({
      type: 'op',
      client_id: 'client-me',
      op: { op: 'selection_claimed', element_ids: ['node-c'] },
    });
    expect(client.getRemoteSelections()['node-c']).toBeUndefined();
  });

  it('setLocalSelection emits claim for added and release for removed ids', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: {},
    });

    client.setLocalSelection(['node-a', 'node-b']);
    await flush();
    const first = fetchImpl.calls.at(-1).body.ops;
    expect(first).toContainEqual({ op: 'selection_claimed', element_ids: ['node-a', 'node-b'] });

    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    const ops = fetchImpl.calls.flatMap((c) => c.body.ops);
    expect(ops).toContainEqual({ op: 'selection_released', element_ids: ['node-a'] });
    expect(ops).toContainEqual({ op: 'selection_claimed', element_ids: ['node-c'] });

    // Re-declaring the same selection is a no-op (no new ops).
    const before = fetchImpl.calls.length;
    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    expect(fetchImpl.calls.length).toBe(before);
  });

  it('re-advertises the local selection on reconnect (server dropped it on disconnect)', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
    client.setLocalSelection(['node-a']);
    await flush();

    // Reconnect delivers a catch_up: the local selection is re-advertised so
    // other clients get the marker back.
    es.emit({ type: 'catch_up', seq: 1, ops: [], roster, claims: {} });
    await flush();
    const reclaims = fetchImpl.calls
      .flatMap((c) => c.body.ops)
      .filter((o) => o.op === 'selection_claimed' && o.element_ids.includes('node-a'));
    expect(reclaims.length).toBeGreaterThanOrEqual(2); // initial claim + reconnect re-advertise
  });

  it('does not queue renewal claims while the stream is disconnected', () => {
    vi.useFakeTimers();
    try {
      const { client, fetchImpl } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
      client.setLocalSelection(['node-a']);
      vi.advanceTimersByTime(5); // let the initial claim flush
      const afterClaim = fetchImpl.calls.length;
      expect(afterClaim).toBeGreaterThan(0);

      es.error(); // stream drops → not ready
      vi.advanceTimersByTime(60_000); // several renewal intervals pass
      // No new ops were sent while disconnected (renewals are skipped).
      expect(fetchImpl.calls.length).toBe(afterClaim);
    } finally {
      vi.useRealTimers();
    }
  });

  it('expires a remote claim client-side once its TTL passes', () => {
    let now = 1000;
    const { client } = makeClient({ nowFn: () => now });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_claimed', element_ids: ['node-a'] },
    });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();
    // Advance beyond the 30 s TTL: the claim no longer renders.
    now += 31_000;
    expect(client.getRemoteSelections()).toEqual({});
  });
});
