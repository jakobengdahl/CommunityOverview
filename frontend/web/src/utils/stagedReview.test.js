import { describe, it, expect } from 'vitest';
import {
  allowedChoices,
  buildMergeRequest,
  dependants,
  dependencyClosure,
  heldByDroppedCreate,
  isResolved,
  itemFingerprint,
  itemLabel,
  mergeOutcomeUnknown,
  nextMergeAttempt,
  parseManualValue,
  reviewDrift,
  stagedStatesFromDiff,
  unresolvedKeys,
  withStatuses,
} from './stagedReview';

const nodeCreate = {
  kind: 'node',
  entity_id: 'n1',
  action: 'create',
  status: 'clean',
  fields: { name: { base: null, staged: 'New one', main: null, conflict: false } },
  depends_on: [],
  session_seq: 3,
};
const edgeCreate = {
  kind: 'edge',
  entity_id: 'e1',
  action: 'create',
  status: 'clean',
  fields: {},
  depends_on: [{ kind: 'node', entity_id: 'n1' }],
  session_seq: 4,
};
const conflictUpdate = {
  kind: 'node',
  entity_id: 'n2',
  action: 'update',
  status: 'conflict',
  fields: {
    description: { base: 'a', staged: 'b', main: 'c', conflict: true },
    tags: { base: [], staged: ['x'], main: [], conflict: false },
  },
  depends_on: [],
  session_seq: 5,
};
const items = [nodeCreate, edgeCreate, conflictUpdate];

describe('canvas markers from the diff', () => {
  it('maps each entry to how it shows on the canvas', () => {
    const orphan = { ...conflictUpdate, entity_id: 'n3', status: 'orphaned' };
    const tomb = { kind: 'edge', entity_id: 'e9', action: 'delete', status: 'clean' };
    expect(stagedStatesFromDiff([...items, orphan, tomb])).toEqual({
      nodes: { n1: 'created', n2: 'updated', n3: 'orphaned' },
      edges: { e1: 'created', e9: 'deleted' },
    });
  });
});

describe('selection stays closed under dependencies', () => {
  it('ticking an edge ticks the node create it needs', () => {
    expect([...dependencyClosure(items, ['edge:e1'])].sort()).toEqual(['edge:e1', 'node:n1']);
  });

  it('ticking an entry ticks its dependencies transitively', () => {
    const chain = [
      { ...edgeCreate, entity_id: 'a', depends_on: [{ kind: 'edge', entity_id: 'b' }] },
      { ...edgeCreate, entity_id: 'b', depends_on: [{ kind: 'node', entity_id: 'c' }] },
      { ...nodeCreate, entity_id: 'c' },
    ];
    expect([...dependencyClosure(chain, ['edge:a'])].sort()).toEqual([
      'edge:a',
      'edge:b',
      'node:c',
    ]);
  });

  it('unticking a node unticks what depends on it, transitively', () => {
    const chained = [
      ...items,
      { ...edgeCreate, entity_id: 'e2', depends_on: [{ kind: 'edge', entity_id: 'e1' }] },
    ];
    expect([...dependants(chained, 'node:n1')].sort()).toEqual(['edge:e1', 'edge:e2']);
  });
});

