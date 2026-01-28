import { useState, useCallback, useEffect } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
import SearchPanel from './components/SearchPanel';
import StatsPanel from './components/StatsPanel';
import EditNodeDialog from './components/EditNodeDialog';
import ChatPanel from './components/ChatPanel';
import * as api from './services/api';
import './App.css';

function App() {
  const {
    nodes,
    edges,
    highlightedNodeIds,
    hiddenNodeIds,
    addNodesToVisualization,
    updateVisualization,
    stats,
    setStats,
    isChatOpen,
    setChatOpen,
  } = useGraphStore();

  const [editingNode, setEditingNode] = useState(null);
  const [notification, setNotification] = useState(null);

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
        addNodesToVisualization(result.nodes, result.edges || []);
        showNotification('success', `Added ${result.nodes.length} related nodes`);
      } else {
        showNotification('info', 'No related nodes found');
      }
    } catch (error) {
      console.error('Error expanding node:', error);
      showNotification('error', 'Failed to expand node');
    }
  }, [addNodesToVisualization, showNotification]);

  // Callback: Edit node
  const handleEdit = useCallback((nodeId, nodeData) => {
    setEditingNode({ id: nodeId, data: nodeData });
  }, []);

  // Callback: Delete node
  const handleDelete = useCallback(async (nodeId) => {
    if (!window.confirm('Are you sure you want to delete this node?')) {
      return;
    }
    try {
      await api.deleteNodes([nodeId], true);
      const newNodes = nodes.filter(n => n.id !== nodeId);
      const newEdges = edges.filter(e => e.source !== nodeId && e.target !== nodeId);
      updateVisualization(newNodes, newEdges);
      showNotification('success', 'Node deleted');
    } catch (error) {
      console.error('Error deleting node:', error);
      showNotification('error', 'Failed to delete node');
    }
  }, [nodes, edges, updateVisualization, showNotification]);

  // Callback: Create group
  const handleCreateGroup = useCallback((position, groupNode) => {
    showNotification('success', 'Group created');
  }, [showNotification]);

  // Callback: Save view
  const handleSaveView = useCallback(async (viewData) => {
    const name = window.prompt('Enter view name:');
    if (!name) return;

    try {
      // Create a SavedView node with metadata
      const viewNode = {
        name,
        type: 'SavedView',
        description: `Saved view: ${name}`,
        summary: `Contains ${viewData.nodes.length} nodes`,
        metadata: {
          node_ids: viewData.nodes.map(n => n.id),
          positions: Object.fromEntries(viewData.nodes.map(n => [n.id, n.position])),
          groups: viewData.groups,
        },
        communities: [],
      };

      await api.addNodes([viewNode], []);
      showNotification('success', `View "${name}" saved`);
    } catch (error) {
      console.error('Error saving view:', error);
      showNotification('error', 'Failed to save view');
    }
  }, [showNotification]);

  // Handle node update from edit dialog
  const handleNodeUpdate = useCallback(async (nodeId, updates) => {
    try {
      await api.updateNode(nodeId, updates);
      const newNodes = nodes.map(n =>
        n.id === nodeId ? { ...n, ...updates } : n
      );
      updateVisualization(newNodes, edges);
      setEditingNode(null);
      showNotification('success', 'Node updated');
    } catch (error) {
      console.error('Error updating node:', error);
      showNotification('error', 'Failed to update node');
    }
  }, [nodes, edges, updateVisualization, showNotification]);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Community Knowledge Graph</h1>
        <div className="header-actions">
          <StatsPanel stats={stats} />
          <button
            className={`chat-toggle-button ${isChatOpen ? 'active' : ''}`}
            onClick={() => setChatOpen(!isChatOpen)}
            title={isChatOpen ? 'Close chat' : 'Open chat assistant'}
          >
            <span className="chat-icon">💬</span>
            <span className="chat-label">{isChatOpen ? 'Close Chat' : 'Chat'}</span>
          </button>
        </div>
      </header>

      <div className="app-content">
        <aside className="app-sidebar">
          <SearchPanel />
        </aside>

        <main className="app-main">
          <GraphCanvas
            nodes={nodes}
            edges={edges}
            highlightedNodeIds={highlightedNodeIds}
            hiddenNodeIds={hiddenNodeIds}
            onExpand={handleExpand}
            onEdit={handleEdit}
            onDelete={handleDelete}
            onCreateGroup={handleCreateGroup}
            onSaveView={handleSaveView}
          />
        </main>

        {isChatOpen && (
          <ChatPanel onClose={() => setChatOpen(false)} />
        )}
      </div>

      {editingNode && (
        <EditNodeDialog
          node={editingNode}
          onClose={() => setEditingNode(null)}
          onSave={(updates) => handleNodeUpdate(editingNode.id, updates)}
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
