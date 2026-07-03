import { useState, useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import { GraphCanvas, positionNewNodes } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
import { useI18n } from './i18n';
import FloatingHeader from './components/FloatingHeader';
import FloatingToolbar from './components/FloatingToolbar';
import FloatingSearch from './components/FloatingSearch';
import CreateNodeDialog from './components/CreateNodeDialog';
import EditNodeDialog from './components/EditNodeDialog';
import ConfirmDialog from './components/ConfirmDialog';
import InputDialog from './components/InputDialog';
import ChatPanel from './components/ChatPanel';
import CreateSubscriptionDialog from './components/CreateSubscriptionDialog';
import CreateSkillDialog from './components/CreateSkillDialog';
import CreateAgentDialog from './components/CreateAgentDialog';
import CreateActiveKnowledgeCollectionDialog from './components/CreateActiveKnowledgeCollectionDialog';
import CollectKioskView from './components/CollectKioskView';
import EditEdgeDialog from './components/EditEdgeDialog';
import NodeDetailDialog from './components/NodeDetailDialog';
import GuideOverlay from './components/GuideOverlay';
import * as api from './services/api';
import './App.css';

const _urlParams = new URLSearchParams(window.location.search);
const _collectShortName = _urlParams.get('collect');
const _akcShortName = _urlParams.get('akc');

// Visualization session ID — generated once per page load, used to connect
// external AI clients to this browser window via MCP.
const _vizSessionId = api.generateVisualizationSessionId();

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
    stats,
    setStats,
    llmAvailable,
    setLlmAvailable,
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
  const [statsDialogOpen, setStatsDialogOpen] = useState(false);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const [notification, setNotification] = useState(null);
  const [deleteDialog, setDeleteDialog] = useState(null);
  const [saveViewDialog, setSaveViewDialog] = useState(null);
  const [showSubscriptionDialog, setShowSubscriptionDialog] = useState(false);
  const [editingSubscriptionData, setEditingSubscriptionData] = useState(null);
  const [showAgentDialog, setShowAgentDialog] = useState(false);
  const [editingAgentData, setEditingAgentData] = useState(null);
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

  const federationDepthLevels = (stats?.federation?.selectable_depth_levels || [1]).filter(v => Number.isInteger(v) && v >= 1);
  const maxFederationDepth = Math.max(1, ...federationDepthLevels, stats?.federation?.max_selectable_depth || 1);

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
      createNodeType || editingNode || detailNode || editingEdge ||
      deleteDialog || saveViewDialog || showSubscriptionDialog ||
      showAgentDialog || skillDialogType || showAKCDialog ||
      statsDialogOpen || headerMenuOpen || (akcShortName && akcConfig && !akcIntroShown)
    );
  }, [createNodeType, editingNode, detailNode, editingEdge,
      deleteDialog, saveViewDialog, showSubscriptionDialog,
      showAgentDialog, skillDialogType, showAKCDialog,
      statsDialogOpen, headerMenuOpen, akcShortName, akcConfig, akcIntroShown]);

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

  // ── Visualization session: SSE connection ──────────────────────────────
  // Opens a persistent SSE stream so external AI clients can push
  // visualization commands to this browser window via MCP.
  useEffect(() => {
    const evtSource = new EventSource(api.getVisualizationStreamUrl(_vizSessionId));
    evtSource.onmessage = (e) => {
      try {
        const cmd = JSON.parse(e.data);
        if (cmd.type === 'ping' || cmd.type === 'connected') return;
        if (cmd.type !== 'tool_result' || !cmd.result) return;

        const toolResult = cmd.result;
        const { nodes: currentNodes, edges: currentEdges,
                addNodesToVisualization: addNodes,
                updateVisualization: updateViz,
                clearVisualization: clearViz } = useGraphStore.getState();

        const filtered = (toolResult.nodes || []).filter(n =>
          n.type !== 'Community' && n.data?.type !== 'Community'
        );

        if (toolResult.action === 'add_to_visualization') {
          if (filtered.length > 0) {
            const allEdges = [...currentEdges, ...(toolResult.edges || [])];
            const vp = latestViewport.current;
            const viewportCenter = vp ? {
              x: (window.innerWidth / 2 - vp.x) / vp.zoom,
              y: (window.innerHeight / 2 - vp.y) / vp.zoom,
            } : null;
            const positioned = positionNewNodes(filtered, currentNodes, allEdges, { viewportCenter });
            addNodes(positioned, toolResult.edges || []);
          }
        } else if (toolResult.action === 'load_visualization' || toolResult.action === 'clear_visualization') {
          clearViz();
          if (filtered.length > 0) {
            updateViz(filtered, toolResult.edges || []);
          }
        } else if (filtered.length > 0) {
          updateViz(filtered, toolResult.edges || []);
        }
      } catch (err) {
        console.error('[Session SSE] parse error:', err);
      }
    };
    evtSource.onerror = () => {
      // Browser auto-reconnects on SSE errors; no manual retry needed.
    };
    return () => evtSource.close();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Visualization session: canvas state upload ─────────────────────────
  // Uploads the current visible node list so MCP tools can query it.
  const _sessionUploadTimer = useRef(null);
  useEffect(() => {
    if (_sessionUploadTimer.current) clearTimeout(_sessionUploadTimer.current);
    _sessionUploadTimer.current = setTimeout(() => {
      const state = useGraphStore.getState();
      api.updateSessionState(_vizSessionId, {
        visible_node_ids: state.nodes.map(n => n.id),
        selected_node_ids: (state.selectedGraphNodes || []).map(n => n.id),
        node_count: state.nodes.length,
      }).catch(() => {});
    }, 1000);
  }, [nodes]); // eslint-disable-line react-hooks/exhaustive-deps

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
      } catch (error) {
        console.error('Error loading configuration:', error);
        api.getGraphStats().then(setStats).catch(console.error);
        setLlmAvailable(false);
      }
    };
    loadConfig();
  }, [setConfig, setStats, setLlmAvailable, t, setLanguage, language]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!akcShortName) return;
    api.getCollectConfig(akcShortName)
      .then(data => setAkcConfig(data))
      .catch(err => console.error('[App] Failed to load AKC config:', err));
  }, [akcShortName]);

  // Trigger guide from URL param ?guide=<id> — fires once when presentation first becomes available
  useEffect(() => {
    if (!presentation?.guides?.length || urlGuideStartedRef.current) return;
    const urlGuideId = new URLSearchParams(window.location.search).get('guide');
    if (!urlGuideId) return;
    const guide = presentation.guides.find(g => g.id === urlGuideId);
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
        const positioned = result.nodes.map(n =>
          result.positions?.[n.id] ? { ...n, _savedPosition: result.positions[n.id] } : n
        );
        clearVisualization();
        addNodesToVisualization(positioned, result.edges || []);
        if (result.hidden_node_ids?.length) setHiddenNodeIds(result.hidden_node_ids);
        if (result.groups?.length) setPendingGroups({ groups: result.groups, parentIds: result.parentIds || {} });
      } catch (err) {
        console.error('[App] Failed to load view from URL:', err);
      }
    })();
  }, [stats, clearVisualization, addNodesToVisualization, setHiddenNodeIds, setPendingGroups]);

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);

  // Callback: Selection changed in GraphCanvas
  const handleSelectionChange = useCallback((selectedNodes) => {
    // Store full node data for the selected nodes
    const selectedWithData = selectedNodes
      .filter(n => n.type !== 'group')
      .map(n => {
        // n.data contains the full node info from the backend
        return n.data || n;
      });
    setSelectedGraphNodes(selectedWithData);
  }, [setSelectedGraphNodes]);

  // Callback: Double-click on node
  const handleNodeDoubleClick = useCallback(async (nodeId, nodeData) => {
    // If it's a SavedView, load it directly
    if (nodeData.type === 'SavedView' || nodeData.nodeType === 'SavedView') {
      try {
        const nodeIds = nodeData.metadata?.node_ids || [];
        const positions = nodeData.metadata?.positions || {};
        const savedEdges = nodeData.metadata?.edges || [];
        const savedGroups = nodeData.metadata?.groups || [];
        const savedParentIds = nodeData.metadata?.parentIds || {};
        if (nodeIds.length > 0) {
          clearVisualization();
          const details = await Promise.all(
            nodeIds.map(id => api.getNodeDetails(id).catch(() => null))
          );
          const loadedNodes = details.filter(d => d?.success).map(d => {
            const n = d.node;
            if (positions[n.id]) {
              return { ...n, _savedPosition: positions[n.id] };
            }
            return n;
          });
          if (loadedNodes.length > 0) {
            let edgesToLoad = savedEdges.length > 0 ? savedEdges : [];
            if (edgesToLoad.length === 0) {
              const loadedIds = new Set(loadedNodes.map(n => n.id));
              const savedEdgeIds = new Set(nodeData.metadata?.edge_ids || []);
              for (const d of details) {
                if (d?.edges) {
                  const relevant = d.edges.filter(
                    e => loadedIds.has(e.source) && loadedIds.has(e.target) &&
                      (savedEdgeIds.size === 0 || savedEdgeIds.has(e.id))
                  );
                  edgesToLoad.push(...relevant);
                }
              }
            }
            const edgeMap = new Map(edgesToLoad.map(e => [e.id, e]));
            addNodesToVisualization(loadedNodes, Array.from(edgeMap.values()));
            if (savedGroups.length > 0) {
              setPendingGroups({ groups: savedGroups, parentIds: savedParentIds });
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
  }, [clearVisualization, addNodesToVisualization, setPendingGroups, setDetailNode, showNotification]);

  // Callback: Expand node to show related nodes
  const handleExpand = useCallback(async (nodeId, nodeData) => {
    try {
      const result = await api.getRelatedNodes(nodeId, { depth: 1 });
      if (result.nodes && result.nodes.length > 0) {
        const existingIds = new Set(nodes.map(n => n.id));
        const newCount = result.nodes.filter(n => !existingIds.has(n.id)).length;
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
  }, [nodes, addNodesToVisualization, showNotification]);

  // Callback: Edit node
  const handleEdit = useCallback(async (nodeId, nodeData) => {
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
  }, [schema, setEditingNode, setEditingSkillData, setSkillDialogType, showNotification]);

  // Callback: Hide node
  const handleHide = useCallback((nodeId) => {
    toggleNodeVisibility(nodeId);
    showNotification('info', 'Node hidden');
  }, [toggleNodeVisibility, showNotification]);

  // Callback: Hide multiple nodes
  const handleHideMultiple = useCallback((nodeIds) => {
    nodeIds.forEach(id => toggleNodeVisibility(id));
    showNotification('info', `${nodeIds.length} nodes hidden`);
  }, [toggleNodeVisibility, showNotification]);

  // Callback: Hide edge
  const handleHideEdge = useCallback((edgeId) => {
    toggleEdgeVisibility(edgeId);
    showNotification('info', 'Edge hidden');
  }, [toggleEdgeVisibility, showNotification]);

  // Callback: Delete edge (from backend and visualization)
  const handleDeleteEdge = useCallback(async (edgeId) => {
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
  }, [removeEdge, showNotification]);

  // Callback: Edit edge - opens EditEdgeDialog
  const handleEditEdge = useCallback((edgeId, edgeData) => {
    const edge = edges.find(e => e.id === edgeId);
    if (edge) {
      setEditingEdge({ ...edge, ...edgeData });
    }
  }, [edges]);

  // Callback: Save edge updates from EditEdgeDialog
  const handleEdgeUpdate = useCallback(async (updates) => {
    if (!editingEdge) return;
    try {
      await api.updateEdge(editingEdge.id, updates);
      const newEdges = edges.map(e =>
        e.id === editingEdge.id ? { ...e, ...updates } : e
      );
      updateVisualization(nodes, newEdges);
      setEditingEdge(null);
      showNotification('success', 'Edge updated');
    } catch (error) {
      console.error('Error updating edge:', error);
      showNotification('error', 'Could not update edge');
    }
  }, [editingEdge, nodes, edges, updateVisualization, showNotification]);

  // Callback: Change an edge's relationship type from the context menu.
  // Persists to the backend and updates the single edge in place so groups and
  // node positions are preserved.
  const handleSetEdgeType = useCallback(async (edgeId, type) => {
    try {
      await api.updateEdge(edgeId, { type: type || null });
      updateEdgeData(edgeId, { type: type || 'RELATES_TO' });
      showNotification('success', 'Connection type updated');
    } catch (error) {
      console.error('Error updating edge type:', error);
      showNotification('error', 'Could not update connection');
    }
  }, [updateEdgeData, showNotification]);

  // Callback: Connect nodes (from drag-connect in canvas)
  const handleConnect = useCallback(async (params) => {
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
  }, [addNodesToVisualization, showNotification]);

  // Callback: Show only selected nodes (hide all others)
  const handleShowOnly = useCallback((nodeIds) => {
    const keepSet = new Set(nodeIds);
    const idsToHide = nodes.filter(n => !keepSet.has(n.id)).map(n => n.id);
    setHiddenNodeIds(idsToHide);
    showNotification('info', t('notifications.showing_nodes', { count: nodeIds.length }));
  }, [nodes, setHiddenNodeIds, showNotification]);

  // Callback: Delete node - shows dialog
  const handleDelete = useCallback((nodeId) => {
    const node = nodes.find(n => n.id === nodeId);
    setDeleteDialog({
      nodeId,
      nodeName: node?.name || node?.data?.label || nodeId,
      isMultiple: false,
    });
  }, [nodes]);

  // Callback: Delete multiple nodes - shows dialog
  const handleDeleteMultiple = useCallback((nodeIds) => {
    const nodeNames = nodeIds.map(id => {
      const node = nodes.find(n => n.id === id);
      return node?.name || node?.data?.label || id;
    });
    setDeleteDialog({
      nodeIds,
      nodeNames,
      isMultiple: true,
    });
  }, [nodes]);

  // Confirm delete
  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialog) return;

    try {
      if (deleteDialog.isMultiple) {
        await api.deleteNodes(deleteDialog.nodeIds, true);
        deleteDialog.nodeIds.forEach(id => removeNode(id));
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

  // Callback: Create group (called when group is created inside GraphCanvas)
  const handleCreateGroup = useCallback((position, groupNode) => {
    showNotification('success', 'Group created');
  }, [showNotification]);

  // Toolbar: trigger group creation in GraphCanvas
  const handleToolbarCreateGroup = useCallback(() => {
    setCreateGroupSignal(prev => prev + 1);
  }, []);

  // Callback: Save view - shows dialog
  const handleSaveView = useCallback((viewData) => {
    setSaveViewDialog({ viewData });
    setSaveViewSignal(0); // Reset signal so it doesn't re-trigger
  }, []);

  // Confirm save view
  const handleConfirmSaveView = useCallback(async (name) => {
    if (!saveViewDialog) return;

    setIsSavingView(true);
    try {
      const viewNode = {
        name,
        type: 'SavedView',
        description: `Saved view: ${name}`,
        summary: `Contains ${saveViewDialog.viewData.nodes.length} nodes`,
        metadata: {
          node_ids: saveViewDialog.viewData.nodes.map(n => n.id),
          positions: Object.fromEntries(saveViewDialog.viewData.nodes.map(n => [n.id, n.position])),
          parentIds: Object.fromEntries(
            saveViewDialog.viewData.nodes
              .filter(n => n.parentId)
              .map(n => [n.id, n.parentId])
          ),
          edge_ids: (saveViewDialog.viewData.edges || []).map(e => e.id),
          edges: saveViewDialog.viewData.edges || [],
          groups: saveViewDialog.viewData.groups,
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
  }, [saveViewDialog, showNotification]);

  // Callback: Create subscription
  const handleCreateSubscription = useCallback(() => {
    setShowSubscriptionDialog(true);
  }, []);

  // Callback: Create agent
  const handleCreateAgent = useCallback(() => {
    setEditingAgentData(null);
    setShowAgentDialog(true);
  }, []);

  // Save subscription node
  const handleSaveSubscription = useCallback(async (data) => {
    try {
      if (data.id && data.updates) {
        await api.updateNode(data.id, data.updates);
        const newNodes = nodes.map(n => n.id === data.id ? { ...n, ...data.updates } : n);
        updateVisualization(newNodes, edges);
        setEditingSubscriptionData(null);
        showNotification('success', t('notifications.subscription_updated', { name: data.updates.name }));
      } else {
        const result = await api.addNodes([data], []);
        if (result.added_node_ids?.length > 0) {
          addNodesToVisualization([{ ...data, id: result.added_node_ids[0] }], []);
        }
        showNotification('success', t('notifications.subscription_created', { name: data.name }));
      }
    } catch (error) {
      console.error('Error saving subscription:', error);
      showNotification('error', data?.updates ? t('notifications.subscription_update_error') : t('notifications.subscription_error'));
    }
  }, [addNodesToVisualization, nodes, edges, updateVisualization, showNotification, t]);

  // Save agent nodes (create or update)
  const handleSaveAgent = useCallback(async (data) => {
    try {
      if (data.agentId) {
        // UPDATE
        const { agentId, agentUpdates, subscriptionId, subscriptionUpdates } = data;

        await api.updateNode(agentId, agentUpdates);
        if (subscriptionId && subscriptionUpdates) {
          await api.updateNode(subscriptionId, subscriptionUpdates);
        }

        const newNodes = nodes.map(n => {
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
            source: result.added_node_ids[agentNodes.findIndex(n => n.type === 'Agent')] || edge.source,
            target: result.added_node_ids[agentNodes.findIndex(n => n.type === 'EventSubscription')] || edge.target,
          }));
          addNodesToVisualization(nodesWithIds, edgesWithIds);
        }

        const agentNode = agentNodes.find(n => n.type === 'Agent');
        showNotification('success', `Agent "${agentNode?.name || 'Agent'}" created`);
      }
    } catch (error) {
      console.error('Error saving agent:', error);
      showNotification('error', 'Could not save agent');
    }
  }, [nodes, edges, addNodesToVisualization, updateVisualization, showNotification]);

  // Callback: Create node from toolbar
  const handleCreateNodeForType = useCallback((nodeType) => {
    if (schema?.node_types?.[nodeType]?.ui_form === 'skill') {
      setEditingSkillData(null);
      setSkillDialogType(nodeType);
    } else {
      setCreateNodeType(nodeType);
    }
  }, [schema]);

  // Handle created node from CreateNodeDialog
  const handleNodeCreated = useCallback((createdNode) => {
    addNodesToVisualization([createdNode], []);
    showNotification('success', `${createdNode.type} "${createdNode.name}" created`);
  }, [addNodesToVisualization, showNotification]);

  // Callback: Save a skill node (create or update)
  const handleSaveSkill = useCallback(async (skillData) => {
    try {
      if ('id' in skillData) {
        const { id, updates } = skillData;
        await api.updateNode(id, updates);
        const newNodes = nodes.map(n => n.id === id ? { ...n, ...updates } : n);
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
  }, [nodes, edges, updateVisualization, addNodesToVisualization, showNotification]);

  const handleCreateAKC = useCallback(() => {
    setEditingAKCData(null);
    setShowAKCDialog(true);
  }, []);

  const handleSaveAKC = useCallback(async (nodeData) => {
    try {
      if (nodeData.id) {
        const { id, ...updates } = nodeData;
        await api.updateNode(id, updates);
        const newNodes = nodes.map(n => n.id === id ? { ...n, ...updates } : n);
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
  }, [nodes, edges, addNodesToVisualization, updateVisualization, showNotification]);

  // Callback: Context menu action triggered from schema-defined callback items
  const handleContextMenuAction = useCallback((actionName, nodeId, nodeData) => {
    // Dispatch named callback actions from schema context_menu entries.
    // Add cases here as new callback-type actions are implemented.
    switch (actionName) {
      default:
        console.warn(`[handleContextMenuAction] Unhandled action: "${actionName}". Wire it up in App.jsx.`);
        showNotification('info', `Action: ${actionName}`);
    }
  }, [addNodesToVisualization, showNotification]);

  // Toolbar save view: signal GraphCanvas to collect positions and trigger dialog
  const handleToolbarSaveView = useCallback(() => {
    setSaveViewSignal(prev => prev + 1);
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
  const handleDropCreateNode = useCallback((nodeType, position) => {
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
  }, [handleCreateAgent, handleCreateSubscription, handleCreateAKC, schema]);

  // Handle node update from edit dialog
  const handleNodeUpdate = useCallback(async (nodeId, updates) => {
    try {
      await api.updateNode(nodeId, updates);
      const newNodes = nodes.map(n =>
        n.id === nodeId ? { ...n, ...updates } : n
      );
      updateVisualization(newNodes, edges);
      closeEditingNode();
      showNotification('success', 'Node updated');
    } catch (error) {
      console.error('Error updating node:', error);
      showNotification('error', 'Could not update node');
    }
  }, [nodes, edges, updateVisualization, closeEditingNode, showNotification]);

  return (
    <div className="app">
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
          }}
          nodeColorResolver={getNodeColor}
          onViewportChange={(vp) => { latestViewport.current = vp; }}
        />
      </div>

      <FloatingHeader stats={stats} onExportGraph={handleExportGraph} sessionId={_vizSessionId} onClear={clearVisualization} onStatsDialogChange={setStatsDialogOpen} onMenuOpenChange={setHeaderMenuOpen} />
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

      {createNodeType && (
        <CreateNodeDialog
          nodeType={createNodeType}
          onClose={() => setCreateNodeType(null)}
          onSave={handleNodeCreated}
        />
      )}

      {editingNode && (
        <EditNodeDialog
          node={editingNode}
          onClose={closeEditingNode}
          onSave={(updates) => handleNodeUpdate(editingNode.id, updates)}
        />
      )}

      {detailNode && (
        <NodeDetailDialog
          node={detailNode}
          onClose={closeDetailNode}
          onEdit={(nodeId, nodeData) => {
            closeDetailNode();
            handleEdit(nodeId, nodeData);
          }}
        />
      )}

      {editingEdge && (
        <EditEdgeDialog
          edge={editingEdge}
          nodes={nodes}
          onClose={() => setEditingEdge(null)}
          onSave={handleEdgeUpdate}
          onDelete={(edgeId) => {
            handleDeleteEdge(edgeId);
            setEditingEdge(null);
          }}
        />
      )}

      {deleteDialog && (
        <ConfirmDialog
          title={deleteDialog.isMultiple ? "Delete Nodes" : "Delete Node"}
          message={
            deleteDialog.isMultiple
              ? `Are you sure you want to delete ${deleteDialog.nodeIds.length} nodes? This action cannot be undone.\n\nNodes to delete:\n• ${deleteDialog.nodeNames.slice(0, 5).join('\n• ')}${deleteDialog.nodeNames.length > 5 ? `\n• ... and ${deleteDialog.nodeNames.length - 5} more` : ''}`
              : `Are you sure you want to delete "${deleteDialog.nodeName}"? This action cannot be undone.`
          }
          confirmText="Delete"
          cancelText="Cancel"
          confirmStyle="danger"
          onConfirm={handleConfirmDelete}
          onCancel={() => setDeleteDialog(null)}
        />
      )}

      {saveViewDialog && (
        <InputDialog
          title="Save View"
          label="View name"
          placeholder="Enter a name for this view..."
          confirmText="Save"
          cancelText="Cancel"
          loadingText={t('common.saving')}
          isLoading={isSavingView}
          onConfirm={handleConfirmSaveView}
          onCancel={() => setSaveViewDialog(null)}
        />
      )}

      {notification && (
        <div className={`app-notification app-notification-${notification.type}`}>
          <span>{notification.message}</span>
          <button onClick={() => setNotification(null)}>×</button>
        </div>
      )}

      {showSubscriptionDialog && (
        <CreateSubscriptionDialog
          onClose={() => { setShowSubscriptionDialog(false); setEditingSubscriptionData(null); }}
          onSave={handleSaveSubscription}
          initialData={editingSubscriptionData}
        />
      )}

      {skillDialogType && (
        <CreateSkillDialog
          nodeType={skillDialogType}
          initialData={editingSkillData}
          onClose={() => { setSkillDialogType(null); setEditingSkillData(null); }}
          onSave={handleSaveSkill}
        />
      )}

      {showAgentDialog && (
        <CreateAgentDialog
          onClose={() => {
            setShowAgentDialog(false);
            setEditingAgentData(null);
          }}
          onSave={handleSaveAgent}
          initialData={editingAgentData}
        />
      )}

      {showAKCDialog && (
        <CreateActiveKnowledgeCollectionDialog
          onClose={() => {
            setShowAKCDialog(false);
            setEditingAKCData(null);
          }}
          onSave={handleSaveAKC}
          initialData={editingAKCData}
        />
      )}

      {akcShortName && akcConfig && !akcIntroShown && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.82)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 3000,
          }}
        >
          <div
            style={{
              background: '#1a1a1a',
              border: '1px solid #2e2e2e',
              borderRadius: '16px',
              boxShadow: '0 12px 48px rgba(0,0,0,0.6)',
              padding: '2.5rem 3rem',
              maxWidth: '520px',
              width: '90%',
              textAlign: 'center',
            }}
          >
            <h2 style={{ margin: '0 0 1rem 0', color: '#fff' }}>Knowledge Collection</h2>
            <p style={{ color: '#bbb', fontSize: '0.95rem', lineHeight: 1.65, marginBottom: '1.5rem' }}>
              The AI assistant has been pre-loaded with special collection instructions.
            </p>
            <button
              onClick={() => setAkcIntroShown(true)}
              style={{
                padding: '0.7rem 2rem',
                background: '#F59E0B',
                color: '#000',
                border: 'none',
                borderRadius: '8px',
                fontSize: '0.95rem',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              Open Graph
            </button>
          </div>
        </div>
      )}

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
