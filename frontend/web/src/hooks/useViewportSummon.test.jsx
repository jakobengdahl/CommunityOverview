import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import * as api from '../services/api';
import { useViewportSummon, visibleModelBounds, SUMMON_NOTICE_MS } from './useViewportSummon';

const BOUNDS = { x: 100, y: 50, width: 400, height: 300 };

function setup({ sessionId = '1234-5678', viewport = { x: -40, y: -20, zoom: 2 } } = {}) {
  const latestViewport = { current: viewport };
  const canvasRef = { current: { getBoundingClientRect: () => ({ width: 800, height: 600 }) } };
  const showNotification = vi.fn();
  const t = (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key);
  const hook = renderHook((props) => useViewportSummon(props), {
    initialProps: { sessionId, latestViewport, canvasRef, showNotification, t },
  });
  return { ...hook, latestViewport, canvasRef, showNotification };
}

describe('visibleModelBounds', () => {
  it('inverts the camera transform so every screen size frames the same region', () => {
    expect(visibleModelBounds({ x: -40, y: -20, zoom: 2 }, 800, 600)).toEqual({
      x: 20,
      y: 10,
      width: 400,
      height: 300,
    });
  });

  it('is null for an unusable camera or pane', () => {
    expect(visibleModelBounds(null, 800, 600)).toBeNull();
    expect(visibleModelBounds({ x: 0, y: 0, zoom: 0 }, 800, 600)).toBeNull();
    expect(visibleModelBounds({ x: NaN, y: 0, zoom: 1 }, 800, 600)).toBeNull();
    expect(visibleModelBounds({ x: 0, y: 0, zoom: 1 }, 0, 600)).toBeNull();
  });
});

describe('useViewportSummon', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('a summon moves the camera to the bounds and names the summoner', () => {
    const { result } = setup();
    act(() =>
      result.current.receiveSummon({ bounds: BOUNDS, summonedBy: 'Ada', sessionId: '1234-5678' })
    );
    expect(result.current.viewportRequest).toEqual({ bounds: BOUNDS });
    expect(result.current.summonNotice).toMatchObject({
      summonedBy: 'Ada',
      returnTo: { x: -40, y: -20, zoom: 2 },
    });
  });

  it('Return restores the camera the viewer had before being summoned', () => {
    const { result } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS, summonedBy: 'Ada' }));
    act(() => result.current.returnFromSummon());
    expect(result.current.viewportRequest).toEqual({ viewport: { x: -40, y: -20, zoom: 2 } });
    expect(result.current.summonNotice).toBeNull();
  });

  it('a second summon while the notice is open keeps the original return point', () => {
    const { result, latestViewport } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS, summonedBy: 'Ada' }));
    latestViewport.current = { x: -999, y: -999, zoom: 1 }; // the summoned camera
    act(() => result.current.receiveSummon({ bounds: { ...BOUNDS, x: 7 }, summonedBy: 'Bo' }));
    expect(result.current.summonNotice.summonedBy).toBe('Bo');
    act(() => result.current.returnFromSummon());
    expect(result.current.viewportRequest).toEqual({ viewport: { x: -40, y: -20, zoom: 2 } });
  });

  it('a summon after a session switch returns to that session, not the one left', () => {
    const { result, rerender, latestViewport, canvasRef, showNotification } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS, summonedBy: 'Ada' }));
    latestViewport.current = { x: 7, y: 8, zoom: 1 }; // camera in the new session
    rerender({ sessionId: '2222-3333', latestViewport, canvasRef, showNotification, t: (k) => k });
    act(() => result.current.receiveSummon({ bounds: BOUNDS, summonedBy: 'Bo' }));
    act(() => result.current.returnFromSummon());
    expect(result.current.viewportRequest).toEqual({ viewport: { x: 7, y: 8, zoom: 1 } });
  });

  it('the notice stays up for ten seconds, counted from the latest summon', () => {
    const { result } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS }));
    act(() => vi.advanceTimersByTime(8000));
    act(() => result.current.receiveSummon({ bounds: BOUNDS }));
    act(() => vi.advanceTimersByTime(9900));
    expect(result.current.summonNotice).not.toBeNull();
    act(() => vi.advanceTimersByTime(200));
    expect(result.current.summonNotice).toBeNull();
  });

  it('ignores a summon addressed to another session', () => {
    const { result } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS, sessionId: '9999-9999' }));
    expect(result.current.viewportRequest).toBeNull();
    expect(result.current.summonNotice).toBeNull();
  });

  it('the notice expires, and a session switch drops it', () => {
    const { result, rerender, latestViewport, canvasRef, showNotification } = setup();
    act(() => result.current.receiveSummon({ bounds: BOUNDS }));
    act(() => vi.advanceTimersByTime(SUMMON_NOTICE_MS));
    expect(result.current.summonNotice).toBeNull();

    act(() => result.current.receiveSummon({ bounds: BOUNDS }));
    rerender({
      sessionId: '2222-3333',
      latestViewport,
      canvasRef,
      showNotification,
      t: (k) => k,
    });
    expect(result.current.summonNotice).toBeNull();
  });

  it('summonEveryone sends the visible model-space region', async () => {
    const spy = vi.spyOn(api, 'summonSessionViewport').mockResolvedValue({ connected_clients: 3 });
    vi.spyOn(api, 'getClientId').mockReturnValue('me');
    const { result, showNotification } = setup();
    await act(() => result.current.summonEveryone());
    expect(spy).toHaveBeenCalledWith('1234-5678', 'me', { x: 20, y: 10, width: 400, height: 300 });
    expect(showNotification).toHaveBeenCalledWith('info', 'summon.sent:{"count":2}');
  });

  it('summonEveryone reports a failure without throwing', async () => {
    vi.spyOn(api, 'summonSessionViewport').mockRejectedValue(new Error('403'));
    const { result, showNotification } = setup();
    await act(() => result.current.summonEveryone());
    expect(showNotification).toHaveBeenCalledWith('error', 'summon.failed');
  });

  it('summonEveryone sends nothing before the camera is known', async () => {
    const spy = vi.spyOn(api, 'summonSessionViewport');
    const { result, showNotification } = setup({ viewport: null });
    await act(() => result.current.summonEveryone());
    expect(spy).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('error', 'summon.failed');
  });
});
