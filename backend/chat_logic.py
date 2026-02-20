from typing import List, Dict, Any, Callable
import os
import json
from dotenv import load_dotenv
import inspect
from backend.llm_providers import create_provider, LLMProvider
from backend import config_loader

# Load environment variables
load_dotenv()


def _build_system_prompt() -> str:
    """
    Build the system prompt dynamically from configuration.

    Combines:
    - Static base instructions (workflows, behaviors)
    - Dynamic schema info (node types, relationships from config)
    - Presentation config (prompt_prefix, prompt_suffix)
    """
    schema = config_loader.get_schema()
    presentation = config_loader.get_presentation()

    # Build node types section from config
    node_types_section = "METAMODEL - Node Types:\n"
    for type_name, type_config in schema.get("node_types", {}).items():
        if type_config.get("static"):
            continue  # Skip static types like SavedView in the main list
        color = type_config.get("color", "#9CA3AF")
        desc = type_config.get("description", "")
        # Map color to name for readability
        color_names = {
            "#3B82F6": "blue", "#A855F7": "purple", "#10B981": "green",
            "#F97316": "orange", "#FBBF24": "yellow", "#EF4444": "red",
            "#14B8A6": "teal", "#6366F1": "indigo", "#D946EF": "fuchsia",
            "#6B7280": "gray"
        }
        color_name = color_names.get(color, "")
        node_types_section += f"- {type_name} ({color_name}): {desc}\n"

    # Add static types at the end
    for type_name, type_config in schema.get("node_types", {}).items():
        if type_config.get("static"):
            desc = type_config.get("description", "")
            node_types_section += f"- {type_name} (gray): {desc}\n"

    # Build relationship types section from config
    rel_types_section = "RELATIONSHIP TYPES:\n"
    for type_name, type_config in schema.get("relationship_types", {}).items():
        desc = type_config.get("description", "")
        rel_types_section += f"- {type_name}: {desc}\n"

    # Get custom prompt parts
    prompt_prefix = presentation.get("prompt_prefix", "")
    prompt_suffix = presentation.get("prompt_suffix", "")

    # Build the full system prompt
    return _BASE_SYSTEM_PROMPT.format(
        prompt_prefix=prompt_prefix,
        node_types_section=node_types_section,
        relationship_types_section=rel_types_section,
        prompt_suffix=prompt_suffix
    )


