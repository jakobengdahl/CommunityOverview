import { describe, it, expect, beforeEach, vi } from 'vitest';
import { computeOps, normalizeMirror, applyOpToMirror, SessionSyncClient } from '../src/services/sessionSyncClient';

// ── Fakes ────────────────────────────────────────────────────────────────
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }
  emit(obj) { this.onmessage?.({ data: JSON.stringify(obj) }); }
  error() { this.onerror?.(); }
  close() { this.closed = true; }
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

const flush = () => new Promise(r => setTimeout(r, 0));

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

beforeEach(() => { FakeEventSource.instances = []; });

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
    expect(ops.filter(o => o.op === 'node_moved')).toEqual([
      { op: 'node_moved', node_id: 'b', position: { x: 50, y: 5 } },
    ]);
  });

  it('collapses a bulk position change into a single layout_applied op', () => {
    const node_refs = Array.from({ length: 25 }, (_, i) => `n${i}`);
    const positions = {};
    node_refs.forEach((id, i) => { positions[id] = { x: i, y: i }; });
    const ops = computeOps({ node_refs, positions: {} }, { node_refs, positions });
    const layout = ops.filter(o => o.op === 'layout_applied');
    expect(layout).toHaveLength(1);
    expect(Object.keys(layout[0].positions)).toHaveLength(25);
    expect(ops.some(o => o.op === 'node_moved')).toBe(false);
  });

  it('emits hidden / shown set ops for nodes and edges', () => {
    const ops = computeOps(
      { hidden_node_ids: ['a'], hidden_edge_ids: ['e1'] },
      { hidden_node_ids: ['b'], hidden_edge_ids: [] },
    );
    expect(ops).toContainEqual({ op: 'nodes_hidden', node_ids: ['b'] });
    expect(ops).toContainEqual({ op: 'nodes_shown', node_ids: ['a'] });
    expect(ops).toContainEqual({ op: 'edges_shown', edge_ids: ['e1'] });
  });

  it('creates, updates and deletes annotations by id', () => {
    const prev = { annotations: [{ id: 'n1', kind: 'note', text: 'hi' }, { id: 'n2', kind: 'label', text: 'x' }] };
    const next = { annotations: [{ id: 'n1', kind: 'note', text: 'bye' }, { id: 'n3', kind: 'note', text: 'new' }] };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({ op: 'annotation_updated', annotation: { id: 'n1', kind: 'note', text: 'bye' } });
    expect(ops).toContainEqual({ op: 'annotation_created', annotation: { id: 'n3', kind: 'note', text: 'new' } });
    expect(ops).toContainEqual({ op: 'annotation_deleted', annotation_id: 'n2' });
  });

  it('routes group membership changes through group_membership_changed', () => {
    const prev = { annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a'] }] };
    const next = { annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a', 'b'] }] };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({ op: 'group_membership_changed', group_id: 'g1', member_node_ids: ['a', 'b'] });
    expect(ops.some(o => o.op === 'annotation_updated')).toBe(false);
  });

  it('emits annotation_updated for a group label change without a phantom membership op', () => {
    const prev = { annotations: [{ id: 'g1', kind: 'group', label: 'Old', member_node_ids: ['a'] }] };
    const next = { annotations: [{ id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] }] };
    const ops = computeOps(prev, next);
    expect(ops.some(o => o.op === 'group_membership_changed')).toBe(false);
    expect(ops).toContainEqual({ op: 'annotation_updated', annotation: { id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] } });
  });

  it('ignores updated_at churn on annotations', () => {
    const prev = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2020' }] };
    const next = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2021' }] };
    expect(computeOps(prev, next)).toEqual([]);
  });

  it('normalizeMirror dedupes and drops malformed positions', () => {
    const m = normalizeMirror({ node_refs: ['a', 'a', 'b'], positions: { a: { x: 1, y: 2 }, bad: { x: 'z' } } });
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
      node_refs: ['a', 'b'], positions: { a: { x: 1, y: 1 } }, hidden_node_ids: ['a'],
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
    m = applyOpToMirror(m, { op: 'annotation_created', annotation: { id: 'n1', kind: 'note', text: 'x' } });
    m = applyOpToMirror(m, { op: 'annotation_updated', annotation: { id: 'n1', kind: 'note', text: 'y' } });
    expect(m.annotations).toEqual([{ id: 'n1', kind: 'note', text: 'y' }]);
    m = applyOpToMirror(m, { op: 'annotation_created', annotation: { id: 'g', kind: 'group', member_node_ids: [] } });
    m = applyOpToMirror(m, { op: 'group_membership_changed', group_id: 'g', member_node_ids: ['a'] });
    expect(m.annotations.find(a => a.id === 'g').member_node_ids).toEqual(['a']);
    m = applyOpToMirror(m, { op: 'annotation_deleted', annotation_id: 'n1' });
    expect(m.annotations.map(a => a.id)).toEqual(['g']);
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

  it('forwards remote ops but suppresses echoes of its own client', async () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'op', client_id: 'client-me', op: { op: 'node_moved', node_id: 'a' }, seq: 1 });
    expect(onRemoteOps).not.toHaveBeenCalled();
    es.emit({ type: 'op', client_id: 'client-other', op: { op: 'node_moved', node_id: 'a' }, seq: 2 });
    expect(onRemoteOps).toHaveBeenCalledWith([{ op: 'node_moved', node_id: 'a' }], { clientId: 'client-other' });
    expect(client.seq).toBe(2);
  });

  it('advances seq from the ops POST response', async () => {
    const fetchImpl = makeFetch([{ ok: true, status: 200, json: async () => ({ applied: [], seq: 42 }) }]);
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
    await new Promise(r => setTimeout(r, 30)); // let retry fire
    expect(fetchImpl.calls.length).toBeGreaterThanOrEqual(2);
    expect(fetchImpl.calls[1].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });

    const dropFetch = makeFetch([{ ok: false, status: 400 }]);
    const onDropped = vi.fn();
    const { client: c2 } = makeClient({ fetchImpl: dropFetch, handlers: { onDropped } });
    c2.connect();
    FakeEventSource.instances[1].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    c2.syncState({ node_refs: ['x'] });
    await new Promise(r => setTimeout(r, 30));
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
    es.emit({ type: 'op', client_id: 'client-other', op: { op: 'nodes_added', node_ids: ['b'] }, seq: 1 });
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
    es.emit({ type: 'op', client_id: 'other', op: { op: 'node_moved', node_id: 'a', position: { x: 7, y: 8 } }, seq: 2 });
    expect(client.baselinePosition('a')).toEqual({ x: 7, y: 8 });
    expect(client.baselinePosition('missing')).toBeNull();
  });

  it('isolates a poison-pill op: a rejected multi-op batch resends singly, dropping only the bad op', async () => {
    const onDropped = vi.fn();
    // The server rejects any batch containing the annotation op; valid ops pass.
    const fetchImpl = vi.fn(async (url, opts) => {
      const ops = JSON.parse(opts.body).ops;
      if (ops.some(o => o.op === 'annotation_created')) return { ok: false, status: 400 };
      return { ok: true, status: 200, json: async () => ({ seq: 1 }) };
    });
    fetchImpl.calls = () => fetchImpl.mock.calls.map(([, o]) => JSON.parse(o.body).ops);
    const { client } = makeClient({ fetchImpl, handlers: { onDropped } });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], annotations: [{ id: 'n1', kind: 'note', text: 'x' }] });
    await new Promise(r => setTimeout(r, 60));
    // The valid nodes_added was delivered in its own batch; the annotation was dropped.
    const sent = fetchImpl.calls();
    expect(sent.some(ops => ops.length === 1 && ops[0].op === 'nodes_added')).toBe(true);
    expect(onDropped).toHaveBeenCalled();
    expect(onDropped.mock.calls.at(-1)[0][0].op).toBe('annotation_created');
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
      type: 'snapshot', seq: 0, session: { state: {} },
      roster, claims: { 'node-a': 'client-other', 'node-b': 'client-me' },
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
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: { 'node-a': 'client-other' } });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();

    es.emit({ type: 'presence_left', client_id: 'client-other' });
    expect(client.getRoster().map(m => m.client_id)).toEqual(['client-me']);
    // The departed member's claim marker is released.
    expect(client.getRemoteSelections()).toEqual({});

    es.emit({ type: 'presence_joined', member: { client_id: 'client-3', display_name: 'Zoe', color: '#3cb44b' } });
    expect(client.getRoster().map(m => m.client_id)).toContain('client-3');
  });

  it('tracks remote claim / release ops but ignores its own echoes', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });

    es.emit({ type: 'op', client_id: 'client-other', op: { op: 'selection_claimed', element_ids: ['node-a', 'node-b'] } });
    expect(Object.keys(client.getRemoteSelections()).sort()).toEqual(['node-a', 'node-b']);

    es.emit({ type: 'op', client_id: 'client-other', op: { op: 'selection_released', element_ids: ['node-a'] } });
    expect(Object.keys(client.getRemoteSelections())).toEqual(['node-b']);

    // Our own claim echo must not render as a remote marker.
    es.emit({ type: 'op', client_id: 'client-me', op: { op: 'selection_claimed', element_ids: ['node-c'] } });
    expect(client.getRemoteSelections()['node-c']).toBeUndefined();
  });

  it('setLocalSelection emits claim for added and release for removed ids', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });

    client.setLocalSelection(['node-a', 'node-b']);
    await flush();
    const first = fetchImpl.calls.at(-1).body.ops;
    expect(first).toContainEqual({ op: 'selection_claimed', element_ids: ['node-a', 'node-b'] });

    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    const ops = fetchImpl.calls.flatMap(c => c.body.ops);
    expect(ops).toContainEqual({ op: 'selection_released', element_ids: ['node-a'] });
    expect(ops).toContainEqual({ op: 'selection_claimed', element_ids: ['node-c'] });

    // Re-declaring the same selection is a no-op (no new ops).
    const before = fetchImpl.calls.length;
    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    expect(fetchImpl.calls.length).toBe(before);
  });

  it('expires a remote claim client-side once its TTL passes', () => {
    let now = 1000;
    const { client } = makeClient({ nowFn: () => now });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
    es.emit({ type: 'op', client_id: 'client-other', op: { op: 'selection_claimed', element_ids: ['node-a'] } });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();
    // Advance beyond the 30 s TTL: the claim no longer renders.
    now += 31_000;
    expect(client.getRemoteSelections()).toEqual({});
  });
});
