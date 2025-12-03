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

  // Update graph visualization
  updateVisualization: (nodes, edges, highlightNodeIds = []) => {
    set({
      nodes,
      edges,
      highlightedNodeIds: highlightNodeIds
    });
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
  clearError: () => set({ error: null })
}));

export default useGraphStore;
