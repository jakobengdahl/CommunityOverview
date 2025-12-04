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
    name: 'list_node_types',
    description: 'List all allowed node types in the metamodel with their colors.',
    input_schema: {
      type: 'object',
      properties: {}
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

WORKFLOW:
1. When user asks about initiatives, actors, or legislation, use search_graph tool
2. When showing results, always visualize them in the graph
3. When user clicks on nodes, use get_related_nodes to show connections
4. Keep responses concise and focused on the graph

IMPORTANT:
- Always respond in Swedish since the user data is in Swedish
- Warn if user tries to store personal data
- Be helpful and guide users to explore the graph`;

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
