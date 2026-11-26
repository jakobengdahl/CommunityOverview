// covers: frontend/web/src/utils/captureContents.js, frontend/web/src/services/sessionSyncClient.js
import { expect, it, vi } from 'vitest';
import { SessionSyncClient } from '../services/sessionSyncClient';
import { buildCaptureContents, captureContents, readyBaseline } from './captureContents';
it('keeps baseline members and positions, filling only missing positions from the canvas', () => {
  const baseline = {
    node_refs: ['hidden', 'unresolved', 'computed', 'unplaced'],
    positions: { hidden: { x: 1, y: 2 }, unresolved: { x: 3, y: 4 } },
    hidden_node_ids: ['hidden'],
    edges: [{ id: 'edge' }],
  };
  const nodes = ['hidden', 'computed', 'extra'].map((id) => ({ id, position: { x: 5, y: 6 } }));
  expect(buildCaptureContents({ baseline, nodes })).toEqual({
    node_ids: baseline.node_refs,
    positions: { ...baseline.positions, computed: { x: 5, y: 6 } },
  });
});

class FakeEventSource {
  constructor() {
    FakeEventSource.last = this;
  }
  close() {}
}

// A real client: seeded as each host call site seeds it, then driven by its stream.
function client(seed, ...events) {
  const sync = new SessionSyncClient({
    sessionId: 's',
    streamUrl: '/stream',
    opsUrl: '/ops',
    fetchImpl: vi.fn(),
    EventSourceImpl: FakeEventSource,
    handlers: { onResync: vi.fn() },
  });
  if (seed) sync.setBaseline(...seed);
  sync.connect();
  for (const event of events) {
    if (event === 'drop') FakeEventSource.last.onerror();
    else FakeEventSource.last.onmessage({ data: JSON.stringify(event) });
  }
  sync.close();
  return sync;
}

const placeholder = [{}, { seq: 0 }];
const loaded = [{ node_refs: ['hidden'], positions: { hidden: { x: 7, y: 8 } } }, { seq: 4 }];
const snapshot = { type: 'snapshot', seq: 4 };
const store = { node_ids: ['a', 'b'], positions: { a: { x: 1, y: 2 } } };
const baseline = { node_ids: ['hidden'], positions: { hidden: { x: 7, y: 8 } } };

it.each([
  ['no sync client', () => null, store],
  [
    'a placeholder an empty session confirmed',
    () => client(placeholder, { type: 'snapshot', seq: 0 }),
    { node_ids: [], positions: {} },
  ],
  ['a loaded baseline before the first snapshot', () => client(loaded), baseline],
  ['a loaded baseline once the client is ready', () => client(loaded, snapshot), baseline],
  ['a loaded baseline after the stream dropped', () => client(loaded, snapshot, 'drop'), baseline],
])('captures from %s', (_label, makeSync, expected) => {
  const nodes = [
    { id: 'a', position: { x: 1, y: 2 } },
    { id: 'hidden', position: { x: 0, y: 0 } },
  ];
  expect(
    buildCaptureContents({ baseline: readyBaseline(makeSync()), members: ['a', 'b'], nodes })
  ).toEqual(expected);
});

it.each([
  ['a placeholder seeded before the stream delivered', () => client(placeholder)],
  ['an unseeded client once the stream delivered', () => client(null, snapshot)],
])('fails the capture from %s', (_label, makeSync) => {
  expect(() => readyBaseline(makeSync())).toThrow();
});

it('captures a placeholder-seeded session only once its resync reload lands', async () => {
  const sync = client(placeholder, { type: 'snapshot', seq: 3 });
  expect(sync.handlers.onResync).toHaveBeenCalled();
  const flush = (onReady) => new Promise((resolve) => resolve(onReady()));
  const read = () => ({ baseline: readyBaseline(sync), members: ['local'] });
  const saved = vi.fn();
  await expect(captureContents(flush, read).then(saved)).rejects.toThrow();
  expect(saved).not.toHaveBeenCalled();
  sync.setBaseline({ node_refs: ['server'], positions: {} }, { seq: 3 });
  expect(await captureContents(flush, read)).toEqual({ node_ids: ['server'], positions: {} });
});

it('reads after a successful flush and never reads after failure', async () => {
  let finish;
  let member = 'new';
  const read = vi.fn(() => ({ baseline: { node_refs: [member] } }));
  const flush = (onReady) => new Promise((resolve) => (finish = () => resolve(onReady())));
  const pending = captureContents(flush, read);
  expect(read).not.toHaveBeenCalled();
  finish();
  member = 'later';
  expect(await pending).toEqual({ node_ids: ['new'], positions: {} });
  read.mockClear();
  await expect(captureContents(() => Promise.reject(new Error()), read)).rejects.toThrow();
  expect(read).not.toHaveBeenCalled();
});
