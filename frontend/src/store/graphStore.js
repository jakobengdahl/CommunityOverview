import { create } from 'zustand';

/**
 * Zustand store för graf-visualisering och chat-state
 */
const useGraphStore = create((set, get) => ({
  // Communities
  selectedCommunities: [],
  setSelectedCommunities: (communities) => set({ selectedCommunities: communities }),

  // Graf-data
  nodes: [],
  edges: [],
  highlightedNodeIds: [],

  // Uppdatera graf-visualisering
  updateVisualization: (nodes, edges, highlightNodeIds = []) => {
    set({
      nodes,
      edges,
      highlightedNodeIds: highlightNodeIds
    });
  },

  // Lägg till noder till befintlig graf
  addNodesToVisualization: (newNodes, newEdges) => {
    const currentNodes = get().nodes;
    const currentEdges = get().edges;

    // Filtrera bort dubletter baserat på ID
    const existingNodeIds = new Set(currentNodes.map(n => n.id));
    const existingEdgeIds = new Set(currentEdges.map(e => e.id));

    const uniqueNewNodes = newNodes.filter(n => !existingNodeIds.has(n.id));
    const uniqueNewEdges = newEdges.filter(e => !existingEdgeIds.has(e.id));

    set({
      nodes: [...currentNodes, ...uniqueNewNodes],
      edges: [...currentEdges, ...uniqueNewEdges]
    });
  },

  // Highlight specifika noder
  highlightNodes: (nodeIds) => set({ highlightedNodeIds: nodeIds }),

  // Rensa graf
  clearVisualization: () => set({ nodes: [], edges: [], highlightedNodeIds: [] }),

  // Chat-messages
  chatMessages: [],
  addChatMessage: (message) => {
    const messages = get().chatMessages;
    set({ chatMessages: [...messages, message] });
  },
  clearChatMessages: () => set({ chatMessages: [] }),

  // Loading state
  isLoading: false,
  setLoading: (loading) => set({ isLoading: loading }),

  // Error state
  error: null,
  setError: (error) => set({ error }),
  clearError: () => set({ error: null })
}));

export default useGraphStore;
