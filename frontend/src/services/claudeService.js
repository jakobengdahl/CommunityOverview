/**
 * Claude API service for chat interactions
 * Handles communication with Claude API and tool execution
 */

import Anthropic from '@anthropic-ai/sdk';
import { DEMO_GRAPH_DATA } from './demoData';

// Tool definitions that match the MCP server tools
const TOOLS = [
  {
    name: 'search_graph',
    description: 'Search for nodes in the graph based on text query. Matches against name, description, and summary.',
    input_schema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Search text to find matching nodes'
        },
        node_types: {
          type: 'array',
          items: { type: 'string' },
          description: 'Optional: Filter by node types (Actor, Initiative, Legislation, etc.)'
        },
        communities: {
          type: 'array',
          items: { type: 'string' },
          description: 'Optional: Filter by communities'
        }
      },
      required: ['query']
    }
  },
  {
    name: 'get_related_nodes',
    description: 'Get nodes connected to a given node. Returns both the nodes and the edges connecting them.',
    input_schema: {
      type: 'object',
      properties: {
        node_id: {
          type: 'string',
          description: 'ID of the starting node'
        },
        depth: {
          type: 'number',
          description: 'How many hops from the starting node (default 1)',
          default: 1
        }
      },
      required: ['node_id']
    }
  },
  {
    name: 'find_similar_nodes',
    description: 'Find similar nodes based on name for duplicate detection. Use this BEFORE proposing to add a new node.',
    input_schema: {
      type: 'object',
      properties: {
        name: {
          type: 'string',
          description: 'The name to search for similar nodes'
        },
        node_type: {
          type: 'string',
          description: 'Optional: Node type to filter on (Actor, Initiative, etc.)'
        },
        threshold: {
          type: 'number',
          description: 'Similarity threshold 0.0-1.0 (default 0.7)',
          default: 0.7
        }
      },
      required: ['name']
    }
  },
  {
    name: 'propose_new_node',
    description: 'Propose a new node to be added. Use find_similar_nodes FIRST to check for duplicates. Returns a proposal that requires user approval.',
    input_schema: {
      type: 'object',
      properties: {
        node: {
          type: 'object',
          description: 'The node to propose',
          properties: {
            type: { type: 'string', description: 'Node type (Actor, Initiative, etc.)' },
            name: { type: 'string', description: 'Node name' },
            description: { type: 'string', description: 'Detailed description' },
            summary: { type: 'string', description: 'Short summary for visualization' },
            communities: { type: 'array', items: { type: 'string' }, description: 'Communities this node belongs to' }
          },
          required: ['type', 'name', 'description', 'communities']
        },
        similar_nodes: {
          type: 'array',
          description: 'List of similar nodes found (from find_similar_nodes)',
          items: { type: 'object' }
        }
      },
      required: ['node', 'similar_nodes']
    }
  },
  {
    name: 'list_node_types',
    description: 'List all allowed node types in the metamodel with their colors.',
    input_schema: {
      type: 'object',
      properties: {}
    }
  },
  {
    name: 'update_node',
    description: 'Update an existing node. Can update name, description, summary, communities, or metadata fields.',
    input_schema: {
      type: 'object',
      properties: {
        node_id: {
          type: 'string',
          description: 'ID of the node to update'
        },
        updates: {
          type: 'object',
          description: 'Fields to update (name, description, summary, communities, metadata)',
          properties: {
            name: { type: 'string', description: 'New name for the node' },
            description: { type: 'string', description: 'New description' },
            summary: { type: 'string', description: 'New summary for visualization' },
            communities: {
              type: 'array',
              items: { type: 'string' },
              description: 'New list of communities'
            }
          }
        }
      },
      required: ['node_id', 'updates']
    }
  },
  {
    name: 'delete_nodes',
    description: 'Delete nodes from the graph. IMPORTANT: Always call with confirmed=false first to show user what will be deleted. Max 10 nodes at a time.',
    input_schema: {
      type: 'object',
      properties: {
        node_ids: {
          type: 'array',
          items: { type: 'string' },
          description: 'List of node IDs to delete (max 10)'
        },
        confirmed: {
          type: 'boolean',
          description: 'Must be false for first call (to get confirmation), true to actually delete',
          default: false
        }
      },
      required: ['node_ids']
    }
  }
];

