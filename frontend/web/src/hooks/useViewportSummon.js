import { useCallback, useEffect, useRef, useState } from 'react';
import * as api from '../services/api';

// How long the "X brought you here" notice (and its Return action) stays up.
export const SUMMON_NOTICE_MS = 10000;

/**
 * The model-space region a ReactFlow camera `{x, y, zoom}` shows in a pane of
 * `width` × `height` screen pixels: the inverse of flow → screen
 * (`screen = model * zoom + offset`). Null when any input is unusable.
 */
export function visibleModelBounds(viewport, width, height) {
  if (!viewport || !(viewport.zoom > 0) || !(width > 0) || !(height > 0)) return null;
  const { x, y, zoom } = viewport;
  if (![x, y, zoom].every(Number.isFinite)) return null;
  return { x: -x / zoom, y: -y / zoom, width: width / zoom, height: height / zoom };
}

/**
 * "Bring everyone here" for shared sessions.
 *
 * - `summonEveryone()` sends the caller's visible region (from the last
 *   reported camera and the canvas element's size) to every client connected
 *   to the session.
 * - `receiveSummon(event)` is the sync client's `onViewportSummoned`: it
 *   remembers where this viewer was, asks the canvas to fit the region
 *   (`viewportRequest`) and raises a notice naming the summoner. A summon that
 *   lands while a notice is still open keeps the *original* return point, so
 *   Return always goes back to where the viewer was before being moved.
 * - `returnFromSummon()` restores that camera; switching session drops the
 *   notice, since its camera belongs to the session left.
 */
export function useViewportSummon({ sessionId, latestViewport, canvasRef, showNotification, t }) {
  const [viewportRequest, setViewportRequest] = useState(null);
  // The notice records the session it was raised in, and a session switch
  // drops it for good (its camera belongs to the session left): switching
  // back must neither revive it nor let a new summon chain onto it.
  const [notice, setNotice] = useState(null);
  const [noticeScope, setNoticeScope] = useState(sessionId);
  if (noticeScope !== sessionId) {
    setNoticeScope(sessionId);
    setNotice(null);
  }
  const summonNotice = notice && notice.sessionId === sessionId ? notice : null;
  // The open notice, for callbacks: a chained summon must read the return
  // point the previous one stored even before React re-renders.
  const noticeRef = useRef(null);
  const timerRef = useRef(null);

  const dismissSummonNotice = useCallback(() => {
    clearTimeout(timerRef.current);
    timerRef.current = null;
    noticeRef.current = null;
    setNotice(null);
  }, []);

  useEffect(() => () => clearTimeout(timerRef.current), []);
  useEffect(() => {
    clearTimeout(timerRef.current);
    timerRef.current = null;
    noticeRef.current = null;
  }, [sessionId]);

  const receiveSummon = useCallback(
    (event) => {
      if (!event?.bounds) return;
      if (event.sessionId && event.sessionId !== sessionId) return;
      const open = noticeRef.current?.sessionId === sessionId ? noticeRef.current : null;
      const next = {
        sessionId,
        summonedBy: event.summonedBy || null,
        returnTo: open?.returnTo ?? latestViewport.current ?? null,
      };
      noticeRef.current = next;
      setNotice(next);
      setViewportRequest({ bounds: event.bounds });
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(dismissSummonNotice, SUMMON_NOTICE_MS);
    },
    [sessionId, latestViewport, dismissSummonNotice]
  );

  const returnFromSummon = useCallback(() => {
    const returnTo = noticeRef.current?.returnTo;
    if (returnTo) setViewportRequest({ viewport: returnTo });
    dismissSummonNotice();
  }, [dismissSummonNotice]);

  const summonEveryone = useCallback(async () => {
    const rect = canvasRef.current?.getBoundingClientRect?.();
    const bounds = visibleModelBounds(latestViewport.current, rect?.width, rect?.height);
    if (!bounds) {
      showNotification('error', t('summon.failed'));
      return;
    }
    try {
      const result = await api.summonSessionViewport(sessionId, api.getClientId(), bounds);
      const others = Math.max(0, (result?.connected_clients ?? 1) - 1);
      showNotification('info', t('summon.sent', { count: others }));
    } catch {
      showNotification('error', t('summon.failed'));
    }
  }, [sessionId, latestViewport, canvasRef, showNotification, t]);

  return {
    viewportRequest,
    summonNotice,
    receiveSummon,
    returnFromSummon,
    dismissSummonNotice,
    summonEveryone,
  };
}
