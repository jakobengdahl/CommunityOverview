import { useState, useCallback, useEffect } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
import StatsPanel from './components/StatsPanel';
import EditNodeDialog from './components/EditNodeDialog';
import ConfirmDialog from './components/ConfirmDialog';
import InputDialog from './components/InputDialog';
import ChatPanel from './components/ChatPanel';
import * as api from './services/api';
import './App.css';

function App() {
  const {
    nodes,
    edges,
    highlightedNodeIds,
    hiddenNodeIds,
    clearGroupsFlag,
    addNodesToVisualization,
    updateVisualization,
    toggleNodeVisibility,
    stats,
    setStats,
    editingNode,
    setEditingNode,
    closeEditingNode,
    removeNode,
  } = useGraphStore();

  const [notification, setNotification] = useState(null);
  const [deleteDialog, setDeleteDialog] = useState(null); // { nodeId, nodeName }
  const [saveViewDialog, setSaveViewDialog] = useState(null); // { viewData }

  // Load initial stats
  useEffect(() => {
    api.getGraphStats().then(setStats).catch(console.error);
  }, [setStats]);

  const showNotification = useCallback((type, message) => {
    setNotification({ type, message });
    setTimeout(() => setNotification(null), 3000);
  }, []);

  // Callback: Expand node to show related nodes
  const handleExpand = useCallback(async (nodeId, nodeData) => {
    try {
      const result = await api.getRelatedNodes(nodeId, { depth: 1 });
      if (result.nodes && result.nodes.length > 0) {
        // Filter out Community nodes
        const filteredNodes = result.nodes.filter(n =>
          n.type !== 'Community' && n.data?.type !== 'Community'
        );
        addNodesToVisualization(filteredNodes, result.edges || []);
        showNotification('success', `Added ${filteredNodes.length} related nodes`);
      } else {
        showNotification('info', 'No related nodes found');
      }
    } catch (error) {
      console.error('Error expanding node:', error);
      showNotification('error', 'Could not expand node');
    }
  }, [addNodesToVisualization, showNotification]);

  // Callback: Edit node
  const handleEdit = useCallback((nodeId, nodeData) => {
    setEditingNode({ id: nodeId, data: nodeData });
  }, [setEditingNode]);

  // Callback: Hide node
  const handleHide = useCallback((nodeId) => {
    toggleNodeVisibility(nodeId);
    showNotification('info', 'Node hidden');
  }, [toggleNodeVisibility, showNotification]);

  // Callback: Delete node - shows dialog
  const handleDelete = useCallback((nodeId) => {
    const node = nodes.find(n => n.id === nodeId);
    setDeleteDialog({
      nodeId,
      nodeName: node?.name || node?.data?.label || nodeId,
    });
  }, [nodes]);

  // Confirm delete
  const handleConfirmDelete = useCallback(async () => {
    if (!deleteDialog) return;

    try {
      await api.deleteNodes([deleteDialog.nodeId], true);
      removeNode(deleteDialog.nodeId);
      showNotification('success', 'Node deleted');
    } catch (error) {
      console.error('Error deleting node:', error);
      showNotification('error', 'Could not delete node');
    } finally {
      setDeleteDialog(null);
    }
  }, [deleteDialog, removeNode, showNotification]);

  // Callback: Create group
  const handleCreateGroup = useCallback((position, groupNode) => {
    showNotification('success', 'Group created');
  }, [showNotification]);

  // Callback: Save view - shows dialog
  const handleSaveView = useCallback((viewData) => {
    setSaveViewDialog({ viewData });
  }, []);

  // Confirm save view
  const handleConfirmSaveView = useCallback(async (name) => {
    if (!saveViewDialog) return;

    try {
      const viewNode = {
        name,
        type: 'SavedView',
        description: `Saved view: ${name}`,
        summary: `Contains ${saveViewDialog.viewData.nodes.length} nodes`,
        metadata: {
          node_ids: saveViewDialog.viewData.nodes.map(n => n.id),
          positions: Object.fromEntries(saveViewDialog.viewData.nodes.map(n => [n.id, n.position])),
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
      setSaveViewDialog(null);
    }
  }, [saveViewDialog, showNotification]);

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
      <header className="app-header">
        <h1>Community Knowledge Graph</h1>
        <StatsPanel stats={stats} />
      </header>

      <div className="app-content">
        <ChatPanel />

        <main className="app-main">
          <GraphCanvas
            nodes={nodes}
            edges={edges}
            highlightedNodeIds={highlightedNodeIds}
            hiddenNodeIds={hiddenNodeIds}
            clearGroupsFlag={clearGroupsFlag}
            onExpand={handleExpand}
            onEdit={handleEdit}
            onDelete={handleDelete}
            onHide={handleHide}
            onCreateGroup={handleCreateGroup}
            onSaveView={handleSaveView}
          />
        </main>
      </div>

      {editingNode && (
        <EditNodeDialog
          node={editingNode}
          onClose={closeEditingNode}
          onSave={(updates) => handleNodeUpdate(editingNode.id, updates)}
        />
      )}

      {deleteDialog && (
        <ConfirmDialog
          title="Delete Node"
          message={`Are you sure you want to delete "${deleteDialog.nodeName}"? This action cannot be undone.`}
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
    </div>
  );
}

export default App;
