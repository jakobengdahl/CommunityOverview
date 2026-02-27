import { useState, useCallback, useEffect } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
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
import CreateAgentDialog from './components/CreateAgentDialog';
import EditEdgeDialog from './components/EditEdgeDialog';
import NodeDetailDialog from './components/NodeDetailDialog';
import * as api from './services/api';
import './App.css';

function App() {
  const {
    nodes,
    edges,
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
    editingNode,
    setEditingNode,
    closeEditingNode,
    removeNode,
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
  } = useGraphStore();

  const { t, setLanguage } = useI18n();

  const [notification, setNotification] = useState(null);
  const [deleteDialog, setDeleteDialog] = useState(null);
  const [saveViewDialog, setSaveViewDialog] = useState(null);
  const [showSubscriptionDialog, setShowSubscriptionDialog] = useState(false);
  const [showAgentDialog, setShowAgentDialog] = useState(false);
  const [editingAgentData, setEditingAgentData] = useState(null);
  const [createNodeType, setCreateNodeType] = useState(null);
  const [createGroupSignal, setCreateGroupSignal] = useState(0);
  const [saveViewSignal, setSaveViewSignal] = useState(0);
  const [isSavingView, setIsSavingView] = useState(false);
  const [editingEdge, setEditingEdge] = useState(null);
  const [exportGraphSignal, setExportGraphSignal] = useState(0);

  const federationDepthLevels = (stats?.federation?.selectable_depth_levels || [1]).filter(v => Number.isInteger(v) && v >= 1);
  const maxFederationDepth = Math.max(1, ...federationDepthLevels, stats?.federation?.max_selectable_depth || 1);

  useEffect(() => {
    if (federationDepth > maxFederationDepth) {
      setFederationDepth(maxFederationDepth);
    }
  }, [federationDepth, maxFederationDepth, setFederationDepth]);

  // Load schema, presentation, and stats on startup
  useEffect(() => {
    const loadConfig = async () => {
      try {
        const [schemaData, presentationData, statsData] = await Promise.all([
          api.getSchema(),
          api.getPresentation(),
          api.getGraphStats(),
        ]);
        // Apply backend default language if no user override
        if (presentationData?.default_language) {
          const urlLang = new URLSearchParams(window.location.search).get('lang');
          const storedLang = localStorage.getItem('app_language');
          if (!urlLang && !storedLang) {
            setLanguage(presentationData.default_language);
          }
        }
        setConfig(schemaData, presentationData, t);
        setStats(statsData);
      } catch (error) {
        console.error('Error loading configuration:', error);
        api.getGraphStats().then(setStats).catch(console.error);
      }
    };
    loadConfig();
  }, [setConfig, setStats, t, setLanguage]);

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
              setPendingGroups(savedGroups);
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
    } else {
      setEditingNode({ id: nodeId, data: nodeData });
    }
  }, [setEditingNode, showNotification]);

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
      await api.deleteEdge(edgeId);
      const newEdges = edges.filter(e => e.id !== edgeId);
      updateVisualization(nodes, newEdges);
      showNotification('success', 'Edge deleted');
    } catch (error) {
      console.error('Error deleting edge:', error);
      // Still remove from visualization even if backend fails
      const newEdges = edges.filter(e => e.id !== edgeId);
      updateVisualization(nodes, newEdges);
      showNotification('info', 'Edge removed from view');
    }
  }, [nodes, edges, updateVisualization, showNotification]);

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

  // Callback: Connect nodes (from drag-connect in canvas)
  const handleConnect = useCallback(async (params) => {
    try {
      const result = await api.addEdge(params.source, params.target);
      if (result.success && result.edge) {
        addNodesToVisualization([], [result.edge]);
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
  const handleSaveSubscription = useCallback(async (subscriptionNode) => {
    try {
      const result = await api.addNodes([subscriptionNode], []);
      console.log('Subscription created:', result);

      if (result.added_node_ids && result.added_node_ids.length > 0) {
        const nodeId = result.added_node_ids[0];
        const nodeWithId = { ...subscriptionNode, id: nodeId };
        addNodesToVisualization([nodeWithId], []);
        console.log('Subscription added to visualization:', nodeId);
      }

      showNotification('success', t('notifications.subscription_created', { name: subscriptionNode.name }));
    } catch (error) {
      console.error('Error creating subscription:', error);
      showNotification('error', t('notifications.subscription_error'));
    }
  }, [addNodesToVisualization, showNotification]);

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
    setCreateNodeType(nodeType);
  }, []);

  // Handle created node from CreateNodeDialog
  const handleNodeCreated = useCallback((createdNode) => {
    addNodesToVisualization([createdNode], []);
    showNotification('success', `${createdNode.type} "${createdNode.name}" created`);
  }, [addNodesToVisualization, showNotification]);

  // Toolbar save view: signal GraphCanvas to collect positions and trigger dialog
  const handleToolbarSaveView = useCallback(() => {
    setSaveViewSignal(prev => prev + 1);
  }, []);

  // Trigger export graph signal (FloatingHeader → GraphCanvas)
  const handleTriggerExportGraph = useCallback(() => {
    setExportGraphSignal(prev => prev + 1);
  }, []);

  // Receive export data from GraphCanvas and download as JSON
  const handleExportGraph = useCallback((exportData) => {
    setExportGraphSignal(0);
    try {
      const output = {
        nodes: exportData.nodes,
        edges: exportData.edges,
        groups: exportData.groups,
        metadata: {
          version: '1.0',
          exported_at: new Date().toISOString(),
          node_count: exportData.nodes.length,
          edge_count: exportData.edges.length,
        },
      };

      const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
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
    } else {
      setCreateNodeType(nodeType);
    }
  }, [handleCreateAgent, handleCreateSubscription]);

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
      <div className="app-canvas">
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          highlightedNodeIds={highlightedNodeIds}
          hiddenNodeIds={hiddenNodeIds}
          hiddenEdgeIds={hiddenEdgeIds}
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
          exportGraphSignal={exportGraphSignal}
          onExportGraph={handleExportGraph}
          groupsToRestore={pendingGroups}
          onGroupsRestored={() => setPendingGroups(null)}
          federationDepth={federationDepth}
          onFederationDepthChange={setFederationDepth}
          maxFederationDepth={maxFederationDepth}
          federationDepthLevels={federationDepthLevels}
          federationDepthLabel={t('federation.depth_label')}
          federationDepthTooltip={t('federation.depth_tooltip')}
        />
      </div>

      <FloatingHeader stats={stats} onExportGraph={handleTriggerExportGraph} />
      <div className="app-a11y-depth-live" aria-live="polite" aria-atomic="true">
        {t('federation.depth_indicator', { current: federationDepth, max: maxFederationDepth })}
      </div>
      <FloatingSearch />
      <FloatingToolbar
        onCreateNode={handleCreateNodeForType}
        onCreateAgent={handleCreateAgent}
        onCreateSubscription={handleCreateSubscription}
        onSaveView={handleToolbarSaveView}
        onCreateGroup={handleToolbarCreateGroup}
      />
      <ChatPanel />

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
          onClose={() => setShowSubscriptionDialog(false)}
          onSave={handleSaveSubscription}
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
    </div>
  );
}

export default App;
