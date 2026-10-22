import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

vi.mock('../services/api', () => ({
  setSessionStaged: vi.fn(),
  setSessionModeUnknown: vi.fn(),
  setSessionModeDirect: vi.fn(),
  setSessionOnServer: vi.fn(),
  getStagedChanges: vi.fn(),
  subscribeStagedMarks: vi.fn((listener) => {
    stagedMarkListeners.add(listener);
    return () => stagedMarkListeners.delete(listener);
  }),
}));
const stagedMarkListeners = new Set();

import * as api from '../services/api';
import { useStagedSession } from './useStagedSession';

const ITEM = {
  kind: 'node',
  entity_id: 'n1',
  action: 'create',
  status: 'clean',
  fields: {},
  depends_on: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('useStagedSession', () => {
  it('learns a staged session from the server and binds requests to it at once', async () => {
    api.getStagedChanges.mockResolvedValue({ items: [ITEM] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    expect(result.current.writeMode).toBe('direct');
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    expect(api.setSessionStaged).toHaveBeenCalledWith('1111-2222', true);
    expect(result.current.writeMode).toBe('staged');
    await waitFor(() => expect(result.current.pendingCount).toBe(1));
    expect(result.current.stagedStates.nodes).toEqual({ n1: 'created' });
  });

  it('never turns a staged session back into a direct one', () => {
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    api.getStagedChanges.mockResolvedValue({ items: [] });
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'direct' }));
    expect(result.current.writeMode).toBe('staged');
    expect(api.setSessionStaged).not.toHaveBeenCalledWith('1111-2222', false);
  });

  it('follows a write_mode_changed op from another participant', async () => {
    api.getStagedChanges.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    let routed;
    act(() => {
      routed = result.current.handleSessionOp({ op: 'write_mode_changed', write_mode: 'staged' });
    });
    expect(routed).toBe(true);
    expect(result.current.writeMode).toBe('staged');
    expect(result.current.handleSessionOp({ op: 'nodes_added', node_ids: [] })).toBe(false);
  });

  it('shows nothing of one session in another, a late answer included', async () => {
    let resolveFirst;
    api.getStagedChanges.mockImplementationOnce(
      () => new Promise((resolve) => (resolveFirst = resolve))
    );
    const { result, rerender } = renderHook(({ id }) => useStagedSession(id), {
      initialProps: { id: '1111-2222' },
    });
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    rerender({ id: '3333-4444' });
    await act(async () => resolveFirst({ items: [ITEM] }));
    expect(result.current.writeMode).toBe('direct');
    expect(result.current.items).toEqual([]);
    expect(result.current.pendingCount).toBe(0);
  });

  it('clears the items when the diff cannot be read, rather than keeping old ones', async () => {
    api.getStagedChanges.mockResolvedValueOnce({ items: [ITEM] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    await waitFor(() => expect(result.current.pendingCount).toBe(1));
    api.getStagedChanges.mockRejectedValueOnce(Object.assign(new Error('no'), { status: 403 }));
    await act(async () => {
      await result.current.refreshDiff();
    });
    expect(result.current.items).toEqual([]);
    expect(result.current.diffError).toBeTruthy();
  });

  it('re-reads the diff after a staged-change op, without using its payload', async () => {
    vi.useFakeTimers();
    api.getStagedChanges.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    await act(async () => {});
    api.getStagedChanges.mockClear();
    api.getStagedChanges.mockResolvedValue({ items: [ITEM] });
    act(() => {
      result.current.handleSessionOp({
        op: 'graph_change_staged',
        entries: [{ kind: 'node', entity_id: 'leaked', staged: { name: 'secret' } }],
        removals: [],
      });
    });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    vi.useRealTimers();
    await waitFor(() => expect(result.current.items).toEqual([ITEM]));
    expect(api.getStagedChanges).toHaveBeenCalledWith('1111-2222');
  });

  it('answers a read superseded by a newer one, so a merge check is not refused', async () => {
    let resolveFirst;
    api.getStagedChanges.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    await act(async () => {});
    api.getStagedChanges.mockImplementationOnce(
      () => new Promise((resolve) => (resolveFirst = resolve))
    );
    api.getStagedChanges.mockResolvedValueOnce({ items: [] });
    let first;
    await act(async () => {
      first = result.current.refreshDiff();
      await result.current.refreshDiff(); // a newer read, e.g. after a staged op
    });
    await act(async () => resolveFirst({ items: [ITEM] }));
    expect(await first).toEqual([ITEM]);
    // The newer read owns what is shown.
    expect(result.current.items).toEqual([]);
  });

  it('holds a linked session as unknown until the server answers, either way', () => {
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.markModeUnknown('1111-2222'));
    expect(api.setSessionModeUnknown).toHaveBeenCalledWith('1111-2222');
    expect(result.current.writeMode).toBe('unknown');
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'direct' }));
    expect(api.setSessionModeDirect).toHaveBeenCalledWith('1111-2222');
    expect(api.setSessionOnServer).toHaveBeenCalledWith('1111-2222');
    expect(result.current.writeMode).toBe('direct');
  });

  it('settles a session the server does not hold as direct (browser-only)', () => {
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.markModeUnknown('1111-2222'));
    act(() => result.current.adoptSessionPayload('1111-2222', null));
    expect(result.current.writeMode).toBe('direct');
    expect(api.setSessionOnServer).not.toHaveBeenCalled();
  });

  it('routes every staged-change op to a diff re-read', () => {
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    for (const op of ['graph_change_staged', 'graph_changes_merged', 'graph_changes_discarded']) {
      expect(result.current.handleSessionOp({ op })).toBe(true);
    }
  });

  it('never reads a diff for a direct session', async () => {
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    await act(async () => {
      await result.current.refreshDiff();
    });
    expect(api.getStagedChanges).not.toHaveBeenCalled();
  });

  it('shows a session as staged the moment api.js marks it so', () => {
    api.getStagedChanges.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => stagedMarkListeners.forEach((listener) => listener('1111-2222')));
    expect(result.current.writeMode).toBe('staged');
  });

  it('forgets a deleted session', () => {
    api.getStagedChanges.mockResolvedValue({ items: [] });
    const { result } = renderHook(() => useStagedSession('1111-2222'));
    act(() => result.current.adoptSessionPayload('1111-2222', { write_mode: 'staged' }));
    act(() => result.current.forgetSession('1111-2222'));
    expect(api.setSessionStaged).toHaveBeenLastCalledWith('1111-2222', false);
    expect(result.current.writeMode).toBe('direct');
  });
});
