// covers: frontend/web/src/utils/captureContents.js
import { expect, it, vi } from 'vitest';
import { buildCaptureContents, captureContents } from './captureContents';

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

it('reads after a successful flush and never reads after failure', async () => {
  let finish;
  const read = vi.fn(() => ({ baseline: { node_refs: ['new'] } }));
  const pending = captureContents(() => new Promise((resolve) => (finish = resolve)), read);
  expect(read).not.toHaveBeenCalled();
  finish();
  expect(await pending).toEqual({ node_ids: ['new'], positions: {} });
  read.mockClear();
  await expect(captureContents(() => Promise.reject(new Error()), read)).rejects.toThrow();
  expect(read).not.toHaveBeenCalled();
});
