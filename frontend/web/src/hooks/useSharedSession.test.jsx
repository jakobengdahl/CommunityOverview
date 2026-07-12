import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import * as api from '../services/api';
import { serverStateToMirror, useSharedSession } from './useSharedSession';

vi.mock('../services/api', () => ({
  getSession: vi.fn(),
}));

const NODE_A = { id: 'node-a', type: 'Actor', name: 'Actor A' };

function makeDeps(overrides = {}) {
  return {
    clearVisualization: vi.fn(),
    addNodesToVisualization: vi.fn(),
    setHiddenNodeIds: vi.fn(),
    setHiddenEdgeIds: vi.fn(),
    setPendingGroups: vi.fn(),
    setPendingAnnotations: vi.fn(),
    ensureSyncConnected: vi.fn(() => ({ setBaseline: vi.fn(), sessionId: null })),
    syncRef: { current: null },
    ...overrides,
  };
}

describe('serverStateToMirror', () => {
  it('uses the resolved node ids for node_refs, not the raw state refs', () => {
    const mirror = serverStateToMirror(
      { node_refs: ['stale'], positions: { 'node-a': { x: 1, y: 2 } } },
      ['node-a']
    );
    expect(mirror.node_refs).toEqual(['node-a']);
    expect(mirror.positions).toEqual({ 'node-a': { x: 1, y: 2 } });
  });

  it('falls back to state node_refs when no resolved ids are passed', () => {
    const mirror = serverStateToMirror({ node_refs: ['x'] }, undefined);
    expect(mirror.node_refs).toEqual(['x']);
  });

  it('translates group and overlay annotations through the shared transforms', () => {
    const mirror = serverStateToMirror(
      {
        annotations: [
          {
            id: 'g1',
            kind: 'group',
            label: 'G',
            position: { x: 0, y: 0 },
            member_node_ids: ['node-a'],
          },
          { id: 'n1', kind: 'note', position: { x: 3, y: 4 }, text: 'hi' },
        ],
      },
      ['node-a']
    );
    const kinds = mirror.annotations.map((a) => a.kind).sort();
    expect(kinds).toEqual(['group', 'note']);
    const group = mirror.annotations.find((a) => a.kind === 'group');
    expect(group.member_node_ids).toEqual(['node-a']);
  });

  it('tolerates a null state', () => {
    expect(serverStateToMirror(null, [])).toMatchObject({ node_refs: [], annotations: [] });
  });
});

describe('useSharedSession.applyServerSession', () => {
  it('clears then applies resolved nodes with saved positions, edges, and annotations', () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useSharedSession(deps));

    act(() => {
      result.current.applyServerSession({
        state: {
          positions: { 'node-a': { x: 9, y: 9 } },
          hidden_node_ids: ['h1'],
          annotations: [{ id: 'g1', kind: 'group', member_node_ids: [] }],
        },
        resolved: { nodes: [NODE_A], edges: [{ id: 'e1', source: 'node-a', target: 'node-a' }] },
      });
    });

    expect(deps.clearVisualization).toHaveBeenCalledTimes(1);
    const [positioned, edges] = deps.addNodesToVisualization.mock.calls[0];
    expect(positioned[0]._savedPosition).toEqual({ x: 9, y: 9 });
    expect(edges).toEqual([{ id: 'e1', source: 'node-a', target: 'node-a' }]);
    expect(deps.setHiddenNodeIds).toHaveBeenCalledWith(['h1']);
    expect(deps.setPendingGroups).toHaveBeenCalled();
  });

  // Regression (SMALL_FIXES 2026-07-10): a truthy non-array resolved.edges must
  // fail *before* any mutating call, not blow up inside addNodesToVisualization
  // after clearVisualization() has already run (half-applied switch).
  it('throws before clearing the canvas when resolved.edges is a non-array', () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useSharedSession(deps));

    expect(() =>
      result.current.applyServerSession({
        state: {},
        resolved: { nodes: [NODE_A], edges: { malformed: true } },
      })
    ).toThrow(/resolved\.edges/);

    expect(deps.clearVisualization).not.toHaveBeenCalled();
    expect(deps.addNodesToVisualization).not.toHaveBeenCalled();
  });

  it('throws before clearing the canvas when resolved.nodes is a non-array', () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useSharedSession(deps));

    expect(() =>
      result.current.applyServerSession({ state: {}, resolved: { nodes: { bad: 1 }, edges: [] } })
    ).toThrow(/resolved\.nodes/);
    expect(deps.clearVisualization).not.toHaveBeenCalled();
  });

  it('coerces nullish resolved lists to empty and still clears', () => {
    const deps = makeDeps();
    const { result } = renderHook(() => useSharedSession(deps));
    act(() => {
      result.current.applyServerSession({ state: {}, resolved: {} });
    });
    expect(deps.clearVisualization).toHaveBeenCalledTimes(1);
    expect(deps.addNodesToVisualization).not.toHaveBeenCalled();
  });
});

