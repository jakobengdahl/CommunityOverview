import { useState, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
import { useI18n } from './i18n';
import FloatingHeader from './components/FloatingHeader';
import FloatingToolbar from './components/FloatingToolbar';
import FloatingSearch from './components/FloatingSearch';
import ChatPanel from './components/ChatPanel';
import CollectKioskView from './components/CollectKioskView';
import GuideOverlay from './components/GuideOverlay';
import SessionDrawer from './components/SessionDrawer';
import RecentActivityDrawer from './components/RecentActivityDrawer';
import AppDialogs from './components/AppDialogs';
import * as api from './services/api';
import * as sessionStore from './services/sessionStore';
import {
  annotationsToGroups,
  groupsToAnnotations,
  annotationsToOverlays,
  overlaysToAnnotations,
} from './utils/sessionAnnotations';
import { serverStateToMirror, useSharedSession } from './hooks/useSharedSession';
import { useSyncConnection } from './hooks/useSyncConnection';
import { useToolResultCommands } from './hooks/useToolResultCommands';
import './App.css';

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
    clearGroupsFlag,
    addNodesToVisualization,
    updateVisualization,
    toggleNodeVisibility,
    toggleEdgeVisibility,
    setHiddenNodeIds,
    setHiddenEdgeIds,
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
    presentation,
    setConfig,
    focusNodeId,
    clearFocusNode,
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
    nodeMarks,
    startGuide,
    getNodeColor,
    closeMenusSignal,
  } = useGraphStore();

  const { t, setLanguage, language } = useI18n();

  const urlGuideStartedRef = useRef(false);
  const urlViewLoadedRef = useRef(false);
  const latestViewport = useRef(null);
  const dialogOpenRef = useRef(false);
  const [notification, setNotification] = useState(null);
  const [deleteDialog, setDeleteDialog] = useState(null);
  const [saveViewDialog, setSaveViewDialog] = useState(null);
  const [showSubscriptionDialog, setShowSubscriptionDialog] = useState(false);
  const [editingSubscriptionData, setEditingSubscriptionData] = useState(null);
  const [showAgentDialog, setShowAgentDialog] = useState(false);
  const [editingAgentData, setEditingAgentData] = useState(null);
  const [showAgentRunsDialog, setShowAgentRunsDialog] = useState(false);
  const [agentRunsAgentId, setAgentRunsAgentId] = useState(null);
  const [createNodeType, setCreateNodeType] = useState(null);
  const [createGroupSignal, setCreateGroupSignal] = useState(0);
  const [saveViewSignal, setSaveViewSignal] = useState(0);
  const [isSavingView, setIsSavingView] = useState(false);
  const [editingEdge, setEditingEdge] = useState(null);
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
  const [sessionId, setSessionId] = useState(() => {
    const urlSession = _urlParams.get('session');
    return sessionStore.isValidSessionId(urlSession)
      ? urlSession
      : api.generateVisualizationSessionId();
  });
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [connectDialogOpen, setConnectDialogOpen] = useState(false);
  const [renameDialog, setRenameDialog] = useState(null);
  const [deleteSessionDialog, setDeleteSessionDialog] = useState(null);
  const [sessionsVersion, setSessionsVersion] = useState(0);
  const sessions = useMemo(() => sessionStore.listSessions(), [sessionsVersion]);

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
    opStreamReady,
  } = useSyncConnection(sessionId);
  const applyServerSessionRef = useRef(null);
  // MCP tool-result push application (external AI agent commands → canvas) and
  // the legacy SSE push stream. The op-stream `command` events are wired below
  // through syncHandlersRef and route through this same applyToolResultCommand.
  const applyToolResultCommand = useToolResultCommands({
    sessionId,
    opStreamReady,
    latestViewport,
  });

  // Apply a single remote op incrementally onto the local store + canvas. This
  // touches only the entities the op names, so a concurrent local edit is never
  // clobbered (unlike a wholesale reload). The sync client has already folded
  // the op into its baseline, so the store changes here do not echo back out.
  const applyRemoteOp = useCallback(
    async (op) => {
      const store = useGraphStore.getState();
      switch (op?.op) {
        case 'nodes_added': {
          const have = new Set(store.nodes.map((n) => n.id));
          const missing = (op.node_ids || []).filter((id) => !have.has(id));
          if (!missing.length) break;
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
          }
          break;
        }
        case 'nodes_removed':
          (op.node_ids || []).forEach((id) => removeNode(id));
          break;
        case 'nodes_hidden':
          setHiddenNodeIds(
            Array.from(new Set([...(store.hiddenNodeIds || []), ...(op.node_ids || [])]))
          );
          break;
        case 'nodes_shown': {
          const drop = new Set(op.node_ids || []);
          setHiddenNodeIds((store.hiddenNodeIds || []).filter((id) => !drop.has(id)));
          break;
        }
        case 'edges_hidden':
          setHiddenEdgeIds(
            Array.from(new Set([...(store.hiddenEdgeIds || []), ...(op.edge_ids || [])]))
          );
          break;
        case 'edges_shown': {
          const drop = new Set(op.edge_ids || []);
          setHiddenEdgeIds((store.hiddenEdgeIds || []).filter((id) => !drop.has(id)));
          break;
        }
        case 'node_moved':
          // Merge, don't replace: a burst of moves in one tick must not lose all
          // but the last node's position.
          if (op.node_id && op.position)
            setRemotePositions((prev) => ({ ...(prev || {}), [op.node_id]: op.position }));
          break;
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
          }
          break;
        case 'annotation_created':
        case 'annotation_updated': {
          const ann = op.annotation;
          if (!ann || !ann.id) break;
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
          break;
        }
        case 'annotation_deleted':
          if (op.annotation_id)
            setRemoteAnnotationOps((prev) => [
              ...(prev || []),
              { action: 'delete', id: op.annotation_id },
            ]);
          break;
        case 'group_membership_changed':
          setRemoteAnnotationOps((prev) => [
            ...(prev || []),
            { action: 'membership', groupId: op.group_id, members: op.member_node_ids || [] },
          ]);
          break;
        default:
          break; // session_renamed handled by its own event
      }
    },
    [
      addNodesToVisualization,
      removeNode,
      setHiddenNodeIds,
      setHiddenEdgeIds,
      setRemotePositions,
      setAnimatedLayout,
      setRemoteAnnotationOps,
      syncRef,
    ]
  );

  // Reconnect / catch-up path (missed ops after a disconnect): reload the whole
  // session from the server and reset the baseline. Destructive, but a resync
  // only fires after a dropped stream, when the local user was not editing.
  const resyncFromServer = useCallback(
    async (targetId) => {
      let payload;
      try {
        payload = await api.getSession(targetId, { resolve: true });
      } catch {
        return;
      }
      if (!syncRef.current || syncRef.current.sessionId !== targetId) return; // switched away
      applyServerSessionRef.current?.(payload);
      const resolvedIds = (payload?.resolved?.nodes || []).map((n) => n.id);
      syncRef.current.setBaseline(serverStateToMirror(payload?.state, resolvedIds));
    },
    [syncRef]
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
      skillDialogType ||
      showAKCDialog ||
      drawerOpen ||
      settingsOpen ||
      connectDialogOpen ||
      renameDialog ||
      deleteSessionDialog ||
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
    skillDialogType,
    showAKCDialog,
    drawerOpen,
    settingsOpen,
    connectDialogOpen,
    renameDialog,
    deleteSessionDialog,
    akcShortName,
    akcConfig,
    akcIntroShown,
  ]);

  // Double-Escape to clear the canvas (works even from input fields)
  useEffect(() => {
    let lastEscape = 0;
    const handleKeyDown = (e) => {
      if (e.key !== 'Escape') return;
      if (e.repeat) return;
      if (dialogOpenRef.current) return;
      const now = Date.now();
      if (now - lastEscape < 400) {
        clearVisualization();
        lastEscape = 0;
      } else {
        lastEscape = now;
      }
    };
    // Capture phase so it fires even when focus is inside an input/textarea
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [clearVisualization]);

  // ── Session bootstrap (once) ────────────────────────────────────────────
  // Reflect the initial session id in the URL and, when it came from a
  // ?session= share link, load its content from the server. A freshly
  // generated id has no server content yet — it materialises on first save.
  useEffect(() => {
    reflectSessionUrl(sessionId);
    const urlSession = _urlParams.get('session');
    if (sessionStore.isValidSessionId(urlSession)) {
      sessionStore.touchSession(urlSession);
      // This id came in via a ?session= deep link, so a 404 means the linked
      // session is gone/expired (not a brand-new local one) — surface it rather
      // than silently opening an empty canvas (contract §5.3).
      loadSessionFromServer(urlSession, {
        eagerConnect: true,
        onMissing: () => showNotification('info', t('sessions.link_not_found')),
      });
    }
    setSessionsVersion((v) => v + 1);
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
      } catch (error) {
        console.error('Error loading configuration:', error);
        api.getGraphStats().then(setStats).catch(console.error);
        setLlmAvailable(false);
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
        const result = await api.getSavedView(urlViewName);
        if (!result?.success || !result.nodes?.length) return;
        const positioned = result.nodes.map((n) =>
          result.positions?.[n.id] ? { ...n, _savedPosition: result.positions[n.id] } : n
        );
        clearVisualization();
        addNodesToVisualization(positioned, result.edges || []);
        if (result.hidden_node_ids?.length) setHiddenNodeIds(result.hidden_node_ids);
        if (result.groups?.length)
          setPendingGroups({ groups: result.groups, parentIds: result.parentIds || {} });
        if (result.annotations?.length) setPendingAnnotations(result.annotations);
      } catch (err) {
        console.error('[App] Failed to load view from URL:', err);
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
      // Advertise the local selection as advisory soft-locks so collaborators see
      // colored markers on the graph nodes this user is working with (design 3.5).
      // Only real graph nodes carry markers, so annotations/groups are excluded.
      const claimIds = selectedNodes.filter((n) => n.type === 'custom').map((n) => n.id);
      syncRef.current?.setLocalSelection(claimIds);
    },
    [setSelectedGraphNodes]
  );

  // Callback: Double-click on node
  const handleNodeDoubleClick = useCallback(
    async (nodeId, nodeData) => {
      // If it's a SavedView, load it directly
      if (nodeData.type === 'SavedView' || nodeData.nodeType === 'SavedView') {
        try {
          const nodeIds = nodeData.metadata?.node_ids || [];
          const positions = nodeData.metadata?.positions || {};
          const savedEdges = nodeData.metadata?.edges || [];
          const savedGroups = nodeData.metadata?.groups || [];
          const savedParentIds = nodeData.metadata?.parentIds || {};
          const savedAnnotations = nodeData.metadata?.annotations || [];
          if (nodeIds.length > 0) {
            clearVisualization();
            const details = await Promise.all(
              nodeIds.map((id) => api.getNodeDetails(id).catch(() => null))
            );
            const loadedNodes = details
              .filter((d) => d?.success)
              .map((d) => {
                const n = d.node;
                if (positions[n.id]) {
                  return { ...n, _savedPosition: positions[n.id] };
                }
                return n;
              });
            if (loadedNodes.length > 0) {
              let edgesToLoad = savedEdges.length > 0 ? savedEdges : [];
              if (edgesToLoad.length === 0) {
                const loadedIds = new Set(loadedNodes.map((n) => n.id));
                const savedEdgeIds = new Set(nodeData.metadata?.edge_ids || []);
                for (const d of details) {
                  if (d?.edges) {
                    const relevant = d.edges.filter(
                      (e) =>
                        loadedIds.has(e.source) &&
                        loadedIds.has(e.target) &&
                        (savedEdgeIds.size === 0 || savedEdgeIds.has(e.id))
                    );
                    edgesToLoad.push(...relevant);
                  }
                }
              }
              const edgeMap = new Map(edgesToLoad.map((e) => [e.id, e]));
              addNodesToVisualization(loadedNodes, Array.from(edgeMap.values()));
              if (savedGroups.length > 0) {
                setPendingGroups({ groups: savedGroups, parentIds: savedParentIds });
              }
              if (savedAnnotations.length > 0) {
                setPendingAnnotations(savedAnnotations);
              }
            }
          }
          showNotification('info', `Loaded saved view: ${nodeData.name || nodeData.label}`);
        } catch (err) {
          console.error('Error loading saved view:', err);
          showNotification('error', 'Could not load saved view');
        }
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

  // Callback: Expand node to show related nodes
  const handleExpand = useCallback(
    async (nodeId, nodeData) => {
      try {
        const result = await api.getRelatedNodes(nodeId, { depth: 1 });
        if (result.nodes && result.nodes.length > 0) {
          const existingIds = new Set(nodes.map((n) => n.id));
          const newCount = result.nodes.filter((n) => !existingIds.has(n.id)).length;
          addNodesToVisualization(result.nodes, result.edges || []);
          if (newCount > 0) {
            showNotification('success', `Added ${newCount} new node${newCount !== 1 ? 's' : ''}`);
          } else {
            showNotification('info', 'All related nodes already in view');
          }
        } else {
          showNotification('info', 'No related nodes found');
        }
      } catch (error) {
        console.error('Error expanding node:', error);
        showNotification('error', 'Could not expand node');
      }
    },
    [nodes, addNodesToVisualization, showNotification]
  );

  // Callback: Edit node
  const handleEdit = useCallback(
    async (nodeId, nodeData) => {
      if (nodeData.type === 'Agent') {
        try {
          let subscriptionNode = null;
          const subId = nodeData.metadata?.subscription_id;

          if (subId) {
            const result = await api.getNodeDetails(subId);
            if (result.success) {
              subscriptionNode = result.node;
            }
          }

          setEditingAgentData({ agent: nodeData, subscription: subscriptionNode });
          setShowAgentDialog(true);
        } catch (error) {
          console.error('Error preparing agent editor:', error);
          showNotification('error', 'Could not load agent details');
        }
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

  // Callback: Delete edge (from backend and visualization)
  const handleDeleteEdge = useCallback(
    async (edgeId) => {
      try {
        const result = await api.deleteEdge(edgeId);
        if (!result?.success) {
          throw new Error('Could not delete edge');
        }
        removeEdge(edgeId);
        showNotification('success', 'Edge deleted');
      } catch (error) {
        console.error('Error deleting edge:', error);
        showNotification('error', 'Could not delete edge');
      }
    },
    [removeEdge, showNotification]
  );

  // Callback: Edit edge - opens EditEdgeDialog
  const handleEditEdge = useCallback(
    (edgeId, edgeData) => {
      const edge = edges.find((e) => e.id === edgeId);
      if (edge) {
        setEditingEdge({ ...edge, ...edgeData });
      }
    },
    [edges]
  );

  // Callback: Save edge updates from EditEdgeDialog
  const handleEdgeUpdate = useCallback(
    async (updates) => {
      if (!editingEdge) return;
      try {
        await api.updateEdge(editingEdge.id, updates);
        const newEdges = edges.map((e) => (e.id === editingEdge.id ? { ...e, ...updates } : e));
        updateVisualization(nodes, newEdges);
        setEditingEdge(null);
        showNotification('success', 'Edge updated');
      } catch (error) {
        console.error('Error updating edge:', error);
        showNotification('error', 'Could not update edge');
      }
    },
    [editingEdge, nodes, edges, updateVisualization, showNotification]
  );

  // Callback: Change an edge's relationship type from the context menu.
  // Persists to the backend and updates the single edge in place so groups and
  // node positions are preserved.
  const handleSetEdgeType = useCallback(
    async (edgeId, type) => {
      try {
        await api.updateEdge(edgeId, { type: type || null });
        updateEdgeData(edgeId, { type: type || 'RELATES_TO' });
        showNotification('success', 'Connection type updated');
      } catch (error) {
        console.error('Error updating edge type:', error);
        showNotification('error', 'Could not update connection');
      }
    },
    [updateEdgeData, showNotification]
  );

  // Callback: Connect nodes (from drag-connect in canvas)
  const handleConnect = useCallback(
    async (params) => {
      try {
        const result = await api.addEdge(params.source, params.target);
        if (result.success && result.edge) {
          addNodesToVisualization([], [result.edge]);
        } else {
          // The edge is only drawn once persisted, so a non-success response must
          // surface an error rather than silently leaving nothing on the canvas.
          showNotification('error', 'Could not create connection');
        }
      } catch (error) {
        console.error('Error creating edge:', error);
        showNotification('error', 'Could not create connection');
      }
    },
    [addNodesToVisualization, showNotification]
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
    [nodes]
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
    [nodes]
  );

  // Confirm delete
  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialog) return;

    try {
      if (deleteDialog.isMultiple) {
        await api.deleteNodes(deleteDialog.nodeIds, true);
        deleteDialog.nodeIds.forEach((id) => removeNode(id));
        showNotification('success', `${deleteDialog.nodeIds.length} nodes deleted`);
      } else {
        await api.deleteNodes([deleteDialog.nodeId], true);
        removeNode(deleteDialog.nodeId);
        showNotification('success', 'Node deleted');
      }
    } catch (error) {
      console.error('Error deleting node(s):', error);
      showNotification('error', 'Could not delete node(s)');
    } finally {
      setDeleteDialog(null);
    }
  }, [deleteDialog, removeNode, showNotification]);

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
      if (state.nodes.length === 0 && !isSessionMaterialized()) return;
      const positions = {};
      const parentIds = {};
      (viewData?.nodes || []).forEach((n) => {
        if (n.position) positions[n.id] = n.position;
        if (n.parentId) parentIds[n.id] = n.parentId;
      });
      // Annotations carry group boxes plus the free-floating overlays (notes,
      // labels, arrows) the canvas collects in viewData.annotations. All kinds
      // share one server-side annotation list (design 3.1).
      const nextState = {
        node_refs: state.nodes.map((n) => n.id),
        positions,
        hidden_node_ids: state.hiddenNodeIds || [],
        hidden_edge_ids: state.hiddenEdgeIds || [],
        annotations: [
          ...groupsToAnnotations(viewData?.groups || [], parentIds),
          ...overlaysToAnnotations(viewData?.annotations || []),
        ],
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

  useEffect(() => {
    scheduleAutoSave();
    return () => {
      if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    };
  }, [nodes, edges, hiddenNodeIds, hiddenEdgeIds, scheduleAutoSave]);

  // Callback: Create group (called when group is created inside GraphCanvas)
  const handleCreateGroup = useCallback(
    (position, groupNode) => {
      showNotification('success', 'Group created');
      scheduleAutoSave();
    },
    [showNotification, scheduleAutoSave]
  );

  // Confirm save view
  const handleConfirmSaveView = useCallback(
    async (name) => {
      if (!saveViewDialog) return;

      setIsSavingView(true);
      try {
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
            parentIds: Object.fromEntries(
              saveViewDialog.viewData.nodes.filter((n) => n.parentId).map((n) => [n.id, n.parentId])
            ),
            edge_ids: (saveViewDialog.viewData.edges || []).map((e) => e.id),
            edges: saveViewDialog.viewData.edges || [],
            groups: saveViewDialog.viewData.groups,
            annotations: saveViewDialog.viewData.annotations || [],
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

  // Save subscription node
  const handleSaveSubscription = useCallback(
    async (data) => {
      try {
        if (data.id && data.updates) {
          await api.updateNode(data.id, data.updates);
          const newNodes = nodes.map((n) => (n.id === data.id ? { ...n, ...data.updates } : n));
          updateVisualization(newNodes, edges);
          setEditingSubscriptionData(null);
          showNotification(
            'success',
            t('notifications.subscription_updated', { name: data.updates.name })
          );
        } else {
          const result = await api.addNodes([data], []);
          if (result.added_node_ids?.length > 0) {
            addNodesToVisualization([{ ...data, id: result.added_node_ids[0] }], []);
          }
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
    [addNodesToVisualization, nodes, edges, updateVisualization, showNotification, t]
  );

  // Save agent nodes (create or update)
  const handleSaveAgent = useCallback(
    async (data) => {
      try {
        if (data.agentId) {
          // UPDATE
          const { agentId, agentUpdates, subscriptionId, subscriptionUpdates } = data;

          await api.updateNode(agentId, agentUpdates);
          if (subscriptionId && subscriptionUpdates) {
            await api.updateNode(subscriptionId, subscriptionUpdates);
          }

          const newNodes = nodes.map((n) => {
            if (n.id === agentId) return { ...n, ...agentUpdates };
            if (n.id === subscriptionId) return { ...n, ...subscriptionUpdates };
            return n;
          });
          updateVisualization(newNodes, edges);

          showNotification('success', 'Agent updated');
        } else {
          // CREATE
          const { nodes: agentNodes, edges: agentEdges } = data;
          const result = await api.addNodes(agentNodes, agentEdges);
          console.log('Agent created:', result);

          if (result.added_node_ids && result.added_node_ids.length > 0) {
            const nodesWithIds = agentNodes.map((node, index) => ({
              ...node,
              id: result.added_node_ids[index] || node.id,
            }));
            const edgesWithIds = agentEdges.map((edge, index) => ({
              ...edge,
              id: result.added_edge_ids?.[index] || edge.id,
              source:
                result.added_node_ids[agentNodes.findIndex((n) => n.type === 'Agent')] ||
                edge.source,
              target:
                result.added_node_ids[
                  agentNodes.findIndex((n) => n.type === 'EventSubscription')
                ] || edge.target,
            }));
            addNodesToVisualization(nodesWithIds, edgesWithIds);
          }

          const agentNode = agentNodes.find((n) => n.type === 'Agent');
          showNotification('success', `Agent "${agentNode?.name || 'Agent'}" created`);
        }
      } catch (error) {
        console.error('Error saving agent:', error);
        showNotification('error', 'Could not save agent');
      }
    },
    [nodes, edges, addNodesToVisualization, updateVisualization, showNotification]
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

  // Handle created node from CreateNodeDialog
  const handleNodeCreated = useCallback(
    (createdNode) => {
      addNodesToVisualization([createdNode], []);
      showNotification('success', `${createdNode.type} "${createdNode.name}" created`);
    },
    [addNodesToVisualization, showNotification]
  );

  // Callback: Save a skill node (create or update)
  const handleSaveSkill = useCallback(
    async (skillData) => {
      try {
        if ('id' in skillData) {
          const { id, updates } = skillData;
          await api.updateNode(id, updates);
          const newNodes = nodes.map((n) => (n.id === id ? { ...n, ...updates } : n));
          updateVisualization(newNodes, edges);
          showNotification('success', 'Skill updated');
        } else {
          const result = await api.addNodes([skillData], []);
          if (result.added_node_ids?.length > 0) {
            const nodeWithId = { ...skillData, id: result.added_node_ids[0] };
            addNodesToVisualization([nodeWithId], []);
          }
          showNotification('success', `${skillData.type} "${skillData.name}" created`);
        }
      } catch (error) {
        console.error('Error saving skill:', error);
        showNotification('error', 'Could not save skill');
      }
    },
    [nodes, edges, updateVisualization, addNodesToVisualization, showNotification]
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
          await api.updateNode(id, updates);
          const newNodes = nodes.map((n) => (n.id === id ? { ...n, ...updates } : n));
          updateVisualization(newNodes, edges);
          showNotification('success', 'Knowledge collection updated');
        } else {
          const result = await api.addNodes([nodeData], []);
          if (result.added_node_ids && result.added_node_ids.length > 0) {
            const withId = { ...nodeData, id: result.added_node_ids[0] };
            addNodesToVisualization([withId], []);
          }
          showNotification('success', `Collection "${nodeData.name}" created`);
        }
      } catch (error) {
        console.error('Error saving AKC:', error);
        showNotification('error', 'Could not save knowledge collection');
      }
    },
    [nodes, edges, addNodesToVisualization, updateVisualization, showNotification]
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
  const { applyServerSession, loadSessionFromServer } = useSharedSession({
    clearVisualization,
    addNodesToVisualization,
    setHiddenNodeIds,
    setHiddenEdgeIds,
    setPendingGroups,
    setPendingAnnotations,
    ensureSyncConnected,
    syncRef,
  });
  // Expose the latest applyServerSession to resyncFromServer (defined earlier).
  applyServerSessionRef.current = applyServerSession;

  // Keep the sync client's handlers pointed at the latest closures. A remote op
  // or resync triggers a debounced authoritative apply; a remote delete of the
  // active session drops the user into a fresh one with a notice (design D11);
  // a remote rename refreshes the recents name.
  useEffect(() => {
    syncHandlersRef.current = {
      onReady: () => {},
      onResync: () => resyncFromServer(sessionId),
      onRemoteOps: (ops) => {
        (ops || []).forEach((op) => applyRemoteOp(op));
      },
      onPresence: (r) => setRoster(r),
      onSelections: (s) => setRemoteSelections(s),
      onSessionRenamed: (name) => {
        sessionStore.renameSession(sessionId, name);
        setSessionsVersion((v) => v + 1);
      },
      onSessionDeleted: (deletedBy) => {
        if (deletedBy && deletedBy === api.getClientId()) return; // our own delete
        sessionStore.removeSession(sessionId);
        clearVisualization();
        const fresh = api.generateVisualizationSessionId();
        setSessionId(fresh);
        reflectSessionUrl(fresh);
        setSessionsVersion((v) => v + 1);
        showNotification('info', t('sessions.session_deleted_remote'));
      },
      onCommand: (command) => {
        if (command?.type === 'tool_result' && command.result) {
          applyToolResultCommand(command.result, command.command_id);
        }
      },
      // A 400/413 drop is terminal (malformed op, or a hard limit like the
      // annotation cap or an oversized layout_applied) — the op's effect
      // stays in the local canvas but will never persist or reach
      // collaborators. Surface it and resync so the canvas converges back to
      // whatever the server actually holds instead of silently drifting (R9).
      onDropped: () => {
        showNotification('error', t('sessions.change_not_saved'));
        resyncFromServer(sessionId);
      },
    };
  }, [
    sessionId,
    resyncFromServer,
    applyRemoteOp,
    clearVisualization,
    showNotification,
    t,
    applyToolResultCommand,
    syncHandlersRef,
    setRoster,
    setRemoteSelections,
  ]);

  // Switch working session: persist the current one first (ops via the snapshot
  // round-trip), then swap the session ID (reconnects the SSE stream, reflects
  // the URL) and load the target's canvas from the server.
  const switchToSession = useCallback(
    (targetId, { register = true, eagerConnect = false } = {}) => {
      requestSessionSnapshot(async () => {
        try {
          await loadSessionFromServer(targetId, { eagerConnect });
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
    switchToSession(api.generateVisualizationSessionId(), { register: false });
    showNotification('info', t('sessions.new_session_started'));
  }, [switchToSession, showNotification, t]);

  const handleSelectSession = useCallback(
    (targetId) => {
      if (targetId !== sessionId) {
        switchToSession(targetId);
      }
    },
    [sessionId, switchToSession]
  );

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

  const handleRenameSession = useCallback(
    (name) => {
      if (!renameDialog) return;
      sessionStore.renameSession(renameDialog.id, name);
      api.renameServerSession(renameDialog.id, name).catch(() => {});
      setSessionsVersion((v) => v + 1);
      setRenameDialog(null);
    },
    [renameDialog]
  );

  // Open the delete confirmation, first checking the roster so the dialog can
  // warn when other users are connected (design 3.6). The roster is populated
  // only once clients subscribe via the realtime stream (step 7), so in step 4
  // it is typically empty and the warning stays dormant.
  const handleRequestDeleteSession = useCallback(async (targetId) => {
    let connectedOthers = 0;
    try {
      const payload = await api.getSession(targetId);
      const roster = payload?.roster || [];
      connectedOthers = roster.filter((m) => m.client_id !== api.getClientId()).length;
    } catch {
      // Session may not exist server-side yet — nothing to warn about.
    }
    setDeleteSessionDialog({ id: targetId, connectedOthers });
  }, []);

  const handleConfirmDeleteSession = useCallback(async () => {
    if (!deleteSessionDialog) return;
    const { id } = deleteSessionDialog;
    setDeleteSessionDialog(null);
    try {
      await api.deleteServerSession(id, api.getClientId());
    } catch {
      // Ignore — the session may only ever have existed locally.
    }
    sessionStore.removeSession(id);
    if (id === sessionId) {
      // Deleting the active session: drop its content and switch into a fresh
      // one (design 3.6). Other connected clients are notified via the
      // server's session_deleted broadcast (handled once realtime lands).
      clearVisualization();
      const fresh = api.generateVisualizationSessionId();
      setSessionId(fresh);
      reflectSessionUrl(fresh);
      showNotification('info', t('sessions.session_deleted'));
    }
    setSessionsVersion((v) => v + 1);
  }, [deleteSessionDialog, sessionId, clearVisualization, showNotification, t]);

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
        await api.updateNode(nodeId, updates);
        const newNodes = nodes.map((n) => (n.id === nodeId ? { ...n, ...updates } : n));
        updateVisualization(newNodes, edges);
        closeEditingNode();
        showNotification('success', 'Node updated');
      } catch (error) {
        console.error('Error updating node:', error);
        showNotification('error', 'Could not update node');
      }
    },
    [nodes, edges, updateVisualization, closeEditingNode, showNotification]
  );

  // Modal dialog open/close (and edit-target) state, bundled for AppDialogs
  // (STRUCTURE_REVIEW B1 slice 3). The state itself stays in App because the
  // double-Escape guard and SessionDrawer's suspendEscape derive from it
  // alongside the store-driven editingNode/detailNode; AppDialogs owns only the
  // rendering of the stack.
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

  return (
    <div className={`app${drawerOpen ? ' session-drawer-open' : ''}`}>
      <div className="app-canvas" id="guide-target-canvas">
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          highlightedNodeIds={highlightedNodeIds}
          hiddenNodeIds={hiddenNodeIds}
          hiddenEdgeIds={hiddenEdgeIds}
          nodeMarks={nodeMarks}
          clearGroupsFlag={clearGroupsFlag}
          onExpand={handleExpand}
          onEdit={handleEdit}
          onDelete={handleDelete}
          onHide={handleHide}
          onDeleteMultiple={handleDeleteMultiple}
          onHideMultiple={handleHideMultiple}
          onHideEdge={handleHideEdge}
          onDeleteEdge={handleDeleteEdge}
          onEditEdge={handleEditEdge}
          onSetEdgeType={handleSetEdgeType}
          onConnect={handleConnect}
          onCreateGroup={handleCreateGroup}
          onNodePositionChange={scheduleAutoSave}
          onSaveView={handleSaveView}
          onCreateSubscription={handleCreateSubscription}
          onCreateAgent={handleCreateAgent}
          onDropCreateNode={handleDropCreateNode}
          onShowOnly={handleShowOnly}
          onSelectionChange={handleSelectionChange}
          onNodeDoubleClick={handleNodeDoubleClick}
          focusNodeId={focusNodeId}
          onFocusComplete={clearFocusNode}
          createGroupSignal={createGroupSignal}
          saveViewSignal={saveViewSignal}
          closeMenusSignal={closeMenusSignal}
          groupsToRestore={pendingGroups}
          onGroupsRestored={() => setPendingGroups(null)}
          annotationsToRestore={pendingAnnotations}
          onAnnotationsRestored={() => setPendingAnnotations(null)}
          onAnnotationChange={scheduleAutoSave}
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
          agentArrangingLabel={t('sessions.agent_arranging')}
          remoteAnnotationOps={remoteAnnotationOps}
          onRemoteAnnotationsApplied={() => setRemoteAnnotationOps(null)}
          remoteSelections={remoteSelections}
          federationDepth={federationDepth}
          onFederationDepthChange={setFederationDepth}
          maxFederationDepth={maxFederationDepth}
          federationDepthLevels={federationDepthLevels}
          federationDepthLabel={t('federation.depth_label')}
          federationDepthTooltip={t('federation.depth_tooltip')}
          showMinimap={showMinimap}
          schema={schema}
          onContextMenuAction={handleContextMenuAction}
          contextMenuLabels={{
            edit: t('context_menu.edit'),
            hide: t('context_menu.hide'),
            expand: t('context_menu.expand'),
            delete: t('context_menu.delete'),
            nodesSelected: t('context_menu.nodes_selected'),
            showOnly: t('context_menu.show_only'),
            selectSameType: t('context_menu.select_same_type'),
            hideAll: t('context_menu.hide_all'),
            deleteAll: t('context_menu.delete_all'),
            changeType: t('context_menu.change_type'),
            generalConnection: t('context_menu.general_connection'),
            addNote: t('context_menu.add_note'),
            addLabel: t('context_menu.add_label'),
            addArrow: t('context_menu.add_arrow'),
            annotationColor: t('context_menu.annotation_color'),
            deleteAnnotation: t('context_menu.delete'),
            notePlaceholder: t('context_menu.note_placeholder'),
            labelPlaceholder: t('context_menu.label_placeholder'),
            annotationTextSize: t('context_menu.annotation_text_size'),
            arrowStartHead: t('context_menu.arrow_start_head'),
            arrowEndHead: t('context_menu.arrow_end_head'),
          }}
          nodeColorResolver={getNodeColor}
          onViewportChange={(vp) => {
            latestViewport.current = vp;
          }}
        />
      </div>

      <FloatingHeader
        sessionId={sessionId}
        roster={roster}
        currentClientId={api.getClientId()}
        onClear={clearVisualization}
        onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
      />
      <SessionDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        sessions={sessions}
        currentSessionId={sessionId}
        onNewSession={handleNewSession}
        onConnectSession={() => setConnectDialogOpen(true)}
        onSelectSession={handleSelectSession}
        onRenameSession={(id) => {
          const entry = sessions.find((s) => s.id === id);
          setRenameDialog({ id, name: entry?.name || '' });
        }}
        onDeleteSession={handleRequestDeleteSession}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenActivity={() => {
          setDrawerOpen(false);
          setActivityOpen(true);
        }}
        suspendEscape={
          !!(
            // The drawer is non-modal, so any dialog can be stacked on top of
            // it; while one is open, Escape belongs to that dialog.
            settingsOpen ||
            connectDialogOpen ||
            renameDialog ||
            deleteSessionDialog ||
            createNodeType ||
            editingNode ||
            detailNode ||
            editingEdge ||
            deleteDialog ||
            saveViewDialog ||
            showSubscriptionDialog ||
            showAgentDialog ||
            showAgentRunsDialog ||
            skillDialogType ||
            showAKCDialog
          )
        }
      />
      <RecentActivityDrawer open={activityOpen} onClose={() => setActivityOpen(false)} />
      {maxFederationDepth > 1 && (
        <div className="app-a11y-depth-live" aria-live="polite" aria-atomic="true">
          {t('federation.depth_indicator', { current: federationDepth, max: maxFederationDepth })}
        </div>
      )}
      <FloatingSearch />
      <FloatingToolbar
        onCreateNode={handleCreateNodeForType}
        onCreateAgent={handleCreateAgent}
        onCreateSubscription={handleCreateSubscription}
        onSaveView={handleToolbarSaveView}
        onCreateGroup={handleToolbarCreateGroup}
        onCreateActiveKnowledgeCollection={handleCreateAKC}
      />
      {llmAvailable && <ChatPanel collectionShortName={akcShortName || undefined} />}

      {notification && (
        <div className={`app-notification app-notification-${notification.type}`}>
          <span>{notification.message}</span>
          <button onClick={() => setNotification(null)}>×</button>
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
