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

  // Update graph visualization
  updateVisualization: (nodes, edges, highlightNodeIds = []) => {
    set({
      nodes,
      edges,
      highlightedNodeIds: highlightNodeIds
    });
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

  // Add nodes to existing graph
  addNodesToVisualization: (newNodes, newEdges) => {
    const currentNodes = get().nodes;
    const currentEdges = get().edges;

    // Filter out duplicates based on ID
    const existingNodeIds = new Set(currentNodes.map(n => n.id));
    const existingEdgeIds = new Set(currentEdges.map(e => e.id));

    const uniqueNewNodes = newNodes.filter(n => !existingNodeIds.has(n.id));
    const uniqueNewEdges = newEdges.filter(e => !existingEdgeIds.has(e.id));

    set({
      nodes: [...currentNodes, ...uniqueNewNodes],
      edges: [...currentEdges, ...uniqueNewEdges]
    });
  },

  // Highlight specific nodes
  highlightNodes: (nodeIds) => set({ highlightedNodeIds: nodeIds }),

  // Clear graph
  clearVisualization: () => set({ nodes: [], edges: [], highlightedNodeIds: [] }),

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
  apiKey: null,
  setApiKey: (key) => set({ apiKey: key })
}));

export default useGraphStore;