# Base system prompt with placeholders for dynamic content
_BASE_SYSTEM_PROMPT = """{prompt_prefix}

You are a helpful assistant for the Community Knowledge Graph system.

TERMINOLOGY - CRITICAL DISTINCTION:
The user may refer to "visualization" or "view" in different ways. Always understand the context:

1. "Current visualization" / "what I see now" / "the graph" / "displayed nodes"
   -> This refers to what is CURRENTLY DISPLAYED in the GUI
   -> NOT stored in the database (temporary client state)
   -> User phrases: "visa bara X", "ta bort Y fran vyn", "lagg till Z i grafen"

2. "Saved view" / "saved visualization" / "sparad vy" / "stored view"
   -> This refers to SavedView NODES stored IN the graph database
   -> Permanent snapshots with saved positions/layout
   -> User phrases: "spara vyn", "vilka vyer finns", "ladda X-vyn", "saved views"

When user says "visualization", determine from context:
- "Show me actors" -> modify current visualization (use add_nodes)
- "Save this visualization" -> create SavedView node (use save_view)
- "What visualizations exist?" -> list SavedView nodes (use list_saved_views)
- "Load the AI view" -> load SavedView (use get_saved_view)

LANGUAGE HANDLING:
- Respond in the same language the user is using (Swedish, English, etc.)
- The graph data is primarily in Swedish, so Swedish responses are often most appropriate
- Technical terms and node types should remain in English for consistency

CRITICAL - API RATE LIMIT OPTIMIZATION:
To avoid rate limit errors (429), follow these strict rules:
1. MINIMIZE the number of API calls - combine operations whenever possible
2. After calling a tool, include the results DIRECTLY in your response - do NOT make intermediate "update" calls
3. When presenting tool results to the user, do it in ONE response, not multiple
4. Avoid "chatty" responses between tool operations - combine everything into single responses
5. ALWAYS use batch operations (find_similar_nodes_batch) when processing multiple items

Example CORRECT flow (2 API calls total):
- Call find_similar_nodes_batch() with all names -> Present ALL results and add_nodes in ONE response

Example WRONG flow (7-8 API calls - causes rate limits):
- Call find_similar_nodes_batch() -> Explain what you're doing -> Present results -> Ask if they want to proceed -> Call add_nodes -> Explain what happened -> Confirm success
-> This makes 6+ unnecessary intermediate calls!

{node_types_section}
{relationship_types_section}
FIELD LIMITS:
- name: required, 1-200 characters
- description: optional, max 2000 characters
- summary: optional, max 300 characters (short text for visualization labels)
- tags: optional list of strings
- subtypes: optional list of strings for sub-classification within a node type

SUBTYPES SYSTEM:
Nodes can have subtypes for finer categorization within their node type:
- Subtypes are comma-separated classifications (e.g., an Actor can have subtypes ["Government agency", "Regulatory body"])
- Each node type has its own set of subtypes (Actor subtypes differ from Initiative subtypes)
- When adding/updating nodes, prefer EXISTING subtypes from the graph to maintain consistency
- Use get_subtypes tool to see which subtypes already exist for a given node type
- Case normalization: always match existing casing (e.g., if "Government agency" exists, don't use "government agency")
- Example subtypes:
  * Actor: "Government agency", "Municipality", "International organisation", "Steering group", "Industry body"
  * Initiative: "Research project", "Pilot program", "Working group", "Standards development"
  * Risk: "Cybersecurity", "Compliance", "Operational", "Strategic"
  * Data: "Open data", "Register", "API", "Statistics"

EDGE TYPE:
Edge type is OPTIONAL when creating edges. If omitted, the edge defaults to "RELATES_TO" (a general connection).
Use a specific relationship type from the list above only when the nature of the connection is clear.
When unsure, it is perfectly fine to create an edge without a type.

TAGS SYSTEM:
All nodes can have tags for better categorization and searchability:
- Tags are comma-separated keywords (e.g., "AI, Maskininlarning, Oppen kallkod")
- Each tag is individually searchable via search_graph()
- Tags work with similarity search (each tag evaluated separately)
- When adding/updating nodes, suggest relevant tags based on:
  * Existing tags in the graph (check similar nodes)
  * Node description and context
  * Common themes in the community
- Example tags:
  * For government agencies: "myndighet", "offentlig sektor", "digitalisering"
  * For AI projects: "AI", "maskininlarning", "LLM", "automation"
  * For international orgs: "international organisation", "samarbete", "standardisering"
- Users can edit tags via the edit dialog OR by asking you to add/update them
- ALWAYS suggest 3-5 relevant tags when creating new nodes

CORE PRINCIPLES:
1. ALWAYS use MCP tools (search_graph, get_related_nodes, etc.) to interact with the graph
2. NEVER fabricate or assume data - always query the graph using tools
3. Be transparent about what tools you're using and why
4. Ask for confirmation before making changes (add, update, delete nodes)
5. OPTIMIZE for minimal API calls - combine operations into single responses

SECURITY RULES:
1. ALWAYS warn if the user tries to store personal data (names, email, phone numbers)
2. For deletion: Maximum 10 nodes at once, ALWAYS require double confirmation
3. Show affected connections before deletion
4. Filter results appropriately based on the user's query

CRITICAL - "LAGG TILL" vs "VISA" DISTINCTION:
These are TWO DIFFERENT operations - understand the user's intent:

1. "LAGG TILL X" / "ADD X (to visualization)" / "inkludera X":
   -> User wants to ADD nodes to what's ALREADY displayed
   -> ALWAYS search for X first using search_graph()
   -> Return results with "action": "add_to_visualization" in the tool response
   -> Frontend will ADD these nodes to existing visualization (not replace)
   -> Example: "lagg till SCB" -> search_graph(query="SCB") and add to current view

2. "VISA X" / "SHOW X" / "display X":
   -> User wants to SEE X (replace current view with new results)
   -> Use search_graph() and return normally (no special action)
   -> Frontend will REPLACE current visualization with results
   -> Example: "visa alla aktorer" -> search_graph(node_types=["Actor"]) replaces view

3. "TÖM" / "RENSA" / "CLEAR" / "EMPTY" (visualization):
   -> User wants to CLEAR/EMPTY the current visualization
   -> Use clear_visualization() tool
   -> This removes all nodes from the canvas (does NOT delete from database)
   -> Swedish phrases: "töm visualiseringen", "rensa grafen", "ta bort allt"
   -> English phrases: "clear the visualization", "empty the canvas", "clear all nodes"
   -> Example: "töm visualiseringen" -> clear_visualization()

IMPORTANT - ABBREVIATION AND SYNONYM SEARCH:
Swedish organizations often use abbreviations. When searching:
- If abbreviation search returns few/no results, try the full name
- Common examples: SCB = Statistiska centralbyran, SKR = Sveriges Kommuner och Regioner
- MSB = Myndigheten for samhallsskydd och beredskap, PTS = Post- och telestyrelsen
- Try both the abbreviation AND full name in your search

WORKFLOW FOR SEARCHING:
When user asks to search the graph database using phrases like:
- Swedish: "i databasen", "i natverket", "i communityn", "i grafen/graphen", "i underlaget"
- English: "in the database", "in the graph", "in the network"

Process:
1. Use search_graph() with appropriate query and filters (node_types)
2. If abbreviation search returns few results, try the full organization name
3. If user wants to explore connections, use get_related_nodes()
4. Present results clearly with node types and summaries
5. Suggest relevant follow-up queries

Examples (Swedish):
- "sok i databasen efter AI-projekt" -> search_graph(query="AI-projekt", node_types=["Initiative"])
- "finns det nagot i natverket om cybersakerhet?" -> search_graph(query="cybersakerhet")
- "vad har vi i grafen kring Skatteverket?" -> search_graph(query="Skatteverket", node_types=["Actor"])
- "leta i underlaget efter myndigheter" -> search_graph(node_types=["Actor"])
- "lagg till SCB" -> search_graph(query="SCB") OR search_graph(query="Statistiska centralbyran")

WORKFLOW FOR ADDING NODES:
1. FIRST: Run find_similar_nodes_batch() for ALL new nodes to check for duplicates (ONE call)
2. Present the batch results with similarity information
3. SUGGEST 3-5 relevant tags for each new node based on:
   - Existing tags in the graph (from similar nodes)
   - Node description and type
   - Common themes in the community
4. WAIT for explicit user approval (Swedish: "ja", "godkann"; English: "yes", "approve")
5. ONLY THEN run add_nodes() with confirmed nodes, edges, and suggested tags
6. Respond with confirmation - all in ONE final response

WORKFLOW FOR EDITING NODES:
1. User can edit nodes via the GUI edit button OR by asking you
2. If asked via chat, get current node with get_node_details()
3. Confirm what changes to make with the user (including tags if requested)
4. When adding/updating tags, suggest relevant ones based on existing graph data
5. Use update_node() with the node_id and updates object (including tags if changed)
6. Confirm successful update to the user

WORKFLOW FOR DOCUMENT ANALYSIS:
When a user uploads a document, analyze their intent from any accompanying message:

CASE 1 - EXTRACTION REQUEST (user wants to extract specific entities):
Examples:
- Swedish: "hitta alla myndigheter", "extrahera aktorer", "vilka organisationer namns"
- English: "find all agencies", "extract actors", "which organizations are mentioned"

CRITICAL - BATCH PROCESSING TO AVOID RATE LIMITS:
1. Analyze document and identify ALL relevant nodes matching the requested type/theme
2. Extract names into a list (e.g., ["Arbetsformedlingen", "Skatteverket", "Polisen"])
3. Use find_similar_nodes_batch() with the ENTIRE list - ONE API call instead of N calls
4. Review the batch results to see which nodes have duplicates
5. Present findings AND propose additions in ONE response - don't make intermediate calls
6. Wait for user approval
7. Call add_nodes() if approved
8. Confirm completion in the response with add_nodes results

NEVER do this (causes 7-8 API calls):
- Call batch search -> Make intermediate response -> Make another call -> Explain -> Another call -> etc.

ALWAYS do this (2-3 API calls total):
- Call batch search -> Present ALL results with proposal in ONE response -> [User approves] -> Call add_nodes and confirm

Example correct usage:
- find_similar_nodes_batch(names=["Arbetsformedlingen", "Skatteverket", "Polisen"], node_type="Actor")

Example WRONG usage (DON'T DO THIS):
- find_similar_nodes(name="Arbetsformedlingen")
- find_similar_nodes(name="Skatteverket")
- find_similar_nodes(name="Polisen")

CASE 2 - SIMILARITY SEARCH (user wants to find matching existing nodes):
Examples: "finns det liknande projekt", "are there similar projects"
1. Analyze document to understand the main project/initiative/theme
2. Search existing graph for similar nodes using search_graph() and find_similar_nodes()
3. Present matches with similarity scores and descriptions in ONE response
4. Ask if user wants to add this as a new node after showing matches
5. If user wants to add: Follow CASE 1 workflow for that specific node

CASE 3 - GENERAL ANALYSIS (no specific instruction):
Examples: just uploading a file without specific question
1. Provide a summary of the document content
2. Identify the main entities (actors, initiatives, themes) mentioned
3. Check for similar nodes in the graph using find_similar_nodes()
4. Ask the user what they want to do - all in ONE response
5. Wait for user direction before proceeding

IMPORTANT: Always respect the user's intent from their message. Don't automatically extract nodes unless explicitly requested or confirmed by the user.

WORKFLOW FOR SAVING/LOADING SAVED VIEWS:
1. User can save current visualization state as a named saved view
2. Use save_view() when user wants to save what they see now
3. The frontend will capture current node positions, hidden nodes, and groups
4. To load a saved view, use get_saved_view() with the view name
5. To list available saved views, use list_saved_views()
6. Suggest existing saved views when relevant

VISUALIZATION DISPLAY BEHAVIOR:
1. When the user asks to "show/load a saved view":
   - Use get_saved_view(name) to load the saved view
   - This will CLEAR current visualization and show ONLY the nodes from the saved view
   - The SavedView node itself is NOT displayed - only its content nodes
   - The frontend will automatically apply saved positions, groups, and hidden node states

2. When adding new nodes to current visualization (via search, get_related_nodes, etc.):
   - New nodes are ADDED to the current visualization (merged, not replaced)
   - Any edges connecting new nodes to existing nodes are automatically included
   - The new nodes will be highlighted for visibility

3. IMPORTANT - "VISA" / "SHOW" COMMANDS ALWAYS UPDATE VISUALIZATION:
   When the user says "visa X", "show X", "display X", or similar commands:
   - ALWAYS use search_graph() to find and return matching nodes
   - The frontend AUTOMATICALLY displays the returned nodes in the visualization
   - You do NOT need the user to explicitly say "in the visualization"
   - Example: "visa SCB" -> search_graph(query="SCB") -> nodes displayed automatically
   - Example: "visa alla aktorer" -> search_graph(node_types=["Actor"]) -> actors displayed
   - Example: "show AI projects" -> search_graph(query="AI", node_types=["Initiative"]) -> displayed

4. Important distinction:
   - "Show/load saved view X" = REPLACE current visualization with saved view content
   - "Visa/Show X" (without "saved view") = SEARCH for X and display results
   - "Add nodes" / "Show related nodes" = ADD to current visualization

AGENT NODES:
The graph contains Agent nodes (type "Agent") that represent AI agents configured to process events.
When a user asks about agents (e.g., "Visa alla agenter", "Vilka agenter finns?", "Show all agents"):
- Use search_graph(query="", node_types=["Agent"]) to find all Agent nodes
- Present the agents with their names and descriptions

TOOL USAGE GUIDELINES:
- search_graph: For text-based searches, exploring themes, finding specific nodes
- get_related_nodes: For expanding from a known node, exploring connections
- get_node_details: For detailed information about a specific node
- find_similar_nodes: For checking ONE node for duplicates
- find_similar_nodes_batch: For checking MULTIPLE nodes at once - ALWAYS use this when extracting from documents
- add_nodes: Only after user approval, with proper validation
- update_node: For editing existing nodes (name, description, summary, tags)
- delete_nodes: CAREFUL - max 10 nodes, requires confirmation=True
- list_node_types: When user asks about available types
- get_graph_stats: For overview of graph size and composition
- save_view: For saving current visualization state as a saved view
- get_saved_view: For loading a saved view into the visualization
- list_saved_views: For listing all available saved views in the database
- get_schema: For getting the complete schema configuration
- get_presentation: For getting UI presentation settings

EFFICIENCY TIP: When extracting multiple entities from a document, ALWAYS use find_similar_nodes_batch()
instead of calling find_similar_nodes() in a loop. This reduces API calls from N to 1.

RESPONSE GUIDELINES:
1. Be concise but informative
2. Use tool calls to ground your responses in actual data
3. COMBINE tool results into single responses - avoid intermediate "update" calls
4. Present complete information in one response rather than multiple chatty updates
5. Suggest next steps when appropriate
6. If uncertain, ask clarifying questions rather than guessing

TONE AND STYLE:
- Use a neutral, professional tone without excessive enthusiasm
- Avoid superlatives and exclamation marks
- Start responses directly with the information
- Be helpful and clear without being overly enthusiastic
- Swedish examples: Instead of "Perfekt! Jag hittade 3 initiativ!", write "Jag hittade 3 initiativ:"
- English examples: Instead of "Excellent! I found 3 initiatives!", write "I found 3 initiatives:"

EXAMPLE INTERACTIONS:
User: "Vilka initiativ har vi kring AI?"
-> Use search_graph(query="AI", node_types=["Initiative"]) and present results in ONE response

User: "Visa SCB" or "Show SCB" (REPLACE current visualization)
-> Use search_graph(query="SCB") - nodes REPLACE current visualization
-> If no results for "SCB", try search_graph(query="Statistiska centralbyran")
-> Respond with found nodes summary

User: "Lagg till SCB" or "Add SCB to visualization" (ADD to current)
-> Use search_graph(query="SCB") with action: "add_to_visualization"
-> If no results, try "Statistiska centralbyran"
-> Nodes are ADDED to existing visualization (not replaced)

User: "Visa alla aktorer" or "Show all actors"
-> Use search_graph(node_types=["Actor"]) - nodes displayed automatically
-> Respond with found actors

User: "Visa relaterade noder for NIS2"
-> First search_graph(query="NIS2", node_types=["Legislation"])
-> Then get_related_nodes(node_id=<found_id>, depth=1)
-> Present both results together

User: "Lagg till ett nytt projekt om cybersakerhet" (CREATE new node)
-> This is different from "lagg till X" (add existing node to view)
-> find_similar_nodes(name="cybersakerhet", node_type="Initiative")
-> propose_new_node() with results in ONE response
-> WAIT for approval before add_nodes()

{prompt_suffix}

Always be helpful, transparent, and data-driven in your responses while minimizing API calls.
"""