describe('resolutions', () => {
  it('offers only what §11.2 allows for the status and action', () => {
    expect(allowedChoices(nodeCreate)).toEqual([]);
    expect(allowedChoices(conflictUpdate)).toEqual([
      'take_session',
      'keep_main',
      'as_new',
      'manual',
    ]);
    const conflictDelete = { ...conflictUpdate, action: 'delete' };
    expect(allowedChoices(conflictDelete)).toEqual(['take_session', 'keep_main']);
    const edgeConflict = { ...conflictUpdate, kind: 'edge' };
    expect(allowedChoices(edgeConflict)).toEqual(['take_session', 'keep_main', 'manual']);
    const idTaken = { ...nodeCreate, status: 'id_taken' };
    expect(allowedChoices(idTaken)).toEqual(['take_session', 'keep_main', 'as_new']);
  });

  it('keeps a dangling edge only as the dependant of a create dropped with keep_main', () => {
    const dangling = { ...edgeCreate, status: 'dangling' };
    expect(allowedChoices(dangling)).toEqual(['keep_main']);
    expect(allowedChoices(dangling, true)).toEqual(['take_session', 'keep_main']);
    expect(heldByDroppedCreate(dangling, items, { 'node:n1': { choice: 'keep_main' } })).toBe(true);
    expect(heldByDroppedCreate(dangling, items, {})).toBe(false);
    expect(
      unresolvedKeys([nodeCreate, dangling], new Set(['edge:e1']), {
        'node:n1': { choice: 'keep_main' },
        'edge:e1': { choice: 'take_session' },
      })
    ).toEqual([]);
    expect(
      unresolvedKeys([nodeCreate, dangling], new Set(['edge:e1']), {
        'edge:e1': { choice: 'take_session' },
      })
    ).toEqual(['edge:e1']);
  });

  it('judges with the statuses a merge_blocked answer gave', () => {
    const judged = withStatuses(items, new Map([['edge:e1', 'dangling']]));
    expect(judged.find((i) => i.entity_id === 'e1').status).toBe('dangling');
    expect(withStatuses(items, new Map())).toBe(items);
  });

  it('a manual resolution covers an entry only with a value for every conflicting field', () => {
    expect(isResolved(conflictUpdate, { choice: 'manual', values: {} })).toBe(false);
    expect(isResolved(conflictUpdate, { choice: 'manual', values: { description: '' } })).toBe(
      true
    );
    expect(isResolved(conflictUpdate, { choice: 'as_new' })).toBe(true);
    const twoConflicts = {
      ...conflictUpdate,
      fields: {
        description: { base: 'a', staged: 'b', main: 'c', conflict: true },
        name: { base: 'n', staged: 'm', main: 'o', conflict: true },
      },
    };
    expect(isResolved(twoConflicts, { choice: 'manual', values: { description: 'x' } })).toBe(
      false
    );
    expect(isResolved({ ...conflictUpdate, kind: 'edge' }, { choice: 'as_new' })).toBe(false);
    expect(isResolved(conflictUpdate, undefined)).toBe(false);
    expect(isResolved(nodeCreate, undefined)).toBe(true);
  });

  it('lists the selected entries still needing a decision', () => {
    const selected = new Set(['node:n1', 'node:n2']);
    expect(unresolvedKeys(items, selected, {})).toEqual(['node:n2']);
  });
});

describe('the merge request', () => {
  it('names exactly the reviewed entries, never all', () => {
    const request = buildMergeRequest(items, new Set(['node:n1', 'edge:e1']), {});
    expect(request).toEqual({
      entries: [
        { kind: 'node', entity_id: 'n1' },
        { kind: 'edge', entity_id: 'e1' },
      ],
    });
  });

  it('sends manual values for the conflicting fields only', () => {
    const request = buildMergeRequest(items, new Set(['node:n2']), {
      'node:n2': { choice: 'manual', values: { description: 'mine', tags: ['ignored'] } },
    });
    expect(request.resolutions).toEqual([
      { kind: 'node', entity_id: 'n2', choice: 'manual', values: { description: 'mine' } },
    ]);
  });

  it('drops a resolution left on a clean or unselected entry', () => {
    const request = buildMergeRequest(items, new Set(['node:n1']), {
      'node:n1': { choice: 'keep_main' },
      'node:n2': { choice: 'take_session' },
    });
    expect(request.resolutions).toBeUndefined();
  });
});

