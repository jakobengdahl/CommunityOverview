import { useState, useEffect, useRef } from 'react'
import './App.css'
import Header from './components/Header'
import ChatPanel from './components/ChatPanel'
import VisualizationPanel from './components/VisualizationPanel'
import useGraphStore from './store/graphStore'
import { loadVisualizationView, executeTool } from './services/api'
// import { loadDemoData } from './services/demoData' // REMOVED

function App() {
  const { selectedCommunities, updateVisualization, loadVisualizationView: loadViewToStore, addChatMessage, setApiKey } = useGraphStore();
  const [viewLoadError, setViewLoadError] = useState(null);
  const hasLoadedFromUrl = useRef(false);

  // Load communities and view from URL query on initial load
  useEffect(() => {
    // Prevent duplicate execution in React Strict Mode
    if (hasLoadedFromUrl.current) return;
    hasLoadedFromUrl.current = true;

    const params = new URLSearchParams(window.location.search);
    const communitiesParam = params.getAll('community');
    const viewParam = params.get('view');
    const loadDataParam = params.get('loaddata');
    const apiKeyParam = params.get('apikey');

    // Set API key if provided
    if (apiKeyParam) {
      setApiKey(decodeURIComponent(apiKeyParam));
      addChatMessage({
        role: 'assistant',
        content: '🔑 Custom API key loaded from URL',
        timestamp: new Date()
      });
    }

    if (communitiesParam.length > 0) {
      useGraphStore.getState().setSelectedCommunities(communitiesParam);
    }

    // Load external data if specified
    if (loadDataParam) {
      loadExternalData(decodeURIComponent(loadDataParam));
    }

    // Load visualization view if specified
    if (viewParam) {
      loadView(viewParam);
    }
  }, []);

  // Function to load external graph data from URL
  const loadExternalData = async (dataUrl) => {
    try {
      addChatMessage({
        role: 'assistant',
        content: `📥 Loading external graph data from: ${dataUrl}`,
        timestamp: new Date()
      });

      const response = await fetch(dataUrl);

      if (!response.ok) {
        throw new Error(`Failed to fetch data: ${response.status} ${response.statusText}`);
      }

      const data = await response.json();

      // Validate data format
      if (!data.nodes || !Array.isArray(data.nodes)) {
        throw new Error('Invalid data format: missing or invalid "nodes" array');
      }

      if (!data.edges || !Array.isArray(data.edges)) {
        throw new Error('Invalid data format: missing or invalid "edges" array');
      }

      // Transform nodes to ensure they have the required format
      const nodes = data.nodes.map(node => ({
        id: node.id,
        data: {
          label: node.label || node.data?.label || node.id,
          ...node.data
        },
        position: node.position || { x: 0, y: 0 },
        type: node.type || 'custom'
      }));

      // Transform edges to ensure they have the required format
      const edges = data.edges.map(edge => ({
        id: edge.id || `${edge.source}-${edge.target}`,
        source: edge.source,
        target: edge.target,
        data: edge.data || {},
        ...edge
      }));

      // Update visualization with loaded data
      updateVisualization(nodes, edges);

      addChatMessage({
        role: 'assistant',
        content: `✅ Successfully loaded ${nodes.length} nodes and ${edges.length} edges from external source`,
        timestamp: new Date()
      });

    } catch (error) {
      console.error('Error loading external data:', error);
      addChatMessage({
        role: 'assistant',
        content: `❌ Failed to load external data: ${error.message}`,
        timestamp: new Date()
      });
    }
  };

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
