import { useState, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import {
  GraphCanvas,
  CANVAS_ANNOTATION_TYPES,
  isSafeReferenceUrl,
  trimReferenceTarget,
} from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
import { useI18n } from './i18n';
import DesktopShell from './components/DesktopShell';
import MobileShell from './components/MobileShell';
import CollectKioskView from './components/CollectKioskView';
import GuideOverlay from './components/GuideOverlay';
import ActivityDrawer from './components/ActivityDrawer';
import NodeHistoryPanel from './components/NodeHistoryPanel';
import AppDialogs from './components/AppDialogs';
import ConfirmDialog from './components/ConfirmDialog';
import AppInstallPrompt from './components/AppInstallPrompt';
import * as api from './services/api';
import { teleportToSourceGraph } from './utils/teleport';
import * as sessionStore from './services/sessionStore';
import {
  annotationsToGroups,
  annotationsToOverlays,
  annotationDocumentToLegacyMetadata,
  legacyMetadataToAnnotationDocument,
} from './utils/sessionAnnotations';
import { serverStateToMirror, useSharedSession } from './hooks/useSharedSession';
import { DEFAULT_REQUEST_TIMEOUT_MS as SYNC_REQUEST_TIMEOUT_MS } from './services/sessionSyncClient';
import { useSyncConnection } from './hooks/useSyncConnection';
import { useStagedSession } from './hooks/useStagedSession';
import WriteModeBar from './components/WriteModeBar';
import NewSessionDialog from './components/NewSessionDialog';
import StagedReviewDialog from './components/StagedReviewDialog';
import { useToolResultCommands } from './hooks/useToolResultCommands';
import { useViewportSummon } from './hooks/useViewportSummon';
import SummonNotice from './components/SummonNotice';
import { useViewportMode } from './hooks/useViewportMode';
import { useFullscreenCanvas } from './hooks/useFullscreenCanvas';
import FullscreenExitButton from './components/FullscreenExitButton';
import { decideClearAction } from './utils/clearBoard';
import { dropIntoFreshSession, receiveRemoteSessionDeleted } from './utils/sessionLifecycle';
import {
  agentCreateToCanvas,
  agentUpdateEntries,
  applyEdgeUpdate,
  captureCanvasScope,
  confirmNodeDelete,
  connectNodes,
  createDialogNode,
  createdNodesToCanvas,
  deleteEdgeEverywhere,
  expandNode,
  loadSavedViewNode,
  openAgentEditor,
  persistNewNodes,
  persistNodeUpdates,
  setEdgeType,
} from './utils/sessionScopedGraphEdits';
import { loadUrlSavedView } from './utils/urlSavedView';
import { createAnnotationChangeScheduler } from './utils/annotationChangeScheduler';
import { createSelfEchoDedup } from './utils/selfEchoDedup';
import { applyIngestedImageOptimistically } from './utils/imageIngestApply';
import { shouldPersistSnapshot } from './utils/sessionSnapshotGuard';
import { refusalText } from './utils/stagedReview';
import './App.css';

// Ceiling on how long one resyncFromServer call may stay in flight before its
// reentrancy guard self-heals. The call spans up to RESYNC_MAX_FETCHES
// api.getSession() requests plus the replay's node fetches, and the timer
// covers all of them together, not each request. api.js's fetch carries
// no timeout (unlike SessionSyncClient's own outbound ops POST, which bounds
// itself against exactly this: "SSE deployments commonly sit behind Cloud
// Run / an ingress that can hold a half-open request open indefinitely" —
// sessionSyncClient.js), so a hung reload must not permanently disable
// reconnect recovery for the rest of the session (review round 3). Reuses
// that same request's own timeout value (imported, not duplicated — a
// hardcoded copy could silently drift out of sync, review round 6).
const RESYNC_GUARD_TIMEOUT_MS = SYNC_REQUEST_TIMEOUT_MS;
// How many times one resync may fetch the session while the op stream keeps
// running ahead of each payload (see resyncFromServer).
const RESYNC_MAX_FETCHES = 3;

const _urlParams = new URLSearchParams(window.location.search);
const _collectShortName = _urlParams.get('collect');
const _akcShortName = _urlParams.get('akc');

// Reflect the active session id in the URL so it can be shared/bookmarked
// (design 3.6). replaceState avoids polluting history on every switch.
function reflectSessionUrl(id) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('session', id);
    window.history.replaceState({}, '', url);
  } catch {
    // ignore — URL reflection is best-effort
  }
}