// System prompt for Claude
const SYSTEM_PROMPT = `You are a helpful assistant for the Community Knowledge Graph system.

METAMODEL:
- Actor (blue): Government agencies, organizations
- Community (purple): eSam, Myndigheter, Officiell Statistik
- Initiative (green): Projects, collaborative activities
- Capability (orange): Capabilities
- Resource (yellow): Reports, software
- Legislation (red): NIS2, GDPR
- Theme (teal): AI, data strategies
- VisualizationView (gray): Predefined views

WORKFLOWS:

1. SEARCH WORKFLOW:
   - When user asks about initiatives, actors, or legislation, use search_graph tool
   - Display results and visualize them in the graph
   - Suggest related nodes user might want to explore

2. ADD NODE WORKFLOW (TWO-STEP):
   - When user wants to add a new node (e.g., "Vi har ett projekt om AI-säkerhet"):
     a) First, use find_similar_nodes to check for duplicates
     b) Then, use propose_new_node with the proposal and similar nodes found
     c) Present the proposal clearly in Swedish:
        - Show the proposed node details
        - Show any similar existing nodes that might be duplicates
        - Ask user: "Vill du lägga till denna nod?" with options [Ja] or [Nej]
     d) Wait for user confirmation before actually adding

3. EXPLORATION WORKFLOW:
   - When user clicks on nodes, use get_related_nodes to show connections
   - Help users discover hidden relationships

4. UPDATE NODE WORKFLOW:
   - When user wants to update/edit/modify a node (e.g., "Uppdatera beskrivningen av X", "Ändra namnet på noden Y")
   - Use search_graph to find the node if you only have a name
   - Use update_node with the node_id and fields to update
   - Confirm the update with the user
   - The graph will automatically refresh to show the changes

5. DELETE NODE WORKFLOW (TWO-STEP):
   - When user wants to delete/remove nodes (e.g., "Ta bort noden X", "Radera denna initiative")
   - STEP 1: Call delete_nodes with confirmed=false to preview deletion
   - STEP 2: Show user what will be deleted (nodes + affected connections)
   - STEP 3: Wait for user confirmation
   - STEP 4: After approval, actually delete with confirmed=true
   - Max 10 nodes per deletion for safety
   - Show clear warning about irreversible action

IMPORTANT:
- Always respond in Swedish since the user data is in Swedish
- ALWAYS check for duplicates with find_similar_nodes before proposing new nodes
- NEVER add nodes without explicit user approval
- Warn if user tries to store personal data
- Keep responses concise and focused on the graph
- Format proposals clearly with bullet points
- When updating nodes, be specific about what changed`;

/**
 * Execute a tool call with demo data
 */