describe('useSharedSession.loadSessionFromServer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads the resolved session and seeds the sync baseline', async () => {
    const setBaseline = vi.fn();
    const deps = makeDeps({ ensureSyncConnected: vi.fn(() => ({ setBaseline, sessionId: null })) });
    api.getSession.mockResolvedValueOnce({
      state: { positions: {}, annotations: [] },
      resolved: { nodes: [NODE_A], edges: [] },
    });
    const { result } = renderHook(() => useSharedSession(deps));

    await act(async () => {
      await result.current.loadSessionFromServer('1234-5678');
    });

    expect(api.getSession).toHaveBeenCalledWith('1234-5678', { resolve: true });
    expect(deps.clearVisualization).toHaveBeenCalledTimes(1);
    expect(deps.addNodesToVisualization).toHaveBeenCalled();
    expect(setBaseline).toHaveBeenCalledWith(expect.objectContaining({ node_refs: ['node-a'] }));
  });

  it('treats a 404 as an empty session and seeds an empty eager baseline', async () => {
    const setBaseline = vi.fn();
    const deps = makeDeps({ ensureSyncConnected: vi.fn(() => ({ setBaseline, sessionId: null })) });
    const err = new Error('not found');
    err.status = 404;
    api.getSession.mockRejectedValueOnce(err);
    const { result } = renderHook(() => useSharedSession(deps));

    await act(async () => {
      await result.current.loadSessionFromServer('1234-5678', { eagerConnect: true });
    });

    expect(deps.clearVisualization).toHaveBeenCalledTimes(1);
    expect(setBaseline).toHaveBeenCalledWith({});
  });

  it('re-throws a non-404 load error without clearing the canvas', async () => {
    const deps = makeDeps();
    const err = new Error('boom');
    err.status = 500;
    api.getSession.mockRejectedValueOnce(err);
    const { result } = renderHook(() => useSharedSession(deps));

    await expect(
      act(async () => {
        await result.current.loadSessionFromServer('1234-5678');
      })
    ).rejects.toThrow('boom');
    expect(deps.clearVisualization).not.toHaveBeenCalled();
  });

  // Regression (SMALL_FIXES 2026-07-10): a malformed resolved.edges from the
  // server must fail the switch atomically — the current canvas stays untouched
  // (no clearVisualization) rather than being half-cleared.
  it('fails atomically on a malformed resolved.edges payload', async () => {
    const deps = makeDeps();
    api.getSession.mockResolvedValueOnce({
      state: { annotations: [] },
      resolved: { nodes: [NODE_A], edges: { malformed: true } },
    });
    const { result } = renderHook(() => useSharedSession(deps));

    await expect(
      act(async () => {
        await result.current.loadSessionFromServer('1234-5678');
      })
    ).rejects.toThrow(/resolved\.edges/);
    expect(deps.clearVisualization).not.toHaveBeenCalled();
    expect(deps.addNodesToVisualization).not.toHaveBeenCalled();
  });
});
