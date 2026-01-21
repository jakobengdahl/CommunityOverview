import { create } from 'zustand';

const WELCOME_MESSAGE = {
  role: 'assistant',
  content: `Welcome to Community Knowledge Graph!

You can ask questions like:
• "Which initiatives relate to NIS2?"
• "Show all actors in the eSam community"
• "Are there any AI strategy projects?"

**Note:** Do not handle personal data in this service.`,
  timestamp: new Date()
};

/**
 * Zustand store for graph visualization and chat state
 */
const useGraphStore = create((set, get) => ({
  // Communities
  selectedCommunities: [],
  setSelectedCommunities: (communities) => set({ selectedCommunities: communities }),

  // Graph data
  nodes: [],
  edges: [],
  highlightedNodeIds: [],
  hiddenNodeIds: [], // Set of IDs for hidden nodes
  clearGroupsFlag: false, // Signal to clear groups in visualization
  groupsToRestore: [], // Groups to restore when loading visualization
  reactFlowNodes: [], // Current React Flow nodes (including groups) for saving

  // Update graph visualization
  updateVisualization: (nodes, edges, highlightNodeIds = []) => {
    console.log('[GraphStore] updateVisualization called with:');
    console.log('[GraphStore]   - Nodes:', nodes.length, 'nodes');
    console.log('[GraphStore]   - Edges:', edges.length, 'edges');
    console.log('[GraphStore]   - Highlighted:', highlightNodeIds.length, 'nodes');
    console.log('[GraphStore]   - First node sample:', nodes[0]);
    console.log('[GraphStore]   - First edge sample:', edges[0]);

    set({
      nodes,
      edges,
      highlightedNodeIds: highlightNodeIds,
      clearGroupsFlag: true // Signal to clear groups when loading new visualization
    });

    console.log('[GraphStore] State updated successfully');

    // Reset flag after a short delay
    setTimeout(() => {
      set({ clearGroupsFlag: false });
    }, 100);
  },

  // Update node positions (from React Flow)
  updateNodePositions: (positionUpdates) => {
    const nodes = get().nodes.map(node => {
        const update = positionUpdates.find(u => u.id === node.id);
        if (update && update.position) {
            return { ...node, position: update.position };
        }
        return node;
    });
    set({ nodes });
  },

  setHiddenNodeIds: (hiddenIds) => set({ hiddenNodeIds: hiddenIds }),
  toggleNodeVisibility: (nodeId) => {
    const hidden = get().hiddenNodeIds;
    if (hidden.includes(nodeId)) {
      set({ hiddenNodeIds: hidden.filter(id => id !== nodeId) });
    } else {
      set({ hiddenNodeIds: [...hidden, nodeId] });
    }
  },

  // Set groups to restore when loading visualization
  setGroupsToRestore: (groups) => set({ groupsToRestore: groups }),

  // Update React Flow nodes (including groups) for saving
  setReactFlowNodes: (reactFlowNodes) => set({ reactFlowNodes }),

  // Add nodes to existing graph (merges with existing nodes)
  addNodesToVisualization: (newNodes, newEdges = []) => {
    const currentState = get();
    const existingNodes = currentState.nodes;
    const existingEdges = currentState.edges;

    // Get IDs of existing and new nodes
    const existingNodeIds = new Set(existingNodes.map(n => n.id));
    const newNodeIds = new Set(newNodes.map(n => n.id));

    // Merge nodes (avoid duplicates)
    const mergedNodes = [...existingNodes];
    for (const newNode of newNodes) {
      if (!existingNodeIds.has(newNode.id)) {
        mergedNodes.push(newNode);
      }
    }

    // Merge edges (avoid duplicates)
    const existingEdgeIds = new Set(existingEdges.map(e => e.id));
    const mergedEdges = [...existingEdges];
    for (const newEdge of newEdges) {
      if (!existingEdgeIds.has(newEdge.id)) {
        mergedEdges.push(newEdge);
      }
    }

    // Update state with merged data
    set({
      nodes: mergedNodes,
      edges: mergedEdges,
      highlightedNodeIds: Array.from(newNodeIds), // Highlight only the newly added nodes
      clearGroupsFlag: true, // Signal to clear groups
    });

    // Reset flag after a short delay
    setTimeout(() => {
      set({ clearGroupsFlag: false });
    }, 100);
  },

  // Highlight specific nodes
  highlightNodes: (nodeIds) => set({ highlightedNodeIds: nodeIds }),

  // Clear graph
  clearVisualization: () => {
    set({
      nodes: [],
      edges: [],
      highlightedNodeIds: [],
      clearGroupsFlag: true // Signal to clear groups
    });

    // Reset flag after a short delay
    setTimeout(() => {
      set({ clearGroupsFlag: false });
    }, 100);
  },

  // Load visualization view
  loadVisualizationView: (viewData) => {
    // viewData comes from the backend (VisualizationView node's metadata)
    // It should contain: node_ids, positions, hidden_node_ids
    const metadata = viewData.metadata || {};
    const nodeIds = metadata.node_ids || [];
    const positions = metadata.positions || {};
    const hiddenNodeIds = metadata.hidden_node_ids || [];

    // Note: We need to fetch the actual nodes based on node_ids
    // For now, we'll just set the hidden nodes and positions
    // The actual node loading should happen via a search/query
    set({
      hiddenNodeIds: hiddenNodeIds,
    });

    // Update positions if nodes already exist in store
    if (Object.keys(positions).length > 0) {
      const nodes = get().nodes.map(node => {
        if (positions[node.id]) {
          return { ...node, position: positions[node.id] };
        }
        return node;
      });
      set({ nodes });
    }
  },

  // Chat messages - initialize with welcome message
  chatMessages: [WELCOME_MESSAGE],
  addChatMessage: (message) => {
    const messages = get().chatMessages;
    set({ chatMessages: [...messages, message] });
  },
  clearChatMessages: () => set({ chatMessages: [WELCOME_MESSAGE] }),

  // Loading state
  isLoading: false,
  setLoading: (loading) => set({ isLoading: loading }),

  // Error state
  error: null,
  setError: (error) => set({ error }),
  clearError: () => set({ error: null }),

  // API Key (temporary, session-only storage)
  // Support both Claude and OpenAI API keys
  apiKey: null,
  setApiKey: (key) => set({ apiKey: key }),

  // LLM Provider type (defaults to what backend is configured to use)
  llmProvider: null, // 'claude' or 'openai' - null means use backend default
  setLlmProvider: (provider) => set({ llmProvider: provider }),
}));

export default useGraphStore;
