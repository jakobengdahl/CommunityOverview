import { useState, useEffect } from 'react'
import './App.css'
import Header from './components/Header'
import ChatPanel from './components/ChatPanel'
import VisualizationPanel from './components/VisualizationPanel'
import useGraphStore from './store/graphStore'
import { loadVisualizationView, executeTool } from './services/api'
// import { loadDemoData } from './services/demoData' // REMOVED

function App() {
  const { selectedCommunities, updateVisualization, loadVisualizationView: loadViewToStore, addChatMessage } = useGraphStore();
  const [viewLoadError, setViewLoadError] = useState(null);

  // Load communities and view from URL query on initial load
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const communitiesParam = params.getAll('community');
    const viewParam = params.get('view');

    if (communitiesParam.length > 0) {
      useGraphStore.getState().setSelectedCommunities(communitiesParam);
    }

    // Load visualization view if specified
    if (viewParam) {
      loadView(viewParam);
    }
  }, []);

  // Function to load a saved view
  const loadView = async (viewName) => {
    try {
      setViewLoadError(null);

      // Add loading message to chat
      addChatMessage({
        role: 'assistant',
        content: `Loading view "${viewName}"...`,
        timestamp: new Date()
      });

      const viewData = await loadVisualizationView(viewName);

      // Extract node IDs from the view metadata
      const metadata = viewData.metadata || {};
      const nodeIds = metadata.node_ids || [];

      if (nodeIds.length === 0) {
        throw new Error('View contains no nodes');
      }

      // Fetch the actual nodes using get_related_nodes or similar
      // For now, we'll use a simple approach: fetch each node
      const nodes = [];
      const edges = [];

      for (const nodeId of nodeIds) {
        try {
          const nodeResult = await executeTool('get_node_details', { node_id: nodeId });
          if (nodeResult.success && nodeResult.node) {
            nodes.push(nodeResult.node);
          }

          // Also get related nodes to build edges
          const relatedResult = await executeTool('get_related_nodes', {
            node_id: nodeId,
            depth: 1
          });

          if (relatedResult.nodes) {
            relatedResult.nodes.forEach(n => {
              if (!nodes.find(existing => existing.id === n.id)) {
                nodes.push(n);
              }
            });
          }

          if (relatedResult.edges) {
            edges.push(...relatedResult.edges);
          }
        } catch (err) {
          console.warn(`Failed to load node ${nodeId}:`, err);
        }
      }

      // Update visualization with loaded nodes
      updateVisualization(nodes, edges);

      // Apply view settings (hidden nodes, positions)
      loadViewToStore(viewData);

      addChatMessage({
        role: 'assistant',
        content: `✅ Loaded view "${viewName}" with ${nodes.length} nodes.`,
        timestamp: new Date()
      });

    } catch (error) {
      console.error('Error loading view:', error);
      setViewLoadError(error.message);

      addChatMessage({
        role: 'assistant',
        content: `❌ Failed to load view "${viewName}": ${error.message}`,
        timestamp: new Date()
      });
    }
  };

  // Load data when communities are selected
  useEffect(() => {
    if (selectedCommunities.length > 0) {
      // TODO: Fetch initial graph data from backend
      // fetchGraphData(selectedCommunities).then(data => updateVisualization(data.nodes, data.edges));
    }
  }, [selectedCommunities, updateVisualization]);

  return (
    <div className="app">
      <Header />

      {selectedCommunities.length === 0 ? (
        <div className="no-community-selected">
          <h2>Select at least one community to get started</h2>
          <p>Use the dropdown menu above to select which communities you belong to.</p>
        </div>
      ) : (
        <div className="main-content">
          <ChatPanel />
          <VisualizationPanel />
        </div>
      )}
    </div>
  )
}

export default App
