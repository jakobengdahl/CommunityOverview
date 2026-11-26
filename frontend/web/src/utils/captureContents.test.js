// covers: frontend/web/src/utils/captureContents.js
import { expect, it, vi } from 'vitest';
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

it.each([
  ['no sync client', null],
  ['a client whose stream has not delivered state', { _ready: false, captureBaseline: () => ({}) }],
])('captures the store members with %s', (_label, sync) => {
  const nodes = [{ id: 'a', position: { x: 1, y: 2 } }];
  expect(
    buildCaptureContents({ baseline: readyBaseline(sync), members: ['a', 'b'], nodes })
  ).toEqual({ node_ids: ['a', 'b'], positions: { a: { x: 1, y: 2 } } });
});
it('captures the baseline, not the store, once the client is ready', () => {
  const sync = { _ready: true, captureBaseline: () => ({ node_refs: [], positions: {} }) };
  expect(buildCaptureContents({ baseline: readyBaseline(sync), members: ['a'] })).toEqual({
    node_ids: [],
    positions: {},
  });
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
