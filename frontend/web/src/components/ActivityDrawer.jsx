import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { X, ClockHistory, ArrowClockwise } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import { useViewportMode } from '../hooks/useViewportMode';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import HistoryList from './HistoryList';
import SessionActivityList from './SessionActivityList';
import { findLatestUndoable, classifyUndoError } from '../utils/sessionActivity';
import './ActivityDrawer.css';

const GRAPH_PAGE_SIZE = 25;
const SESSION_ACTIVITY_LIMIT = 100;

// Mirrors SessionDrawer.jsx's (and BottomSheet.jsx's) focus-trap contract,
// applied here only for the mobile full-screen variant — see the
// isMobile-gated effects below. Each of those components keeps its own copy
// rather than sharing one (no such hook has been extracted yet in this
// codebase); this is a third, matching the same shape deliberately.
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function getFocusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR));
}

/**
 * ActivityDrawer — full-height panel docked to the right screen edge (the
 * drawer formerly named RecentActivityDrawer), now hosting two tabs per
 * task-annotation-activity-undo-persistence's design: **Session** (this
 * session's annotation/canvas activity, PR #423 — actor-scoped, undoable)
 * and **Graph** (the backend's canonical graph-mutation log, the original
 * content of this drawer, unchanged). Both read the same `activity`
 * vocabulary the task description uses, but are deliberately separate feeds:
 * Session activity lives on the session document and disappears with it;
 * Graph history is the permanent record of what changed in the graph itself.
 */
