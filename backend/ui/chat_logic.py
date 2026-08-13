from typing import List, Dict, Callable, Optional
import os
import json
import logging
from dotenv import load_dotenv
import inspect
from backend.llm.llm_providers import create_provider, LLMProvider
from backend.config import config_loader
from backend.config.model_profiles import (
    create_provider_from_profile,
    resolve_profile_reference,
)
from backend.llm.language_policy import format_language_policy_for_prompt

# Initialize logger
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Visualization actions that decide how node/edge results affect the current
# canvas (add vs. replace vs. clear vs. in-place update). These are the only
# actions that determine view-content placement; pure overlays/side-effects
# (mark_nodes, present_form, save_view, start_guide, node_pulse) are
# intentionally excluded so they never override how returned nodes are
# displayed. When a node-returning tool leaves the action unset, the assistant
# defaults to additive placement — a plain "add X" request must only add, never
# silently clear the view. Mirrors the additive default of
# _push_visualization_command (backend/service/mcp_tools.py).
_VIEW_CONTENT_ACTIONS = frozenset(
    {
        "add_to_visualization",
        "replace_visualization",
        "load_visualization",
        "clear_visualization",
        "update_in_visualization",
    }
)


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
        if type_config.get("category") == "system":
            continue  # Skip system types in the main domain list
        color = type_config.get("color", "#9CA3AF")
        desc = type_config.get("description", "")
        # Map color to name for readability
        color_names = {
            "#3B82F6": "blue",
            "#A855F7": "purple",
            "#10B981": "green",
            "#F97316": "orange",
            "#FBBF24": "yellow",
            "#EF4444": "red",
            "#14B8A6": "teal",
            "#6366F1": "indigo",
            "#D946EF": "fuchsia",
            "#6B7280": "gray",
        }
        color_name = color_names.get(color, "")
        node_types_section += f"- {type_name} ({color_name}): {desc}\n"

    # Add system types at the end
    for type_name, type_config in schema.get("node_types", {}).items():
        if type_config.get("category") == "system":
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
    language_policy_section = format_language_policy_for_prompt(presentation)

    # Build the full system prompt
    return _BASE_SYSTEM_PROMPT.format(
        prompt_prefix=prompt_prefix,
        node_types_section=node_types_section,
        relationship_types_section=rel_types_section,
        language_policy_section=language_policy_section,
        prompt_suffix=prompt_suffix,
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
- Technical terms and node types should remain in English for consistency when appropriate
- Follow the graph language policy below for any new or updated graph content

{language_policy_section}
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

ARCHIVED LIFECYCLE:
Nodes and edges have an `archived` flag (default false) that is separate from deletion:
- ARCHIVE = hide by default while keeping the item (and its history) in the graph.
  Reversible. Use archive_nodes / archive_edges to archive, and
  unarchive_nodes / unarchive_edges to restore.
- DELETE = permanent removal. Use delete_nodes / delete_edges only when the user
  truly wants the data gone.
- When a user asks to "remove", "hide", "retire", "put away" or "arkivera" something
  but does not clearly want permanent deletion, PREFER archiving and say so.
- search_graph and get_related_nodes EXCLUDE archived nodes and edges by default.
  To see or work with archived items, pass include_archived=true. So if a user asks
  "what have I archived?" or wants to restore something, search with
  include_archived=true first to find it, then unarchive it.
- Archived items still exist: an archived node is not deleted, just hidden from the
  default views.

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

CRITICAL - ADD vs REPLACE vs CLEAR (visualization intent):
The default is ADDITIVE. Only clear or replace the current view when the user
EXPLICITLY asks for it. When in doubt, add — never silently clear the canvas.

1. ADDITIVE (the default) — "LAGG TILL X" / "ADD X" / "inkludera X", and any
   plain request to bring nodes into the view that does NOT say replace/clear:
   -> User wants to ADD nodes to what's ALREADY displayed, keeping current content
   -> ALWAYS search for X first using search_graph()
   -> Pass action="add_to_visualization" (this is also the safe default if omitted)
   -> Frontend ADDS these nodes to the existing visualization (does not replace)
   -> NEVER call clear_visualization() for a plain additive request
   -> Examples:
      "lagg till SCB" -> search_graph(query="SCB", action="add_to_visualization")
      "add all actor nodes in the view" ->
        search_graph(node_types=["Actor"], action="add_to_visualization")

2. REPLACE — the user EXPLICITLY asks to replace the view, or to show something
   INSTEAD of what is there ("VISA X" / "SHOW X", "replace the view with X",
   "byt ut vyn mot X", "visa istallet X"):
   -> Use search_graph() with action="replace_visualization"
   -> Frontend REPLACES the current visualization with the results
   -> Example: "visa alla aktorer" ->
        search_graph(node_types=["Actor"], action="replace_visualization")

3. CLEAR-AND-ADD — the user EXPLICITLY asks to clear/empty first, then show:
   ("clear the view and show all actors in sector X", "rensa och visa X")
   -> Use search_graph() with action="replace_visualization" (clears then shows)

4. CLEAR ONLY — "TÖM" / "RENSA" / "CLEAR" / "EMPTY" with nothing to show after:
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

3. IMPORTANT - "VISA" / "SHOW" COMMANDS UPDATE THE VISUALIZATION:
   When the user says "visa X", "show X", "display X", or similar commands:
   - ALWAYS use search_graph() to find and return matching nodes
   - "visa/show X" means show X INSTEAD of the current content, so pass
     action="replace_visualization"
   - You do NOT need the user to explicitly say "in the visualization"
   - Example: "visa SCB" -> search_graph(query="SCB", action="replace_visualization")
   - Example: "visa alla aktorer" ->
       search_graph(node_types=["Actor"], action="replace_visualization")
   - Example: "show AI projects" ->
       search_graph(query="AI", node_types=["Initiative"], action="replace_visualization")

4. Important distinction:
   - "Show/load saved view X" = REPLACE current visualization with saved view content
   - "Visa/Show X" (without "saved view") = REPLACE current view with the results
     (action="replace_visualization")
   - "Add X" / "Show related nodes" = ADD to current visualization
     (action="add_to_visualization" — the default; never clears the view)

AGENT NODES:
The graph contains Agent nodes (type "Agent") that represent AI agents configured to process events.
When a user asks about agents (e.g., "Visa alla agenter", "Vilka agenter finns?", "Show all agents"):
- Use search_graph(query="", node_types=["Agent"]) to find all Agent nodes
- Present the agents with their names and descriptions

TOOL USAGE GUIDELINES:
- search_graph: For text-based searches, exploring themes, finding specific nodes
- get_related_nodes: For expanding from a known node, exploring connections, or traversing lineage/provenance chains by specifying relationship_types (e.g. production-step relationships in a data pipeline)
- get_node_details: For detailed information about a specific node
- find_similar_nodes: For checking ONE node for duplicates
- find_similar_nodes_batch: For checking MULTIPLE nodes at once - ALWAYS use this when extracting from documents
- add_nodes: Only after user approval, with proper validation
- update_node: For editing existing nodes (name, description, summary, tags)
- delete_nodes: CAREFUL - max 10 nodes, requires confirmation=True
- delete_edges: CAREFUL - max 50 edges, requires confirmation=True
- list_node_types: When user asks about available types
- get_graph_stats: For overview of graph size and composition
- save_view: For saving current visualization state as a saved view
- get_saved_view: For loading a saved view into the visualization
- list_saved_views: For listing all available saved views in the database
- get_schema: For getting the complete schema configuration
- get_presentation: For getting UI presentation settings
- mark_nodes: For applying visual color annotations to nodes currently in the visualization (session-only, does not change the database). Call with empty marks array to clear all marks.

WORKFLOW FOR MARKING NODES:
Use mark_nodes to annotate nodes in the current visualization with colors and labels:
1. Choose a meaningful color (e.g. '#EF4444' red, '#F97316' orange, '#FBBF24' yellow, '#10B981' green)
2. Provide a short label that describes the mark's meaning in context
3. Marked nodes show a color badge and the labels appear in an on-canvas legend
4. Marks are session-only — they never persist to the database
5. Call mark_nodes with an empty array to remove all marks
6. Example: to show analysis results, mark critical nodes red, medium-priority orange, reviewed green

WORKFLOW FOR LINEAGE AND IMPACT TRAVERSAL:
To answer questions like "what downstream steps use this dataset?" or "what is affected if this changes?":
1. Call get_schema to discover available relationship types for the current schema
2. Call get_related_nodes with relevant relationship_types and depth=2 (or deeper) to traverse the chain
3. Optionally repeat from a different direction (upstream vs downstream) using the inverse relationship types
4. Visualize results with mark_nodes: e.g. source node green, intermediate steps orange, leaf outputs red
5. Summarize the traversal path in plain language, not just a list of nodes
Example: upstream data quality question — find input datasets → process steps → output datasets using the
appropriate relationship chain, then mark each tier a distinct color so the user sees the full dependency tree.

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
-> Use search_graph(query="SCB", action="replace_visualization")
-> If no results for "SCB", try search_graph(query="Statistiska centralbyran", action="replace_visualization")
-> Respond with found nodes summary

User: "Lagg till SCB" or "Add SCB to visualization" (ADD to current)
-> Use search_graph(query="SCB", action="add_to_visualization")
-> If no results, try "Statistiska centralbyran"
-> Nodes are ADDED to existing visualization (current content is kept)

User: "Add all actor nodes in the view" (ADD to current - additive)
-> Use search_graph(node_types=["Actor"], action="add_to_visualization")
-> Actors are ADDED to the existing visualization; nothing is cleared

User: "Visa alla aktorer" or "Show all actors" (REPLACE current visualization)
-> Use search_graph(node_types=["Actor"], action="replace_visualization")
-> Respond with found actors

User: "Replace the view with all actors" / "clear the view and show all actors"
-> Use search_graph(node_types=["Actor"], action="replace_visualization")
-> The current view is replaced with the actors

User: "Visa relaterade noder for NIS2" (ADD related nodes to current)
-> First search_graph(query="NIS2", node_types=["Legislation"], action="replace_visualization")
-> Then get_related_nodes(node_id=<found_id>, depth=1) — related nodes are ADDED
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
            logger.info(f"Using OpenAI provider (LLM_PROVIDER={self.provider_type})")
            if not self.default_api_key:
                logger.warning("OPENAI_API_KEY not found in environment variables")
        else:  # claude
            self.default_api_key = os.getenv("ANTHROPIC_API_KEY")
            logger.info(f"Using Claude provider (LLM_PROVIDER={self.provider_type})")
            if not self.default_api_key:
                logger.warning("ANTHROPIC_API_KEY not found in environment variables")

        self.tools_map = tools_map
        self.tool_definitions = self._generate_tool_definitions()

        # Build system prompt dynamically from configuration
        self.system_prompt = _build_system_prompt()
        logger.info("Loaded system prompt from schema configuration")

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
                logger.info(f"Provider explicitly set via LLM_PROVIDER: {provider}")
                return provider
            else:
                logger.warning(
                    f"Invalid LLM_PROVIDER value '{explicit_provider}', falling back to auto-detection"
                )

        # Auto-detect based on available API keys
        has_openai = bool(os.getenv("OPENAI_API_KEY"))
        has_claude = bool(os.getenv("ANTHROPIC_API_KEY"))

        if has_openai and has_claude:
            # Both keys available - prefer OpenAI (more cost-effective)
            logger.info(
                "Both API keys found, auto-selecting OpenAI (more cost-effective)"
            )
            return "openai"
        elif has_openai:
            logger.info("OPENAI_API_KEY found, auto-selecting OpenAI provider")
            return "openai"
        elif has_claude:
            logger.info("ANTHROPIC_API_KEY found, auto-selecting Claude provider")
            return "claude"
        else:
            # No keys found, default to claude
            logger.info("No API keys found in environment, defaulting to Claude")
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
                            "description": "Search text to find matching nodes",
                        },
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: Filter by node types (Actor, Initiative, Legislation, Goal, Event, etc.)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max number of results",
                            "default": 50,
                        },
                        "action": {
                            "type": "string",
                            "enum": ["add_to_visualization", "replace_visualization"],
                            "description": "How results affect the current view. 'add_to_visualization' ADDS results to the current view, keeping existing content (for 'lagg till X' and any plain additive request). 'replace_visualization' REPLACES the current view (only when the user EXPLICITLY asks to replace/clear-and-show, e.g. 'visa X', 'replace the view with X'). When omitted the default is ADDITIVE — a plain request never clears the view.",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "description": "When false (default) archived nodes and edges are excluded. Set true to include archived items (e.g. to find something the user wants to restore).",
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_related_nodes",
                "description": "Get nodes connected to a given node. Returns both the nodes and the edges connecting them.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "ID of the starting node",
                        },
                        "depth": {
                            "type": "number",
                            "description": "How many hops from the starting node (default 1)",
                            "default": 1,
                        },
                        "relationship_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional: Filter by relationship types",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "description": "When false (default) archived edges are not traversed and archived neighbour nodes are excluded. Set true to include them.",
                            "default": False,
                        },
                    },
                    "required": ["node_id"],
                },
            },
            {
                "name": "find_similar_nodes",
                "description": "Find similar nodes based on name for duplicate detection. Use this BEFORE proposing to add a new node.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name to search for similar nodes",
                        },
                        "node_type": {
                            "type": "string",
                            "description": "Optional: Node type to filter on (Actor, Initiative, etc.)",
                        },
                        "threshold": {
                            "type": "number",
                            "description": "Similarity threshold 0.0-1.0 (default 0.7)",
                            "default": 0.7,
                        },
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["name"],
                },
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
                            "description": "List of names to search for similar nodes",
                        },
                        "node_type": {
                            "type": "string",
                            "description": "Optional: Node type to filter on (Actor, Initiative, etc.)",
                        },
                        "threshold": {
                            "type": "number",
                            "description": "Similarity threshold 0.0-1.0 (default 0.7)",
                            "default": 0.7,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results per name (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["names"],
                },
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
                                    "type": {
                                        "type": "string",
                                        "description": "Node type (required)",
                                    },
                                    "name": {
                                        "type": "string",
                                        "description": "Node name (required, 1-200 chars)",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Description (optional, max 2000 chars)",
                                    },
                                    "summary": {
                                        "type": "string",
                                        "description": "Short summary for visualization (optional, max 300 chars)",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Tags for categorization",
                                    },
                                    "subtypes": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Sub-classifications within the node type (optional, use existing subtypes when possible)",
                                    },
                                },
                            },
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {
                                        "type": "string",
                                        "description": "Source node ID or name",
                                    },
                                    "target": {
                                        "type": "string",
                                        "description": "Target node ID or name",
                                    },
                                    "type": {
                                        "type": "string",
                                        "description": "Relationship type (optional, defaults to RELATES_TO)",
                                    },
                                },
                            },
                        },
                    },
                    "required": ["nodes", "edges"],
                },
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
                            "additionalProperties": True,
                        },
                        "similar_nodes": {
                            "type": "array",
                            "description": "List of similar nodes found",
                            "items": {"type": "object", "additionalProperties": True},
                        },
                    },
                    "required": ["node", "similar_nodes"],
                },
            },
            {
                "name": "update_node",
                "description": "Update an existing node.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "ID of the node to update",
                        },
                        "updates": {
                            "type": "object",
                            "description": "Fields to update",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["node_id", "updates"],
                },
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
                            "description": "List of node IDs to delete",
                        },
                        "confirmed": {
                            "type": "boolean",
                            "description": "Must be True to execute deletion",
                            "default": False,
                        },
                    },
                    "required": ["node_ids"],
                },
            },
            {
                "name": "delete_edges",
                "description": "Delete edges from the graph.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "edge_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of edge IDs to delete (max 50)",
                        },
                        "confirmed": {
                            "type": "boolean",
                            "description": "Must be True to execute deletion",
                            "default": False,
                        },
                    },
                    "required": ["edge_ids"],
                },
            },
            {
                "name": "archive_nodes",
                "description": "Archive nodes: hide them from search/traversal by default without deleting. Reversible via unarchive_nodes. Prefer this over delete_nodes when the user wants to hide/retire a node rather than permanently remove it.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of node IDs to archive",
                        },
                    },
                    "required": ["node_ids"],
                },
            },
            {
                "name": "unarchive_nodes",
                "description": "Unarchive nodes: make previously archived nodes visible again. Find archived nodes first with search_graph(include_archived=true).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of node IDs to unarchive",
                        },
                    },
                    "required": ["node_ids"],
                },
            },
            {
                "name": "archive_edges",
                "description": "Archive edges: hide them from search/traversal by default without deleting. Reversible via unarchive_edges.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "edge_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of edge IDs to archive",
                        },
                    },
                    "required": ["edge_ids"],
                },
            },
            {
                "name": "unarchive_edges",
                "description": "Unarchive edges: make previously archived edges visible again.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "edge_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of edge IDs to unarchive",
                        },
                    },
                    "required": ["edge_ids"],
                },
            },
            {
                "name": "list_node_types",
                "description": "List all allowed node types.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_subtypes",
                "description": "Get existing subtypes used in the graph, grouped by node type. Use this to suggest consistent subtypes when adding or updating nodes.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "node_type": {
                            "type": "string",
                            "description": "Optional: filter subtypes for a specific node type (e.g. 'Actor')",
                        }
                    },
                },
            },
            {
                "name": "save_view",
                "description": "Save the current visualization state as a saved view. Use this when the user wants to save what they see now.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name for the saved view",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "get_saved_view",
                "description": "Load a saved view by name to display it in the visualization. Use when user wants to open/load/show a saved view.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the saved view to load",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "list_saved_views",
                "description": "List all saved views stored in the graph. Use this when user asks what saved views/visualizations exist in the database.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "clear_visualization",
                "description": "Clear the current visualization, removing all nodes and edges from the canvas. Use this when the user wants to clear, reset, or empty the visualization (Swedish: 'töm', 'rensa', 'ta bort allt från'). This does NOT delete nodes from the database - it only clears the visual display.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "mark_nodes",
                "description": "Apply a visual color annotation to specific nodes currently in the visualization. Marks are session-only overlays — they do NOT modify the graph database. Use any CSS color string and provide an optional label that describes what the color means. Marks appear as a colored badge on the node and are listed in a legend. Call with an empty 'marks' array to clear all marks. Useful for: highlighting findings, indicating priority, showing analysis results, categorizing nodes visually, etc.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "marks": {
                            "type": "array",
                            "description": "Nodes to mark. Pass an empty array to clear all marks.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "node_id": {
                                        "type": "string",
                                        "description": "ID of the node to mark",
                                    },
                                    "color": {
                                        "type": "string",
                                        "description": "CSS color (e.g. '#EF4444' red, '#F97316' orange, '#FBBF24' yellow, '#10B981' green, '#3B82F6' blue)",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "Short label shown in the legend (e.g. 'High priority', 'Needs review', 'Confirmed')",
                                    },
                                },
                                "required": ["node_id", "color"],
                            },
                        }
                    },
                    "required": ["marks"],
                },
            },
            {
                "name": "present_form",
                "description": (
                    "Render an interactive input form in the chat so the user can answer with GUI "
                    "controls (radio buttons, checkboxes, sliders, dropdowns) instead of free text. "
                    "Use this in a data-collection session whenever a question has a fixed set of "
                    "options or a bounded numeric range — it makes answering faster and keeps the "
                    "collected data consistent for later aggregation. After the user submits, you "
                    "receive their answers as a normal message; then call save_collection_response "
                    "to store them. Present one focused form at a time."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Optional short heading for the form",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional helper text shown above the fields",
                        },
                        "submit_label": {
                            "type": "string",
                            "description": "Optional label for the submit button (default 'Submit')",
                        },
                        "fields": {
                            "type": "array",
                            "description": "The input fields to render, in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "Stable machine key for the field (e.g. 'role'). Reused when saving the answer.",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "Question text shown to the user",
                                    },
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "text",
                                            "textarea",
                                            "number",
                                            "radio",
                                            "checkbox",
                                            "select",
                                            "slider",
                                            "boolean",
                                        ],
                                        "description": "Control type. radio/select = one choice; checkbox = multiple choices; slider = bounded number.",
                                    },
                                    "options": {
                                        "type": "array",
                                        "description": "Choices for radio/checkbox/select. Each item may be a string or {value, label}.",
                                        "items": {"type": ["string", "object"]},
                                    },
                                    "min": {
                                        "type": "number",
                                        "description": "Minimum for slider/number",
                                    },
                                    "max": {
                                        "type": "number",
                                        "description": "Maximum for slider/number",
                                    },
                                    "step": {
                                        "type": "number",
                                        "description": "Step for slider/number",
                                    },
                                    "required": {
                                        "type": "boolean",
                                        "description": "Whether an answer is mandatory",
                                    },
                                    "placeholder": {
                                        "type": "string",
                                        "description": "Placeholder for text/number fields",
                                    },
                                },
                                "required": ["id", "label", "type"],
                            },
                        },
                    },
                    "required": ["fields"],
                },
            },
            {
                "name": "save_collection_response",
                "description": (
                    "Persist one structured submission gathered in the current collection session as "
                    "a CollectionResponse node, linked to the active collection. Only available in "
                    "collection mode. Call this after the user submits a form (or answers the "
                    "equivalent questions in free text) so the answers are stored in a consistent, "
                    "aggregatable shape. Pass every answered field."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "answers": {
                            "type": "array",
                            "description": "The answered fields.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field_id": {
                                        "type": "string",
                                        "description": "Matches the form field 'id' (or a stable key you choose)",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "Human-readable question text",
                                    },
                                    "type": {
                                        "type": "string",
                                        "description": "Field type (radio, checkbox, slider, text, etc.)",
                                    },
                                    "value": {
                                        "description": "The submitted value: string, number, boolean, or array for multi-select"
                                    },
                                },
                                "required": ["field_id", "value"],
                            },
                        },
                        "respondent_label": {
                            "type": "string",
                            "description": "Optional label identifying the respondent (only if publicly appropriate — never store personal data without consent)",
                        },
                        "form_title": {
                            "type": "string",
                            "description": "Optional title of the form these answers came from",
                        },
                    },
                    "required": ["answers"],
                },
            },
            {
                "name": "get_schema",
                "description": "Get the complete schema configuration including all node types with their fields, colors, and descriptions, as well as all relationship types.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_presentation",
                "description": "Get the presentation configuration for the UI including colors, introduction text, and prompt settings.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]

    def _select_tool_definitions(
        self, tool_allowlist: Optional[List[str]]
    ) -> List[Dict]:
        """
        Return the tool definitions advertised to the LLM for this request.

        Mirrors the AIAgent tool-permission model (backend/agents/governance/
        gate.py): when ``tool_allowlist`` is unset/empty the full tool set is
        offered (unrestricted); when it is a non-empty list only tools whose
        name is in the list are advertised. This is the "hide from the LLM"
        layer — execution is independently gated in _handle_tool_use so a
        disallowed tool is blocked even if the model calls it anyway.
        """
        if not tool_allowlist:
            return self.tool_definitions
        allowed = set(tool_allowlist)
        return [t for t in self.tool_definitions if t.get("name") in allowed]

    def _resolve_llm_provider(
        self,
        api_key: Optional[str],
        provider: Optional[str],
        model_profile_id: Optional[str],
    ):
        """
        Resolve which LLMProvider instance to use for this request.

        When model profiles are configured (backend/config/model_profiles.py),
        profile resolution takes precedence over the legacy provider/api_key
        params — once profiles are configured they are the source of truth.
        Falls back unchanged to the legacy single-provider path when no
        profiles are configured.

        Returns:
            (llm_provider, error_message) — exactly one of the two is set.
        """
        profiles = config_loader.get_model_profiles()
        if profiles:
            selection_enabled = config_loader.get_model_profile_selection_enabled()
            effective_profile_id = model_profile_id if selection_enabled else None
            resolution = resolve_profile_reference(profiles, effective_profile_id)
            if resolution.profile is None:
                return (
                    None,
                    f"❌ Error: {resolution.error or 'no model profile available'}",
                )
            try:
                llm_provider = create_provider_from_profile(
                    resolution.profile, api_key_override=api_key
                )
            except Exception as e:
                return None, f"❌ Error: {e}"
            return llm_provider, None

        # Legacy single-provider path
        provider_to_use = provider if provider else self.provider_type
        key_to_use = api_key if api_key else self.default_api_key

        if not key_to_use:
            provider_name = provider_to_use.upper()
            return None, (
                f"❌ Error: No API key available. Please set {provider_name}_API_KEY "
                "environment variable or provide your own key in settings."
            )

        return create_provider(key_to_use, provider_to_use), None

    def process_message(
        self,
        messages: List[Dict],
        api_key: str = None,
        provider: str = None,
        model_profile_id: str = None,
        extra_context: str = None,
        skills_override: str = None,
        tools_override: Dict[str, Callable] = None,
        visualization_context: str = None,
        tool_allowlist: Optional[List[str]] = None,
    ) -> Dict:
        """
        Process a message history, call LLM, handle tools, return final response.

        Args:
            messages: Conversation history
            api_key: Optional API key to use instead of default
            provider: Optional provider override ('claude' or 'openai'). Ignored
                when model profiles are configured — model_profile_id applies then.
            model_profile_id: Optional explicit model profile id (see
                backend/config/model_profiles.py). Only used when profiles are
                configured; None inherits the application default profile.
            extra_context: Optional context prepended before the base system prompt
                (expert agent persona — should be established before base instructions).
            tools_override: Optional dict of tool_name → callable that replaces entries
                in self.tools_map for this request only (used for permission enforcement).
            skills_override: Optional user-selected skill instructions appended
                AFTER the base system prompt for recency precedence over defaults.
            visualization_context: Optional snapshot of the browser's current canvas state
                (visible node IDs, selected nodes). Appended last so it is the freshest
                context and helps the AI decide between add vs. replace actions.
            tool_allowlist: Optional explicit list of tool names the assistant may
                use for this request. Unset/empty means unrestricted (all tools).
                When set, only listed tools are advertised to the LLM and executed;
                any other tool is blocked server-side. Mirrors the AIAgent
                tool-permission model (used by the collection kiosk).
        """
        try:
            llm_provider, error = self._resolve_llm_provider(
                api_key, provider, model_profile_id
            )
            if error:
                return {"content": error, "toolUsed": None, "toolResult": None}

            # Build per-request system prompt:
            # 1. expert persona (extra_context) comes first — establishes who the model is
            # 2. base system prompt in the middle — tools, schema, behaviors
            # 3. skill overrides (skills_override) — recency precedence for behavioral overrides
            # 4. visualization_context comes last — most immediate situational snapshot
            active_system_prompt = (
                f"{extra_context}\n\n{self.system_prompt}"
                if extra_context
                else self.system_prompt
            )
            if skills_override:
                active_system_prompt = f"{active_system_prompt}\n\n{skills_override}"
            if visualization_context:
                active_system_prompt = (
                    f"{active_system_prompt}\n\n{visualization_context}"
                )

            # Tools advertised to the LLM: filtered by the per-request allowlist
            # (unrestricted when the allowlist is unset). Execution is gated
            # independently in _handle_tool_use.
            active_tool_definitions = self._select_tool_definitions(tool_allowlist)

            # First call to LLM
            response = llm_provider.create_completion(
                messages=messages,
                system_prompt=active_system_prompt,
                tools=active_tool_definitions,
                max_tokens=4096,
            )

            # Check if tool use
            if response.stop_reason == "tool_use":
                return self._handle_tool_use(
                    messages,
                    response,
                    llm_provider,
                    system_prompt=active_system_prompt,
                    tools_override=tools_override,
                    tool_allowlist=tool_allowlist,
                    active_tool_definitions=active_tool_definitions,
                )

            # Just text response
            # Extract text from content blocks
            text_content = ""
            for block in response.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content += block.get("text", "")

            return {
                "content": text_content if text_content else "No text response from AI",
                "toolUsed": None,
                "toolResult": None,
            }

        except Exception as e:
            logger.error(f"Error in process_message: {e}")
            error_msg = str(e)

            # Provide user-friendly message for rate limits
            if "rate_limit" in error_msg.lower() or "429" in error_msg:
                error_msg = (
                    "API rate limit reached. This happens when many nodes are processed simultaneously. "
                    "Try again in ~60 seconds, or request fewer nodes at a time (5-10)."
                )

            return {"content": error_msg, "toolUsed": None, "toolResult": None}

    def _handle_tool_use(
        self,
        messages: List[Dict],
        response,
        provider: LLMProvider,
        accumulated_nodes=None,
        accumulated_edges=None,
        system_prompt: str = None,
        tools_override: Dict[str, Callable] = None,
        pending_form=None,
        pending_extra_actions=None,
        visualization_action=None,
        tool_allowlist: Optional[List[str]] = None,
        active_tool_definitions: List[Dict] = None,
    ) -> Dict:
        """Handle tool use with support for tool chaining and result aggregation.

        pending_form carries a present_form action result across tool-chaining
        recursion so the form spec survives even when later tools return nodes/edges
        (which would otherwise take over final_tool_result and drop the form).

        pending_extra_actions carries pure-action tool results (mark_nodes,
        clear_visualization, start_guide, save_view) that co-occur with present_form
        in the same turn.  They are emitted as toolResult.extra_actions so the
        frontend can execute them without losing the form.

        visualization_action carries the most recent explicit view-content action
        (add/replace/load/clear) a tool requested, across node/edge accumulation
        and tool-chaining recursion. When the assistant accumulates nodes/edges
        into the single final tool result the per-tool ``action`` would otherwise
        be dropped, silently turning an additive "add X" request into a full-view
        replace. Preserving it — and defaulting to additive when unset — is what
        keeps a plain additive request from clearing the current view.
        """
        if accumulated_nodes is None:
            accumulated_nodes = []
        if accumulated_edges is None:
            accumulated_edges = []
        if pending_extra_actions is None:
            pending_extra_actions = []
        active_system_prompt = (
            system_prompt if system_prompt is not None else self.system_prompt
        )
        if active_tool_definitions is None:
            active_tool_definitions = self._select_tool_definitions(tool_allowlist)
        effective_tools = {**self.tools_map, **(tools_override or {})}

        # Find ALL tool_use blocks (LLM can request multiple tools in parallel)
        tool_uses = [
            block
            for block in response.content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

        if not tool_uses:
            # No tool uses found, shouldn't happen but handle gracefully
            return {
                "content": "No tool uses found in response",
                "toolUsed": None,
                "toolResult": None,
            }

        # Execute all tools
        tool_results = []
        last_tool_name = None

        for tool_use in tool_uses:
            tool_name = tool_use.get("name")
            tool_input = tool_use.get("input")
            tool_id = tool_use.get("id")
            last_tool_name = tool_name

            logger.info(f"Executing tool: {tool_name} with input: {tool_input}")

            # Server-side allowlist gate — mirrors AIAgent AutonomyGate.wrap
            # (backend/agents/governance/gate.py). A tool outside the request's
            # allowlist is blocked before any execution, including the pure-action
            # special cases below, so enforcement never depends on the LLM only
            # seeing the filtered tool list.
            if tool_allowlist is not None and tool_name not in tool_allowlist:
                tool_results.append(
                    {
                        "tool_use_id": tool_id,
                        "result": {
                            "error": (
                                f"Tool '{tool_name}' is not in this collection's "
                                f"tool allowlist and was blocked."
                            )
                        },
                    }
                )
                continue

            # Execute the tool
            tool_result = None

            # Special case for propose_new_node which is a helper tool, not in the graph
            if tool_name == "propose_new_node":
                tool_result = {
                    "proposed_node": tool_input.get("node"),
                    "similar_nodes": tool_input.get("similar_nodes"),
                    "requires_approval": True,
                }

            # Special case for clear_visualization - signals frontend to clear the canvas
            elif tool_name == "clear_visualization":
                tool_result = {
                    "action": "clear_visualization",
                    "success": True,
                    "message": "Visualization cleared",
                }

            # Special case for mark_nodes - signals frontend to apply color overlays
            elif tool_name == "mark_nodes":
                tool_result = {
                    "action": "mark_nodes",
                    "marks": tool_input.get("marks", []),
                }

            # Special case for present_form - signals frontend to render input widgets.
            # No graph access; the form spec is passed straight through for the client to render.
            elif tool_name == "present_form":
                tool_result = {
                    "action": "present_form",
                    "form": {
                        "title": tool_input.get("title"),
                        "description": tool_input.get("description"),
                        "submit_label": tool_input.get("submit_label"),
                        "fields": tool_input.get("fields", []),
                    },
                }

            elif tool_name in effective_tools:
                try:
                    # Call the actual python function
                    func = effective_tools[tool_name]

                    # Check signature
                    sig = inspect.signature(func)
                    valid_args = {
                        k: v for k, v in tool_input.items() if k in sig.parameters
                    }

                    tool_result = func(**valid_args)
                except Exception as e:
                    tool_result = {"error": str(e)}
            else:
                tool_result = {"error": f"Tool {tool_name} not found"}

            # Accumulate nodes and edges from tools that return them
            if tool_result and isinstance(tool_result, dict):
                if "nodes" in tool_result and isinstance(tool_result["nodes"], list):
                    # Add unique nodes (avoid duplicates by ID)
                    existing_ids = {
                        n.get("id")
                        for n in accumulated_nodes
                        if isinstance(n, dict) and "id" in n
                    }
                    for node in tool_result["nodes"]:
                        if (
                            isinstance(node, dict)
                            and node.get("id") not in existing_ids
                        ):
                            accumulated_nodes.append(node)
                            existing_ids.add(node.get("id"))

                if "edges" in tool_result and isinstance(tool_result["edges"], list):
                    # Add unique edges (avoid duplicates by ID)
                    existing_edge_ids = {
                        e.get("id")
                        for e in accumulated_edges
                        if isinstance(e, dict) and "id" in e
                    }
                    for edge in tool_result["edges"]:
                        if (
                            isinstance(edge, dict)
                            and edge.get("id") not in existing_edge_ids
                        ):
                            accumulated_edges.append(edge)
                            existing_edge_ids.add(edge.get("id"))

            # Remember the explicit view-content action a tool requested so it
            # survives node/edge accumulation into the single final tool result.
            # The last such action in the turn wins (e.g. clear then search =
            # clear-and-add); pure overlays like mark_nodes are excluded.
            if (
                isinstance(tool_result, dict)
                and tool_result.get("action") in _VIEW_CONTENT_ACTIONS
            ):
                visualization_action = tool_result["action"]

            # Preserve a present_form action so a later node/edge-returning tool in
            # the same turn (or a subsequent chained tool) cannot drop the form spec.
            if (
                isinstance(tool_result, dict)
                and tool_result.get("action") == "present_form"
            ):
                pending_form = tool_result

            # Collect pure-action tools (mark_nodes, clear_visualization, start_guide,
            # save_view, …) that co-occur with present_form.  When present_form wins
            # the single toolResult.action slot these would otherwise be silently
            # dropped; they are emitted as toolResult.extra_actions instead.
            elif (
                isinstance(tool_result, dict)
                and tool_result.get("action")
                and "nodes" not in tool_result
            ):
                pending_extra_actions.append(tool_result)

            # Store tool result with its ID for the response
            tool_results.append({"tool_use_id": tool_id, "result": tool_result})

        # Send the results back to LLM
        messages.append({"role": "assistant", "content": response.content})

        # Add all tool results in a single user message
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": json.dumps(tr["result"], default=str),
                    }
                    for tr in tool_results
                ],
            }
        )

        final_response = provider.create_completion(
            messages=messages,
            system_prompt=active_system_prompt,
            tools=active_tool_definitions,
            max_tokens=4096,
        )

        # Check if LLM wants to use another tool (tool chaining)
        if final_response.stop_reason == "tool_use":
            # LLM wants to use another tool - continue recursively with accumulated data
            return self._handle_tool_use(
                messages,
                final_response,
                provider,
                accumulated_nodes,
                accumulated_edges,
                system_prompt=active_system_prompt,
                tools_override=tools_override,
                pending_form=pending_form,
                pending_extra_actions=pending_extra_actions,
                visualization_action=visualization_action,
                tool_allowlist=tool_allowlist,
                active_tool_definitions=active_tool_definitions,
            )

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

        # Preserve the visualization intent across node/edge accumulation. The
        # per-tool ``action`` is otherwise dropped here, which silently turns an
        # additive request into a full-view replace on the frontend. Honour an
        # explicit action when the model set one; otherwise default to additive
        # so a plain "add X" only adds and never clears the current view.
        if final_tool_result:
            final_tool_result["action"] = visualization_action or "add_to_visualization"

        # If no accumulated data but we have tool results, use the last one
        if not final_tool_result and tool_results:
            final_tool_result = tool_results[-1]["result"]

        # A present_form action must always reach the client (it renders the input
        # widgets). Overlay it last so its action/form survive alongside any nodes/edges.
        if pending_form:
            if isinstance(final_tool_result, dict):
                final_tool_result = {**final_tool_result, **pending_form}
            else:
                final_tool_result = pending_form
            # Carry any co-occurring pure-action tools so the frontend can execute
            # them while still rendering the form (see toolResult.extra_actions).
            if pending_extra_actions:
                final_tool_result["extra_actions"] = list(pending_extra_actions)

        return {
            "content": final_text,
            "toolUsed": last_tool_name,  # Return the name of the last tool executed
            "toolResult": final_tool_result,
        }