function executeTool(toolName, toolInput) {
  switch (toolName) {
    case 'search_graph': {
      const { query, node_types, communities } = toolInput;
      const queryLower = query.toLowerCase();

      let results = DEMO_GRAPH_DATA.nodes.filter(node => {
        // Filter by node type
        if (node_types && node_types.length > 0 && !node_types.includes(node.type)) {
          return false;
        }

        // Filter by communities
        if (communities && communities.length > 0) {
          if (!node.communities.some(c => communities.includes(c))) {
            return false;
          }
        }

        // Text search
        const searchText = `${node.name} ${node.description} ${node.summary}`.toLowerCase();
        return searchText.includes(queryLower);
      });

      return {
        nodes: results,
        total: results.length,
        query: query
      };
    }

    case 'get_related_nodes': {
      const { node_id, depth = 1 } = toolInput;

      // Find all edges connected to this node
      const relatedEdges = DEMO_GRAPH_DATA.edges.filter(
        edge => edge.source === node_id || edge.target === node_id
      );

      // Find related node IDs
      const relatedNodeIds = new Set([node_id]);
      relatedEdges.forEach(edge => {
        if (edge.source === node_id) relatedNodeIds.add(edge.target);
        if (edge.target === node_id) relatedNodeIds.add(edge.source);
      });

      // Get the nodes
      const nodes = DEMO_GRAPH_DATA.nodes.filter(n => relatedNodeIds.has(n.id));

      return {
        nodes: nodes,
        edges: relatedEdges,
        total_nodes: nodes.length,
        total_edges: relatedEdges.length
      };
    }

    case 'find_similar_nodes': {
      const { name, node_type, threshold = 0.7 } = toolInput;
      const nameLower = name.toLowerCase();

      // Simple Levenshtein distance implementation
      const levenshtein = (a, b) => {
        const matrix = [];
        for (let i = 0; i <= b.length; i++) {
          matrix[i] = [i];
        }
        for (let j = 0; j <= a.length; j++) {
          matrix[0][j] = j;
        }
        for (let i = 1; i <= b.length; i++) {
          for (let j = 1; j <= a.length; j++) {
            if (b.charAt(i - 1) === a.charAt(j - 1)) {
              matrix[i][j] = matrix[i - 1][j - 1];
            } else {
              matrix[i][j] = Math.min(
                matrix[i - 1][j - 1] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j] + 1
              );
            }
          }
        }
        return matrix[b.length][a.length];
      };

      const results = [];
      DEMO_GRAPH_DATA.nodes.forEach(node => {
        // Filter by type if specified
        if (node_type && node.type !== node_type) {
          return;
        }

        // Calculate similarity
        const nodeNameLower = node.name.toLowerCase();
        const distance = levenshtein(nameLower, nodeNameLower);
        const maxLen = Math.max(nameLower.length, nodeNameLower.length);
        const similarity = maxLen === 0 ? 1.0 : 1.0 - (distance / maxLen);

        if (similarity >= threshold) {
          results.push({
            node: node,
            similarity_score: Math.round(similarity * 100) / 100,
            match_reason: `Name similarity: ${Math.round(similarity * 100)}%`
          });
        }
      });

      // Sort by similarity score
      results.sort((a, b) => b.similarity_score - a.similarity_score);

      return {
        similar_nodes: results.slice(0, 5),
        total: results.length,
        search_name: name
      };
    }

    case 'propose_new_node': {
      const { node, similar_nodes } = toolInput;

      // Generate ID for the proposed node
      const proposedNode = {
        ...node,
        id: `temp-${Date.now()}`,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      };

      return {
        success: true,
        proposed_node: proposedNode,
        similar_nodes: similar_nodes,
        message: 'Node proposal created. Waiting for user approval.',
        requires_approval: true
      };
    }

    case 'list_node_types': {
      return {
        node_types: [
          { type: 'Actor', color: '#3B82F6', description: 'Government agencies, organizations' },
          { type: 'Community', color: '#A855F7', description: 'Communities' },
          { type: 'Initiative', color: '#10B981', description: 'Projects, activities' },
          { type: 'Capability', color: '#F97316', description: 'Capabilities' },
          { type: 'Resource', color: '#FBBF24', description: 'Reports, software' },
          { type: 'Legislation', color: '#EF4444', description: 'Laws, directives' },
          { type: 'Theme', color: '#14B8A6', description: 'Themes' },
          { type: 'VisualizationView', color: '#6B7280', description: 'Predefined views' }
        ]
      };
    }

    case 'update_node': {
      const { node_id, updates } = toolInput;

      // Find the node in demo data
      const nodeIndex = DEMO_GRAPH_DATA.nodes.findIndex(n => n.id === node_id);

      if (nodeIndex === -1) {
        return {
          success: false,
          error: `Node with ID ${node_id} not found`
        };
      }

      // Update the node
      const existingNode = DEMO_GRAPH_DATA.nodes[nodeIndex];
      const updatedNode = {
        ...existingNode,
        ...updates,
        updated_at: new Date().toISOString()
      };

      // Replace the node in the array
      DEMO_GRAPH_DATA.nodes[nodeIndex] = updatedNode;

      return {
        success: true,
        node: updatedNode,
        nodes: [updatedNode], // For visualization update
        message: `Updated node ${updatedNode.name}`,
        tool_type: 'update' // Signal that this is an update operation
      };
    }

    case 'delete_nodes': {
      const { node_ids, confirmed = false } = toolInput;

      // Security: Max 10 nodes
      if (node_ids.length > 10) {
        return {
          success: false,
          error: 'Maximum 10 nodes can be deleted at a time for security reasons'
        };
      }

      // Find nodes to delete
      const nodesToDelete = DEMO_GRAPH_DATA.nodes.filter(n => node_ids.includes(n.id));

      if (nodesToDelete.length === 0) {
        return {
          success: false,
          error: 'No nodes found with the provided IDs'
        };
      }

      // Find affected edges
      const affectedEdges = DEMO_GRAPH_DATA.edges.filter(
        edge => node_ids.includes(edge.source) || node_ids.includes(edge.target)
      );

      if (!confirmed) {
        // Preview mode: Return what will be deleted
        return {
          success: false,
          requires_confirmation: true,
          nodes_to_delete: nodesToDelete,
          affected_edges: affectedEdges,
          message: `This will delete ${nodesToDelete.length} node(s) and ${affectedEdges.length} connection(s). This action is irreversible.`
        };
      }

      // Confirmed deletion: Actually delete
      DEMO_GRAPH_DATA.nodes = DEMO_GRAPH_DATA.nodes.filter(n => !node_ids.includes(n.id));
      DEMO_GRAPH_DATA.edges = DEMO_GRAPH_DATA.edges.filter(
        edge => !node_ids.includes(edge.source) && !node_ids.includes(edge.target)
      );

      return {
        success: true,
        deleted_node_ids: node_ids,
        affected_edge_ids: affectedEdges.map(e => e.id),
        message: `Successfully deleted ${nodesToDelete.length} node(s) and ${affectedEdges.length} connection(s)`,
        tool_type: 'delete' // Signal that this is a delete operation
      };
    }

    default:
      return { error: `Unknown tool: ${toolName}` };
  }
}