function ActivityDrawer({ open, onClose, sessionId, currentClientId, roster }) {
  const { t } = useI18n();
  const { isMobile } = useViewportMode();
  const [tab, setTab] = useState('session');
  const drawerRef = useRef(null);
  const lastFocusedRef = useRef(null);

  // Closing while a descendant still holds focus would commit aria-hidden on
  // an ancestor of the active element in the same render that flips `open`
  // (the browser blocks this and force-blurs it, and axe/Lighthouse flag it)
  // — because like SessionDrawer, this drawer stays mounted through close for
  // its slide-out transition rather than unmounting outright. Blurring
  // synchronously, before the state update that flips `open` is even
  // committed, avoids the race regardless of when the focus-restore effect
  // cleanup later runs. Mirrors SessionDrawer.jsx's identical guard.
  const blurFocusedDescendant = useCallback(() => {
    if (
      isMobile &&
      drawerRef.current &&
      document.activeElement &&
      drawerRef.current.contains(document.activeElement)
    ) {
      document.activeElement.blur();
    }
  }, [isMobile]);

  const closeDrawer = useCallback(() => {
    blurFocusedDescendant();
    onClose();
  }, [blurFocusedDescendant, onClose]);

  // ---- Graph tab (unchanged behaviour, previously this whole drawer) ----
  const [graphEntries, setGraphEntries] = useState([]);
  const [graphOffset, setGraphOffset] = useState(0);
  const [graphHasMore, setGraphHasMore] = useState(false);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState(false);
  const graphRequestSeq = useRef(0);

  const loadGraphPage = useCallback(async (nextOffset, replace) => {
    const seq = ++graphRequestSeq.current;
    setGraphLoading(true);
    setGraphError(false);
    try {
      const result = await api.getGraphHistory({ limit: GRAPH_PAGE_SIZE, offset: nextOffset });
      if (seq !== graphRequestSeq.current) return;
      const page = result.entries || [];
      setGraphEntries((prev) => (replace ? page : [...prev, ...page]));
      setGraphOffset(nextOffset + page.length);
      setGraphHasMore(page.length === GRAPH_PAGE_SIZE);
    } catch {
      if (seq !== graphRequestSeq.current) return;
      setGraphError(true);
    } finally {
      if (seq === graphRequestSeq.current) setGraphLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open || tab !== 'graph') return;
    setGraphEntries([]);
    setGraphOffset(0);
    setGraphHasMore(false);
    loadGraphPage(0, true);
    // Re-run whenever the Graph tab is (re)selected while open, not just on
    // open, so switching back to it after a while shows fresh entries.
  }, [open, tab, loadGraphPage]);

  // ---- Session tab (new: PR #423's per-session activity + undo) ----
  const [sessionRecords, setSessionRecords] = useState([]);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [undoNotice, setUndoNotice] = useState(null); // {kind: 'success'|'error', textKey}
  const sessionRequestSeq = useRef(0);
  const nodes = useGraphStore((s) => s.nodes);
  const nodesById = useMemo(() => {
    const map = {};
    for (const n of nodes || []) map[n.id] = n;
    return map;
  }, [nodes]);

  const loadSessionActivity = useCallback(async () => {
    if (!sessionId) return;
    const seq = ++sessionRequestSeq.current;
    setSessionLoading(true);
    setSessionError(false);
    try {
      const result = await api.getSessionActivity(sessionId, { limit: SESSION_ACTIVITY_LIMIT });
      if (seq !== sessionRequestSeq.current) return;
      setSessionRecords(result.activity || []);
    } catch {
      if (seq !== sessionRequestSeq.current) return;
      setSessionError(true);
    } finally {
      if (seq === sessionRequestSeq.current) setSessionLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (!open || tab !== 'session') return;
    setUndoNotice(null);
    loadSessionActivity();
  }, [open, tab, sessionId, loadSessionActivity]);

  const latestUndoable = useMemo(
    () => findLatestUndoable(sessionRecords, currentClientId),
    [sessionRecords, currentClientId]
  );

  const handleUndo = useCallback(async () => {
    if (!sessionId || !currentClientId || undoing) return;
    setUndoing(true);
    setUndoNotice(null);
    try {
      await api.undoSessionAction(sessionId, currentClientId);
      setUndoNotice({ kind: 'success' });
      // The undo's inverse op reaches this browser's own canvas via the
      // existing session-stream broadcast (session_manager.py's
      // _apply_op_sync publishes it the same as any other op) — no local
      // canvas mutation needed here. This relies on that broadcast being
      // attributed to session_manager.py's dedicated _UNDO_REPLAY_CLIENT_ID
      // marker rather than to `currentClientId`: sessionSyncClient.js drops
      // an incoming op as a self-echo whenever its client_id matches this
      // browser's own (see the "echo of our own op" check in
      // sessionSyncClient.js), and this browser's own SSE subscription is
      // exactly the one that receives this broadcast — so replaying the
      // inverse op under `currentClientId` would make it indistinguishable
      // from a self-authored echo and silently drop it before it ever
      // reached the canvas. Re-fetching just refreshes this list (the record
      // flips to undone; no new record is added).
      await loadSessionActivity();
    } catch (err) {
      setUndoNotice({ kind: 'error', reason: classifyUndoError(err) });
    } finally {
      setUndoing(false);
    }
  }, [sessionId, currentClientId, undoing, loadSessionActivity]);

  // Escape closes the drawer regardless of which tab is active. In mobile
  // mode (full-screen overlay) Tab is also trapped inside the drawer, the
  // same contract SessionDrawer.jsx and BottomSheet.jsx use for the sibling
  // session/search/create sheets — desktop's docked panel keeps its
  // untrapped tab order.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeDrawer();
        return;
      }
      if (!isMobile || e.key !== 'Tab') return;

      const focusables = getFocusableElements(drawerRef.current);
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (!focusables.includes(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [open, closeDrawer, isMobile]);

  // Modal focus management for the mobile overlay only: move focus in on
  // open, restore it to whatever had focus beforehand on close.
  useEffect(() => {
    if (!open || !isMobile) return undefined;
    lastFocusedRef.current = typeof document !== 'undefined' ? document.activeElement : null;

    const focusable = getFocusableElements(drawerRef.current);
    (focusable[0] || drawerRef.current)?.focus();

    return () => {
      const toRestore = lastFocusedRef.current;
      if (toRestore && typeof toRestore.focus === 'function' && document.contains(toRestore)) {
        toRestore.focus();
      }
    };
  }, [open, isMobile]);

  // Body scroll lock for the mobile overlay only, mirroring
  // SessionDrawer.jsx/BottomSheet.jsx's contract.
  useEffect(() => {
    if (!isMobile || !open || typeof document === 'undefined') return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isMobile, open]);

  const isGraphEmpty = !graphLoading && !graphError && graphEntries.length === 0;
  const undoNoticeKey = undoNotice
    ? undoNotice.kind === 'success'
      ? 'history.session_undo_done'
      : `history.session_undo_${undoNotice.reason}`
    : null;

  return (
    <>
      {isMobile && (
        <div
          className={`activity-drawer-scrim${open ? ' open' : ''}`}
          onClick={closeDrawer}
          data-testid="activity-drawer-scrim"
          aria-hidden="true"
        />
      )}
      <div
        ref={drawerRef}
        className={`activity-drawer${open ? ' open' : ''}${isMobile ? ' activity-drawer--mobile' : ''}`}
        aria-hidden={!open}
        role={isMobile ? 'dialog' : undefined}
        aria-modal={isMobile ? open : undefined}
        aria-label={t('history.panel_title')}
        // Matches SessionDrawer.jsx's drawerRef: makes the container itself a
        // valid focus target so the `focusable[0] || drawerRef.current`
        // fallback above can move focus into an (unreachable today, but
        // defensively handled) drawer with no focusable descendants.
        tabIndex={-1}
      >
        <div className="activity-drawer-header">
          <ClockHistory size={17} className="activity-drawer-icon" />
          <span className="activity-drawer-title">{t('history.panel_title')}</span>
          {/* Close precedes Refresh in DOM/tab order — so a freshly-opened
              mobile drawer's initial focus (and Shift+Tab wrap) always lands
              on the one header button that is never mid-load-disabled —
              while a CSS `order` keeps it visually last (rightmost), matching
              convention. See ActivityDrawer.css. */}
          <button
            className="activity-drawer-close"
            onClick={closeDrawer}
            title={t('history.close')}
            aria-label={t('history.close')}
          >
            <X size={20} />
          </button>
          {tab === 'graph' && (
            <button
              className="activity-drawer-refresh"
              onClick={() => loadGraphPage(0, true)}
              title={t('history.refresh')}
              aria-label={t('history.refresh')}
              disabled={graphLoading}
            >
              <ArrowClockwise size={15} />
            </button>
          )}
          {tab === 'session' && (
            <button
              className="activity-drawer-refresh"
              onClick={loadSessionActivity}
              title={t('history.refresh')}
              aria-label={t('history.refresh')}
              disabled={sessionLoading}
            >
              <ArrowClockwise size={15} />
            </button>
          )}
        </div>

        <div className="activity-drawer-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'session'}
            className={`activity-drawer-tab${tab === 'session' ? ' active' : ''}`}
            onClick={() => setTab('session')}
          >
            {t('history.tab_session')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'graph'}
            className={`activity-drawer-tab${tab === 'graph' ? ' active' : ''}`}
            onClick={() => setTab('graph')}
          >
            {t('history.tab_graph')}
          </button>
        </div>

        {tab === 'session' ? (
          <div className="activity-drawer-body">
            <button
              type="button"
              className="session-activity-undo-last"
              onClick={handleUndo}
              disabled={!latestUndoable || undoing}
            >
              {undoing ? t('history.session_undoing') : t('history.session_undo_last')}
            </button>
            {undoNotice && (
              <div
                className={`session-activity-notice session-activity-notice--${undoNotice.kind}`}
                role="status"
              >
                {t(undoNoticeKey)}
              </div>
            )}
            {sessionError && (
              <div className="activity-drawer-error">{t('history.session_error')}</div>
            )}
            {sessionLoading && sessionRecords.length === 0 && !sessionError && (
              <div className="activity-drawer-loading">{t('history.loading')}</div>
            )}
            {!sessionLoading && !sessionError && (
              <SessionActivityList
                records={sessionRecords}
                currentClientId={currentClientId}
                roster={roster}
                latestUndoableId={latestUndoable?.id}
                undoing={undoing}
                onUndo={handleUndo}
                nodesById={nodesById}
              />
            )}
          </div>
        ) : (
          <div className="activity-drawer-body">
            {graphError && <div className="activity-drawer-error">{t('history.error')}</div>}
            {isGraphEmpty && <div className="activity-drawer-empty">{t('history.empty')}</div>}
            {graphEntries.length > 0 && <HistoryList entries={graphEntries} />}
            {graphLoading && graphEntries.length === 0 && (
              <div className="activity-drawer-loading">{t('history.loading')}</div>
            )}
            {graphHasMore && !graphError && (
              <button
                className="activity-drawer-load-more"
                onClick={() => loadGraphPage(graphOffset, false)}
                disabled={graphLoading}
              >
                {graphLoading ? t('history.loading') : t('history.load_more')}
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}

export default ActivityDrawer;
