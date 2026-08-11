import { describe, it, expect } from 'vitest';
import {
  directNeighborIds,
  neighborStartPositions,
  neighborDragPositions,
} from '../src/utils/dragConnected';

describe('directNeighborIds', () => {
  const edges = [
    { source: 'a', target: 'b' },
    { source: 'a', target: 'c' },
    { source: 'd', target: 'b' },
    { source: 'e', target: 'f' },
  ];

  it('returns the nodes directly connected to the anchor, excluding the anchor', () => {
    const result = directNeighborIds(edges, new Set(['a']));
    expect(result).toEqual(new Set(['b', 'c']));
  });

  it('follows edges in both directions', () => {
    // b is reached as a target from a and as a target from d; anchor d yields b.
    expect(directNeighborIds(edges, new Set(['d']))).toEqual(new Set(['b']));
  });

  it('never includes a node that is itself being dragged', () => {
    const result = directNeighborIds(edges, new Set(['a', 'b']));
    // a and b are both anchors, so neither trails; c (via a) and d (via d->b)
    // are genuine external neighbours that follow.
    expect(result).toEqual(new Set(['c', 'd']));
  });

  it('returns an empty set for an isolated node', () => {
    expect(directNeighborIds(edges, new Set(['z'])).size).toBe(0);
  });

  it('tolerates missing edges', () => {
    expect(directNeighborIds(undefined, new Set(['a'])).size).toBe(0);
  });
});

describe('neighborStartPositions', () => {
  const nodes = [
    { id: 'a', type: 'default', position: { x: 0, y: 0 } },
    { id: 'b', type: 'default', position: { x: 10, y: 20 } },
    { id: 'c', type: 'default', position: { x: 30, y: 40 } },
    { id: 'note1', type: 'note', position: { x: 5, y: 5 } },
    { id: 'grp1', type: 'group', position: { x: 1, y: 1 } },
  ];

  it('snapshots positions for the requested neighbour ids', () => {
    const start = neighborStartPositions(nodes, new Set(['b', 'c']));
    expect(start.get('b')).toEqual({ x: 10, y: 20 });
    expect(start.get('c')).toEqual({ x: 30, y: 40 });
    expect(start.size).toBe(2);
  });

  it('excludes annotation and group nodes even if connected', () => {
    const start = neighborStartPositions(nodes, new Set(['note1', 'grp1', 'b']));
    expect(start.has('note1')).toBe(false);
    expect(start.has('grp1')).toBe(false);
    expect(start.has('b')).toBe(true);
  });

  it('skips a neighbour whose parent group is itself being dragged (no double-move)', () => {
    // C is a child of group G; when G is in the dragged set, ReactFlow already
    // moves C via its parent, so C must not also be translated by the delta.
    const withChild = [
      { id: 'b', type: 'default', position: { x: 10, y: 20 } },
      { id: 'c', type: 'default', parentId: 'G', position: { x: 5, y: 5 } },
    ];
    const draggedIds = new Set(['A', 'G']);
    const start = neighborStartPositions(withChild, new Set(['b', 'c']), draggedIds);
    expect(start.has('c')).toBe(false);
    expect(start.has('b')).toBe(true);
  });

  it('keeps a child neighbour when its parent is NOT being dragged', () => {
    const withChild = [{ id: 'c', type: 'default', parentId: 'G', position: { x: 5, y: 5 } }];
    const start = neighborStartPositions(withChild, new Set(['c']), new Set(['A']));
    expect(start.has('c')).toBe(true);
  });
});

describe('neighborDragPositions', () => {
  it('translates every neighbour by the anchor delta', () => {
    const start = new Map([
      ['b', { x: 10, y: 20 }],
      ['c', { x: 30, y: 40 }],
    ]);
    const updates = neighborDragPositions(start, { x: 0, y: 0 }, { x: 15, y: -5 });
    expect(updates.get('b')).toEqual({ x: 25, y: 15 });
    expect(updates.get('c')).toEqual({ x: 45, y: 35 });
  });

  it('is a no-op translation when the anchor has not moved', () => {
    const start = new Map([['b', { x: 10, y: 20 }]]);
    const updates = neighborDragPositions(start, { x: 7, y: 7 }, { x: 7, y: 7 });
    expect(updates.get('b')).toEqual({ x: 10, y: 20 });
  });

  it('returns an empty map when there are no neighbours', () => {
    expect(neighborDragPositions(new Map(), { x: 0, y: 0 }, { x: 5, y: 5 }).size).toBe(0);
  });
});
