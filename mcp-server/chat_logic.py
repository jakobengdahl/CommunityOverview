from typing import List, Dict, Any, Callable
import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv
import inspect

# Load environment variables
load_dotenv()

class ChatProcessor:
    def __init__(self, tools_map: Dict[str, Callable]):
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            print("Warning: ANTHROPIC_API_KEY not found in environment variables")

        self.client = Anthropic(api_key=self.api_key)
        self.tools_map = tools_map
        self.tool_definitions = self._generate_tool_definitions()

        self.system_prompt = """You are a helpful assistant for the Community Knowledge Graph system.

METAMODEL:
- Actor (blue): Government agencies, organizations
- Community (purple): eSam, Myndigheter, Officiell Statistik
- Initiative (green): Projects, collaborative activities
- Capability (orange): Capabilities
- Resource (yellow): Reports, software
- Legislation (red): NIS2, GDPR
- Theme (teal): AI, data strategies
- VisualizationView (gray): Predefined views

SECURITY RULES:
1. ALWAYS warn if the user tries to store personal data
2. For deletion: Max 10 nodes, require double confirmation
3. Always filter based on the user's active communities

WORKFLOW FOR ADDING NODES:
1. Run find_similar_nodes() to find duplicates
2. Present proposal + similar existing nodes
3. Wait for user approval
4. Run add_nodes() only after approval

WORKFLOW FOR DOCUMENT UPLOAD:
1. Extract text from document
2. Identify potential nodes according to metamodel
3. Run find_similar_nodes() for each
4. Present proposal + duplicates
5. Let user choose what to add
6. Automatically link to user's active communities

Always be clear about what you're doing and ask for confirmation for important operations.
Respond in Swedish by default as the data is Swedish.
"""

    def _generate_tool_definitions(self) -> List[Dict]:
        """
        Manually define tools to match what the frontend was sending.
        In a more advanced setup, we could inspect the functions, but
        for now we want to ensure compatibility with the existing prompts.
        """
        return [
            {
                "name": "search_graph",
                "description": "Search for nodes in the graph based on text query. Matches against name, description, and summary.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search text to find matching nodes"
                        },
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: Filter by node types (Actor, Initiative, Legislation, etc.)"
                        },
                        "communities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: Filter by communities"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max number of results",
                            "default": 50
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_related_nodes",
                "description": "Get nodes connected to a given node. Returns both the nodes and the edges connecting them.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "ID of the starting node"
                        },
                        "depth": {
                            "type": "number",
                            "description": "How many hops from the starting node (default 1)",
                            "default": 1
                        },
                         "relationship_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: Filter by relationship types"
                        }
                    },
                    "required": ["node_id"]
                }
            },
            {
                "name": "find_similar_nodes",
                "description": "Find similar nodes based on name for duplicate detection. Use this BEFORE proposing to add a new node.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name to search for similar nodes"
                        },
                        "node_type": {
                            "type": "string",
                            "description": "Optional: Node type to filter on (Actor, Initiative, etc.)"
                        },
                        "threshold": {
                            "type": "number",
                            "description": "Similarity threshold 0.0-1.0 (default 0.7)",
                            "default": 0.7
                        },
                        "limit": {
                            "type": "integer",
                            "default": 5
                        }
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "add_nodes",
                "description": "Add new nodes and edges to the graph. Use this AFTER user confirmation.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "summary": {"type": "string"},
                                    "communities": {"type": "array", "items": {"type": "string"}}
                                }
                            }
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string"},
                                    "target": {"type": "string"},
                                    "type": {"type": "string"}
                                }
                            }
                        }
                    },
                    "required": ["nodes", "edges"]
                }
            },
            {
                "name": "propose_new_node",
                "description": "Propose a new node to be added. Helper tool to format proposal for user.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node": {
                            "type": "object",
                            "description": "The node to propose"
                        },
                        "similar_nodes": {
                            "type": "array",
                            "description": "List of similar nodes found"
                        }
                    },
                    "required": ["node", "similar_nodes"]
                }
            },
            {
                "name": "update_node",
                "description": "Update an existing node.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "ID of the node to update"
                        },
                        "updates": {
                            "type": "object",
                            "description": "Fields to update"
                        }
                    },
                    "required": ["node_id", "updates"]
                }
            },
            {
                "name": "delete_nodes",
                "description": "Delete nodes from the graph.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of node IDs to delete"
                        },
                        "confirmed": {
                            "type": "boolean",
                            "description": "Must be True to execute deletion",
                            "default": False
                        }
                    },
                    "required": ["node_ids"]
                }
            },
            {
                "name": "list_node_types",
                "description": "List all allowed node types.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "save_visualization_metadata",
                "description": "Signal intent to save the current visualization view. Use this when the user wants to save the view.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the view"
                        }
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "get_visualization",
                "description": "Get/Open a saved visualization view by name.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the view"
                        }
                    },
                    "required": ["name"]
                }
            }
        ]

    def process_message(self, messages: List[Dict]) -> Dict:
        """
        Process a message history, call Claude, handle tools, return final response.
        """
        try:
            # First call to Claude
            response = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                system=self.system_prompt,
                tools=self.tool_definitions,
                messages=messages
            )

            # Check if tool use
            if response.stop_reason == "tool_use":
                return self._handle_tool_use(messages, response)

            # Just text response
            return {
                "content": response.content[0].text,
                "toolUsed": None,
                "toolResult": None
            }

        except Exception as e:
            print(f"Error in process_message: {e}")
            return {
                "content": f"Error: {str(e)}",
                "toolUsed": None,
                "toolResult": None
            }

    def _handle_tool_use(self, messages: List[Dict], response) -> Dict:
        tool_use = next(block for block in response.content if block.type == "tool_use")
        tool_name = tool_use.name
        tool_input = tool_use.input
        tool_id = tool_use.id

        print(f"Executing tool: {tool_name} with input: {tool_input}")

        # Execute the tool
        tool_result = None

        # Special case for propose_new_node which is a helper tool, not in the graph
        if tool_name == "propose_new_node":
            tool_result = {
                "proposed_node": tool_input.get("node"),
                "similar_nodes": tool_input.get("similar_nodes"),
                "requires_approval": True
            }
            # For this one, we might want to return early or let Claude wrap it up?
            # The frontend logic for 'propose_new_node' was: return result to frontend so it can show the UI.
            # In server-side logic, we can still do that.

        elif tool_name in self.tools_map:
            try:
                # Call the actual python function
                func = self.tools_map[tool_name]
                # Filter input args to match function signature
                # But for now assuming mapping is clean or using **tool_input
                # Some functions might need specific handling if signatures don't match exactly

                # Check signature
                sig = inspect.signature(func)
                valid_args = {k: v for k, v in tool_input.items() if k in sig.parameters}

                tool_result = func(**valid_args)
            except Exception as e:
                tool_result = {"error": str(e)}
        else:
            tool_result = {"error": f"Tool {tool_name} not found"}

        # Now we need to send the result back to Claude to get the final text response
        # OR if it's a specific tool that requires frontend interaction (like propose),
        # we might want to return the structured data to the frontend.

        # The frontend expects:
        # {
        #   role: 'assistant',
        #   content: response.content,
        #   toolUsed: response.toolUsed,
        #   proposal: ...,
        #   deleteConfirmation: ...
        # }

        # If we just return the text from Claude after feeding back the tool result,
        # we lose the "structured" aspect that the frontend uses to render buttons.

        # However, the frontend logic shows:
        # if (toolResult.tool_type === 'update' || toolResult.tool_type === 'delete') -> reload
        # if (toolResult.nodes) -> update visualization

        # So we definitely need to return the tool result payload to the frontend.

        # Let's get Claude's final response text
        messages.append({
            "role": "assistant",
            "content": response.content
        })
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps(tool_result, default=str)
            }]
        })

        final_response = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=4096,
            system=self.system_prompt,
            tools=self.tool_definitions,
            messages=messages
        )

        final_text = final_response.content[0].text

        return {
            "content": final_text,
            "toolUsed": tool_name,
            "toolResult": tool_result
        }
