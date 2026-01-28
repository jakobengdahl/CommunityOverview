import { useState, useCallback, useEffect } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import useGraphStore from './store/graphStore';
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
    editingNode,
    setEditingNode,
    closeEditingNode,
    removeNode,
  } = useGraphStore();

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
        // Filter out Community nodes
        const filteredNodes = result.nodes.filter(n =>
          n.type !== 'Community' && n.data?.type !== 'Community'
        );
        addNodesToVisualization(filteredNodes, result.edges || []);
        showNotification('success', `Lade till ${filteredNodes.length} relaterade noder`);
      } else {
        showNotification('info', 'Inga relaterade noder hittades');
      }
    } catch (error) {
      console.error('Error expanding node:', error);
      showNotification('error', 'Kunde inte expandera nod');
    }
  }, [addNodesToVisualization, showNotification]);

  // Callback: Edit node
  const handleEdit = useCallback((nodeId, nodeData) => {
    setEditingNode({ id: nodeId, data: nodeData });
  }, [setEditingNode]);

  // Callback: Delete node
  const handleDelete = useCallback(async (nodeId) => {
    if (!window.confirm('Är du säker på att du vill ta bort denna nod?')) {
      return;
    }
    try {
      await api.deleteNodes([nodeId], true);
      removeNode(nodeId);
      showNotification('success', 'Nod borttagen');
    } catch (error) {
      console.error('Error deleting node:', error);
      showNotification('error', 'Kunde inte ta bort nod');
    }
  }, [removeNode, showNotification]);

  // Callback: Create group
  const handleCreateGroup = useCallback((position, groupNode) => {
    showNotification('success', 'Grupp skapad');
  }, [showNotification]);

  // Callback: Save view
  const handleSaveView = useCallback(async (viewData) => {
    const name = window.prompt('Ange vynamn:');
    if (!name) return;

    try {
      const viewNode = {
        name,
        type: 'SavedView',
        description: `Sparad vy: ${name}`,
        summary: `Innehåller ${viewData.nodes.length} noder`,
        metadata: {
          node_ids: viewData.nodes.map(n => n.id),
          positions: Object.fromEntries(viewData.nodes.map(n => [n.id, n.position])),
          groups: viewData.groups,
        },
        communities: [],
      };

      await api.addNodes([viewNode], []);
      showNotification('success', `Vy "${name}" sparad`);
    } catch (error) {
      console.error('Error saving view:', error);
      showNotification('error', 'Kunde inte spara vy');
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
      closeEditingNode();
      showNotification('success', 'Nod uppdaterad');
    } catch (error) {
      console.error('Error updating node:', error);
      showNotification('error', 'Kunde inte uppdatera nod');
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
            onExpand={handleExpand}
            onEdit={handleEdit}
            onDelete={handleDelete}
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
