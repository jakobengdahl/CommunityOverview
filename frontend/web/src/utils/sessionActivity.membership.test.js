// covers: frontend/web/src/utils/sessionActivity.js
import { it, expect } from 'vitest';
import { describeActivity, isUndoableRecord } from './sessionActivity.js';

it.each(['nodes_added', 'nodes_removed'])('describes %s with its total count and no undo', (op) => {
  const record = { op, affected: { count: 105, ids: ['sample'] }, inverse_op: null };
  expect(describeActivity(record)).toEqual({ key: `history.desc.${op}`, params: { count: 105 } });
  expect(isUndoableRecord(record)).toBe(false);
});