/**
 * Send a message to Claude and handle the conversation
 * @param {Array} messages - Conversation history
 * @param {string} apiKey - Anthropic API key
 * @param {Function} onToolUse - Callback when tool results should update visualization
 * @returns {Promise<Object>} Claude's response
 */
export async function sendMessageToClaude(messages, apiKey, onToolUse = null) {
  if (!apiKey) {
    throw new Error('API key is required. Please set VITE_ANTHROPIC_API_KEY in your .env file.');
  }

  const anthropic = new Anthropic({
    apiKey: apiKey,
    dangerouslyAllowBrowser: true // Note: In production, use a backend proxy
  });

  // Convert messages to Anthropic format (filter out system messages)
  const anthropicMessages = messages
    .filter(m => m.role !== 'system')
    .map(m => ({
      role: m.role,
      content: m.content
    }));

  try {
    const response = await anthropic.messages.create({
      model: 'claude-3-5-sonnet-20241022',
      max_tokens: 4096,
      system: SYSTEM_PROMPT,
      tools: TOOLS,
      messages: anthropicMessages
    });

    // Handle tool use
    if (response.stop_reason === 'tool_use') {
      const toolUseBlock = response.content.find(block => block.type === 'tool_use');

      if (toolUseBlock) {
        // Execute the tool
        const toolResult = executeTool(toolUseBlock.name, toolUseBlock.input);

        // Notify callback about tool use (for visualization update)
        if (onToolUse && toolResult.nodes) {
          onToolUse(toolResult);
        }

        // Continue conversation with tool result
        const followUpMessages = [
          ...anthropicMessages,
          {
            role: 'assistant',
            content: response.content
          },
          {
            role: 'user',
            content: [
              {
                type: 'tool_result',
                tool_use_id: toolUseBlock.id,
                content: JSON.stringify(toolResult)
              }
            ]
          }
        ];

        // Get final response from Claude
        const finalResponse = await anthropic.messages.create({
          model: 'claude-3-5-sonnet-20241022',
          max_tokens: 4096,
          system: SYSTEM_PROMPT,
          tools: TOOLS,
          messages: followUpMessages
        });

        return {
          content: finalResponse.content.find(block => block.type === 'text')?.text || '',
          toolUsed: toolUseBlock.name,
          toolResult: toolResult
        };
      }
    }

    // Return text response
    const textBlock = response.content.find(block => block.type === 'text');
    return {
      content: textBlock?.text || 'No response',
      toolUsed: null,
      toolResult: null
    };

  } catch (error) {
    console.error('Error calling Claude API:', error);
    throw error;
  }
}