describe('review drift', () => {
  const ticked = (...list) =>
    new Map(list.map((item) => [`${item.kind}:${item.entity_id}`, itemFingerprint(item)]));

  it('flags an entry restaged since it was ticked', () => {
    const fresh = [nodeCreate, edgeCreate, { ...conflictUpdate, session_seq: 9 }];
    expect(reviewDrift(ticked(conflictUpdate, nodeCreate), fresh)).toEqual(['node:n2']);
  });

  it('flags an entry the main graph now judges differently', () => {
    const fresh = [{ ...nodeCreate, status: 'id_taken' }, edgeCreate, conflictUpdate];
    expect(reviewDrift(ticked(nodeCreate), fresh)).toEqual(['node:n1']);
  });

  it('flags an entry that is gone, and ignores changes to entries not ticked', () => {
    expect(reviewDrift(ticked(nodeCreate), [conflictUpdate])).toEqual(['node:n1']);
    const fresh = [nodeCreate, edgeCreate, { ...conflictUpdate, session_seq: 9 }];
    expect(reviewDrift(ticked(nodeCreate), fresh)).toEqual([]);
  });

  it('flags a changed value, dependency, action or unstaged edge', () => {
    const variants = [
      {
        ...conflictUpdate,
        fields: { description: { ...conflictUpdate.fields.description, staged: 'x' } },
      },
      { ...conflictUpdate, depends_on: [{ kind: 'node', entity_id: 'n1' }] },
      { ...conflictUpdate, action: 'delete' },
      { ...conflictUpdate, unstaged_edges: ['e7'] },
    ];
    for (const changed of variants) {
      expect(reviewDrift(ticked(conflictUpdate), [changed])).toEqual(['node:n2']);
    }
  });

  it('measures from when the entry was ticked, not from the latest live read', () => {
    const restaged = { ...nodeCreate, session_seq: 7 };
    expect(reviewDrift(ticked(nodeCreate), [restaged])).toEqual(['node:n1']);
  });
});

describe('merge ids', () => {
  const newIds = () => {
    let n = 0;
    return () => `m${++n}`;
  };

  it('reuses the id only to retry the same request whose outcome is unknown', () => {
    const ids = newIds();
    const request = { entries: [{ kind: 'node', entity_id: 'n1' }] };
    const first = nextMergeAttempt(null, request, ids);
    first.outcomeUnknown = true;
    expect(nextMergeAttempt(first, request, ids).mergeId).toBe(first.mergeId);
  });

  it('takes a fresh id for a different request, even after an unknown outcome', () => {
    const ids = newIds();
    const first = nextMergeAttempt(null, { entries: [{ kind: 'node', entity_id: 'n1' }] }, ids);
    first.outcomeUnknown = true;
    const second = nextMergeAttempt(first, { entries: [{ kind: 'node', entity_id: 'n2' }] }, ids);
    expect(second.mergeId).not.toBe(first.mergeId);
  });

  it('takes a fresh id after a definite answer', () => {
    const ids = newIds();
    const request = { entries: [] };
    const first = nextMergeAttempt(null, request, ids);
    expect(nextMergeAttempt(first, request, ids).mergeId).not.toBe(first.mergeId);
  });

  it('treats a network failure or a server error as possibly landed, a refusal as not', () => {
    expect(mergeOutcomeUnknown(new TypeError('Failed to fetch'))).toBe(true);
    expect(mergeOutcomeUnknown(Object.assign(new Error('x'), { status: 503 }))).toBe(true);
    expect(mergeOutcomeUnknown(Object.assign(new Error('x'), { status: 409 }))).toBe(false);
    expect(mergeOutcomeUnknown(Object.assign(new Error('x'), { status: 422 }))).toBe(false);
  });
});

describe('display helpers', () => {
  it('labels an entry by its staged name, else the canvas, else its id', () => {
    expect(itemLabel(nodeCreate)).toBe('New one');
    expect(itemLabel(edgeCreate, () => 'A → B')).toBe('A → B');
    expect(itemLabel(edgeCreate, () => null)).toBe('e1');
  });

  it('parses a manual value as text for a text field and as JSON otherwise', () => {
    expect(parseManualValue('hello', 'old')).toEqual({ ok: true, value: 'hello' });
    expect(parseManualValue('["a"]', ['b'])).toEqual({ ok: true, value: ['a'] });
    expect(parseManualValue('[oops', ['b'])).toEqual({ ok: false });
  });
});