function App() {
  const akcShortName = _akcShortName;
  const {
    nodes,
    edges,
    schema,
    highlightedNodeIds,
    hiddenNodeIds,
    hiddenEdgeIds,
    dimmedNodeIds,
    dimmedEdgeIds,
    edgeIntensity,
    clearGroupsFlag,
    canvasBaselineEpoch,
    addNodesToVisualization,
    updateVisualization,
    toggleNodeVisibility,
    toggleEdgeVisibility,
    setHiddenNodeIds,
    setHiddenEdgeIds,
    dimNodes,
    restoreNodes,
    setDimmedNodeIds,
    dimEdges,
    restoreEdges,
    setDimmedEdgeIds,
    setEdgeIntensity,
    stats,
    setStats,
    llmAvailable,
    setLlmAvailable,
    setModelProfilesCapability,
    editingNode,
    setEditingNode,
    closeEditingNode,
    removeNode,
    removeEdge,
    updateEdgeData,
    setNodes,
    setEdges,
    presentation,
    setConfig,
    focusNodeId,
    clearFocusNode,
    setFocusNodeId,
    setGuideSearchInput,
    pendingGroups,
    setPendingGroups,
    pendingAnnotations,
    setPendingAnnotations,
    setSelectedGraphNodes,
    setDetailNode,
    detailNode,
    closeDetailNode,
    clearVisualization,
    federationDepth,
    setFederationDepth,
    showMinimap,
    nodePreviewEnabled,
    canvasLocked,
    setCanvasLocked,
    nodeMarks,
    pulsedNodeIds,
    startGuide,
    getNodeColor,
    closeMenusSignal,
    resetSessionScopedState,
    editingEdge,
    setEditingEdge,
    deleteDialog,
    setDeleteDialog,
  } = useGraphStore();

  const { t, setLanguage, language } = useI18n();

  const urlGuideStartedRef = useRef(false);
  const urlViewLoadedRef = useRef(false);
  // The in-flight ?session= bootstrap load, so a ?view= in the same URL can wait
  // for it and be applied on top.
  const urlSessionBootstrapRef = useRef(null);
  const canvasEditEpochRef = useRef(0);
  const urlFocusNodeLoadedRef = useRef(false);
  // The saved-view load clears the canvas after its own await, so focusing a
  // ?node= before it settles can have the node wiped out from under the focus.
  // Starts settled when there is no ?view= to wait for, which is also why this
  // is derived at mount rather than set from inside the view effect.
  const [urlViewSettled, setUrlViewSettled] = useState(
    () => !new URLSearchParams(window.location.search).get('view')
  );
  const latestViewport = useRef(null);
  const dialogOpenRef = useRef(false);
  const appRef = useRef(null);
  const canvasContainerRef = useRef(null);
  // Image annotation ids this browser has already rendered optimistically
  // (handleImageIngest below); see createSelfEchoDedup for why the confirming
  // SSE echo of the same op must be swallowed, not reapplied.
  const selfIngestedImageAnnotationIdsRef = useRef(createSelfEchoDedup());
  const { enterFullscreenCanvas, exitFullscreenCanvas, fullscreenCanvasActive } =
    useFullscreenCanvas(appRef);
  const [notification, setNotification] = useState(null);
  const [isOffline, setIsOffline] = useState(() => window.navigator?.onLine === false);
  const [saveViewDialog, setSaveViewDialog] = useState(null);
  const [showSubscriptionDialog, setShowSubscriptionDialog] = useState(false);
  const [editingSubscriptionData, setEditingSubscriptionData] = useState(null);
  const [showAgentDialog, setShowAgentDialog] = useState(false);
  const [editingAgentData, setEditingAgentData] = useState(null);
  const [showAgentRunsDialog, setShowAgentRunsDialog] = useState(false);
  const [agentRunsAgentId, setAgentRunsAgentId] = useState(null);
  const [showAgentProposalsDialog, setShowAgentProposalsDialog] = useState(false);
  const [agentProposalsAgentId, setAgentProposalsAgentId] = useState(null);
  const [createNodeType, setCreateNodeType] = useState(null);
  const [createGroupSignal, setCreateGroupSignal] = useState(0);
  const [saveViewSignal, setSaveViewSignal] = useState(0);
  const [isSavingView, setIsSavingView] = useState(false);
  const [skillDialogType, setSkillDialogType] = useState(null);
  const [editingSkillData, setEditingSkillData] = useState(null);
  const [showAKCDialog, setShowAKCDialog] = useState(false);
  const [editingAKCData, setEditingAKCData] = useState(null);
  const [akcIntroShown, setAkcIntroShown] = useState(false);
  const [akcConfig, setAkcConfig] = useState(null);

  // ── Session navigation state ────────────────────────────────────────────
  // The visualization session ID doubles as the working-session identity.
  // It is state (not a constant) so the user can switch sessions; the SSE
  // stream and state uploads reconnect automatically when it changes.
  const [sessionId, setSessionIdState] = useState(() => {
    const urlSession = _urlParams.get('session');
    const initial = sessionStore.isValidSessionId(urlSession)
      ? urlSession
      : api.generateVisualizationSessionId();
    api.setActiveSessionForRequests(initial);
    // A linked session may be staged: hold graph writes until its load says.
    // So may the starting one, until the capabilities say whether the app
    // starts in a server-created session of the installation's default mode.
    api.setSessionModeUnknown(initial);
    return initial;
  });
  // Written together with the state, not from an effect: a switch bumps the
  // session epoch before React re-renders, and the ?view= load must not see the
  // previous id in that gap.
  const sessionIdRef = useRef(sessionId);
  const setSessionId = useCallback((id) => {
    sessionIdRef.current = id;
    // Before any request is made from the new session: graph requests carry a
    // session's header only while it is the active one and known staged.
    api.setActiveSessionForRequests(id);
    setSessionIdState(id);
  }, []);
  // The session the page was opened into, for the ?view= load to compare against.
  const urlSessionIdRef = useRef(sessionId);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Independent viewport signals (WAVE 0 mobile-shell enabler) — isMobile is a
  // width breakpoint, isCoarsePointer is an input-type signal; neither implies
  // the other (e.g. a touch-enabled laptop is coarse but not mobile-width).
  const { isMobile, isCoarsePointer } = useViewportMode();
  // The mobile annotate sheet's content DOM node (MobileShell renders it,
  // GraphCanvas portals AnnotationToolbox into it) — see MobileShell.jsx's
  // component doc comment and GraphCanvas's annotationToolboxPortalContainer
  // prop. null whenever the sheet is closed or MobileShell isn't mounted.
  const [mobileAnnotationContainer, setMobileAnnotationContainer] = useState(null);
  // The EDIT-time counterpart of mobileAnnotationContainer above
  // (task-annotation-responsive-bottom-toolbox): the mobile Edit sheet's own
  // content DOM node, and the `{open, close}` pair MobileShell hands up so a
  // node component deep inside GraphCanvas can ask MobileShell's own
  // `useSurfaceManager` instance to open/close the `'detail'` surface — see
  // MobileShell.jsx's component doc comment. Starts `null` (not stable
  // no-ops): GraphCanvas's `editSheet.capable` is gated on this being
  // present, so a node's Edit button correctly falls back to the floating
  // menu rather than silently no-op'ing during the one-paint window between
  // MobileShell mounting and its own ready-effect actually firing.
  const [mobileAnnotationEditContainer, setMobileAnnotationEditContainer] = useState(null);
  const [detailSheetController, setDetailSheetController] = useState(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [connectDialogOpen, setConnectDialogOpen] = useState(false);
  const [renameDialog, setRenameDialog] = useState(null);
  const [deleteSessionDialog, setDeleteSessionDialog] = useState(null);
  // Pending clear-board confirmation. null when closed; { locked: boolean }
  // otherwise — the locked variant shows a stronger warning (see requestClear).
  const [clearConfirm, setClearConfirm] = useState(null);
  const [sessionsVersion, setSessionsVersion] = useState(0);
  const sessions = useMemo(() => sessionStore.listSessions(), [sessionsVersion]);
  // A session is "named" once the user has given it a title; the clear-board
  // guard treats named boards as worth protecting (unnamed ones clear freely).
  const currentSessionName = useMemo(
    () => sessions.find((s) => s.id === sessionId)?.name || '',
    [sessions, sessionId]
  );

  // ── Realtime sync (design step 6) ───────────────────────────────────────
  // The sync client owns the op-protocol stream and op emission for the active
  // session. Its create / connect / teardown lifecycle and the connection-scoped
  // state (remote positions/annotations, roster, remote selections, op-stream
  // readiness) live in useSyncConnection (STRUCTURE_REVIEW B1 slice 2); handlers
  // are routed through syncHandlersRef so the long-lived client always calls the
  // latest closures. Positions arriving from other clients reach the canvas via
  // remotePositions.
  const {
    syncRef,
    syncHandlersRef,
    ensureSyncConnected,
    remotePositions,
    setRemotePositions,
    animatedLayout,
    setAnimatedLayout,
    remoteAnnotationOps,
    setRemoteAnnotationOps,
    roster,
    setRoster,
    remoteSelections,
    setRemoteSelections,
    remoteLeases,
    setRemoteLeases,
    opStreamReady,
  } = useSyncConnection(sessionId);
  // The active session's write mode and staged changes
  // (docs/SESSION_OVERLAY_CONTRACT.md §17).
  const {
    writeMode: sessionWriteMode,
    items: stagedItems,
    pendingCount: stagedPendingCount,
    diffError: stagedDiffError,
    stagedStates,
    adoptWriteMode,
    adoptSessionPayload,
    markModeUnknown,
    forgetSession: forgetStagedSession,
    refreshDiff: refreshStagedDiff,
    handleSessionOp: handleStagedSessionOp,
  } = useStagedSession(sessionId);
  // api.js's own messages (held writes, a changed session) in the UI language.
  useEffect(() => {
    api.setMessageTranslator(t);
  }, [t]);
  const sessionWriteModeRef = useRef(sessionWriteMode);
  sessionWriteModeRef.current = sessionWriteMode;
  const setStoreWriteMode = useGraphStore((state) => state.setSessionWriteMode);
  useEffect(() => {
    setStoreWriteMode(sessionWriteMode);
  }, [sessionWriteMode, setStoreWriteMode]);
  const [stagedAvailable, setStagedAvailable] = useState(false);
  const stagedAvailableRef = useRef(false);
  stagedAvailableRef.current = stagedAvailable;
  // The browser-made id the app opened in without a ?session= link, until
  // the capabilities settle how it starts (settleStartingSession).
  const startingSessionRef = useRef(null);
  const settleStartingSessionRef = useRef(null);
  // While the start is settled its mode bar is not shown: graph writes are
  // held meanwhile, and its Retry would settle the start before the
  // capabilities say how it starts. An installation without staging sees no
  // bar at all, as before.
  const [settlingStartId, setSettlingStartId] = useState(() =>
    sessionStore.isValidSessionId(_urlParams.get('session')) ? null : sessionIdRef.current
  );
  const [defaultWriteMode, setDefaultWriteMode] = useState('direct');
  const defaultWriteModeRef = useRef('direct');
  defaultWriteModeRef.current = defaultWriteMode;
  const [newSessionDialogOpen, setNewSessionDialogOpen] = useState(false);
  const [stagedReviewOpen, setStagedReviewOpen] = useState(false);
  const [switchToStagedConfirm, setSwitchToStagedConfirm] = useState(false);
  // Sessions this tab is deleting, so its own delete notice is told apart from
  // one another tab of the same browser (same client id) caused.
  const locallyDeletingRef = useRef(new Set());
  // A deleted direct id can be opened again (a link recreates it): once this
  // tab switches to it, a later delete of it is someone else's again.
  // Not while its delete is still in flight, or the delete's own notice would
  // read as someone else's and the session be left twice.
  const deletesInFlightRef = useRef(new Set());
  const previousSessionIdRef = useRef(sessionId);
  useEffect(() => {
    if (previousSessionIdRef.current === sessionId) return;
    previousSessionIdRef.current = sessionId;
    if (!deletesInFlightRef.current.has(sessionId)) locallyDeletingRef.current.delete(sessionId);
  }, [sessionId]);
  // Sessions this tab is deleting that the server's notice says were staged,
  // even if this tab had not learnt it: they are replaced by staged ones.
  const deletedStagedRef = useRef(new Set());
  const applyServerSessionRef = useRef(null);
  // Guards resyncFromServer against overlapping reconnects (review round 2):
  // a flaky connection can drop and reconnect more than once before a slow
  // reload request settles, and running two resyncs concurrently would
  // replay the same pending ops twice and show a duplicate recovery toast.
  // resyncGuardTokenRef lets a request that eventually settles after its own
  // timeout guard already self-cleared (review round 3 — api.js's fetch has
  // no timeout, unlike SessionSyncClient's own outbound ops POST, so a hung
  // GET must not wedge reconnect recovery forever) recognise it is no longer
  // the current owner and not stomp on a newer resync that started meanwhile.
  // Holds `{ client }`, the sync client the in-flight resync was started
  // for (null when idle), not a bare boolean or a session id: a slow resync
  // for the session being left must not swallow the first resync of the
  // session just switched to, and switching away and back builds a new
  // client for the same id whose first resync must not be swallowed either.
  const resyncInFlightRef = useRef(null);
  const resyncGuardTokenRef = useRef(0);
  // Ops the sync client has told us (via onDropped) were terminally rejected
  // by the server — 400/413/404/410, never retryable — since the current
  // resyncFromServer call captured its `pendingOpsBefore` snapshot (review
  // round 10). A drop concurrent with a resync's own getSession() wait can
  // otherwise resurrect the rejected content: the op is gone from a second
  // capture taken after the wait (so a plain "union the two reads" keeps it
  // only via the *first* capture, which predates the drop), gets folded into
  // the sync baseline as if it had synced successfully, and gets replayed
  // onto the canvas — silently undoing the very rejection the "change not
  // saved" notice just reported. The reentrancy guard suppresses onDropped's
  // *own* resync call while one is already in flight for the same sync
  // client and its guard timer has not fired, so that in-flight call is the
  // only thing that will ever act on this — it reads and clears this set for
  // itself right before finalising which ops to fold/replay. A resync for a
  // newer client, or one started after the guard timer fired, is not
  // suppressed: it supersedes the older call by token, which stops at its
  // next checkpoint, so whichever call reads this set next is the newer one.
  const recentlyDroppedOpsRef = useRef(new Set());
  // MCP tool-result push application (external AI agent commands → canvas) and
  // the legacy SSE push stream. The op-stream `command` events are wired below
  // through syncHandlersRef and route through these same appliers. Pulse
  // commands (external pulse-trigger URLs) share the channel and dedup.
  const { applyToolResultCommand, applyPulseCommand } = useToolResultCommands({
    sessionId,
    opStreamReady,
    latestViewport,
  });

  // Apply a single remote op incrementally onto the local store + canvas. This
  // touches only the entities the op names, so a concurrent local edit is never
  // clobbered (unlike a wholesale reload). The sync client has already folded
  // the op into its baseline, so the store changes here do not echo back out.
  // The actual canvas-mutating half of an annotation create/update — shared by
  // applyRemoteOp's claim-gated case below (a genuine remote op, or one of an
  // image ingest's two racing deliveries — see createSelfEchoDedup) and by
  // onLocalAnnotationsApplied (this client's own op just acked with a fresh
  // server version — see sessionSyncClient.js's _flush). The two callers must
  // NOT share the claim() gate: onLocalAnnotationsApplied's id was never
  // markPending'd for an image-ingest race (that mechanism is scoped to the
  // dedicated ingest endpoint, never the ops queue this ack comes from), but
  // claim() cannot tell "an id that happens to collide with an unrelated
  // in-flight ingest race" apart from "the second half of that race" — routing
  // an ack through claim() risks consuming a marker the real echo still needs
  // (e.g. an image annotation moved immediately after being pasted, before its
  // own echo has arrived), silently reverting that move when the stale echo
  // then lands unguarded.
  const applyAnnotationUpsertToCanvas = useCallback(
    (ann) => {
      if (!ann || !ann.id) return false;
      if (ann.kind === 'group') {
        const [group] = annotationsToGroups([ann]).groups;
        setRemoteAnnotationOps((prev) => [
          ...(prev || []),
          { action: 'upsert-group', group, members: ann.member_node_ids || [] },
        ]);
      } else {
        const [overlay] = annotationsToOverlays([ann]);
        if (overlay)
          setRemoteAnnotationOps((prev) => [
            ...(prev || []),
            { action: 'upsert-overlay', overlay },
          ]);
      }
      return true;
    },
    [setRemoteAnnotationOps]
  );

  // When each edge was last removed by a remote op (a running count), so a
  // composed re-read still in flight does not put back an edge removed after
  // it started.
  // inFlight counts the re-reads started at each seq (several can share one).
  const edgeRemovalSeqRef = useRef({ seq: 0, removed: new Map(), inFlight: new Map() });
  const applyRemoteOp = useCallback(
    async (op) => {
      const store = useGraphStore.getState();
      if (
        (op?.op === 'edges_added' || op?.op === 'edges_updated') &&
        api.isActiveSessionPossiblyStaged()
      ) {
        // In a staged session the edge an op carries is staged content, and
        // the stream reaches anyone holding the session id: read it from the
        // composed view instead, which asks the authorization hook
        // (docs/SESSION_OVERLAY_CONTRACT.md §15).
        const sessionAtStart = syncRef.current?.sessionId;
        const removalsAtStart = edgeRemovalSeqRef.current.seq;
        const inFlight = edgeRemovalSeqRef.current.inFlight;
        inFlight.set(removalsAtStart, (inFlight.get(removalsAtStart) || 0) + 1);
        const onCanvas = new Set(store.nodes.map((n) => n.id));
        const wanted = (op.edges || []).filter(
          (e) => e && e.id && onCanvas.has(e.source) && onCanvas.has(e.target)
        );
        const sources = [...new Set(wanted.map((e) => e.source))];
        let reads;
        try {
          reads = await Promise.all(sources.map((id) => api.getNodeDetails(id).catch(() => null)));
        } finally {
          const left = inFlight.get(removalsAtStart) - 1;
          if (left > 0) inFlight.set(removalsAtStart, left);
          else inFlight.delete(removalsAtStart);
        }
        if (syncRef.current?.sessionId !== sessionAtStart) return false; // switched away while fetching
        const listed = new Map(reads.flatMap((r) => r?.edges || []).map((e) => [e.id, e]));
        const have = new Set(useGraphStore.getState().edges.map((e) => e.id));
        const fresh = wanted.map((e) => listed.get(e.id)).filter(Boolean);
        fresh.filter((e) => have.has(e.id)).forEach((e) => updateEdgeData(e.id, e));
        // Only an add adds, and not an edge a later op removed while this
        // read was in flight (the sync baseline already has that removal).
        const added =
          op.op === 'edges_added'
            ? fresh.filter(
                (e) =>
                  !have.has(e.id) &&
                  !((edgeRemovalSeqRef.current.removed.get(e.id) ?? -1) > removalsAtStart)
              )
            : [];
        if (added.length) addNodesToVisualization([], added);
        return fresh.length > 0;
      }
      switch (op?.op) {
        case 'nodes_added': {
          const have = new Set(store.nodes.map((n) => n.id));
          const missing = (op.node_ids || []).filter((id) => !have.has(id));
          if (!missing.length) return false;
          // The only case here with an internal await, so the only one where
          // the active session can change out from under it (a session
          // switch mid-fetch, review round 5): capture which session this op
          // is for and re-check before writing to the store below, which is
          // not itself session-scoped.
          const sessionAtStart = syncRef.current?.sessionId;
          const addNodes = [];
          const addEdges = [];
          await Promise.all(
            missing.map(async (id) => {
              try {
                const r = await api.getNodeDetails(id);
                if (r?.node) {
                  addNodes.push(r.node);
                  (r.edges || []).forEach((e) => addEdges.push(e));
                }
              } catch {
                /* node may have been deleted from the graph — skip */
              }
            })
          );
          if (syncRef.current?.sessionId !== sessionAtStart) return false; // switched away while fetching
          // Seed positions from the sync baseline: the originator emits nodes_added
          // then node_moved as separate ops, so by the time this async resolve
          // finishes the follow-up position is already folded into the baseline.
          // Placing the node here (rather than relying on the racing remotePositions
          // op, which is dropped when the node isn't mounted yet) prevents the node
          // from settling at an auto-layout spot and re-emitting a wrong position.
          if (addNodes.length) {
            const positioned = addNodes.map((n) => {
              const pos = syncRef.current?.baselinePosition(n.id);
              return pos ? { ...n, _savedPosition: pos } : n;
            });
            addNodesToVisualization(positioned, addEdges);
            return true;
          }
          return false;
        }
        case 'nodes_removed': {
          const removeIds = op.node_ids || [];
          const existing = new Set(store.nodes.map((n) => n.id));
          (op.node_ids || []).forEach((id) => removeNode(id));
          return removeIds.some((id) => existing.has(id));
        }
        case 'nodes_hidden': {
          const current = store.hiddenNodeIds || [];
          const currentSet = new Set(current);
          const additions = (op.node_ids || []).filter((id) => !currentSet.has(id));
          if (!additions.length) return false;
          setHiddenNodeIds(Array.from(new Set([...current, ...additions])));
          return true;
        }
        case 'nodes_shown': {
          const drop = new Set(op.node_ids || []);
          if (!(store.hiddenNodeIds || []).some((id) => drop.has(id))) return false;
          setHiddenNodeIds((store.hiddenNodeIds || []).filter((id) => !drop.has(id)));
          return true;
        }
        case 'edges_added': {
          // A collaborator drew an edge between nodes already present here. Its
          // endpoints are in the graph, so render it directly; addNodesToVisualization
          // dedupes by edge id, so a redraw after a later re-hydration is harmless.
          const list = (op.edges || []).filter((e) => e && e.id);
          const haveEdges = new Set(store.edges.map((e) => e.id));
          const missingEdges = list.filter((e) => !haveEdges.has(e.id));
          if (missingEdges.length) {
            addNodesToVisualization([], missingEdges);
            return true;
          }
          return false;
        }
        case 'edges_removed': {
          const removeIds = op.edge_ids || [];
          const removals = edgeRemovalSeqRef.current;
          removeIds.forEach((id) => {
            removals.seq += 1;
            removals.removed.set(id, removals.seq);
          });
          if (removals.removed.size > 1000) {
            // Forget only removals no re-read still in flight could compare
            // against.
            const oldest = Math.min(removals.seq, ...removals.inFlight.keys());
            for (const [id, seq] of removals.removed) {
              if (seq <= oldest) removals.removed.delete(id);
            }
          }
          const existing = new Set(store.edges.map((e) => e.id));
          removeIds.forEach((id) => removeEdge(id));
          return removeIds.some((id) => existing.has(id));
        }
        case 'edges_updated': {
          // A collaborator changed an edge's attributes (e.g. its relationship
          // type). Merge them in place; if the edge isn't present here,
          // updateEdgeData is a harmless no-op and a later hydration recovers
          // the current value from the graph.
          const existing = new Set(store.edges.map((e) => e.id));
          let applied = false;
          (op.edges || []).forEach((e) => {
            if (e && e.id) {
              if (existing.has(e.id)) applied = true;
              updateEdgeData(e.id, e);
            }
          });
          return applied;
        }
        case 'edges_hidden': {
          const current = store.hiddenEdgeIds || [];
          const currentSet = new Set(current);
          const additions = (op.edge_ids || []).filter((id) => !currentSet.has(id));
          if (!additions.length) return false;
          setHiddenEdgeIds(Array.from(new Set([...current, ...additions])));
          return true;
        }
        case 'edges_shown': {
          const drop = new Set(op.edge_ids || []);
          if (!(store.hiddenEdgeIds || []).some((id) => drop.has(id))) return false;
          setHiddenEdgeIds((store.hiddenEdgeIds || []).filter((id) => !drop.has(id)));
          return true;
        }
        case 'nodes_dimmed': {
          const current = store.dimmedNodeIds || [];
          const currentSet = new Set(current);
          const additions = (op.node_ids || []).filter((id) => !currentSet.has(id));
          if (!additions.length) return false;
          setDimmedNodeIds(Array.from(new Set([...current, ...additions])));
          return true;
        }
        case 'nodes_undimmed': {
          const drop = new Set(op.node_ids || []);
          if (!(store.dimmedNodeIds || []).some((id) => drop.has(id))) return false;
          setDimmedNodeIds((store.dimmedNodeIds || []).filter((id) => !drop.has(id)));
          return true;
        }
        case 'edges_dimmed': {
          const current = store.dimmedEdgeIds || [];
          const currentSet = new Set(current);
          const additions = (op.edge_ids || []).filter((id) => !currentSet.has(id));
          if (!additions.length) return false;
          setDimmedEdgeIds(Array.from(new Set([...current, ...additions])));
          return true;
        }
        case 'edges_undimmed': {
          const drop = new Set(op.edge_ids || []);
          if (!(store.dimmedEdgeIds || []).some((id) => drop.has(id))) return false;
          setDimmedEdgeIds((store.dimmedEdgeIds || []).filter((id) => !drop.has(id)));
          return true;
        }
        case 'edge_intensity_set':
          if (typeof op.value !== 'number') return false;
          if (store.edgeIntensity === op.value) return false;
          setEdgeIntensity(op.value);
          return true;
        case 'node_moved':
          // Merge, don't replace: a burst of moves in one tick must not lose all
          // but the last node's position.
          if (op.node_id && op.position) {
            setRemotePositions((prev) => ({ ...(prev || {}), [op.node_id]: op.position }));
            return true;
          }
          return false;
        case 'layout_applied':
          if (op.positions) {
            // An MCP agent's arrange carries an animation hint (contract §9–§10):
            // route it to the tweening channel so the whole batch moves as one
            // coherent transition. A human bulk drag arrives without the hint and
            // still applies instantly.
            if (op.animation && op.animation.animate) {
              // Queue, don't replace: two layout_applied ops delivered in one
              // tick (a split arrange) must both survive React's batching —
              // replacing would drop all but the last (mirrors node_moved).
              setAnimatedLayout((prev) => [
                ...(prev || []),
                { positions: op.positions, animation: op.animation, seq: op.seq },
              ]);
            } else {
              setRemotePositions((prev) => ({ ...(prev || {}), ...op.positions }));
            }
            return Object.keys(op.positions).length > 0;
          }
          return false;
        case 'annotation_created':
        case 'annotation_updated': {
          const ann = op.annotation;
          if (!ann || !ann.id) return false;
          // No version check here on purpose (smallfix-applyremoteop-canvas-
          // no-version-guard, round 5): a stale/reordered broadcast for this
          // annotation is already filtered out before it ever reaches this
          // function. Ops arrive here from three places — onRemoteOps
          // (sessionSyncClient.js's `_handleEvent` now suppresses delivery
          // for a stale annotation_created/annotation_updated the same
          // `isAnnotationOpStale` check keeps out of its own sync baseline,
          // so a genuine remote broadcast that gets here is never stale
          // relative to what this canvas already shows); resyncFromServer's
          // replay of this client's own not-yet-confirmed local ops (never
          // "stale" — they are this client's own pending edits); and
          // handleImageIngest's direct optimistic apply of a brand-new
          // annotation this client just created (nothing to be stale
          // relative to). Adding a second, separately-maintained version
          // check here would risk it drifting from the one upstream rather
          // than adding real protection — see sessionSyncClient.js's
          // `isAnnotationOpStale` docstring.
          //
          // This function is the one shared place both of an image ingest's
          // two deliveries end up — this browser's own direct optimistic
          // apply (handleImageIngest) and its confirming SSE echo (via
          // onRemoteOps below) — so whichever of the two got here first for
          // this id renders it, and the other is a no-op (see
          // createSelfEchoDedup for why "whichever is second" cannot be
          // assumed to always be the echo). The return value tells the
          // direct-apply caller whether *this* call was the one that won,
          // so it knows whether to also fold the op into the sync baseline
          // (see handleImageIngest / SessionSyncClient.foldLocalOp) — the
          // loser must not, since the winner (or the winner's own baseline
          // fold, for the echo case) already did.
          if (!selfIngestedImageAnnotationIdsRef.current.claim(ann.id)) return false;
          return applyAnnotationUpsertToCanvas(ann);
        }
        case 'annotation_deleted':
          if (op.annotation_id) {
            setRemoteAnnotationOps((prev) => [
              ...(prev || []),
              { action: 'delete', id: op.annotation_id },
            ]);
            return true;
          }
          return false;
        case 'group_membership_changed':
          if (!op.group_id) return false;
          setRemoteAnnotationOps((prev) => [
            ...(prev || []),
            { action: 'membership', groupId: op.group_id, members: op.member_node_ids || [] },
          ]);
          return true;
        default:
          return false; // session_renamed handled by its own event
      }
    },
    [
      addNodesToVisualization,
      removeNode,
      removeEdge,
      updateEdgeData,
      setHiddenNodeIds,
      setHiddenEdgeIds,
      setDimmedNodeIds,
      setDimmedEdgeIds,
      setEdgeIntensity,
      setRemotePositions,
      setAnimatedLayout,
      setRemoteAnnotationOps,
      syncRef,
      applyAnnotationUpsertToCanvas,
    ]
  );

  // Reconnect / catch-up path (missed ops after a disconnect, or a delayed op
  // finally landing): reload the whole session from the server — the
  // authoritative source for node/edge visibility — and reset the sync
  // baseline. The reload itself is a wholesale replace, but it would silently
  // discard whatever this client edited while offline (queued ops the server
  // never received) if nothing put them back: capture them before the reload
  // and replay each through applyRemoteOp afterwards, which — like any remote
  // op — touches only the entities it names rather than clobbering the fresh
  // server state (task fbd32fc9). Replaying does not touch the sync client's
  // own queue, so the same ops still flush to the server exactly once normal
  // delivery would; a concurrent edit to the same entity resolves the same
  // way any two racing ops already do (last write wins), which is the
  // existing idempotent-op contract, not something new this adds.
  // Returns the number of local ops recovered this way, so the caller can
  // report the recovery back to the user.
  const resyncFromServer = useCallback(
    async (targetId) => {
      // A flaky connection can drop and reconnect more than once before a
      // slow reload below settles; without this guard a second resync would
      // replay the same still-pending ops a second time and the caller would
      // show a duplicate recovery toast (review round 2). The in-flight
      // resync already reloads current server truth, so skipping a
      // concurrent one loses nothing a later op/resync wouldn't also catch —
      // including onDropped's own resync call, whose "converge back to
      // server truth" goal an already-in-flight resync accomplishes anyway.
      //
      // Only a resync for the *same* sync client is skipped. One for the
      // current client's session supersedes an in-flight one started for a
      // client that has since been replaced (a session switch, including away
      // and back to the same id): taking the token below makes the old call
      // stop at its next checkpoint. A resync naming a session the current
      // client is not on never supersedes, or it would cancel the current
      // session's resync and then bail at its own switched-away check,
      // leaving nothing to reload.
      //
      // Every switched-away check below compares the client instance, not the
      // session id: switching away and back builds a new client for the same
      // id, and this call's payload and captured ops belong to the old one.
      // That is also why a call naming a session the current client is not on
      // stops here: from then on `client` is the only identity it checks.
      const client = syncRef.current;
      if (!client || client.sessionId !== targetId) return 0;
      const inFlight = resyncInFlightRef.current;
      if (inFlight !== null && inFlight.client === client) return 0;
      resyncInFlightRef.current = { client };
      // A token, not just the marker: if the guard timer below fires (its
      // request never settles), or a resync for a newer client supersedes
      // this one, this call's eventual finally must not clear a marker it no
      // longer owns (review round 3). Every checkpoint below that could run
      // after an arbitrarily long await (the reload, and each recovered
      // nodes_added's node fetch inside the replay loop) re-checks this same
      // token, not just the session id — so if the timer ever does fire
      // *while this call is still legitimately running* (merely slow, not
      // actually hung) and a newer resync starts, this call notices at its
      // very next checkpoint and stops, instead of continuing to apply now-
      // superseded results on top of what the newer resync already
      // established (review round 7).
      const myToken = ++resyncGuardTokenRef.current;
      // Spans the *whole* call, not just the initial reload request (reverted
      // from round 5's narrower scoping — review round 7): api.js's fetch has
      // no timeout of its own, and that is equally true of the replay loop's
      // own per-op api.getNodeDetails calls (applyRemoteOp, below) as of the
      // initial api.getSession reload — a hang in either must not wedge
      // reconnect recovery forever. The per-checkpoint token re-checks above
      // are what keep this safe against the failure mode round 5 originally
      // found in a whole-call timer (a false self-heal firing mid-legitimate-
      // run letting a redundant resync start): they stop this call from
      // acting on stale state instead of preventing the timer from firing.
      const guardTimer = setTimeout(() => {
        if (resyncGuardTokenRef.current === myToken) resyncInFlightRef.current = null;
      }, RESYNC_GUARD_TIMEOUT_MS);
      try {
        // Selection claims are excluded from every capture below:
        // `_readvertiseSelection()` re-queues one on every reconnect whenever
        // the user merely has something selected, regardless of whether
        // anything was actually edited offline, and applyRemoteOp has no
        // case for it (a no-op) — counting it would misreport a reconnect
        // with zero real edits as a recovery (review round 1).
        const capturePendingOps = () =>
          (syncRef.current === client ? client.getPendingOps() : []).filter(
            (op) => op?.op !== 'selection_claimed' && op?.op !== 'selection_released'
          );
        // Read whatever is already queued *before* the network round-trip
        // below, not only after: SessionSyncClient arms its own flush
        // (_flushSoon) right after the onResync handler it called this from
        // returns, so an `await` ahead of this first read would race that
        // flush — it can splice the very ops we want to capture out of the
        // queue first (review round 1; SessionSyncClient's own in-flight
        // tracking, added in round 5, means a second read below no longer
        // loses ops that race is finished spliced into, but reading once
        // before starting the request is still the only way to see an op
        // that flushes and gets fully confirmed *during* that request).
        const pendingOpsBefore = capturePendingOps();
        // Stream ops keep arriving while the GET is in flight, and each one
        // is applied to the canvas and advances the client's appliedSeq (so
        // does this client's own echo, which is not re-applied). A
        // payload whose seq is below that predates them, and the wholesale
        // reload below would wipe them for good (the stream never resends
        // them), so fetch again instead. Bounded: under a steady op stream
        // the last payload is applied anyway rather than never converging.
        let payload;
        for (let attempt = 1; ; attempt += 1) {
          try {
            payload = await api.getSession(targetId, { resolve: true });
          } catch {
            return 0;
          }
          adoptSessionPayload(targetId, payload);
          // Also bail if the guard timeout already fired and a newer resync
          // now owns it (review round 4): the token check above only stops
          // *this* stale call from clearing a flag it no longer owns — without
          // this check here too, a call that finally resolves after its own
          // timeout would still go on to apply its now-outdated payload/replay
          // over whatever the newer resync already established, silently
          // reintroducing the very data loss this PR fixes.
          if (resyncGuardTokenRef.current !== myToken) return 0;
          if (syncRef.current !== client) return 0; // switched away
          const behindStream = typeof payload?.seq === 'number' && payload.seq < client.appliedSeq;
          if (!behindStream || attempt >= RESYNC_MAX_FETCHES) break;
        }
        // Capture again, right before the destructive reload below: an op
        // enqueued *during* the getSession request above is not reflected in
        // `payload` either, and without this second read it would be
        // silently wiped by the reload with nothing to bring it back (review
        // round 6). An op present only in the first read (delivered and
        // confirmed by the server while we waited) is harmless to keep too —
        // replaying an already-confirmed op is redundant, never wrong, under
        // the same idempotent-op contract this whole function already relies
        // on (see this function's opening comment), so the union below errs
        // on the side of including rather than trying to guess which read is
        // more current.
        const pendingOpsAfter = capturePendingOps();
        const seenBefore = new Set(pendingOpsBefore);
        const unionedOps = pendingOpsBefore.concat(
          pendingOpsAfter.filter((op) => !seenBefore.has(op))
        );
        // Drop anything the server terminally rejected while we were waiting
        // above (review round 10) — kept in `pendingOpsBefore` by the union's
        // own "err on the side of including" rule (it cannot tell "confirmed
        // delivered" apart from "rejected" just from a second op-queue read),
        // but onDropped already recorded it. Consumed here, not left for a
        // later call: what this resync doesn't act on now, nothing else will.
        const pendingOps = unionedOps.filter((op) => !recentlyDroppedOpsRef.current.has(op));
        recentlyDroppedOpsRef.current.clear();
        applyServerSessionRef.current?.(payload);
        const resolvedIds = (payload?.resolved?.nodes || []).map((n) => n.id);
        client.setBaseline(serverStateToMirror(payload?.state, resolvedIds));
        // A full reload re-hydrates every annotation from server truth, so any
        // image-ingest race this browser was still waiting to resolve (see
        // createSelfEchoDedup) is moot — drop it rather than let it linger and
        // wrongly veto an unrelated later update for the same id.
        //
        // Accepted narrow edge case: a resync landing in the brief window
        // between markPending(id) and either delivery reaching claim() (an
        // upload genuinely in flight when the stream drops) clears that
        // reservation too, so both deliveries would then render unguarded —
        // the second one, if the annotation was already edited in between,
        // would revert that edit.
        selfIngestedImageAnnotationIdsRef.current.clear();
        // Fold every recovered op into the sync baseline *before* replaying
        // any of them onto the canvas. Two consequences of skipping this
        // (review round 3): the next auto-save's diff would see the baseline
        // as if these ops never happened and re-send every one of them again
        // (our own echo for them never arrives to fold it in later, since the
        // client skips its own echoes by design); and nodes_added's
        // position-seed lookup (baselinePosition, below) would not see a
        // node_moved for the same id that is later in this same batch, so
        // the recovered node would settle at an auto-layout spot instead of
        // where it was actually left. Folding the whole batch first — rather
        // than interleaved with replay — means that lookup already sees it.
        //
        // foldOpIntoBaseline, not foldLocalOp (review round 7): these ops
        // flush under this client's own id, so their eventual echo is always
        // filtered by the "echo of our own op" check before it could reach
        // foldLocalOp's dedup marker — setting that marker here would leak
        // it forever and could wrongly swallow a different collaborator's
        // later genuine edit to the same annotation. See foldLocalOp's own
        // docstring for the full reasoning; foldLocalOp itself stays correct
        // for its original caller (handleImageIngest), whose op is broadcast
        // under a shared, non-personal client id specifically so its own
        // echo is *not* filtered there.
        for (const op of pendingOps) {
          client.foldOpIntoBaseline(op);
        }
        // Sequential, not Promise.all: ops must replay in their original order
        // (e.g. nodes_added before a node_moved for the same id), not race.
        // Re-checked every iteration, not just once before the loop (review
        // round 5): applyRemoteOp awaits a network call for nodes_added, and
        // the canvas store it writes to is not scoped by session — if the
        // user switches to a different session while that await is pending,
        // this session's recovered op would otherwise land on the newly
        // loaded session's canvas instead. The token is re-checked too
        // (review round 7): if the guard timer fired mid-replay because this
        // call was merely slow, not hung, and a newer resync has since taken
        // over, stop here rather than keep applying this call's now-stale
        // ops on top of what the newer one already established — that newer
        // resync's own return value is the one the caller should act on.
        let appliedCount = 0;
        for (const op of pendingOps) {
          if (resyncGuardTokenRef.current !== myToken) return 0;
          if (syncRef.current !== client) break;
          if (await applyRemoteOp(op)) appliedCount += 1;
        }
        // Not pendingOps.length unconditionally (review round 7): a session
        // switch mid-replay (the break above) can stop this short of the
        // full batch, and reporting the full count either as "recovered" (a
        // misleading toast in the session the user has since left) or a
        // basis for double-counting would both be wrong.
        return appliedCount;
      } finally {
        clearTimeout(guardTimer);
        if (resyncGuardTokenRef.current === myToken) resyncInFlightRef.current = null;
      }
    },
    [syncRef, applyRemoteOp, adoptSessionPayload]
  );

  // Re-read, in the session's composed view, the canvas entities a staged
  // change touched (docs/SESSION_OVERLAY_CONTRACT.md §15). Only the keys of the
  // op are used: its content reaches anyone holding the session id, while a
  // composed read asks the authorization hook. A node the composed view no
  // longer holds is taken off the canvas when `removeMissing` is set (after a
  // merge or discard: a discarded create, a merged delete); after a staging
  // op that is left to the session ops that remove it, as in direct mode.
  // Deliberately not a full reload: canvas additions not yet flushed to the
  // session would be lost with it.
  const refreshStagedEntities = useCallback(
    async (op, { removeMissing = false } = {}) => {
      const sessionAtStart = syncRef.current?.sessionId;
      const keys = [
        ...(op.entries || []).map((e) => [e?.kind, e?.entity_id]),
        ...(op.removals || []).map((r) => (Array.isArray(r) ? r : [r?.kind, r?.entity_id])),
      ];
      const store = useGraphStore.getState();
      const onCanvas = new Set(store.nodes.map((n) => n.id));
      const nodeIds = new Set();
      const namedEdges = new Set();
      for (const [kind, id] of keys) {
        if (kind === 'node' && onCanvas.has(id)) nodeIds.add(id);
        if (kind === 'edge') {
          namedEdges.add(id);
          const edge = store.edges.find((e) => e.id === id);
          if (edge && onCanvas.has(edge.source)) nodeIds.add(edge.source);
        }
      }
      const missing = [];
      const fetched = await Promise.all(
        [...nodeIds].map((id) =>
          api.getNodeDetails(id).catch((error) => {
            if (error?.status === 404) missing.push(id);
            return null;
          })
        )
      );
      if (syncRef.current?.sessionId !== sessionAtStart) return; // switched away while fetching
      if (removeMissing) missing.forEach((id) => removeNode(id));
      const current = useGraphStore.getState();
      const present = new Set(current.nodes.map((n) => n.id));
      const nodeUpdates = new Map();
      const listed = new Map();
      fetched.forEach((r) => {
        if (!r?.node) return;
        nodeUpdates.set(r.node.id, r.node);
        (r.edges || []).forEach((e) => listed.set(e.id, e));
      });
      if (!nodeUpdates.size) return;
      const nodes = current.nodes.map((n) =>
        nodeUpdates.has(n.id) ? { ...n, ...nodeUpdates.get(n.id) } : n
      );
      // An edge the op names that its re-read endpoint no longer lists is gone
      // from the session's view (a staged delete). Other edges stay: a node
      // read leaves out archived edges the canvas may still be showing.
      const edges = current.edges
        .filter(
          (e) =>
            listed.has(e.id) ||
            !namedEdges.has(e.id) ||
            !(nodeUpdates.has(e.source) || nodeUpdates.has(e.target))
        )
        .map((e) => (listed.has(e.id) ? { ...e, ...listed.get(e.id) } : e));
      const known = new Set(edges.map((e) => e.id));
      listed.forEach((e, id) => {
        if (!known.has(id) && present.has(e.source) && present.has(e.target)) edges.push(e);
      });
      setNodes(nodes);
      setEdges(edges);
    },
    [syncRef, setNodes, setEdges, removeNode]
  );

  // Callbacks waiting for the next canvas snapshot (positions/groups arrive
  // from GraphCanvas via the saveViewSignal round-trip).
  const snapshotCallbacksRef = useRef([]);
  // Set when the user explicitly asked for the Save View dialog from the
  // toolbar, so the snapshot round-trip knows to open it.
  const viewDialogRequestedRef = useRef(false);

  const federationDepthLevels = (stats?.federation?.selectable_depth_levels || [1]).filter(
    (v) => Number.isInteger(v) && v >= 1
  );
  const maxFederationDepth = Math.max(
    1,
    ...federationDepthLevels,
    stats?.federation?.max_selectable_depth || 1
  );

  useEffect(() => {
    if (federationDepth > maxFederationDepth) {
      setFederationDepth(maxFederationDepth);
    }
  }, [federationDepth, maxFederationDepth, setFederationDepth]);

  // Track whether any dialog is currently open (used by the double-Escape handler).
  // useLayoutEffect runs synchronously after commit so the ref is up to date before
  // the next paint — closing the window where a rapid double-Escape right after a
  // dialog opens could slip past the guard and clear the canvas.
  useLayoutEffect(() => {
    dialogOpenRef.current = !!(
      createNodeType ||
      editingNode ||
      detailNode ||
      editingEdge ||
      deleteDialog ||
      saveViewDialog ||
      showSubscriptionDialog ||
      showAgentDialog ||
      showAgentRunsDialog ||
      showAgentProposalsDialog ||
      skillDialogType ||
      showAKCDialog ||
      drawerOpen ||
      settingsOpen ||
      connectDialogOpen ||
      renameDialog ||
      deleteSessionDialog ||
      newSessionDialogOpen ||
      stagedReviewOpen ||
      switchToStagedConfirm ||
      clearConfirm ||
      (akcShortName && akcConfig && !akcIntroShown)
    );
  }, [
    createNodeType,
    editingNode,
    detailNode,
    editingEdge,
    deleteDialog,
    saveViewDialog,
    showSubscriptionDialog,
    showAgentDialog,
    showAgentRunsDialog,
    showAgentProposalsDialog,
    skillDialogType,
    showAKCDialog,
    drawerOpen,
    settingsOpen,
    connectDialogOpen,
    renameDialog,
    deleteSessionDialog,
    newSessionDialogOpen,
    stagedReviewOpen,
    switchToStagedConfirm,
    clearConfirm,
    akcShortName,
    akcConfig,
    akcIntroShown,
  ]);

  // Decide how a clear-board request is handled, based on how protected the
  // board is (task: confirm before clearing a named or locked visualization):
  //   • locked   → keyboard esc-esc does nothing; the button asks for an
  //                emphatic confirmation warning everything will be removed.
  //   • named    → confirm before clearing (both esc-esc and the button).
  //   • unnamed  → clear immediately, no confirmation.
  const requestClear = useCallback(
    (source) => {
      const action = decideClearAction({
        locked: canvasLocked,
        named: !!currentSessionName,
        source,
      });
      if (action === 'clear') {
        clearVisualization();
      } else if (action === 'confirm') {
        setClearConfirm({ locked: false });
      } else if (action === 'confirm-locked') {
        setClearConfirm({ locked: true });
      }
      // 'noop' — locked board, esc-esc does nothing.
    },
    [canvasLocked, currentSessionName, clearVisualization]
  );

  // The capture-phase keydown listener is registered once and reads the latest
  // decision logic through a ref, so the double-Escape timer isn't reset by
  // re-registration whenever the guard inputs change.
  const requestClearRef = useRef(requestClear);
  useLayoutEffect(() => {
    requestClearRef.current = requestClear;
  }, [requestClear]);

  // Double-Escape to clear the canvas (works even from input fields)
  useEffect(() => {
    let lastEscape = 0;
    const handleKeyDown = (e) => {
      if (e.key !== 'Escape') return;
      if (e.repeat) return;
      if (dialogOpenRef.current) return;
      const now = Date.now();
      if (now - lastEscape < 400) {
        requestClearRef.current('keyboard');
        lastEscape = 0;
      } else {
        lastEscape = now;
      }
    };
    // Capture phase so it fires even when focus is inside an input/textarea
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, []);

  // ── Session bootstrap (once) ────────────────────────────────────────────
  // Reflect the initial session id in the URL and, when it came from a
  // ?session= share link, load its content from the server. A freshly
  // generated id has no server content yet — it materialises on first save.
  useEffect(() => {
    reflectSessionUrl(sessionId);
    const urlSession = _urlParams.get('session');
    if (!sessionStore.isValidSessionId(urlSession)) {
      startingSessionRef.current = sessionId;
      markModeUnknown(sessionId);
    }
    const scope = captureCanvasScope();
    let expired = false;
    let timer;
    let settleBootstrapWait;
    if (sessionStore.isValidSessionId(urlSession)) {
      markModeUnknown(urlSession);
      sessionStore.touchSession(urlSession);
      // This id came in via a ?session= deep link, so a 404 means the linked
      // session is gone/expired (not a brand-new local one) — surface it rather
      // than silently opening an empty canvas (contract §5.3).
      const load = loadSessionFromServer(urlSession, {
        eagerConnect: true,
        isStale: () => expired || scope.canvasReplaced(),
        onMissing: () => showNotification('info', t('sessions.link_not_found')),
        // Settled direct after the wait: connect its stream anyway, as the
        // load would have, so its ready handler reads the mode again should
        // someone else materialise it and switch it to staged meanwhile.
        onStaleMissing: () => {
          if (sessionIdRef.current !== urlSession) return;
          // Seq 0, as the in-time 404 does: any later seq is someone else's
          // write this empty load never saw, and is loaded on ready.
          ensureSyncConnected(urlSession)?.setBaseline({}, { seq: 0 });
        },
      });
      // Bound the wait even if fetch never settles. Invalidate the load itself
      // too, so a late reply cannot undo the view/focus that proceeds afterward.
      urlSessionBootstrapRef.current = Promise.race([
        load,
        new Promise((resolve) => {
          settleBootstrapWait = resolve;
          timer = setTimeout(() => {
            expired = true;
            resolve();
          }, SYNC_REQUEST_TIMEOUT_MS);
        }),
      ])
        .catch(() => {}) // The loader logs errors; URL view/focus may still proceed.
        .finally(() => clearTimeout(timer));
    }
    setSessionsVersion((v) => v + 1);
    return () => {
      expired = true;
      clearTimeout(timer);
      // StrictMode may already have a saved-view effect awaiting this promise.
      // Release that waiter when retiring this bootstrap, even if fetch hangs.
      settleBootstrapWait?.();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Refresh recent-session names from the server whenever the drawer opens
  // (localStorage names are only a cached hint — design 3.6).
  useEffect(() => {
    if (!drawerOpen) return;
    let cancelled = false;
    api
      .listServerSessions()
      .then(({ sessions: serverSessions }) => {
        if (cancelled) return;
        let changed = false;
        for (const s of serverSessions || []) {
          // A null server name means the session hasn't materialised with a
          // name yet (or was never renamed) — never overwrite a locally kept
          // name with it (R7). The backend now materialises on rename
          // (get-or-create), so this is defense in depth for any session
          // renamed before that fix, or one that simply has no name.
          if (s.name != null && sessionStore.hasSession(s.id)) {
            sessionStore.renameSession(s.id, s.name);
            changed = true;
          }
        }
        if (changed) setSessionsVersion((v) => v + 1);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [drawerOpen]);

  // Load schema, presentation, stats and UI capabilities on startup (runs once)
  useEffect(() => {
    const loadConfig = async () => {
      try {
        const [schemaData, presentationData, statsData, capabilitiesData] = await Promise.all([
          api.getSchema(),
          api.getPresentation(),
          api.getGraphStats(),
          api.getUiCapabilities().catch(() => ({ llm_available: false })),
        ]);
        // Apply backend default language if no user override
        if (presentationData?.default_language) {
          const urlLang = new URLSearchParams(window.location.search).get('lang');
          const storedLang = localStorage.getItem('app_language');
          if (!urlLang && !storedLang) {
            setLanguage(presentationData.default_language);
          }
        }
        setConfig(schemaData, presentationData, t, language);
        setStats(statsData);
        setLlmAvailable(capabilitiesData.llm_available ?? false);
        setModelProfilesCapability(capabilitiesData.model_profiles);
        setStagedAvailable(capabilitiesData.staged_sessions === true);
        api.setStagingAvailable(capabilitiesData.staged_sessions === true);
        setDefaultWriteMode(
          capabilitiesData.session_write_mode_default === 'staged' ? 'staged' : 'direct'
        );
        settleStartingSessionRef.current?.(capabilitiesData);
      } catch (error) {
        console.error('Error loading configuration:', error);
        api.getGraphStats().then(setStats).catch(console.error);
        setLlmAvailable(false);
        settleStartingSessionRef.current?.(null);
      }
    };
    loadConfig();
  }, [setConfig, setStats, setLlmAvailable, setModelProfilesCapability, t, setLanguage, language]);

  useEffect(() => {
    if (!akcShortName) return;
    api
      .getCollectConfig(akcShortName)
      .then((data) => setAkcConfig(data))
      .catch((err) => console.error('[App] Failed to load AKC config:', err));
  }, [akcShortName]);

  // Trigger guide from URL param ?guide=<id> — fires once when presentation first becomes available
  useEffect(() => {
    if (!presentation?.guides?.length || urlGuideStartedRef.current) return;
    const urlGuideId = new URLSearchParams(window.location.search).get('guide');
    if (!urlGuideId) return;
    const guide = presentation.guides.find((g) => g.id === urlGuideId);
    if (guide) {
      urlGuideStartedRef.current = true;
      startGuide(guide);
    }
  }, [presentation, startGuide]);

  // Load saved view from URL param ?view=<name> — fires once after stats confirm backend is ready
  useEffect(() => {
    if (!stats || urlViewLoadedRef.current) return;
    const urlViewName = new URLSearchParams(window.location.search).get('view');
    if (!urlViewName) return;
    urlViewLoadedRef.current = true;
    (async () => {
      try {
        await loadUrlSavedView({
          name: urlViewName,
          sessionBootstrap: urlSessionBootstrapRef.current,
          sessionId: urlSessionIdRef.current,
          getCurrentSessionId: () => sessionIdRef.current,
          getSavedView: api.getSavedView,
          getCanvasEditEpoch: () => canvasEditEpochRef.current,
          applyView: (result) => {
            const positioned = result.nodes.map((n) =>
              result.positions?.[n.id] ? { ...n, _savedPosition: result.positions[n.id] } : n
            );
            clearVisualization();
            addNodesToVisualization(positioned, result.edges || []);
            if (result.hidden_node_ids?.length) setHiddenNodeIds(result.hidden_node_ids);
            if (result.groups?.length)
              setPendingGroups({ groups: result.groups, parentIds: result.parentIds || {} });
            if (result.annotations?.length) setPendingAnnotations(result.annotations);
          },
        });
      } catch (err) {
        console.error('[App] Failed to load view from URL:', err);
      } finally {
        setUrlViewSettled(true);
      }
    })();
  }, [
    stats,
    clearVisualization,
    addNodesToVisualization,
    setHiddenNodeIds,
    setPendingGroups,
    setPendingAnnotations,
  ]);

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);

  // "Bring everyone here": the canvas camera request, the summoned-viewer
  // notice with its Return point, and the summon action itself.
  const {
    viewportRequest,
    summonNotice,
    receiveSummon,
    returnFromSummon,
    dismissSummonNotice,
    summonEveryone,
  } = useViewportSummon({
    sessionId,
    latestViewport,
    canvasRef: canvasContainerRef,
    showNotification,
    t,
  });

  // Arrive from a teleport: ?node=<id> focuses that node, and ?from_graph
  // names where the visitor came from. This is the receiving half of the
  // navigation contract whose outbound half handleTeleportToSourceGraph
  // builds — the parameter names live in backend/federation/teleport.py, so a
  // route this app hands out is one it can also consume.
  useEffect(() => {
    // Waits for ?view= to settle: a saved-view load clears the canvas after its
    // own await, and a gui_url may legitimately carry both parameters.
    if (!stats || !urlViewSettled || urlFocusNodeLoadedRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const focusId = params.get('node');
    if (!focusId) return;
    urlFocusNodeLoadedRef.current = true;
    const searchContext = params.get('q');
    const fromGraph = params.get('from_graph');
    (async () => {
      try {
        const result = await api.getNodeDetails(focusId);
        if (!result?.success || !result.node) {
          showNotification('info', t('federation.teleport_focus_not_found'));
          return;
        }
        addNodesToVisualization([result.node], result.edges || []);
        setFocusNodeId(result.node.id);
        // Carry the sender's search text into the search box so the visitor
        // lands on the node without losing what they were looking for.
        if (searchContext) {
          setGuideSearchInput({ text: searchContext, animated: false });
        }
        if (fromGraph) {
          showNotification('info', t('federation.teleport_returned_from', { graph: fromGraph }));
        }
      } catch (err) {
        console.error('[App] Failed to focus node from URL:', err);
        showNotification('info', t('federation.teleport_focus_not_found'));
      }
    })();
  }, [
    stats,
    urlViewSettled,
    addNodesToVisualization,
    setFocusNodeId,
    setGuideSearchInput,
    showNotification,
    t,
  ]);

  useEffect(() => {
    const syncOfflineState = () => setIsOffline(window.navigator?.onLine === false);
    window.addEventListener('online', syncOfflineState);
    window.addEventListener('offline', syncOfflineState);
    syncOfflineState();
    return () => {
      window.removeEventListener('online', syncOfflineState);
      window.removeEventListener('offline', syncOfflineState);
    };
  }, []);

  // Callback: Selection changed in GraphCanvas
  const handleSelectionChange = useCallback(
    (selectedNodes) => {
      // Store full node data for the selected nodes
      const selectedWithData = selectedNodes
        .filter((n) => n.type !== 'group')
        .map((n) => {
          // n.data contains the full node info from the backend
          return n.data || n;
        });
      setSelectedGraphNodes(selectedWithData);
      // Advertise the local selection as selection claims so collaborators see
      // colored markers on the elements this user is working with (design 3.5).
      // Extended to every annotation kind, not just graph nodes. Purely
      // cosmetic (task-annotation-exclusive-edit-leases): selecting an
      // annotation never blocks another client's dragging or editing of it —
      // only an actual edit-lease acquisition (handleBeginEditing below) does.
      const claimIds = selectedNodes
        .filter((n) => n.type === 'custom' || CANVAS_ANNOTATION_TYPES.has(n.type))
        .map((n) => n.id);
      syncRef.current?.setLocalSelection(claimIds);
    },
    [setSelectedGraphNodes]
  );

  // GraphCanvas's edit-lease acquire/release pair (task-annotation-exclusive-
  // edit-leases): every real edit-start entry point in the canvas package
  // (text field open, geometry gesture, property editor, bulk mutation)
  // calls these via AnnotationContext before mutating. Thin wrappers over
  // the sync client so GraphCanvas never has to know about SessionSyncClient
  // directly — mirrors how handleSelectionChange above wraps
  // setLocalSelection. Optimistic-fail-open when no session is connected
  // yet (nothing server-side could hold a competing lease before the
  // session exists), matching SessionSyncClient.beginEditing's own
  // reasoning.
  const handleBeginEditing = useCallback(async (elementIds) => {
    if (!syncRef.current) return { granted: elementIds || [], denied: {} };
    return syncRef.current.beginEditing(elementIds);
  }, []);
  const handleEndEditing = useCallback((elementIds) => {
    syncRef.current?.endEditing(elementIds);
  }, []);

  // Callback: Double-click on node
  const handleNodeDoubleClick = useCallback(
    async (nodeId, nodeData) => {
      // If it's a SavedView, load it directly
      if (nodeData.type === 'SavedView' || nodeData.nodeType === 'SavedView') {
        await loadSavedViewNode({
          nodeData,
          getNodeDetails: api.getNodeDetails,
          clearVisualization,
          addNodesToVisualization,
          setPendingGroups,
          setPendingAnnotations,
          showNotification,
        });
        return;
      }

      // For other nodes, show detail dialog
      setDetailNode({ id: nodeId, data: nodeData });
    },
    [
      clearVisualization,
      addNodesToVisualization,
      setPendingGroups,
      setPendingAnnotations,
      setDetailNode,
      showNotification,
    ]
  );

  // Callback: leave for the graph that owns this node. The status-to-outcome
  // mapping lives in utils/teleport.js so the canvas context menu and the
  // search result list answer the four defined behaviours identically.
  const handleTeleportToSourceGraph = useCallback(
    (nodeId, _nodeData, { searchQuery = '' } = {}) =>
      teleportToSourceGraph({
        nodeId,
        sessionId,
        searchQuery,
        t,
        showNotification,
      }),
    [sessionId, showNotification, t]
  );

  // Callback: open the node detail dialog directly on its change-history tab
  const handleViewNodeHistory = useCallback(
    (nodeId, nodeData) => {
      setDetailNode({ id: nodeId, data: nodeData || {}, view: 'history' });
    },
    [setDetailNode]
  );

  // Callback: Expand node to show related nodes
  const handleExpand = useCallback(
    (nodeId) =>
      expandNode({
        nodeId,
        getRelatedNodes: api.getRelatedNodes,
        addNodesToVisualization,
        showNotification,
      }),
    [addNodesToVisualization, showNotification]
  );

  // Callback: Edit node
  const handleEdit = useCallback(
    async (nodeId, nodeData) => {
      if (nodeData.type === 'Agent') {
        await openAgentEditor({
          agent: nodeData,
          getNodeDetails: api.getNodeDetails,
          openEditor: (data) => {
            setEditingAgentData(data);
            setShowAgentDialog(true);
          },
          showNotification,
        });
      } else if (nodeData.type === 'EventSubscription') {
        setEditingSubscriptionData(nodeData);
        setShowSubscriptionDialog(true);
      } else if (nodeData.type === 'ActiveKnowledgeCollection') {
        setEditingAKCData({ node: nodeData });
        setShowAKCDialog(true);
      } else if (schema?.node_types?.[nodeData.type]?.ui_form === 'skill') {
        setEditingSkillData(nodeData);
        setSkillDialogType(nodeData.type);
      } else {
        setEditingNode({ id: nodeId, data: nodeData });
      }
    },
    [schema, setEditingNode, setEditingSkillData, setSkillDialogType, showNotification]
  );

  // Callback: Hide node
  const handleHide = useCallback(
    (nodeId) => {
      toggleNodeVisibility(nodeId);
      showNotification('info', 'Node hidden');
    },
    [toggleNodeVisibility, showNotification]
  );

  // Callback: Hide multiple nodes
  const handleHideMultiple = useCallback(
    (nodeIds) => {
      nodeIds.forEach((id) => toggleNodeVisibility(id));
      showNotification('info', `${nodeIds.length} nodes hidden`);
    },
    [toggleNodeVisibility, showNotification]
  );

  // Callback: Hide edge
  const handleHideEdge = useCallback(
    (edgeId) => {
      toggleEdgeVisibility(edgeId);
      showNotification('info', 'Edge hidden');
    },
    [toggleEdgeVisibility, showNotification]
  );

  // Dim/restore actions (task-session-focus-dimming-controls): session-local
  // focus, never a graph edit. Bulk primitives — a single node/edge context
  // menu action passes a one-element array, a multi-selection or an
  // incident-edges action passes the whole set.
  const handleDimNodes = useCallback(
    (nodeIds) => {
      dimNodes(nodeIds);
      showNotification('info', t('history.desc.nodes_dimmed', { count: nodeIds.length }));
    },
    [dimNodes, showNotification, t]
  );

  const handleRestoreNodes = useCallback(
    (nodeIds) => {
      restoreNodes(nodeIds);
      showNotification('info', t('history.desc.nodes_undimmed', { count: nodeIds.length }));
    },
    [restoreNodes, showNotification, t]
  );

  const handleDimEdges = useCallback(
    (edgeIds) => {
      dimEdges(edgeIds);
      showNotification('info', t('history.desc.edges_dimmed', { count: edgeIds.length }));
    },
    [dimEdges, showNotification, t]
  );

  const handleRestoreEdges = useCallback(
    (edgeIds) => {
      restoreEdges(edgeIds);
      showNotification('info', t('history.desc.edges_undimmed', { count: edgeIds.length }));
    },
    [restoreEdges, showNotification, t]
  );

  // Callback: Delete edge (from backend and visualization)
  const handleDeleteEdge = useCallback(
    (edgeId) =>
      deleteEdgeEverywhere({
        edgeId,
        deleteEdge: api.deleteEdge,
        removeEdge,
        syncRef,
        showNotification,
      }),
    [removeEdge, showNotification, syncRef]
  );

  // Callback: Edit edge - opens EditEdgeDialog
  const handleEditEdge = useCallback(
    (edgeId, edgeData) => {
      const edge = edges.find((e) => e.id === edgeId);
      if (edge) {
        setEditingEdge({ ...edge, ...edgeData });
      }
    },
    [edges, setEditingEdge]
  );

  // Callback: Save edge updates from EditEdgeDialog
  const handleEdgeUpdate = useCallback(
    async (updates) => {
      if (!editingEdge) return;
      await applyEdgeUpdate({
        editingEdge,
        updates,
        updateEdge: api.updateEdge,
        updateEdgeData,
        syncRef,
        setEditingEdge,
        showNotification,
      });
    },
    [editingEdge, setEditingEdge, updateEdgeData, showNotification, syncRef]
  );

  // Callback: Change an edge's relationship type from the context menu.
  // Persists to the backend and updates the single edge in place so groups and
  // node positions are preserved.
  const handleSetEdgeType = useCallback(
    (edgeId, type) =>
      setEdgeType({
        edgeId,
        type,
        updateEdge: api.updateEdge,
        updateEdgeData,
        syncRef,
        showNotification,
      }),
    [updateEdgeData, showNotification, syncRef]
  );

  // Callback: Connect nodes (from drag-connect in canvas)
  const handleConnect = useCallback(
    (params) =>
      connectNodes({
        source: params.source,
        target: params.target,
        addEdge: api.addEdge,
        addNodesToVisualization,
        syncRef,
        showNotification,
      }),
    [addNodesToVisualization, showNotification, syncRef]
  );

  // Callback: Show only selected nodes (hide all others)
  const handleShowOnly = useCallback(
    (nodeIds) => {
      const keepSet = new Set(nodeIds);
      const idsToHide = nodes.filter((n) => !keepSet.has(n.id)).map((n) => n.id);
      setHiddenNodeIds(idsToHide);
      showNotification('info', t('notifications.showing_nodes', { count: nodeIds.length }));
    },
    [nodes, setHiddenNodeIds, showNotification]
  );

  // Callback: Delete node - shows dialog
  const handleDelete = useCallback(
    (nodeId) => {
      const node = nodes.find((n) => n.id === nodeId);
      setDeleteDialog({
        nodeId,
        nodeName: node?.name || node?.data?.label || nodeId,
        isMultiple: false,
      });
    },
    [nodes, setDeleteDialog]
  );

  // Callback: Delete multiple nodes - shows dialog
  const handleDeleteMultiple = useCallback(
    (nodeIds) => {
      const nodeNames = nodeIds.map((id) => {
        const node = nodes.find((n) => n.id === id);
        return node?.name || node?.data?.label || id;
      });
      setDeleteDialog({
        nodeIds,
        nodeNames,
        isMultiple: true,
      });
    },
    [nodes, setDeleteDialog]
  );

  // Confirm delete
  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialog) return;
    await confirmNodeDelete({
      deleteDialog,
      deleteNodes: api.deleteNodes,
      removeNode,
      setDeleteDialog,
      showNotification,
    });
  }, [deleteDialog, setDeleteDialog, removeNode, showNotification]);

  // Toolbar: trigger group creation in GraphCanvas
  const handleToolbarCreateGroup = useCallback(() => {
    setCreateGroupSignal((prev) => prev + 1);
  }, []);

  // Persist the current canvas to the server for the active session.
  // viewData carries node positions and groups collected by GraphCanvas;
  // node references come from the store. Session content lives server-side
  // and is propagated as incremental ops (design step 6). The session is
  // materialised server-side on the first non-empty save (get-or-create), so a
  // fresh/shared id needs no eager POST.
  // Whether the active session already exists server-side (a sync client is
  // connected for it). Once true, an empty canvas is real content, not an
  // unmaterialised session to protect (D14 — see design §8.1 R4). Shared by
  // persistSessionSnapshot and scheduleAutoSave, which both gate on emptiness.
  const isSessionMaterialized = useCallback(
    () => syncRef.current?.sessionId === sessionId && syncRef.current.connected,
    [sessionId]
  );

  const persistSessionSnapshot = useCallback(
    (viewData) => {
      const state = useGraphStore.getState();
      const targetId = sessionId;
      // Suppress only while the session has never materialised server-side: an
      // empty, never-edited session must not register (D13).
      //
      // "Empty" has to mean the whole canvas, not just the graph. Annotations
      // and group boxes live only in ReactFlow's own node state — never in the
      // graph store — so asking `state.nodes.length` alone declared a session
      // holding nothing but annotations to be empty and dropped every save.
      // A canvas used purely for annotating (a blank session, notes and
      // shapes on it, no graph nodes at all) therefore persisted nothing, and
      // the annotations vanished the moment the session was switched away and
      // back. `viewData` is the snapshot being written and already carries
      // both lists, so the question can simply be asked correctly here.
      if (
        !shouldPersistSnapshot({
          isMaterialized: isSessionMaterialized(),
          graphNodeCount: state.nodes.length,
          annotationCount: viewData?.annotations?.length ?? 0,
          groupCount: viewData?.groups?.length ?? 0,
        })
      ) {
        return;
      }
      const positions = {};
      const parentIds = {};
      (viewData?.nodes || []).forEach((n) => {
        if (n.position) positions[n.id] = n.position;
        if (n.parentId) parentIds[n.id] = n.parentId;
      });
      // Annotations carry group boxes plus the free-floating overlays (notes,
      // labels, arrows) the canvas collects in viewData.annotations. All kinds
      // share one server-side annotation list (design 3.1).
      const annotationDocument = legacyMetadataToAnnotationDocument({
        groups: viewData?.groups || [],
        parentIds,
        annotations: viewData?.annotations || [],
      });
      const nextState = {
        node_refs: state.nodes.map((n) => n.id),
        positions,
        hidden_node_ids: state.hiddenNodeIds || [],
        hidden_edge_ids: state.hiddenEdgeIds || [],
        dimmed_node_ids: state.dimmedNodeIds || [],
        dimmed_edge_ids: state.dimmedEdgeIds || [],
        edge_intensity: typeof state.edgeIntensity === 'number' ? state.edgeIntensity : 1.0,
        annotation_schema_version: annotationDocument.schema_version,
        annotations: annotationDocument.annotations,
      };
      // Emit the change as incremental ops (design step 6, replacing step 4's
      // full-state PUT). Connecting here — the first non-empty save — materialises
      // the session server-side lazily, preserving the "no empty session files"
      // behaviour of step 4 (an empty, never-edited session never connects).
      const sync = ensureSyncConnected(targetId);
      sync?.syncState(nextState);
      sessionStore.touchSession(targetId);
      setSessionsVersion((v) => v + 1);
    },
    [sessionId, ensureSyncConnected, isSessionMaterialized]
  );

  // Human clipboard-paste / file-upload image creation (GraphCanvas's
  // onImageIngest). The server validates, optimizes and embeds the image (the
  // same pipeline the MCP create_image_annotation tool uses) and returns the
  // finished annotation, which applyIngestedImageOptimistically applies to
  // the canvas — through the same call site every other remote/self-authored
  // annotation change goes through (applyRemoteOp) — rather than waiting on
  // this browser's own SSE subscription to deliver it back. It also folds
  // the op into the sync client's baseline directly (foldLocalOp), so a
  // snapshot save triggered before the echo arrives (e.g. the user
  // repositions the annotation right away) diffs against a baseline that
  // already has it, instead of re-emitting a redundant create. The SSE
  // subscription still exists and still receives this op (attributed to a
  // dedicated server client id rather than this browser's own, precisely so
  // sessionSyncClient.js does not drop it as an echo of a self-authored op —
  // see backend/service/rest_api.py's ingest_session_image): whichever of
  // {this direct call, that echo} reaches applyRemoteOp first is the one that
  // actually renders it (createSelfEchoDedup.claim, consulted inside
  // applyRemoteOp's annotation_created/updated case) — the other is a no-op,
  // since the two are not guaranteed to arrive in a fixed order. `whenReady()`
  // waits for the same "session exists server-side" fact the op queue's own
  // flush already guards on (D13/D14: a session is never created until its
  // first real write), since this request bypasses that queue.
  //
  // The annotation id is generated *here*, client-side, and reserved in the
  // dedup (markPending) before the request is even sent — passed through as
  // `annotationId` so the server uses it rather than minting its own. This
  // must happen before either of the two deliveries can possibly reach
  // applyRemoteOp's claim() check, or that first delivery would find nothing
  // reserved and treat itself as an ordinary, never-raced annotation.
  const handleImageIngest = useCallback(
    async (dataUrl, position) => {
      const targetId = sessionId;
      const sync = ensureSyncConnected(targetId);
      const dedup = selfIngestedImageAnnotationIdsRef.current;
      const annotationId = crypto.randomUUID();
      dedup.markPending(annotationId);
      let delivered = false;
      try {
        await sync?.whenReady();
        if (syncRef.current?.sessionId !== targetId) return; // switched sessions mid-flight
        const result = await api.ingestSessionImage(targetId, {
          x: position.x,
          y: position.y,
          imageData: dataUrl,
          annotationId,
        });
        // Re-check after this second await too: the ingest POST is a real
        // network round trip (server-side fetch/optimize included), long
        // enough for the user to have switched to a different session while
        // it was in flight. The annotation is real and correctly created for
        // `targetId` server-side either way — only applying it to whatever
        // session happens to be active *now* (a different one) would be
        // wrong, since applyRemoteOp/foldLocalOp act on the current graph
        // store and the current sync client, not on `targetId` specifically.
        if (syncRef.current?.sessionId !== targetId) return; // switched sessions mid-flight
        const carriedAnnotation = await applyIngestedImageOptimistically({
          annotation: result?.annotation,
          applyRemoteOp,
          foldLocalOp: (op) => syncRef.current?.foldLocalOp(op),
        });
        // A 200 whose body carries no usable annotation is a failure, not a
        // delivery. Marking it delivered here is how "I picked a file and
        // nothing happened — no image, no error" used to arise: the canvas
        // never changed and nothing said why. Throw into the same catch every
        // other failure already uses, so it reports identically.
        if (!carriedAnnotation) {
          throw new Error(t('canvas.image_ingest_failed'));
        }
        delivered = true;
        sessionStore.touchSession(targetId);
        setSessionsVersion((v) => v + 1);
      } catch (error) {
        console.error('Error ingesting image:', error);
        showNotification('error', error.message || t('canvas.image_ingest_failed'));
      } finally {
        // This attempt never resolved into a rendered-here annotation: either
        // no server-side write ever happened at all (the request failed, or
        // this bailed out before ever sending it — the first guard above), or
        // one did but this browser gave up tracking it (switched sessions
        // after the POST resolved — the second guard above; a switch caught
        // by the first guard, before the POST, can never have a completed
        // write behind it). Either way, forgetting the mark is correct: there
        // is nothing left for *this* browser's own echo handling to resolve
        // against, so nothing should stay reserved
        // waiting for it.
        if (!delivered) dedup.forget(annotationId);
      }
    },
    [sessionId, ensureSyncConnected, syncRef, showNotification, t, applyRemoteOp]
  );

  // Ask GraphCanvas for a snapshot (positions + groups); the callback runs
  // after the snapshot has been persisted for the current session.
  const requestSessionSnapshot = useCallback((onDone) => {
    snapshotCallbacksRef.current.push(onDone);
    setSaveViewSignal((prev) => prev + 1);
  }, []);

  // Callback from GraphCanvas when the saveViewSignal round-trip completes.
  // Always persists the session snapshot; additionally opens the Save View
  // dialog when that was what triggered the signal (toolbar button).
  const handleSaveView = useCallback(
    (viewData) => {
      setSaveViewSignal(0); // Reset signal so it doesn't re-trigger
      persistSessionSnapshot(viewData);
      const callbacks = snapshotCallbacksRef.current.splice(0);
      callbacks.forEach((cb) => cb?.());
      if (viewDialogRequestedRef.current) {
        viewDialogRequestedRef.current = false;
        setSaveViewDialog({ viewData });
      }
    },
    [persistSessionSnapshot]
  );

  // Auto-save the current session (debounced) so it can be restored from the
  // session drawer later. Scheduled both from store-level changes (effect
  // below) and from canvas-internal changes that never reach the store, like
  // node drags and group creation.
  const autoSaveTimerRef = useRef(null);
  const scheduleAutoSave = useCallback(() => {
    // Mirrors persistSessionSnapshot's own emptiness guard (D14): without this
    // check, an empty canvas never even reaches persistSessionSnapshot, since
    // this guard runs first on every store-level change (last node removed,
    // double-Escape clear, an MCP clear_visualization).
    if (useGraphStore.getState().nodes.length === 0 && !isSessionMaterialized()) return;
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    autoSaveTimerRef.current = setTimeout(() => {
      requestSessionSnapshot(null);
    }, 1500);
  }, [requestSessionSnapshot, isSessionMaterialized]);

  // Realtime publish timing for annotation changes — the "accepted operation
  // timing" from task-annotation-shared-session-realtime's slice_scope, split
  // out from the generic 1500ms autosave debounce above (which stays as-is
  // for graph-node positions and the local session-restore bookkeeping the
  // effect below triggers on every store-level change). The actual
  // immediate-vs-debounced decision lives in annotationChangeScheduler.js
  // (framework-agnostic, unit-tested against fake timers); this is just its
  // React wiring. `publishRef` carries the latest emptiness guard and
  // `requestSessionSnapshot` so the scheduler (created once and kept for the
  // component's lifetime) always calls through to the current session, not a
  // stale one captured at creation time.
  const publishRef = useRef(() => {});
  publishRef.current = () => {
    // No emptiness guard here, deliberately. This runs only for annotation
    // changes, and an annotation change IS a real write — it is precisely the
    // case D14's guard must not suppress. Mirroring scheduleAutoSave's
    // graph-node count instead meant that on a canvas with no graph nodes,
    // every annotation change was discarded before a snapshot was even
    // requested. `persistSessionSnapshot` still applies the corrected
    // whole-canvas guard at the end of the round trip, so a genuinely empty,
    // never-materialised session still does not register (D13).
    requestSessionSnapshot(null);
  };
  const annotationSchedulerRef = useRef(null);
  if (!annotationSchedulerRef.current) {
    annotationSchedulerRef.current = createAnnotationChangeScheduler({
      publish: () => publishRef.current(),
    });
  }
  const publishAnnotationChange = useCallback((kind) => {
    canvasEditEpochRef.current += 1;
    annotationSchedulerRef.current.schedule(kind);
  }, []);

  // GraphCanvas's onAnnotationChange callback (see AnnotationContext's
  // notifyChange doc comment for the kind vocabulary).
  const handleAnnotationChange = useCallback(
    (kind) => publishAnnotationChange(kind),
    [publishAnnotationChange]
  );

  useEffect(() => {
    return () => annotationSchedulerRef.current?.clearPending();
  }, []);

  // Position-change callback GraphCanvas's onNodePositionChange fires for
  // both graph-node and annotation drags/moves alike. Graph node moves keep
  // the existing 1500ms scheduleAutoSave debounce (unaffected by this task);
  // annotation geometry publishes immediately at this release-time call.
  const handleNodePositionChange = useCallback(
    (_nodeId, _position, nodeType) => {
      canvasEditEpochRef.current += 1;
      if (nodeType && CANVAS_ANNOTATION_TYPES.has(nodeType)) {
        publishAnnotationChange('geometry');
        return;
      }
      scheduleAutoSave();
    },
    [publishAnnotationChange, scheduleAutoSave]
  );

  useEffect(() => {
    scheduleAutoSave();
    return () => {
      if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    };
  }, [
    nodes,
    edges,
    hiddenNodeIds,
    hiddenEdgeIds,
    dimmedNodeIds,
    dimmedEdgeIds,
    edgeIntensity,
    scheduleAutoSave,
  ]);

  // Callback: Create group (called when group is created inside GraphCanvas).
  // A group box is itself an annotation kind (ANNOTATION_TYPES includes
  // 'group'), so its creation publishes immediately like any other
  // create/delete/style/geometry annotation change, not the generic 1500ms
  // autosave debounce.
  const handleCreateGroup = useCallback(
    (position, groupNode) => {
      showNotification('success', 'Group created');
      publishAnnotationChange('create');
    },
    [showNotification, publishAnnotationChange]
  );

  // Confirm save view
  const handleConfirmSaveView = useCallback(
    async (name) => {
      if (!saveViewDialog) return;

      setIsSavingView(true);
      try {
        const parentIds = Object.fromEntries(
          saveViewDialog.viewData.nodes.filter((n) => n.parentId).map((n) => [n.id, n.parentId])
        );
        const annotationDocument = legacyMetadataToAnnotationDocument({
          groups: saveViewDialog.viewData.groups || [],
          parentIds,
          annotations: saveViewDialog.viewData.annotations || [],
        });
        const legacyAnnotationMetadata = annotationDocumentToLegacyMetadata(annotationDocument);
        const viewNode = {
          name,
          type: 'SavedView',
          description: `Saved view: ${name}`,
          summary: `Contains ${saveViewDialog.viewData.nodes.length} nodes`,
          metadata: {
            node_ids: saveViewDialog.viewData.nodes.map((n) => n.id),
            positions: Object.fromEntries(
              saveViewDialog.viewData.nodes.map((n) => [n.id, n.position])
            ),
            parentIds,
            edge_ids: (saveViewDialog.viewData.edges || []).map((e) => e.id),
            edges: saveViewDialog.viewData.edges || [],
            annotation_schema_version: annotationDocument.schema_version,
            annotation_document: annotationDocument,
            groups: legacyAnnotationMetadata.groups,
            annotations: legacyAnnotationMetadata.annotations,
          },
          communities: [],
        };

        await api.addNodes([viewNode], []);
        showNotification('success', `View "${name}" saved`);
      } catch (error) {
        console.error('Error saving view:', error);
        showNotification('error', 'Could not save view');
      } finally {
        setIsSavingView(false);
        setSaveViewDialog(null);
      }
    },
    [saveViewDialog, showNotification]
  );

  // Callback: Create subscription
  const handleCreateSubscription = useCallback(() => {
    setShowSubscriptionDialog(true);
  }, []);

  // Callback: Create agent
  const handleCreateAgent = useCallback(() => {
    setEditingAgentData(null);
    setShowAgentDialog(true);
  }, []);

  // Callback: View durable AgentRun history (optionally scoped to one agent)
  const handleViewAgentRuns = useCallback((agentId = null) => {
    setAgentRunsAgentId(agentId);
    setShowAgentDialog(false);
    setEditingAgentData(null);
    setShowAgentRunsDialog(true);
  }, []);

  // Callback: View agent proposals (optionally scoped to one agent)
  const handleViewAgentProposals = useCallback((agentId = null) => {
    setAgentProposalsAgentId(agentId);
    setShowAgentDialog(false);
    setEditingAgentData(null);
    setShowAgentProposalsDialog(true);
  }, []);

  // Save subscription node
  const handleSaveSubscription = useCallback(
    async (data) => {
      try {
        if (data.id && data.updates) {
          await persistNodeUpdates({
            entries: [{ id: data.id, updates: data.updates }],
            updateNode: api.updateNode,
            updateVisualization,
            onApplied: () => setEditingSubscriptionData(null),
          });
          showNotification(
            'success',
            t('notifications.subscription_updated', { name: data.updates.name })
          );
        } else {
          await persistNewNodes({
            nodes: [data],
            addNodes: api.addNodes,
            addNodesToVisualization,
            toCanvas: (result) => createdNodesToCanvas([data], result),
          });
          showNotification('success', t('notifications.subscription_created', { name: data.name }));
        }
      } catch (error) {
        console.error('Error saving subscription:', error);
        showNotification(
          'error',
          data?.updates
            ? t('notifications.subscription_update_error')
            : t('notifications.subscription_error')
        );
      }
    },
    [addNodesToVisualization, updateVisualization, showNotification, t]
  );

  // Save agent nodes (create or update)
  const handleSaveAgent = useCallback(
    async (data) => {
      try {
        if (data.agentId) {
          // UPDATE
          await persistNodeUpdates({
            entries: agentUpdateEntries(data),
            updateNode: api.updateNode,
            updateVisualization,
          });

          showNotification('success', 'Agent updated');
        } else {
          // CREATE
          const { nodes: agentNodes, edges: agentEdges } = data;
          await persistNewNodes({
            nodes: agentNodes,
            edges: agentEdges,
            addNodes: api.addNodes,
            addNodesToVisualization,
            toCanvas: (result) => agentCreateToCanvas(agentNodes, agentEdges, result),
          });

          const agentNode = agentNodes.find((n) => n.type === 'Agent');
          showNotification('success', `Agent "${agentNode?.name || 'Agent'}" created`);
        }
      } catch (error) {
        console.error('Error saving agent:', error);
        showNotification('error', 'Could not save agent');
      }
    },
    [addNodesToVisualization, updateVisualization, showNotification]
  );

  // Callback: Create node from toolbar
  const handleCreateNodeForType = useCallback(
    (nodeType) => {
      if (schema?.node_types?.[nodeType]?.ui_form === 'skill') {
        setEditingSkillData(null);
        setSkillDialogType(nodeType);
      } else {
        setCreateNodeType(nodeType);
      }
    },
    [schema]
  );

  // Touch has no drag-to-canvas step to choose a position, so a node
  // created via a toolbar tap is centered in the viewport directly
  // (reusing the existing focus/center-camera primitive) instead of
  // landing wherever the layout defaults new nodes to. Desktop's
  // click-to-create and drag-to-canvas paths are unchanged.
  //
  // Deferred like FloatingSearch's identical newly-added-node case
  // (FloatingSearch.jsx): the canvas's own node state only picks up
  // addNodesToVisualization's update on a later render, so focusing
  // synchronously would target a node the canvas doesn't know about yet.
  const focusCreatedNode = useCallback(
    (createdNode) => {
      if (isCoarsePointer) {
        setTimeout(() => setFocusNodeId(createdNode.id), 100);
      }
    },
    [isCoarsePointer, setFocusNodeId]
  );

  // Create a node from CreateNodeDialog. API errors propagate so the dialog
  // can show them.
  const handleNodeCreated = useCallback(
    (node) =>
      createDialogNode({
        node,
        addNodes: api.addNodes,
        addNodesToVisualization,
        showNotification,
        onDrawn: focusCreatedNode,
      }),
    [addNodesToVisualization, showNotification, focusCreatedNode]
  );

  // Callback: Save a skill node (create or update)
  const handleSaveSkill = useCallback(
    async (skillData) => {
      try {
        if ('id' in skillData) {
          const { id, updates } = skillData;
          await persistNodeUpdates({
            entries: [{ id, updates }],
            updateNode: api.updateNode,
            updateVisualization,
          });
          showNotification('success', 'Skill updated');
        } else {
          await persistNewNodes({
            nodes: [skillData],
            addNodes: api.addNodes,
            addNodesToVisualization,
            toCanvas: (result) => createdNodesToCanvas([skillData], result),
          });
          showNotification('success', `${skillData.type} "${skillData.name}" created`);
        }
      } catch (error) {
        console.error('Error saving skill:', error);
        showNotification('error', 'Could not save skill');
      }
    },
    [updateVisualization, addNodesToVisualization, showNotification]
  );

  const handleCreateAKC = useCallback(() => {
    setEditingAKCData(null);
    setShowAKCDialog(true);
  }, []);

  const handleSaveAKC = useCallback(
    async (nodeData) => {
      try {
        if (nodeData.id) {
          const { id, ...updates } = nodeData;
          await persistNodeUpdates({
            entries: [{ id, updates }],
            updateNode: api.updateNode,
            updateVisualization,
          });
          showNotification('success', 'Knowledge collection updated');
        } else {
          await persistNewNodes({
            nodes: [nodeData],
            addNodes: api.addNodes,
            addNodesToVisualization,
            toCanvas: (result) => createdNodesToCanvas([nodeData], result),
          });
          showNotification('success', `Collection "${nodeData.name}" created`);
        }
      } catch (error) {
        console.error('Error saving AKC:', error);
        showNotification('error', 'Could not save knowledge collection');
      }
    },
    [addNodesToVisualization, updateVisualization, showNotification]
  );

  // Callback: Context menu action triggered from schema-defined callback items
  const handleContextMenuAction = useCallback(
    (actionName, nodeId, nodeData) => {
      // Dispatch named callback actions from schema context_menu entries.
      // Add cases here as new callback-type actions are implemented.
      switch (actionName) {
        default:
          console.warn(
            `[handleContextMenuAction] Unhandled action: "${actionName}". Wire it up in App.jsx.`
          );
          showNotification('info', `Action: ${actionName}`);
      }
    },
    [addNodesToVisualization, showNotification]
  );

  // Toolbar save view: signal GraphCanvas to collect positions and trigger dialog
  const handleToolbarSaveView = useCallback(() => {
    viewDialogRequestedRef.current = true;
    setSaveViewSignal((prev) => prev + 1);
  }, []);

  // ── Session navigation ──────────────────────────────────────────────────

  // Shared-session lifecycle (STRUCTURE_REVIEW B1 slice 1): load a server-backed
  // session's canvas + seed the sync baseline. Extracted into useSharedSession
  // so the transition logic is testable in isolation.
  // Bound reset used on every session switch: drops assistant history, experts,
  // node overlays and selection carried over from the previous session and bumps
  // the assistant epoch so an in-flight reply can't land in the new session.
  const resetSessionScopedUi = useCallback(
    () => resetSessionScopedState(t, language),
    [resetSessionScopedState, t, language]
  );

  const { applyServerSession, loadSessionFromServer } = useSharedSession({
    clearVisualization,
    addNodesToVisualization,
    setHiddenNodeIds,
    setHiddenEdgeIds,
    setDimmedNodeIds,
    setDimmedEdgeIds,
    setEdgeIntensity,
    setPendingGroups,
    setPendingAnnotations,
    ensureSyncConnected,
    syncRef,
    resetSessionScopedState: resetSessionScopedUi,
    onSessionPayload: adoptSessionPayload,
  });
  // Expose the latest applyServerSession to resyncFromServer (defined earlier).
  applyServerSessionRef.current = applyServerSession;

  // Leave a session that was deleted (by this user or another) for a new one
  // (design D11). A staged session is replaced by a staged one, created on the
  // server before anything is done in it, so a user who was staging keeps
  // staging (docs/SESSION_OVERLAY_CONTRACT.md §13, I2). Until the replacement
  // exists the deleted id stays active, and the server refuses anything still
  // naming it, rather than letting a write land in the graph meanwhile.
  const leaveDeletedSession = useCallback(
    async (deletedId, wasStaged) => {
      if (!wasStaged) {
        if (stagedAvailableRef.current && defaultWriteModeRef.current === 'staged') {
          // The installation's default governs the new session: created on
          // the server (the hook asked), with nothing written from the deleted
          // id meanwhile. Refused or unreachable: today's browser session.
          markModeUnknown(deletedId);
          clearVisualization();
          resetSessionScopedUi();
          try {
            const created = await api.createSession(null);
            if (!created?.id) throw new Error('no session created');
            await loadSessionFromServer(created.id, { eagerConnect: true });
            setSessionId(created.id);
            reflectSessionUrl(created.id);
            sessionStore.touchSession(created.id);
            forgetStagedSession(deletedId);
            return true;
          } catch (error) {
            console.error('Error creating the new session:', error);
          }
        }
        forgetStagedSession(deletedId);
        dropIntoFreshSession({
          freshId: api.generateVisualizationSessionId(),
          clearVisualization,
          resetSessionScopedState: resetSessionScopedUi,
          setSessionId,
          reflectSessionUrl,
        });
        return true;
      }
      clearVisualization();
      resetSessionScopedUi();
      try {
        const created = await api.createSession(null, { writeMode: 'staged' });
        if (created?.write_mode !== 'staged' || !created?.id) {
          throw new Error('replacement session is not staged');
        }
        adoptWriteMode(created.id, 'staged');
        await loadSessionFromServer(created.id, { eagerConnect: true });
        setSessionId(created.id);
        reflectSessionUrl(created.id);
        sessionStore.touchSession(created.id);
        forgetStagedSession(deletedId);
        return true;
      } catch (error) {
        console.error('Error creating the staged replacement session:', error);
        forgetStagedSession(deletedId);
        dropIntoFreshSession({
          freshId: api.generateVisualizationSessionId(),
          clearVisualization,
          resetSessionScopedState: resetSessionScopedUi,
          setSessionId,
          reflectSessionUrl,
        });
        showNotification('error', t('staged.replacement_failed'));
        return false;
      }
    },
    [
      forgetStagedSession,
      adoptWriteMode,
      markModeUnknown,
      clearVisualization,
      resetSessionScopedUi,
      loadSessionFromServer,
      setSessionId,
      showNotification,
      t,
    ]
  );

  // A session id the server refuses as retired (a deleted staged session's,
  // §5.3) answers its load like one it never held, so it opens as a
  // browser-only direct session that can never save: its stream and its
  // rename are refused. On the first such refusal, leave it as a deleted
  // staged session is left, with a notice. Not while this tab is deleting it:
  // that path leaves it.
  const retiredLeftRef = useRef(new Set());
  // Left again only after a stream has opened since: where something else
  // answers every stream with 404 (a proxy that does not route them), each
  // replacement would be refused too, and leaving must not repeat for ever.
  const retiredLeaveArmedRef = useRef(true);
  const leaveRetiredSession = useCallback(
    (id) => {
      if (!id || sessionIdRef.current !== id) return;
      if (retiredLeftRef.current.has(id) || locallyDeletingRef.current.has(id)) return;
      if (!retiredLeaveArmedRef.current) {
        console.error('Session stream refused again; not leaving a second time:', id);
        return;
      }
      retiredLeaveArmedRef.current = false;
      retiredLeftRef.current.add(id);
      // Refuse writes naming it until the replacement exists.
      adoptWriteMode(id, 'staged');
      sessionStore.removeSession(id);
      setSessionsVersion((v) => v + 1);
      showNotification('info', t('sessions.link_retired'));
      leaveDeletedSession(id, true).then(() => setSessionsVersion((v) => v + 1));
    },
    [adoptWriteMode, leaveDeletedSession, showNotification, t]
  );

  // How the app starts without a ?session= link, once the capabilities are
  // known (or failed): where the installation stages by default, in a session
  // created on the server, so that default (and the hook) decide its mode;
  // otherwise, or if that is refused or unreachable, in the browser-made id
  // it opened with, which materialises direct on first save, as before. Until
  // then the starting id holds graph writes, so nothing is written from a
  // session about to be left, and nothing shows a staged one as direct.
  const settleStartingSession = useCallback(
    async (capabilities) => {
      const startId = startingSessionRef.current;
      if (!startId) return;
      startingSessionRef.current = null;
      const settleDirect = () => adoptSessionPayload(startId, null);
      const stagesByDefault =
        capabilities?.staged_sessions === true &&
        capabilities?.session_write_mode_default === 'staged';
      // A link that fills the canvas (?view=, ?node=) loads into the session
      // it was opened in, rather than racing the create; a collection kiosk
      // (?collect=, ?akc=) keeps writing its responses as before.
      const linkedStart = ['view', 'node', 'collect', 'akc'].some((key) => _urlParams.get(key));
      if (!stagesByDefault || linkedStart) {
        settleDirect();
        return;
      }
      let created;
      try {
        created = await api.createSession(null);
      } catch (error) {
        console.error('Error creating the starting session:', error);
        settleDirect();
        return;
      }
      // Only a start the user has not moved on from is replaced: a switch, or
      // a canvas already in use, keeps the session the user is working in.
      const untouched =
        sessionIdRef.current === startId && useGraphStore.getState().nodes.length === 0;
      if (!created?.id || !untouched) {
        // Unused: nothing was written in it.
        if (created?.id) {
          api
            .deleteServerSession(created.id, api.getClientId())
            .catch((error) => console.error('Error deleting the unused starting session:', error));
        }
        settleDirect();
        return;
      }
      adoptSessionPayload(created.id, created);
      // New and empty, like the untouched canvas: entered at once, with no
      // load to wait on that a switch by the user could overtake. Its stream
      // connects now, so the first staged change shows as it happens.
      setSessionId(created.id);
      reflectSessionUrl(created.id);
      sessionStore.touchSession(created.id);
      setSessionsVersion((v) => v + 1);
      ensureSyncConnected(created.id)?.setBaseline(
        {},
        { seq: typeof created.seq === 'number' ? created.seq : 0 }
      );
      settleDirect();
    },
    [adoptSessionPayload, ensureSyncConnected, setSessionId]
  );
  settleStartingSessionRef.current = (capabilities) =>
    settleStartingSession(capabilities).finally(() => setSettlingStartId(null));

  // Keep the sync client's handlers pointed at the latest closures. A remote op
  // or resync triggers a debounced authoritative apply; a remote delete of the
  // active session drops the user into a fresh one with a notice (design D11);
  // a remote rename refreshes the recents name.
  useEffect(() => {
    syncHandlersRef.current = {
      // The stream's first snapshot: the server holds this session now. A
      // session this tab never read from the server (browser-only until now)
      // may have been switched to staged by someone else meanwhile, and that
      // op is not replayed to a first connect: read its mode.
      onReady: () => {
        retiredLeaveArmedRef.current = true;
        const readyId = syncRef.current?.sessionId;
        api.setSessionOnServer(readyId);
        if (!readyId || !stagedAvailableRef.current) return;
        // Staged changes made before the stream existed are not replayed.
        refreshStagedDiff();
        api.getSession(readyId).then(
          (payload) => {
            if (sessionIdRef.current === readyId) adoptSessionPayload(readyId, payload);
          },
          () => {}
        );
      },
      // Report recovery status back to the user (task fbd32fc9): a resync
      // that had to replay locally-queued ops means real, user-visible
      // content survived a dropped connection — worth a confirmation rather
      // than a silent reconciliation.
      onResync: async (resyncSessionId = sessionId) => {
        // Staged-change ops missed while disconnected are not replayed to the
        // canvas; the diff is read again instead.
        refreshStagedDiff();
        const recovered = await resyncFromServer(resyncSessionId);
        if (recovered > 0) {
          showNotification('info', t('sessions.reconnect_recovered', { count: recovered }));
        }
      },
      onRemoteOps: (ops, meta = {}) => {
        if (meta.sessionId && meta.sessionId !== sessionId) return;
        (ops || []).forEach((op) => {
          if (!handleStagedSessionOp(op)) {
            applyRemoteOp(op);
          } else if (op.op === 'graph_change_staged') {
            refreshStagedEntities(op);
          } else if (op.op === 'graph_changes_merged' || op.op === 'graph_changes_discarded') {
            // Merged entities now read from the graph; discarded ones are
            // back to what the graph holds, or gone.
            refreshStagedEntities(op, { removeMissing: true });
          }
        });
      },
      // This client's own annotation write just got acked with a fresh
      // server version/field_versions (dec-annotation-field-patches-and-
      // conflicts) — sessionSyncClient.js already folded it into its own
      // sync baseline; thread it onto the *live canvas node* too, so the
      // next local edit's autosave snapshot (which rebuilds from canvas node
      // data, not from the sync baseline) carries the true version forward
      // instead of the stale one from before this write. Without this, this
      // client's own next edit to the same annotation would send a stale
      // base_version and could spuriously conflict against nothing but its
      // own prior write. Goes through applyAnnotationUpsertToCanvas directly
      // rather than applyRemoteOp/its claim() gate — see that helper's own
      // comment for why this ack must not touch the image-ingest dedup.
      onLocalAnnotationsApplied: (ops) => {
        (ops || []).forEach((op) => applyAnnotationUpsertToCanvas(op?.annotation));
      },
      onPresence: (r) => setRoster(r),
      onSelections: (s) => setRemoteSelections(s),
      onLeases: (l) => setRemoteLeases(l),
      onSessionRenamed: (name) => {
        sessionStore.renameSession(sessionId, name);
        setSessionsVersion((v) => v + 1);
      },
      onSessionDeleted: (deletedBy, deletedWriteMode) => {
        // Either side knowing the session was staged is enough: the notice
        // can come without its mode (a drained stream), and staged is final.
        // A mode not yet known counts as staged: leaving it for a direct
        // session could be the silent switch §13 rules out.
        const wasStaged =
          deletedWriteMode === 'staged' ||
          sessionWriteMode === 'staged' ||
          (!deletedWriteMode && sessionWriteMode === 'unknown');
        if (wasStaged) {
          // Handled by the local delete path only in the tab that deleted it:
          // the client id is shared by every tab of this browser.
          if (locallyDeletingRef.current.has(sessionId)) {
            deletedStagedRef.current.add(sessionId);
            return;
          }
          // Refuse writes naming the deleted id until the replacement exists,
          // even if this client had not learnt it was staged.
          adoptWriteMode(sessionId, 'staged');
          sessionStore.removeSession(sessionId);
          setSessionsVersion((v) => v + 1);
          showNotification('info', t('staged.session_deleted_remote'));
          leaveDeletedSession(sessionId, true).then(() => setSessionsVersion((v) => v + 1));
          return;
        }
        // Per tab, not per client id (shared by every tab of this browser).
        if (locallyDeletingRef.current.has(sessionId)) return;
        if (stagedAvailableRef.current && defaultWriteModeRef.current === 'staged') {
          // The new session is created as the installation's default says.
          sessionStore.removeSession(sessionId);
          setSessionsVersion((v) => v + 1);
          showNotification('info', t('sessions.session_deleted_remote'));
          leaveDeletedSession(sessionId, false).then(() => setSessionsVersion((v) => v + 1));
          return;
        }
        forgetStagedSession(sessionId);
        const dropped = receiveRemoteSessionDeleted({
          deletedBy: null,
          clientId: api.getClientId(),
          sessionId,
          generateSessionId: api.generateVisualizationSessionId,
          removeSession: sessionStore.removeSession,
          clearVisualization,
          resetSessionScopedState: resetSessionScopedUi,
          setSessionId,
          reflectSessionUrl,
        });
        if (!dropped) return; // our own delete — already handled locally
        setSessionsVersion((v) => v + 1);
        showNotification('info', t('sessions.session_deleted_remote'));
      },
      onViewportSummoned: receiveSummon,
      onStreamRefused: () => leaveRetiredSession(syncRef.current?.sessionId),
      onCommand: (command) => {
        if (command?.type === 'tool_result' && command.result) {
          applyToolResultCommand(command.result, command.command_id);
        } else if (command?.type === 'node_pulse') {
          applyPulseCommand(command, command.command_id);
        }
      },
      // A 400/413 drop is terminal (malformed op, or a hard limit like the
      // annotation cap or an oversized layout_applied) — the op's effect
      // stays in the local canvas but will never persist or reach
      // collaborators. Surface it and resync so the canvas converges back to
      // whatever the server actually holds instead of silently drifting (R9).
      // Record the dropped op(s) first (review round 10): a resync already
      // in flight when this fires must know to exclude them from what it
      // folds/replays, or it would resurrect content the server just
      // permanently rejected.
      //
      // A 409 drop is the same "never retry this stale content" terminal
      // handling, but two distinct causes now share the status code:
      // LeaseConflict (task-annotation-exclusive-edit-leases) means another
      // client holds a live edit lease on the annotation this op targeted;
      // AnnotationFieldConflict (dec-annotation-field-patches-and-conflicts)
      // means no lease was held at all, but this op's base_version was stale
      // for a field someone else genuinely changed since. The two read very
      // differently to a user, so the response body (parsed by
      // sessionSyncClient.js only for a terminal drop) tells them apart —
      // `error: 'field_conflict'` is the REST /ops 409's structured detail
      // for the second cause; anything else (including LeaseConflict's plain
      // string detail) falls back to the pre-existing lease notice, the same
      // "someone else is editing this annotation" text the direct-acquire
      // denial already uses (GraphCanvas.jsx's annotationRemoteLocked).
      onDropped: (batch, status, body) => {
        (batch || []).forEach((op) => recentlyDroppedOpsRef.current.add(op));
        if (status === 409) {
          const detail = body && typeof body.detail === 'object' ? body.detail : null;
          if (detail && detail.error === 'field_conflict') {
            showNotification('info', t('context_menu.annotation_field_conflict'));
          } else {
            showNotification('info', t('context_menu.annotation_remote_locked'));
          }
        } else {
          showNotification('error', t('sessions.change_not_saved'));
        }
        resyncFromServer(sessionId);
      },
    };
  }, [
    sessionId,
    sessionWriteMode,
    leaveDeletedSession,
    leaveRetiredSession,
    adoptWriteMode,
    forgetStagedSession,
    handleStagedSessionOp,
    refreshStagedEntities,
    refreshStagedDiff,
    resyncFromServer,
    applyRemoteOp,
    applyAnnotationUpsertToCanvas,
    clearVisualization,
    resetSessionScopedUi,
    showNotification,
    t,
    applyToolResultCommand,
    applyPulseCommand,
    receiveSummon,
    syncHandlersRef,
    setRoster,
    setRemoteSelections,
    setRemoteLeases,
  ]);

  // Switch working session: persist the current one first (ops via the snapshot
  // round-trip), then swap the session ID (reconnects the SSE stream, reflects
  // the URL) and load the target's canvas from the server.
  const switchToSession = useCallback(
    (targetId, { register = true, eagerConnect = false, onMissing } = {}) => {
      requestSessionSnapshot(async () => {
        try {
          await loadSessionFromServer(targetId, { eagerConnect, onMissing });
          setSessionId(targetId);
          reflectSessionUrl(targetId);
          if (register) sessionStore.touchSession(targetId);
          setSessionsVersion((v) => v + 1);
        } catch (error) {
          showNotification('error', t('sessions.connect_error'));
        }
      });
    },
    [requestSessionSnapshot, loadSessionFromServer, showNotification, t]
  );

  const handleNewSession = useCallback(() => {
    if (stagedAvailable) {
      setNewSessionDialogOpen(true);
      return;
    }
    switchToSession(api.generateVisualizationSessionId(), { register: false });
    showNotification('info', t('sessions.new_session_started'));
  }, [stagedAvailable, switchToSession, showNotification, t]);

  const handleSelectSession = useCallback(
    (targetId) => {
      if (targetId !== sessionId) {
        switchToSession(targetId);
      }
    },
    [sessionId, switchToSession]
  );

  // Copy the canonical share link for a session (contract §5 form
  // `<base>/?session=<id>`), built from the current origin+path so it stays
  // correct across deployments without needing the server base URL client-side.
  const handleCopySessionLink = useCallback(
    async (targetId) => {
      const url = new URL(window.location.href);
      url.hash = '';
      url.search = '';
      url.searchParams.set('session', targetId);
      const link = url.toString();
      try {
        await navigator.clipboard.writeText(link);
      } catch {
        const el = document.createElement('textarea');
        el.value = link;
        document.body.appendChild(el);
        el.select();
        try {
          document.execCommand('copy');
        } finally {
          document.body.removeChild(el);
        }
      }
      showNotification('success', t('sessions.link_copied'));
    },
    [showNotification, t]
  );

  // Mint a pulse-trigger token for the live session and copy the external
  // trigger URL to the clipboard. Re-minting rotates the token, so this both
  // creates and (re)issues the credential; any URL handed out earlier stops
  // working. Only offered for the session this browser is live on.
  const handleCopyTriggerUrl = useCallback(async () => {
    let link;
    try {
      ({ url: link } = await api.mintPulseTriggerUrl(sessionId));
    } catch {
      showNotification('error', t('sessions.trigger_url_failed'));
      return;
    }
    try {
      await navigator.clipboard.writeText(link);
    } catch {
      const el = document.createElement('textarea');
      el.value = link;
      document.body.appendChild(el);
      el.select();
      try {
        document.execCommand('copy');
      } finally {
        document.body.removeChild(el);
      }
    }
    showNotification('success', t('sessions.trigger_url_copied'));
  }, [sessionId, showNotification, t]);

  const handleConnectSession = useCallback(
    (targetId) => {
      if (!sessionStore.isValidSessionId(targetId)) {
        showNotification('error', t('sessions.invalid_session_id'));
        return;
      }
      setConnectDialogOpen(false);
      if (targetId !== sessionId) {
        // Explicit connect-by-id is a join, not a locally generated id: connect
        // eagerly even if the session hasn't been saved server-side yet (R1).
        switchToSession(targetId, { eagerConnect: true });
      }
    },
    [sessionId, switchToSession, showNotification, t]
  );

  // ── Reference tiles (docs/ANNOTATION_CONTRACT.md "Reference tiles") ──────
  // What the canvas can decide for itself (no target; a url target whose
  // scheme it refuses to link) it already has. This answers the one thing
  // only the host knows, and answers `undefined` — "no opinion" — for
  // everything it does not actually know, because greying out a live tile is
  // as wrong as leaving a dead one looking live.
  const isReferenceTargetAvailable = useCallback((targetKind, target) => {
    // A session id that cannot be a session id is unreachable with certainty.
    // A well-formed one that is not in this browser's local session list is
    // NOT: that list is what this browser has visited, not what exists on the
    // server, so a colleague's shared session would be marked broken on the
    // strength of never having been opened here.
    if (targetKind === 'session') {
      return sessionStore.isValidSessionId(target) ? undefined : false;
    }
    // `url` is the canvas's own scheme check, and a `resource` target needs a
    // graph lookup this cannot do synchronously during render — it is resolved
    // when the reference is actually opened (handleReferenceOpen below).
    return undefined;
  }, []);

  const handleReferenceOpen = useCallback(
    async ({ targetKind, target }) => {
      if (targetKind === 'session') {
        if (!sessionStore.isValidSessionId(target)) {
          showNotification('error', t('sessions.invalid_session_id'));
          return;
        }
        if (target === sessionId) {
          showNotification('info', t('context_menu.reference_already_here'));
          return;
        }
        // Eager connect for the same reason handleConnectSession above does
        // it: this is a join by id, not a session this browser created.
        //
        // `onMissing` for the same reason the ?session= deep-link path passes
        // it (contract §5.3): a 404 does not throw — the hook clears the
        // canvas and seeds a new session under that id — so without this,
        // following a tile to a session that has since been deleted would move
        // the user off their board onto a blank canvas with no explanation.
        // A reference tile is the same kind of deep link.
        switchToSession(target, {
          eagerConnect: true,
          onMissing: () => showNotification('info', t('sessions.link_not_found')),
        });
        return;
      }
      if (targetKind === 'url') {
        // Re-checked here even though the canvas refuses to render an unsafe
        // target as openable and the server refuses to store one. This is the
        // call that actually hands a string to the browser, and a guard at the
        // point of use costs one function call; the alternative is trusting
        // every layer upstream of it to have stayed correct.
        //
        // Trimmed with the gate's OWN set before both the check and the open,
        // so the string validated is the string handed to the browser.
        // `isSafeReferenceUrl` trims internally, so passing it a raw value
        // made it approve one string while `window.open` received another —
        // and U+0085 survives `String.prototype.trim()`, so a stored target
        // the shared fixture lists as valid resolved against the app's own
        // origin instead of the author's page.
        const safeTarget = trimReferenceTarget(target);
        if (!isSafeReferenceUrl(safeTarget)) {
          showNotification('error', t('context_menu.reference_unsafe_target'));
          return;
        }
        // `noopener,noreferrer`: the opened page must not get a handle on this
        // window (reverse tabnabbing) and should not be told where it came
        // from.
        window.open(safeTarget, '_blank', 'noopener,noreferrer');
        return;
      }
      if (targetKind === 'resource') {
        // Bring the node into the view and focus it. The existence check is
        // here rather than in the resolver above because it needs the server:
        // a resource reference therefore reports a missing target when it is
        // followed, not before.
        try {
          const details = await api.getNodeDetails(target);
          if (!details?.node) {
            showNotification('error', t('context_menu.reference_broken_target'));
            return;
          }
          await addNodesToVisualization([details.node], []);
          setFocusNodeId(target);
        } catch {
          showNotification('error', t('context_menu.reference_broken_target'));
        }
      }
    },
    [sessionId, switchToSession, showNotification, t, addNodesToVisualization, setFocusNodeId]
  );

  const handleRenameSession = useCallback(
    (name) => {
      if (!renameDialog) return;
      const { id } = renameDialog;
      sessionStore.renameSession(id, name);
      api.renameServerSession(id, name).catch((error) => {
        // A rename creates a session the server does not hold: a 404 means
        // the id is retired.
        if (error?.status === 404) leaveRetiredSession(id);
      });
      setSessionsVersion((v) => v + 1);
      setRenameDialog(null);
    },
    [renameDialog, leaveRetiredSession]
  );

  // Open the delete confirmation, first checking the roster so the dialog can
  // warn when other users are connected (design 3.6). The roster is populated
  // only once clients subscribe via the realtime stream (step 7), so in step 4
  // it is typically empty and the warning stays dormant.
  const handleRequestDeleteSession = useCallback(
    async (targetId) => {
      let connectedOthers = 0;
      let staged = false;
      try {
        const payload = await api.getSession(targetId);
        const roster = payload?.roster || [];
        connectedOthers = roster.filter((m) => m.client_id !== api.getClientId()).length;
        staged = payload?.write_mode === 'staged';
        adoptSessionPayload(targetId, payload);
      } catch {
        // Session may not exist server-side yet — nothing to warn about.
      }
      // Deleting a staged session drops its unmerged changes with it (§13),
      // and the dialog must say so whenever the delete will: the confirm
      // discards exactly when this flag is set. No count: the diff counts
      // only what this reader may see, and the whole layer goes.
      if (targetId === sessionId && sessionWriteMode === 'staged') staged = true;
      setDeleteSessionDialog({ id: targetId, connectedOthers, staged });
    },
    [adoptSessionPayload, sessionId, sessionWriteMode]
  );

  const handleConfirmDeleteSession = useCallback(async () => {
    if (!deleteSessionDialog) return;
    const { id, staged: wasStaged } = deleteSessionDialog;
    setDeleteSessionDialog(null);
    // The deleted session's own mode: the user may switch sessions while the
    // delete is in flight.
    const modeAtConfirm = sessionIdRef.current === id ? sessionWriteModeRef.current : null;
    const modeOfDeleted = () =>
      sessionIdRef.current === id ? sessionWriteModeRef.current : modeAtConfirm;
    locallyDeletingRef.current.add(id);
    deletesInFlightRef.current.add(id);
    // What an earlier delete of this id learnt does not decide this one: a
    // direct id can be opened, and deleted, again.
    deletedStagedRef.current.delete(id);
    try {
      let browserOnly = false;
      try {
        // The user confirmed a dialog that said a staged session's changes
        // go with it.
        const answer = await api.deleteServerSession(id, api.getClientId(), {
          discardStaged: wasStaged,
        });
        // Switched to staged after the dialog opened, with the notice not here yet.
        if (answer?.write_mode === 'staged') deletedStagedRef.current.add(id);
      } catch (error) {
        browserOnly = error?.status === 404;
        let gone = browserOnly || deletedStagedRef.current.has(id);
        if (!gone && (typeof error?.status !== 'number' || error.status >= 500)) {
          // No answer, or a server error that may have come after the delete
          // landed: ask whether the session is still there rather than report
          // a failure that did not happen.
          gone = await api.getSession(id).then(
            () => false,
            (e) => e?.status === 404
          );
        }
        if (!gone) {
          // A session that was not deleted keeps its work: never act as if it
          // were gone.
          locallyDeletingRef.current.delete(id);
          showNotification('error', refusalText(t, error, 'staged.delete_failed'));
          return;
        }
        // 404: the session only ever existed locally.
      }
      sessionStore.removeSession(id);
      // The server's notice may say staged where this tab thought direct (it
      // was switched meanwhile): then the replacement is staged too. So does a
      // mode not known yet - unless the server never held the session, which
      // makes it browser-only and direct (§5.1). The current mode, not the one
      // this callback closed over: it may have changed during the delete.
      const modeNow = modeOfDeleted();
      const leftStaged =
        wasStaged ||
        deletedStagedRef.current.has(id) ||
        modeNow === 'staged' ||
        (modeNow === 'unknown' && !browserOnly);
      if (sessionIdRef.current === id) {
        // Deleting the active session: drop its content and switch into a fresh
        // one (design 3.6); a staged one into a staged one (§13). Other
        // connected clients are notified via the server's session_deleted
        // broadcast.
        showNotification(
          'info',
          t(leftStaged ? 'staged.session_deleted' : 'sessions.session_deleted')
        );
        await leaveDeletedSession(id, leftStaged);
      } else if (leftStaged) {
        forgetStagedSession(id);
      }
      setSessionsVersion((v) => v + 1);
    } finally {
      deletesInFlightRef.current.delete(id);
    }
  }, [
    deleteSessionDialog,
    sessionId,
    leaveDeletedSession,
    forgetStagedSession,
    showNotification,
    t,
  ]);

  // Start a new session. Where the backend can stage, the user chooses how it
  // writes; a staged session is created on the server first (§5.1).
  const startNewSession = useCallback(
    async (mode) => {
      setNewSessionDialogOpen(false);
      if (mode !== 'staged') {
        switchToSession(api.generateVisualizationSessionId(), { register: false });
        showNotification('info', t('sessions.new_session_started'));
        return;
      }
      try {
        const created = await api.createSession(null, { writeMode: 'staged' });
        if (created?.write_mode !== 'staged' || !created?.id) {
          throw new Error(t('staged.create_failed'));
        }
        api.setSessionOnServer(created.id);
        adoptWriteMode(created.id, 'staged');
        switchToSession(created.id, { eagerConnect: true });
        showNotification('info', t('staged.new_session_started'));
      } catch (error) {
        showNotification('error', refusalText(t, error, 'staged.create_failed'));
      }
    },
    [switchToSession, adoptWriteMode, showNotification, t]
  );

  // Switch the active session from direct to staged (§5.2): irreversible, so
  // only after the user confirmed it. A session that so far exists only in
  // this browser is created on the server first, as direct, then switched.
  const confirmSwitchToStaged = useCallback(async () => {
    setSwitchToStagedConfirm(false);
    const targetId = sessionId;
    const clientId = api.getClientId();
    try {
      let payload;
      try {
        payload = await api.setSessionWriteMode(targetId, 'staged', clientId);
      } catch (error) {
        if (error.status !== 404) throw error;
        await api.renameServerSession(targetId, currentSessionName || null);
        payload = await api.setSessionWriteMode(targetId, 'staged', clientId);
      }
      if (payload?.write_mode !== 'staged') throw new Error(t('staged.switch_failed'));
      adoptWriteMode(targetId, 'staged');
      // Only while it is still the session the user is in: the stream must
      // never be bound to one the user has left (its notices act on the
      // active session).
      if (sessionIdRef.current === targetId) ensureSyncConnected(targetId);
      showNotification('info', t('staged.switched'));
    } catch (error) {
      if (typeof error?.status !== 'number' || error.status >= 500) {
        // The switch may have landed with its answer lost: read what the
        // server holds, so a staged session never goes on looking direct.
        const now = await api.getSession(targetId).catch(() => null);
        if (now) adoptSessionPayload(targetId, now);
        if (now?.write_mode === 'staged') {
          if (sessionIdRef.current === targetId) ensureSyncConnected(targetId);
          showNotification('info', t('staged.switched'));
          return;
        }
        if (!now) {
          // Neither answered: the session may be staged now. Hold its graph
          // writes until its mode is read again (the bar offers Retry).
          markModeUnknown(targetId);
        }
      }
      showNotification('error', refusalText(t, error, 'staged.switch_failed'));
    }
  }, [
    sessionId,
    currentSessionName,
    adoptWriteMode,
    adoptSessionPayload,
    markModeUnknown,
    ensureSyncConnected,
    showNotification,
    t,
  ]);

  // After a merge or discard from the review: re-read what it touched (its op
  // does the same when it arrives; this covers a stream that is down), and put
  // nodes merged as new on the canvas.
  const handleStagedLayerChanged = useCallback(
    async (result) => {
      refreshStagedDiff();
      await refreshStagedEntities(
        { removals: result?.removed_entries || [] },
        { removeMissing: true }
      );
      const created = (result?.merged || [])
        .map((item) => item?.new_entity_id)
        .filter((id) => typeof id === 'string' && id);
      if (!created.length) return;
      const sessionAtStart = sessionId;
      const found = await Promise.all(
        created.map((id) => api.getNodeDetails(id).catch(() => null))
      );
      if (syncRef.current?.sessionId !== sessionAtStart) return;
      const nodes = found.map((r) => r?.node).filter(Boolean);
      if (nodes.length) {
        addNodesToVisualization(
          nodes,
          found.flatMap((r) => r?.edges || [])
        );
      }
    },
    [refreshStagedDiff, refreshStagedEntities, sessionId, syncRef, addNodesToVisualization]
  );

  const closeStagedReview = useCallback(() => setStagedReviewOpen(false), []);

  // The diff's states; when the diff cannot be read, those the composed reads
  // put on each entity (`staged_state`), so a staged entity stays marked.
  const canvasStagedStates = useMemo(() => {
    if (!stagedDiffError) return stagedStates;
    const fromReads = (list) =>
      Object.fromEntries(
        (list || [])
          .filter((e) => e?.staged_state && e.staged_state !== 'none')
          .map((e) => [e.id, e.staged_state])
      );
    return {
      nodes: { ...fromReads(nodes), ...stagedStates.nodes },
      edges: { ...fromReads(edges), ...stagedStates.edges },
    };
  }, [nodes, edges, stagedStates, stagedDiffError]);

  const stagedStateLabels = useMemo(
    () => ({
      created: t('staged.marker.created'),
      updated: t('staged.marker.updated'),
      orphaned: t('staged.marker.orphaned'),
      deleted: t('staged.marker.deleted'),
    }),
    [t]
  );

  const stagedCanvasName = useCallback((kind, id) => {
    const store = useGraphStore.getState();
    if (kind === 'node') return store.nodes.find((n) => n.id === id)?.name || null;
    const edge = store.edges.find((e) => e.id === id);
    if (!edge) return null;
    const name = (nid) => store.nodes.find((n) => n.id === nid)?.name || nid;
    return `${name(edge.source)} → ${name(edge.target)}${edge.type ? ` (${edge.type})` : ''}`;
  }, []);

  // Export full graph from backend API
  const handleExportGraph = useCallback(async () => {
    try {
      const data = await api.exportGraph();

      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `graph-export-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      showNotification('success', t('menu.export_success'));
    } catch (error) {
      console.error('Error exporting graph:', error);
      showNotification('error', t('menu.export_error'));
    }
  }, [showNotification, t]);

  // Handle drop from toolbar onto canvas
  const handleDropCreateNode = useCallback(
    (nodeType, position) => {
      if (nodeType === 'Agent') {
        handleCreateAgent();
      } else if (nodeType === 'EventSubscription') {
        handleCreateSubscription();
      } else if (nodeType === 'ActiveKnowledgeCollection') {
        handleCreateAKC();
      } else if (schema?.node_types?.[nodeType]?.ui_form === 'skill') {
        setEditingSkillData(null);
        setSkillDialogType(nodeType);
      } else {
        setCreateNodeType(nodeType);
      }
    },
    [handleCreateAgent, handleCreateSubscription, handleCreateAKC, schema]
  );

  // Handle node update from edit dialog
  const handleNodeUpdate = useCallback(
    async (nodeId, updates) => {
      try {
        await persistNodeUpdates({
          entries: [{ id: nodeId, updates }],
          updateNode: api.updateNode,
          updateVisualization,
          onApplied: closeEditingNode,
        });
        showNotification('success', 'Node updated');
      } catch (error) {
        console.error('Error updating node:', error);
        showNotification('error', 'Could not update node');
      }
    },
    [updateVisualization, closeEditingNode, showNotification]
  );

  // Modal dialog open/close (and edit-target) state, bundled for AppDialogs
  // (STRUCTURE_REVIEW B1 slice 3). The double-Escape guard and SessionDrawer's
  // suspendEscape derive from this bundle, so it stays assembled in App;
  // AppDialogs owns only the rendering of the stack. The dialogs that address
  // graph content (editingEdge, deleteDialog) live in the store next to
  // editingNode/detailNode so a session switch resets them all at once.
  const dialogs = {
    createNodeType,
    setCreateNodeType,
    editingEdge,
    setEditingEdge,
    deleteDialog,
    setDeleteDialog,
    saveViewDialog,
    setSaveViewDialog,
    isSavingView,
    showSubscriptionDialog,
    setShowSubscriptionDialog,
    editingSubscriptionData,
    setEditingSubscriptionData,
    showAgentDialog,
    setShowAgentDialog,
    editingAgentData,
    setEditingAgentData,
    showAgentRunsDialog,
    setShowAgentRunsDialog,
    agentRunsAgentId,
    onViewAgentRuns: handleViewAgentRuns,
    showAgentProposalsDialog,
    setShowAgentProposalsDialog,
    agentProposalsAgentId,
    onViewAgentProposals: handleViewAgentProposals,
    skillDialogType,
    setSkillDialogType,
    editingSkillData,
    setEditingSkillData,
    showAKCDialog,
    setShowAKCDialog,
    editingAKCData,
    setEditingAKCData,
    settingsOpen,
    setSettingsOpen,
    connectDialogOpen,
    setConnectDialogOpen,
    renameDialog,
    setRenameDialog,
    deleteSessionDialog,
    setDeleteSessionDialog,
  };

  // The session drawer is non-modal (in either shell), so any dialog can be
  // stacked on top of it; while one is open, Escape belongs to that dialog.
  const suspendEscape = !!(
    settingsOpen ||
    connectDialogOpen ||
    renameDialog ||
    deleteSessionDialog ||
    newSessionDialogOpen ||
    stagedReviewOpen ||
    switchToStagedConfirm ||
    clearConfirm ||
    createNodeType ||
    editingNode ||
    detailNode ||
    editingEdge ||
    deleteDialog ||
    saveViewDialog ||
    showSubscriptionDialog ||
    showAgentDialog ||
    showAgentRunsDialog ||
    showAgentProposalsDialog ||
    skillDialogType ||
    showAKCDialog
  );

  const shellProps = {
    sessionId,
    onTeleportToSourceGraph: handleTeleportToSourceGraph,
    sessions,
    currentSessionId: sessionId,
    onNewSession: handleNewSession,
    onConnectSession: () => setConnectDialogOpen(true),
    onSelectSession: handleSelectSession,
    onRenameSession: (id) => {
      const entry = sessions.find((s) => s.id === id);
      setRenameDialog({ id, name: entry?.name || '' });
    },
    onDeleteSession: handleRequestDeleteSession,
    onCopySessionLink: handleCopySessionLink,
    onCopyTriggerUrl: handleCopyTriggerUrl,
    onOpenSettings: () => setSettingsOpen(true),
    canvasLocked,
    onToggleLock: () => setCanvasLocked(!canvasLocked),
    suspendEscape,
    onCreateNodeForType: handleCreateNodeForType,
    onCreateAgent: handleCreateAgent,
    onCreateSubscription: handleCreateSubscription,
    onSaveView: handleToolbarSaveView,
    onCreateGroup: handleToolbarCreateGroup,
    onCreateActiveKnowledgeCollection: handleCreateAKC,
    llmAvailable,
    akcShortName,
    onEnterFullscreen: enterFullscreenCanvas,
  };

  return (
    <div
      ref={appRef}
      className={`app${drawerOpen ? ' session-drawer-open' : ''}${isMobile ? ' is-mobile' : ''}${isCoarsePointer ? ' is-touch' : ''}${fullscreenCanvasActive ? ' fullscreen-canvas-mode' : ''}`}
    >
      <div className="app-canvas" id="guide-target-canvas" ref={canvasContainerRef}>
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          highlightedNodeIds={highlightedNodeIds}
          hiddenNodeIds={hiddenNodeIds}
          hiddenEdgeIds={hiddenEdgeIds}
          dimmedNodeIds={dimmedNodeIds}
          dimmedEdgeIds={dimmedEdgeIds}
          edgeIntensity={edgeIntensity}
          nodeMarks={nodeMarks}
          pulsedNodeIds={pulsedNodeIds}
          clearGroupsFlag={clearGroupsFlag}
          onExpand={handleExpand}
          onEdit={handleEdit}
          onDelete={handleDelete}
          onHide={handleHide}
          onDeleteMultiple={handleDeleteMultiple}
          onHideMultiple={handleHideMultiple}
          onHideEdge={handleHideEdge}
          onDeleteEdge={handleDeleteEdge}
          onDimNodes={handleDimNodes}
          onRestoreNodes={handleRestoreNodes}
          onDimEdges={handleDimEdges}
          onRestoreEdges={handleRestoreEdges}
          onEditEdge={handleEditEdge}
          onSetEdgeType={handleSetEdgeType}
          onConnect={handleConnect}
          onCreateGroup={handleCreateGroup}
          onNodePositionChange={handleNodePositionChange}
          onSaveView={handleSaveView}
          onCreateSubscription={handleCreateSubscription}
          onCreateAgent={handleCreateAgent}
          onDropCreateNode={handleDropCreateNode}
          onImageIngest={handleImageIngest}
          onReferenceOpen={handleReferenceOpen}
          isReferenceTargetAvailable={isReferenceTargetAvailable}
          onShowOnly={handleShowOnly}
          onSelectionChange={handleSelectionChange}
          onNodeDoubleClick={handleNodeDoubleClick}
          onViewNodeHistory={handleViewNodeHistory}
          focusNodeId={focusNodeId}
          onFocusComplete={clearFocusNode}
          createGroupSignal={createGroupSignal}
          saveViewSignal={saveViewSignal}
          closeMenusSignal={closeMenusSignal}
          groupsToRestore={pendingGroups}
          onGroupsRestored={() => setPendingGroups(null)}
          annotationsToRestore={pendingAnnotations}
          onAnnotationsRestored={() => setPendingAnnotations(null)}
          onAnnotationChange={handleAnnotationChange}
          remotePositions={remotePositions}
          onRemotePositionsApplied={() => setRemotePositions(null)}
          animatedLayout={animatedLayout}
          onAnimatedLayoutApplied={(drained) =>
            setAnimatedLayout((prev) => {
              // Clear only the batches the canvas actually drained: a new op
              // enqueued between the drain and this reset must not be lost.
              if (!prev || !drained || drained.length === 0) return prev || null;
              const done = new Set(drained);
              const rest = prev.filter((b) => !done.has(b));
              return rest.length ? rest : null;
            })
          }
          animatedLayoutResetKey={sessionId}
          canvasBaselineEpoch={canvasBaselineEpoch}
          agentArrangingLabel={t('sessions.agent_arranging')}
          remoteAnnotationOps={remoteAnnotationOps}
          onRemoteAnnotationsApplied={() => setRemoteAnnotationOps(null)}
          remoteSelections={remoteSelections}
          remoteLeases={remoteLeases}
          onBeginEditing={handleBeginEditing}
          onEndEditing={handleEndEditing}
          federationDepth={federationDepth}
          onFederationDepthChange={setFederationDepth}
          maxFederationDepth={maxFederationDepth}
          federationDepthLevels={federationDepthLevels}
          federationDepthLabel={t('federation.depth_label')}
          federationDepthTooltip={t('federation.depth_tooltip')}
          lazyLoadShowingLabel={t('canvas.lazy_showing')}
          lazyLoadMoreLabel={t('canvas.lazy_load_more')}
          lazyLoadHiddenConnectionsLabel={t('canvas.lazy_hidden_connections')}
          showMinimap={showMinimap}
          compactMode={isMobile ? 'on' : 'off'}
          compactZoomInLabel={t('canvas.zoom_in')}
          compactZoomOutLabel={t('canvas.zoom_out')}
          compactFitViewLabel={t('canvas.fit_view')}
          focusViewLabel={t('canvas.focus_view')}
          exitFocusViewLabel={t('canvas.exit_focus_view')}
          compactControlsLabel={t('canvas.controls_group')}
          nodePreviewEnabled={nodePreviewEnabled}
          schema={schema}
          onContextMenuAction={handleContextMenuAction}
          stagedStates={sessionWriteMode === 'staged' ? canvasStagedStates : null}
          stagedStateLabels={stagedStateLabels}
          contextMenuLabels={{
            edit: t('context_menu.edit'),
            hide: t('context_menu.hide'),
            expand: t('context_menu.expand'),
            delete: t('context_menu.delete'),
            nodesSelected: t('context_menu.nodes_selected'),
            showOnly: t('context_menu.show_only'),
            selectSameType: t('context_menu.select_same_type'),
            selectRelated: t('context_menu.select_related'),
            viewHistory: t('context_menu.view_history'),
            organize: t('context_menu.organize'),
            autoTidy: t('context_menu.auto_tidy'),
            organizeCluster: t('context_menu.organize_cluster'),
            organizeHorizontal: t('context_menu.organize_horizontal'),
            organizeVertical: t('context_menu.organize_vertical'),
            organizeTree: t('context_menu.organize_tree'),
            organizeDateHorizontal: t('context_menu.organize_date_horizontal'),
            organizeDateVertical: t('context_menu.organize_date_vertical'),
            organizeDateUnavailable: t('context_menu.organize_date_unavailable'),
            organizeHint: t('context_menu.organize_hint'),
            align: t('context_menu.align'),
            alignLeft: t('context_menu.align_left'),
            alignCenterHorizontal: t('context_menu.align_center_horizontal'),
            alignRight: t('context_menu.align_right'),
            alignTop: t('context_menu.align_top'),
            alignCenterVertical: t('context_menu.align_center_vertical'),
            alignBottom: t('context_menu.align_bottom'),
            distribute: t('context_menu.distribute'),
            distributeHorizontal: t('context_menu.distribute_horizontal'),
            distributeVertical: t('context_menu.distribute_vertical'),
            annotationAttachedSkipped: t('context_menu.annotation_attached_skipped'),
            hideAll: t('context_menu.hide_all'),
            deleteAll: t('context_menu.delete_all'),
            dimNode: t('context_menu.dim_node'),
            restoreNode: t('context_menu.restore_node'),
            dimSelected: t('context_menu.dim_selected'),
            restoreSelected: t('context_menu.restore_selected'),
            dimIncidentEdges: t('context_menu.dim_incident_edges'),
            restoreIncidentEdges: t('context_menu.restore_incident_edges'),
            dimEdge: t('context_menu.dim_edge'),
            restoreEdge: t('context_menu.restore_edge'),
            changeType: t('context_menu.change_type'),
            generalConnection: t('context_menu.general_connection'),
            addNote: t('context_menu.add_note'),
            addLabel: t('context_menu.add_label'),
            addArrow: t('context_menu.add_arrow'),
            annotationColor: t('context_menu.annotation_color'),
            annotationFill: t('context_menu.annotation_fill'),
            annotationBorder: t('context_menu.annotation_border'),
            annotationTransparent: t('context_menu.annotation_transparent'),
            deleteAnnotation: t('context_menu.delete'),
            unlockAnnotation: t('context_menu.annotation_unlock'),
            duplicateAnnotation: t('context_menu.annotation_duplicate'),
            notePlaceholder: t('context_menu.note_placeholder'),
            labelPlaceholder: t('context_menu.label_placeholder'),
            annotationTextSize: t('context_menu.annotation_text_size'),
            annotationTextAlign: t('context_menu.annotation_align'),
            annotationAlignTop: t('context_menu.annotation_align_top'),
            annotationAlignMiddle: t('context_menu.annotation_align_middle'),
            annotationAlignBottom: t('context_menu.annotation_align_bottom'),
            annotationAlignLeft: t('context_menu.annotation_align_left'),
            annotationAlignCenter: t('context_menu.annotation_align_center'),
            annotationAlignRight: t('context_menu.annotation_align_right'),
            annotationFontFamily: t('context_menu.annotation_font'),
            annotationFontDefault: t('context_menu.annotation_font_default'),
            annotationFontFamilySerif: t('context_menu.annotation_font_serif'),
            annotationFontFamilyMonospace: t('context_menu.annotation_font_monospace'),
            annotationFontFamilyCursive: t('context_menu.annotation_font_cursive'),
            arrowStartHead: t('context_menu.arrow_start_head'),
            arrowEndHead: t('context_menu.arrow_end_head'),
            annotationShape: t('context_menu.annotation_shape'),
            annotationIcon: t('context_menu.annotation_icon'),
            annotationRotation: t('context_menu.annotation_rotation'),
            annotationRotateLeft: t('context_menu.annotation_rotate_left'),
            annotationRotateRight: t('context_menu.annotation_rotate_right'),
            annotationRotateReset: t('context_menu.annotation_rotate_reset'),
            undoNotification: t('context_menu.undo_notification'),
            redoNotification: t('context_menu.redo_notification'),
            imageIngestFailed: t('canvas.image_ingest_failed'),
            annotationRemoteLocked: t('context_menu.annotation_remote_locked'),
            annotationLockedSkipped: t('context_menu.annotation_locked_skipped'),
            annotationBroken: t('context_menu.annotation_broken'),
            freehandColor: t('context_menu.freehand_color'),
            freehandWidth: t('context_menu.freehand_width'),
            freehandSmoothing: t('context_menu.freehand_smoothing'),
            freehandOpacity: t('context_menu.freehand_opacity'),
            freehandDrawingHint: t('canvas.freehand_drawing_hint'),
            freehandConcurrentInputBlocked: t('canvas.freehand_concurrent_input_blocked'),
            annotationLayer: t('context_menu.annotation_layer'),
            annotationLayerFront: t('context_menu.annotation_layer_front'),
            annotationLayerBack: t('context_menu.annotation_layer_back'),
            groupLayer: t('context_menu.group_layer'),
            groupLayerFront: t('context_menu.group_layer_front'),
            groupLayerBack: t('context_menu.group_layer_back'),
            annotationNearbyMenu: t('context_menu.annotation_nearby_menu'),
            annotationNearbyLabel: t('context_menu.annotation_nearby_label'),
            annotationNearbyIcon: t('context_menu.annotation_nearby_icon'),
            annotationNearbyText: t('context_menu.annotation_nearby_text'),
            annotationOpacity: t('context_menu.annotation_opacity'),
            editAnnotation: t('context_menu.edit_annotation'),
            ariaKindNote: t('context_menu.aria_kind_note'),
            ariaKindLabel: t('context_menu.aria_kind_label'),
            ariaKindText: t('context_menu.aria_kind_text'),
            ariaKindShape: t('context_menu.aria_kind_shape'),
            ariaKindIcon: t('context_menu.aria_kind_icon'),
            ariaKindVoteDot: t('context_menu.aria_kind_vote_dot'),
            ariaKindHeatmap: t('context_menu.aria_kind_heatmap'),
            heatmapIntensity: t('context_menu.heatmap_intensity'),
            ariaKindImage: t('context_menu.aria_kind_image'),
            ariaKindArrow: t('context_menu.aria_kind_arrow'),
            ariaKindFreehand: t('context_menu.aria_kind_freehand'),
            ariaKindGroup: t('context_menu.aria_kind_group'),
            ariaKindReference: t('context_menu.aria_kind_reference'),
            ariaKindReferenceSession: t('context_menu.aria_kind_reference_session'),
            ariaKindReferenceUrl: t('context_menu.aria_kind_reference_url'),
            ariaKindReferenceResource: t('context_menu.aria_kind_reference_resource'),
            ariaKindReferenceBroken: t('context_menu.aria_kind_reference_broken'),
            referenceUntitled: t('context_menu.reference_untitled'),
            referenceOpen: t('context_menu.reference_open'),
            referenceRename: t('context_menu.reference_rename'),
            referenceLabel: t('context_menu.reference_label'),
            referenceTarget: t('context_menu.reference_target'),
            referenceTargetSession: t('context_menu.reference_target_session'),
            referenceTargetUrl: t('context_menu.reference_target_url'),
            referenceTargetResource: t('context_menu.reference_target_resource'),
            referenceTargetUnknown: t('context_menu.reference_target_unknown'),
            referenceBrokenTarget: t('context_menu.reference_broken_target'),
            referenceUnsafeTarget: t('context_menu.reference_unsafe_target'),
            referencePasteCreated: t('context_menu.reference_paste_created'),
            annotationWidth: t('context_menu.annotation_width'),
            annotationHeight: t('context_menu.annotation_height'),
            annotationSize: t('context_menu.annotation_size'),
            annotationMoreActions: t('context_menu.annotation_more_actions'),
            annotationApplySize: t('context_menu.annotation_apply_size'),
            annotationAttachTo: t('context_menu.annotation_attach_to'),
            annotationDetach: t('context_menu.annotation_detach'),
            annotationAttachToHint: t('context_menu.annotation_attach_to_hint'),
            annotationAttachToCancel: t('context_menu.annotation_attach_to_cancel'),
            annotationMultiSelectMode: t('context_menu.annotation_multi_select_mode'),
            annotationOverlapPickerTitle: t('context_menu.annotation_overlap_picker_title'),
            openInSourceGraph: t('context_menu.open_in_source_graph'),
            openInSourceGraphTooltip: t('federation.teleport_action_tooltip'),
          }}
          onTeleportToSourceGraph={handleTeleportToSourceGraph}
          annotationToolboxLabels={{
            toggleExpand: t('annotation_toolbox.toggle_expand'),
            toggleCollapse: t('annotation_toolbox.toggle_collapse'),
            note: t('annotation_toolbox.note'),
            text: t('annotation_toolbox.text'),
            label: t('annotation_toolbox.label'),
            shapeRectangle: t('annotation_toolbox.shape_rectangle'),
            shapeCircle: t('annotation_toolbox.shape_circle'),
            shapeTriangle: t('annotation_toolbox.shape_triangle'),
            shapeRhombus: t('annotation_toolbox.shape_rhombus'),
            shapeHexagon: t('annotation_toolbox.shape_hexagon'),
            shapeProcessArrow: t('annotation_toolbox.shape_process_arrow'),
            shapePickerOpen: t('annotation_toolbox.shape_picker_open'),
            shapePicker: t('annotation_toolbox.shape_picker'),
            icon: t('annotation_toolbox.icon'),
            iconPickerOpen: t('annotation_toolbox.icon_picker_open'),
            iconPicker: t('annotation_toolbox.icon_picker'),
            voteDot: t('annotation_toolbox.vote_dot'),
            heatmap: t('annotation_toolbox.heatmap'),
            image: t('annotation_toolbox.image'),
            freehand: t('annotation_toolbox.freehand'),
            noteHint: t('annotation_toolbox.note_hint'),
            textHint: t('annotation_toolbox.text_hint'),
            labelHint: t('annotation_toolbox.label_hint'),
            shapeRectangleHint: t('annotation_toolbox.shape_rectangle_hint'),
            shapeCircleHint: t('annotation_toolbox.shape_circle_hint'),
            shapeTriangleHint: t('annotation_toolbox.shape_triangle_hint'),
            shapeRhombusHint: t('annotation_toolbox.shape_rhombus_hint'),
            shapeHexagonHint: t('annotation_toolbox.shape_hexagon_hint'),
            shapeProcessArrowHint: t('annotation_toolbox.shape_process_arrow_hint'),
            iconHint: t('annotation_toolbox.icon_hint'),
            voteDotHint: t('annotation_toolbox.vote_dot_hint'),
            heatmapHint: t('annotation_toolbox.heatmap_hint'),
            imageHint: t('annotation_toolbox.image_hint'),
            freehandHint: t('annotation_toolbox.freehand_hint'),
            select: t('annotation_toolbox.select'),
            eraser: t('annotation_toolbox.eraser'),
            freehandPickerOpen: t('annotation_toolbox.freehand_picker_open'),
            freehandPicker: t('annotation_toolbox.freehand_picker'),
            voteDotPickerOpen: t('annotation_toolbox.vote_dot_picker_open'),
            voteDotPicker: t('annotation_toolbox.vote_dot_picker'),
            selectHint: t('annotation_toolbox.select_hint'),
            eraserHint: t('annotation_toolbox.eraser_hint'),
          }}
          annotationToolboxPortalContainer={isMobile ? mobileAnnotationContainer : null}
          annotationEditSheetPortalContainer={isMobile ? mobileAnnotationEditContainer : null}
          onRequestAnnotationEditSheet={isMobile ? (detailSheetController?.open ?? null) : null}
          onCloseAnnotationEditSheet={isMobile ? (detailSheetController?.close ?? null) : null}
          nodeColorResolver={getNodeColor}
          sessionKey={sessionId}
          onViewportChange={(vp) => {
            latestViewport.current = vp;
          }}
          viewportRequest={viewportRequest}
        />
      </div>

      {isMobile ? (
        <MobileShell
          {...shellProps}
          onClear={() => requestClear('button')}
          onOpenActivity={() => setActivityOpen(true)}
          onAnnotationSheetContainerChange={setMobileAnnotationContainer}
          onAnnotationEditSheetContainerChange={setMobileAnnotationEditContainer}
          onDetailSheetControllerReady={setDetailSheetController}
        />
      ) : (
        <DesktopShell
          {...shellProps}
          roster={roster}
          currentClientId={api.getClientId()}
          onClear={() => requestClear('button')}
          onSummonEveryone={summonEveryone}
          drawerOpen={drawerOpen}
          onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
          onCloseDrawer={() => setDrawerOpen(false)}
          onOpenActivity={() => {
            setDrawerOpen(false);
            setActivityOpen(true);
          }}
        />
      )}
      {fullscreenCanvasActive && (
        <FullscreenExitButton onExit={exitFullscreenCanvas} label={t('fullscreen.exit')} />
      )}
      <ActivityDrawer
        open={activityOpen}
        onClose={() => setActivityOpen(false)}
        sessionId={sessionId}
        currentClientId={api.getClientId()}
        roster={roster}
      />
      {clearConfirm && (
        <ConfirmDialog
          title={
            clearConfirm.locked ? t('clear_confirm.locked_title') : t('clear_confirm.named_title')
          }
          message={
            clearConfirm.locked
              ? t('clear_confirm.locked_message')
              : t('clear_confirm.named_message')
          }
          confirmText={t('clear_confirm.confirm')}
          cancelText={t('common.cancel')}
          confirmStyle="danger"
          onConfirm={() => {
            clearVisualization();
            setClearConfirm(null);
          }}
          onCancel={() => setClearConfirm(null)}
        />
      )}
      {maxFederationDepth > 1 && (
        <div className="app-a11y-depth-live" aria-live="polite" aria-atomic="true">
          {t('federation.depth_indicator', { current: federationDepth, max: maxFederationDepth })}
        </div>
      )}
      <NodeHistoryPanel />

      {notification && (
        <div
          className={`app-notification app-notification-${notification.type}`}
          role={notification.type === 'error' ? 'alert' : 'status'}
          aria-live={notification.type === 'error' ? 'assertive' : 'polite'}
        >
          <span>{notification.message}</span>
          <button
            onClick={() => setNotification(null)}
            aria-label={t('common.close')}
            title={t('common.close')}
          >
            ×
          </button>
        </div>
      )}
      {sessionId !== settlingStartId && (
        <WriteModeBar
          writeMode={sessionWriteMode}
          stagedAvailable={stagedAvailable}
          pendingCount={stagedPendingCount}
          diffUnavailable={!!stagedDiffError}
          onReview={() => setStagedReviewOpen(true)}
          onSwitchToStaged={() => setSwitchToStagedConfirm(true)}
          onRetryMode={() =>
            // As a switch to this session: a load that answers after the user
            // moved on then moves the user back, consistently, rather than
            // leaving canvas, stream and requests on different sessions.
            switchToSession(sessionId, { register: false, eagerConnect: true })
          }
        />
      )}
      {newSessionDialogOpen && (
        <NewSessionDialog
          defaultMode={defaultWriteMode}
          onChoose={startNewSession}
          onCancel={() => setNewSessionDialogOpen(false)}
        />
      )}
      {switchToStagedConfirm && (
        <ConfirmDialog
          title={t('staged.switch_title')}
          message={t('staged.switch_message')}
          confirmText={t('staged.switch_confirm')}
          cancelText={t('common.cancel')}
          onConfirm={confirmSwitchToStaged}
          onCancel={() => setSwitchToStagedConfirm(false)}
        />
      )}
      {stagedReviewOpen && sessionWriteMode === 'staged' && (
        <StagedReviewDialog
          sessionId={sessionId}
          items={stagedItems}
          diffError={stagedDiffError}
          onRefresh={refreshStagedDiff}
          onMerged={handleStagedLayerChanged}
          onDiscarded={handleStagedLayerChanged}
          onClose={closeStagedReview}
          canvasName={stagedCanvasName}
        />
      )}
      <SummonNotice
        notice={summonNotice}
        onReturn={returnFromSummon}
        onDismiss={dismissSummonNotice}
      />
      {isOffline && (
        <div className="app-offline-banner" role="status">
          {t('offline.read_only')}
        </div>
      )}

      <AppDialogs
        dialogs={dialogs}
        t={t}
        nodes={nodes}
        stats={stats}
        editingNode={editingNode}
        closeEditingNode={closeEditingNode}
        detailNode={detailNode}
        closeDetailNode={closeDetailNode}
        akcShortName={akcShortName}
        akcConfig={akcConfig}
        akcIntroShown={akcIntroShown}
        onAkcIntroShown={() => setAkcIntroShown(true)}
        onNodeCreated={handleNodeCreated}
        onNodeUpdate={handleNodeUpdate}
        onEdit={handleEdit}
        onEdgeUpdate={handleEdgeUpdate}
        onDeleteEdge={handleDeleteEdge}
        onConfirmDelete={handleConfirmDelete}
        onConfirmSaveView={handleConfirmSaveView}
        onExportGraph={handleExportGraph}
        onConnectSession={handleConnectSession}
        onRenameSession={handleRenameSession}
        onConfirmDeleteSession={handleConfirmDeleteSession}
        onSaveSubscription={handleSaveSubscription}
        onSaveSkill={handleSaveSkill}
        onSaveAgent={handleSaveAgent}
        onSaveAKC={handleSaveAKC}
      />

      <GuideOverlay />
      <AppInstallPrompt />
    </div>
  );
}

function AppRoot() {
  if (_collectShortName) {
    return <CollectKioskView shortName={_collectShortName} />;
  }
  return <App />;
}

export default AppRoot;
