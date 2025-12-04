/**
 * Demo data for testing the graph visualization
 * This data is a subset of the actual graph.json from the MCP server
 * In production, this would be fetched from the MCP server API
 */

export const DEMO_GRAPH_DATA = {
  nodes: [
    {
      id: "comm-001",
      type: "Community",
      name: "eSam",
      description: "En svensk samverkansorganisation för offentlig sektor",
      summary: "Samverkan för digital förvaltning",
      communities: ["eSam"]
    },
    {
      id: "comm-002",
      type: "Community",
      name: "Myndigheter",
      description: "Svenska myndigheter",
      summary: "Myndighetsgemenskapen",
      communities: ["Myndigheter"]
    },
    {
      id: "actor-001",
      type: "Actor",
      name: "DIGG",
      description: "Myndigheten för digital förvaltning",
      summary: "Digital förvaltning",
      communities: ["eSam", "Myndigheter"]
    },
    {
      id: "actor-002",
      type: "Actor",
      name: "MSB",
      description: "Myndigheten för samhällsskydd och beredskap",
      summary: "Samhällsskydd",
      communities: ["eSam", "Myndigheter"]
    },
    {
      id: "actor-003",
      type: "Actor",
      name: "SCB",
      description: "Statistiska centralbyrån",
      summary: "Officiell statistik",
      communities: ["Myndigheter"]
    },
    {
      id: "leg-001",
      type: "Legislation",
      name: "NIS2-direktivet",
      description: "EU-direktiv om åtgärder för en hög gemensam nivå av cybersäkerhet",
      summary: "Cybersäkerhetsdirektiv",
      communities: ["eSam", "Myndigheter"]
    },
    {
      id: "init-001",
      type: "Initiative",
      name: "NIS2-implementering DIGG",
      description: "Projekt för att implementera NIS2-direktivet inom DIGG",
      summary: "NIS2-projekt",
      communities: ["eSam", "Myndigheter"]
    },
    {
      id: "init-002",
      type: "Initiative",
      name: "Cybersäkerhetsstrategi MSB",
      description: "MSBs arbete med nationell cybersäkerhetsstrategi",
      summary: "Cybersäkerhetsstrategi",
      communities: ["eSam", "Myndigheter"]
    },
    {
      id: "theme-001",
      type: "Theme",
      name: "Cybersäkerhet",
      description: "Tema för cybersäkerhet och informationssäkerhet",
      summary: "Cybersäkerhet",
      communities: ["eSam", "Myndigheter"]
    }
  ],
  edges: [
    {
      id: "edge-001",
      source: "actor-001",
      target: "comm-001",
      type: "BELONGS_TO"
    },
    {
      id: "edge-002",
      source: "actor-001",
      target: "comm-002",
      type: "BELONGS_TO"
    },
    {
      id: "edge-003",
      source: "actor-002",
      target: "comm-001",
      type: "BELONGS_TO"
    },
    {
      id: "edge-004",
      source: "actor-002",
      target: "comm-002",
      type: "BELONGS_TO"
    },
    {
      id: "edge-005",
      source: "actor-003",
      target: "comm-002",
      type: "BELONGS_TO"
    },
    {
      id: "edge-006",
      source: "init-001",
      target: "actor-001",
      type: "BELONGS_TO"
    },
    {
      id: "edge-007",
      source: "init-001",
      target: "leg-001",
      type: "IMPLEMENTS"
    },
    {
      id: "edge-008",
      source: "init-002",
      target: "actor-002",
      type: "BELONGS_TO"
    },
    {
      id: "edge-009",
      source: "init-001",
      target: "theme-001",
      type: "RELATES_TO"
    },
    {
      id: "edge-010",
      source: "init-002",
      target: "theme-001",
      type: "RELATES_TO"
    },
    {
      id: "edge-011",
      source: "leg-001",
      target: "theme-001",
      type: "RELATES_TO"
    }
  ]
};

/**
 * Load demo data into the graph store
 * @param {Function} updateVisualization - Store function to update visualization
 * @param {Array} selectedCommunities - Currently selected communities for filtering
 */
export function loadDemoData(updateVisualization, selectedCommunities = []) {
  const { nodes, edges } = DEMO_GRAPH_DATA;

  // Filter nodes by selected communities if any are selected
  let filteredNodes = nodes;
  if (selectedCommunities.length > 0) {
    filteredNodes = nodes.filter(node =>
      node.communities.some(comm => selectedCommunities.includes(comm))
    );
  }

  // Filter edges to only include those where both source and target are in filtered nodes
  const filteredNodeIds = new Set(filteredNodes.map(n => n.id));
  const filteredEdges = edges.filter(edge =>
    filteredNodeIds.has(edge.source) && filteredNodeIds.has(edge.target)
  );

  updateVisualization(filteredNodes, filteredEdges);
}

/**
 * Add a new node to the demo data
 * @param {Object} node - The node to add
 * @param {Array} edges - Optional edges to add
 * @returns {Object} Result with success status
 */
export function addNodeToDemoData(node, edges = []) {
  // Generate a proper ID if it's a temporary one
  if (node.id.startsWith('temp-')) {
    const typePrefix = node.type.toLowerCase().substring(0, 4);
    const count = DEMO_GRAPH_DATA.nodes.filter(n => n.type === node.type).length + 1;
    node.id = `${typePrefix}-${String(count).padStart(3, '0')}`;
  }

  // Add node to demo data
  DEMO_GRAPH_DATA.nodes.push(node);

  // Add edges if provided
  if (edges.length > 0) {
    edges.forEach((edge, index) => {
      const edgeId = `edge-${String(DEMO_GRAPH_DATA.edges.length + index + 1).padStart(3, '0')}`;
      DEMO_GRAPH_DATA.edges.push({
        ...edge,
        id: edgeId
      });
    });
  }

  return {
    success: true,
    node_id: node.id,
    message: `Added node ${node.name} with ID ${node.id}`
  };
}