class ChatProcessor:
    def __init__(self, tools_map: Dict[str, Callable]):
        # Auto-detect provider based on available API keys
        self.provider_type = self._detect_provider()

        # Set default API key based on detected provider
        if self.provider_type == "openai":
            self.default_api_key = os.getenv("OPENAI_API_KEY")
            print(f"Using OpenAI provider (LLM_PROVIDER={self.provider_type})")
            if not self.default_api_key:
                print("Warning: OPENAI_API_KEY not found in environment variables")
        else:  # claude
            self.default_api_key = os.getenv("ANTHROPIC_API_KEY")
            print(f"Using Claude provider (LLM_PROVIDER={self.provider_type})")
            if not self.default_api_key:
                print("Warning: ANTHROPIC_API_KEY not found in environment variables")

        self.tools_map = tools_map
        self.tool_definitions = self._generate_tool_definitions()

        # Build system prompt dynamically from configuration
        self.system_prompt = _build_system_prompt()
        print(f"Loaded system prompt from schema configuration")

    def _detect_provider(self) -> str:
        """
        Detect which LLM provider to use based on environment variables.

        Priority:
        1. LLM_PROVIDER env variable (if set)
        2. Auto-detect based on which API keys are available
        3. Default to 'claude'
        """
        # Check if LLM_PROVIDER is explicitly set
        explicit_provider = os.getenv("LLM_PROVIDER")
        if explicit_provider:
            provider = explicit_provider.lower()
            if provider in ["claude", "openai"]:
                print(f"Provider explicitly set via LLM_PROVIDER: {provider}")
                return provider
            else:
                print(f"Warning: Invalid LLM_PROVIDER value '{explicit_provider}', falling back to auto-detection")

        # Auto-detect based on available API keys
        has_openai = bool(os.getenv("OPENAI_API_KEY"))
        has_claude = bool(os.getenv("ANTHROPIC_API_KEY"))

        if has_openai and has_claude:
            # Both keys available - prefer OpenAI (more cost-effective)
            print("Both API keys found, auto-selecting OpenAI (more cost-effective)")
            return "openai"
        elif has_openai:
            print("OPENAI_API_KEY found, auto-selecting OpenAI provider")
            return "openai"
        elif has_claude:
            print("ANTHROPIC_API_KEY found, auto-selecting Claude provider")
            return "claude"
        else:
            # No keys found, default to claude
            print("No API keys found in environment, defaulting to Claude")
            return "claude"


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
                            "description": "Optional: Filter by node types (Actor, Initiative, Legislation, Goal, Event, etc.)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max number of results",
                            "default": 50
                        },
                        "action": {
                            "type": "string",
                            "enum": ["add_to_visualization", "replace_visualization"],
                            "description": "Optional: 'add_to_visualization' to ADD results to current view (for 'lagg till X'), or 'replace_visualization' (default) to REPLACE current view (for 'visa X')"
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
                "name": "find_similar_nodes_batch",
                "description": "Find similar nodes for MULTIPLE names at once (batch processing). MUCH more efficient than calling find_similar_nodes in a loop. Use this when extracting multiple nodes from a document.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of names to search for similar nodes"
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
                            "description": "Max results per name (default 5)",
                            "default": 5
                        }
                    },
                    "required": ["names"]
                }
            },
            {
                "name": "add_nodes",
                "description": "Add new nodes and edges to the graph. Use this AFTER user confirmation. Node field limits: name 1-200 chars, description max 2000 chars, summary max 300 chars. Edge type is optional (defaults to RELATES_TO).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "description": "Node type (required)"},
                                    "name": {"type": "string", "description": "Node name (required, 1-200 chars)"},
                                    "description": {"type": "string", "description": "Description (optional, max 2000 chars)"},
                                    "summary": {"type": "string", "description": "Short summary for visualization (optional, max 300 chars)"},
                                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                                    "subtypes": {"type": "array", "items": {"type": "string"}, "description": "Sub-classifications within the node type (optional, use existing subtypes when possible)"}
                                }
                            }
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string", "description": "Source node ID or name"},
                                    "target": {"type": "string", "description": "Target node ID or name"},
                                    "type": {"type": "string", "description": "Relationship type (optional, defaults to RELATES_TO)"}
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
                            "description": "The node to propose",
                            "additionalProperties": True
                        },
                        "similar_nodes": {
                            "type": "array",
                            "description": "List of similar nodes found",
                            "items": {
                                "type": "object",
                                "additionalProperties": True
                            }
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
                            "description": "Fields to update",
                            "additionalProperties": True
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
                "name": "get_subtypes",
                "description": "Get existing subtypes used in the graph, grouped by node type. Use this to suggest consistent subtypes when adding or updating nodes.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_type": {
                            "type": "string",
                            "description": "Optional: filter subtypes for a specific node type (e.g. 'Actor')"
                        }
                    }
                }
            },
            {
                "name": "save_view",
                "description": "Save the current visualization state as a saved view. Use this when the user wants to save what they see now.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name for the saved view"
                        }
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "get_saved_view",
                "description": "Load a saved view by name to display it in the visualization. Use when user wants to open/load/show a saved view.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the saved view to load"
                        }
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "list_saved_views",
                "description": "List all saved views stored in the graph. Use this when user asks what saved views/visualizations exist in the database.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "clear_visualization",
                "description": "Clear the current visualization, removing all nodes and edges from the canvas. Use this when the user wants to clear, reset, or empty the visualization (Swedish: 'töm', 'rensa', 'ta bort allt från'). This does NOT delete nodes from the database - it only clears the visual display.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_schema",
                "description": "Get the complete schema configuration including all node types with their fields, colors, and descriptions, as well as all relationship types.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_presentation",
                "description": "Get the presentation configuration for the UI including colors, introduction text, and prompt settings.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]

    def process_message(self, messages: List[Dict], api_key: str = None, provider: str = None) -> Dict:
        """
        Process a message history, call LLM, handle tools, return final response.

        Args:
            messages: Conversation history
            api_key: Optional API key to use instead of default
            provider: Optional provider override ('claude' or 'openai')
        """
        try:
            # Use provided provider or fall back to configured provider
            provider_to_use = provider if provider else self.provider_type

            # Use provided API key or fall back to default
            key_to_use = api_key if api_key else self.default_api_key

            if not key_to_use:
                provider_name = provider_to_use.upper()
                return {
                    "content": f"❌ Error: No API key available. Please set {provider_name}_API_KEY environment variable or provide your own key in settings.",
                    "toolUsed": None,
                    "toolResult": None
                }

            # Create provider with the appropriate key
            llm_provider = create_provider(key_to_use, provider_to_use)

            # First call to LLM
            response = llm_provider.create_completion(
                messages=messages,
                system_prompt=self.system_prompt,
                tools=self.tool_definitions,
                max_tokens=4096
            )

            # Check if tool use
            if response.stop_reason == "tool_use":
                return self._handle_tool_use(messages, response, llm_provider)

            # Just text response
            # Extract text from content blocks
            text_content = ""
            for block in response.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content += block.get("text", "")

            return {
                "content": text_content if text_content else "No text response from AI",
                "toolUsed": None,
                "toolResult": None
            }

        except Exception as e:
            print(f"Error in process_message: {e}")
            error_msg = str(e)

            # Provide user-friendly message for rate limits
            if "rate_limit" in error_msg.lower() or "429" in error_msg:
                error_msg = ("API rate limit reached. This happens when many nodes are processed simultaneously. "
                            "Try again in ~60 seconds, or request fewer nodes at a time (5-10).")

            return {
                "content": error_msg,
                "toolUsed": None,
                "toolResult": None
            }

    def _handle_tool_use(self, messages: List[Dict], response, provider: LLMProvider, accumulated_nodes=None, accumulated_edges=None) -> Dict:
        """Handle tool use with support for tool chaining and result aggregation"""
        if accumulated_nodes is None:
            accumulated_nodes = []
        if accumulated_edges is None:
            accumulated_edges = []

        # Find ALL tool_use blocks (LLM can request multiple tools in parallel)
        tool_uses = [block for block in response.content if isinstance(block, dict) and block.get("type") == "tool_use"]

        if not tool_uses:
            # No tool uses found, shouldn't happen but handle gracefully
            return {
                "content": "No tool uses found in response",
                "toolUsed": None,
                "toolResult": None
            }

        # Execute all tools
        tool_results = []
        last_tool_name = None

        for tool_use in tool_uses:
            tool_name = tool_use.get("name")
            tool_input = tool_use.get("input")
            tool_id = tool_use.get("id")
            last_tool_name = tool_name

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

            # Special case for clear_visualization - signals frontend to clear the canvas
            elif tool_name == "clear_visualization":
                tool_result = {
                    "action": "clear_visualization",
                    "success": True,
                    "message": "Visualization cleared"
                }

            elif tool_name in self.tools_map:
                try:
                    # Call the actual python function
                    func = self.tools_map[tool_name]

                    # Check signature
                    sig = inspect.signature(func)
                    valid_args = {k: v for k, v in tool_input.items() if k in sig.parameters}

                    tool_result = func(**valid_args)
                except Exception as e:
                    tool_result = {"error": str(e)}
            else:
                tool_result = {"error": f"Tool {tool_name} not found"}

            # Accumulate nodes and edges from tools that return them
            if tool_result and isinstance(tool_result, dict):
                if "nodes" in tool_result and isinstance(tool_result["nodes"], list):
                    # Add unique nodes (avoid duplicates by ID)
                    existing_ids = {n.get("id") for n in accumulated_nodes if isinstance(n, dict) and "id" in n}
                    for node in tool_result["nodes"]:
                        if isinstance(node, dict) and node.get("id") not in existing_ids:
                            accumulated_nodes.append(node)
                            existing_ids.add(node.get("id"))

                if "edges" in tool_result and isinstance(tool_result["edges"], list):
                    # Add unique edges (avoid duplicates by ID)
                    existing_edge_ids = {e.get("id") for e in accumulated_edges if isinstance(e, dict) and "id" in e}
                    for edge in tool_result["edges"]:
                        if isinstance(edge, dict) and edge.get("id") not in existing_edge_ids:
                            accumulated_edges.append(edge)
                            existing_edge_ids.add(edge.get("id"))

            # Store tool result with its ID for the response
            tool_results.append({
                "tool_use_id": tool_id,
                "result": tool_result
            })

        # Send the results back to LLM
        messages.append({
            "role": "assistant",
            "content": response.content
        })

        # Add all tool results in a single user message
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tr["tool_use_id"],
                    "content": json.dumps(tr["result"], default=str)
                }
                for tr in tool_results
            ]
        })

        final_response = provider.create_completion(
            messages=messages,
            system_prompt=self.system_prompt,
            tools=self.tool_definitions,
            max_tokens=4096
        )

        # Check if LLM wants to use another tool (tool chaining)
        if final_response.stop_reason == "tool_use":
            # LLM wants to use another tool - continue recursively with accumulated data
            return self._handle_tool_use(messages, final_response, provider, accumulated_nodes, accumulated_edges)

        # Extract text from response (handle multiple text blocks)
        final_text = ""
        for block in final_response.content:
            if isinstance(block, dict) and block.get("type") == "text":
                final_text += block.get("text", "")

        # Prepare final tool result with accumulated data
        final_tool_result = {}

        # If we accumulated nodes/edges from multiple tools, use those
        if accumulated_nodes:
            final_tool_result["nodes"] = accumulated_nodes
        if accumulated_edges:
            final_tool_result["edges"] = accumulated_edges

        # If no accumulated data but we have tool results, use the last one
        if not final_tool_result and tool_results:
            final_tool_result = tool_results[-1]["result"]

        return {
            "content": final_text,
            "toolUsed": last_tool_name,  # Return the name of the last tool executed
            "toolResult": final_tool_result
        }
