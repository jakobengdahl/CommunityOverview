"""
MCP (Model Context Protocol) tools registration for graph operations.

This module registers all GraphService methods as MCP tools that can be
called by LLMs through the MCP protocol.

Usage:
    from mcp.server.fastmcp import FastMCP
    from backend.service import GraphService, register_mcp_tools
    from backend.core import GraphStorage

    mcp = FastMCP("community-knowledge-graph")
    storage = GraphStorage("graph.json")
    service = GraphService(storage)
    tools_map = register_mcp_tools(mcp, service)
"""

import secrets
from typing import List, Optional, Dict, Any, Callable

from .service import GraphService


def register_mcp_tools(mcp, service: GraphService, session_registry=None, session_manager=None) -> Dict[str, Callable]:
    """
    Register all GraphService methods as MCP tools.

    Args:
        mcp: FastMCP instance to register tools with
        service: GraphService instance to use for operations
        session_registry: legacy single-consumer visualization push registry
        session_manager: new shared-session manager; pushes are additionally
            broadcast to its hub subscribers so an AI agent is just another
            collaborator (design 3.8)

    Returns:
        Dict mapping tool names to their functions (for ChatProcessor)
    """
    tools_map = {}

    def _push(session_id, tool_name, result):
        _push_to_session(session_registry, session_id, tool_name, result, session_manager)

    def _session_view_state(session_id):
        """Return ``(visible_node_ids, selected_node_ids)`` as the server sees them.

        Session state is server-owned (design §3.8): visible nodes come from the
        shared-session store's node references, the current selection from the
        advisory claim map. The browser no longer uploads canvas state — an MCP
        tool reads the same state every collaborator converges on.
        """
        visible: list = []
        selected: list = []
        if session_manager is not None:
            session = session_manager.get_session(session_id)
            if session is not None:
                hidden = set(session.state.get("hidden_node_ids", []))
                visible = [n for n in session.state.get("node_refs", []) if n not in hidden]
            selected = list(session_manager.claimed_elements(session_id))
        return visible, selected

    def register_tool(func: Callable) -> Callable:
        """Register a function as both MCP tool and in tools_map."""
        mcp.tool()(func)
        tools_map[func.__name__] = func
        return func

    # ==================== Search Tools ====================

    @register_tool
    def search_graph(
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search for nodes in the graph based on text query

        Args:
            query: Search text (matches against name, description, summary)
            node_types: List of node types to filter on (Actor, Initiative, etc.)
            limit: Max number of results (default 50)
            action: Optional action for frontend ('add_to_visualization' to add to current view)
            visualization_session_id: Optional browser session ID — when provided, the result
                is pushed live to the connected browser window via SSE

        Returns:
            Dict with matching nodes and edges connecting them
        """
        result = service.search_graph(
            query=query,
            node_types=node_types,
            limit=limit,
            action=action,
            federation_depth=federation_depth,
        )
        _push(visualization_session_id, "search_graph", result)
        return result

    @register_tool
    def get_node_details(node_id: str) -> Dict[str, Any]:
        """
        Get complete information about a specific node

        Args:
            node_id: ID of the node

        Returns:
            Dict with node data or error
        """
        return service.get_node_details(node_id)

    @register_tool
    def get_related_nodes(
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        depth: int = 1,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get nodes connected to the given node

        Args:
            node_id: ID of the starting node
            relationship_types: List of relationship types to filter on
            depth: How many hops from the starting node (default 1)
            visualization_session_id: Optional browser session ID — when provided, the result
                is pushed live to the connected browser window via SSE

        Returns:
            Dict with nodes and edges
        """
        result = service.get_related_nodes(
            node_id=node_id,
            relationship_types=relationship_types,
            depth=depth,
        )
        _push(visualization_session_id, "get_related_nodes", result)
        return result

    # ==================== Similarity Tools ====================

    @register_tool
    def find_similar_nodes(
        name: str,
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        Find similar nodes based on name (for duplicate detection)

        Args:
            name: The name to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results (default 5)

        Returns:
            Dict with similar nodes and similarity scores
        """
        return service.find_similar_nodes(
            name=name,
            node_type=node_type,
            threshold=threshold,
            limit=limit
        )

    @register_tool
    def find_similar_nodes_batch(
        names: List[str],
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        Find similar nodes for multiple names at once (batch processing)

        This is MUCH more efficient than calling find_similar_nodes multiple times
        when processing documents with many entities. Use this when extracting
        multiple nodes from a document.

        Args:
            names: List of names to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results per name (default 5)

        Returns:
            Dict with results for each name
        """
        return service.find_similar_nodes_batch(
            names=names,
            node_type=node_type,
            threshold=threshold,
            limit=limit
        )

    # ==================== CRUD Tools ====================

    @register_tool
    def add_nodes(
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add new nodes and edges to the graph

        Field limits for nodes:
          - name: required, 1-200 characters
          - description: optional, max 2000 characters
          - summary: optional, max 300 characters (short text for visualization)
          - tags: optional list of strings
          - subtypes: optional list of strings for sub-classification within the node type
          - aliases: optional list of alternative names/synonyms; also matched in search

        Edge type is optional. If omitted, it defaults to "RELATES_TO".

        Args:
            nodes: List of node objects to add
            edges: List of edge objects (source, target, type). Type is optional.
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (added_node_ids, added_edge_ids, success, message)
        """
        return service.add_nodes(
            nodes=nodes,
            edges=edges,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def update_node(
        node_id: str,
        updates: Dict[str, Any],
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing node

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update (name, description, summary, tags, aliases, metadata)
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with updated node or error
        """
        return service.update_node(
            node_id,
            updates,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def delete_nodes(
        node_ids: List[str],
        confirmed: bool = False,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete nodes from the graph (max 10 at a time)

        SECURITY: Requires confirmed=True and max 10 nodes per call

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (deleted_node_ids, affected_edge_ids, success, message)
        """
        return service.delete_nodes(
            node_ids=node_ids,
            confirmed=confirmed,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    @register_tool
    def delete_edges(
        edge_ids: List[str],
        confirmed: bool = False,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete edges from the graph (max 50 at a time)

        Args:
            edge_ids: List of edge IDs to delete
            confirmed: Must be True to execute deletion
            event_session_id: Optional session ID for webhook loop prevention
            event_correlation_id: Optional correlation ID for chaining events

        Returns:
            Dict with result (deleted_edge_ids, success, message)
        """
        return service.delete_edges(
            edge_ids=edge_ids,
            confirmed=confirmed,
            event_origin="mcp",
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    # ==================== Statistics & Metadata Tools ====================

    @register_tool
    def get_graph_stats() -> Dict[str, Any]:
        """
        Get statistics for the graph

        Returns:
            Dict with statistics (total_nodes, total_edges, nodes_by_type)
        """
        return service.get_graph_stats()

    @register_tool
    def list_node_types() -> Dict[str, Any]:
        """
        List all allowed node types according to the metamodel

        Returns:
            Dict with node types and their color coding
        """
        return service.list_node_types()

    @register_tool
    def get_subtypes(node_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Get existing subtypes used in the graph, grouped by node type.
        Use this to suggest consistent subtypes when adding or updating nodes.

        Args:
            node_type: Optional filter for a specific node type (e.g. 'Actor')

        Returns:
            Dict with subtypes grouped by node type
        """
        return service.get_subtypes(node_type)

    @register_tool
    def list_relationship_types() -> Dict[str, Any]:
        """
        List all allowed relationship types

        Returns:
            Dict with relationship types
        """
        return service.list_relationship_types()

    @register_tool
    def get_schema() -> Dict[str, Any]:
        """
        Get the complete schema configuration.

        Returns the full schema including all node types with their fields,
        colors, and descriptions, as well as all relationship types.

        Returns:
            Dict with node_types and relationship_types
        """
        return service.get_schema()

    @register_tool
    def get_presentation() -> Dict[str, Any]:
        """
        Get the presentation configuration for the UI.

        Returns settings for UI display including colors, introduction text,
        and prompt configuration.

        Returns:
            Dict with title, introduction, colors, prompt_prefix, prompt_suffix
        """
        return service.get_presentation()

    @register_tool
    def get_capabilities() -> Dict[str, Any]:
        """Get the public capability manifest for client discovery."""
        return service.get_capabilities()

    @register_tool
    def get_runtime_info() -> Dict[str, Any]:
        """Get the public runtime metadata for deployment introspection."""
        return service.get_runtime_info()

    @register_tool
    def get_tenant_context() -> Dict[str, Any]:
        """Get the tenant/deployment context metadata.

        Returns the tenant identifier, name, and deployment environment
        for this CommunityOverview instance.

        Returns:
            Dict with tenant_id, tenant_name, and environment
        """
        return service.get_tenant_context()

    @register_tool
    def get_config_context() -> Dict[str, Any]:
        """Get the effective config scope and non-sensitive config source metadata."""
        return service.get_config_context()

    @register_tool
    def get_request_actor(
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        auth_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public request actor context with optional safe overrides."""
        return service.get_request_actor_info(
            actor_id=actor_id,
            actor_type=actor_type,
            auth_source=auth_source,
        )

    @register_tool
    def get_request_scope(
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public workspace/graph scope context with optional safe overrides."""
        return service.get_request_scope_info(
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    @register_tool
    def get_request_selection(
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public graph/workspace selection summary with optional safe overrides."""
        return service.get_request_graph_selection_info(
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    # ==================== Saved Views Tools ====================

    @register_tool
    def save_view(name: str) -> Dict[str, Any]:
        """
        Signal intent to save the current view state.

        This tool does NOT save the view data itself (positions, etc.) because
        the backend does not know the client state. Instead, it acts as a signal
        for the frontend to capture the current visualization state and save it
        as a SavedView.

        Args:
            name: Name of the view to save

        Returns:
            A signal object that the frontend will intercept.
        """
        return service.save_view(name)

    @register_tool
    def get_saved_view(
        name: str,
        visualization_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get a saved view by name and load its content for display.

        This returns the actual nodes and edges that are part of the saved view,
        NOT the SavedView node itself. The SavedView node is just metadata
        storage - what the user wants to see is the content it references.

        Note: "Saved view" = a snapshot of nodes/edges/positions saved in the graph.
              "Current visualization" = what is currently displayed in the GUI.

        Args:
            name: Name of the saved view
            visualization_session_id: Optional browser session ID — when provided, the view
                is loaded live in the connected browser window via SSE

        Returns:
            The nodes and edges to display in the visualization, with position and hidden node data
        """
        result = service.get_saved_view(name)
        _push(visualization_session_id, "get_saved_view", result)
        return result

    @register_tool
    def list_saved_views() -> Dict[str, Any]:
        """
        List all saved views.

        Returns a list of all saved view snapshots stored in the graph.
        These are NOT the current visualization - they are saved snapshots
        that can be loaded to restore a specific graph view.

        Returns:
            List of all SavedView nodes with their names and summaries
        """
        return service.list_saved_views()

    # ==================== Visualization Session Tools ====================

    @register_tool
    def clear_visualization(visualization_session_id: str) -> Dict[str, Any]:
        """
        Clear all nodes, edges, and annotations from the visualization canvas.

        Removes everything currently displayed in the browser window without
        affecting the underlying graph data. Use this to start a fresh view.

        Args:
            visualization_session_id: The browser session ID shown in the header
                (e.g. "8244-1742")

        Returns:
            Dict with success status and message
        """
        if not session_registry:
            return {"success": False, "error": "Session registry not available"}
        if not session_registry.is_valid_session_id(visualization_session_id):
            return {"success": False, "error": "Invalid session ID format — expected DDDD-DDDD"}
        if not session_registry.session_exists(visualization_session_id):
            return {
                "success": False,
                "error": (
                    f"Session '{visualization_session_id}' not found. "
                    "Call connect_to_visualization_session first to verify the session is open."
                ),
            }
        result = {"action": "clear_visualization", "nodes": [], "edges": [], "success": True}
        _push(visualization_session_id, "clear_visualization", result)
        return {
            "success": True,
            "message": f"Canvas cleared in session '{visualization_session_id}'",
        }

    @register_tool
    def connect_to_visualization_session(session_id: str) -> Dict[str, Any]:
        """
        Verify that a browser visualization session is open and ready.

        Use this tool first to confirm the session ID before using the
        visualization_session_id parameter in other tools.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")

        Returns:
            Dict with connected status and current canvas summary
        """
        if not session_registry:
            return {"connected": False, "error": "Session registry not available"}
        if not session_registry.is_valid_session_id(session_id):
            return {"connected": False, "error": "Invalid session ID format — expected DDDD-DDDD"}
        if not session_registry.session_exists(session_id):
            return {
                "connected": False,
                "message": (
                    f"Session '{session_id}' not found. "
                    "Open the application in a browser and use the displayed session ID."
                ),
            }
        visible, _ = _session_view_state(session_id)
        return {
            "connected": True,
            "session_id": session_id,
            "message": (
                f"Session '{session_id}' is active. "
                "You can now pass visualization_session_id to search_graph, "
                "get_related_nodes, get_saved_view, and clear_visualization."
            ),
            "visible_node_count": len(visible),
        }

    @register_tool
    def get_visualization_session_state(session_id: str) -> Dict[str, Any]:
        """
        Get the current visualization state from an open browser session.

        Returns the node IDs currently displayed and selected in the canvas.
        Use this to understand what the user is looking at before deciding
        which nodes to add or which view to load.

        Args:
            session_id: The session ID shown in the browser header (e.g. "8244-1742")

        Returns:
            Dict with visible_node_ids, selected_node_ids, and node_count
        """
        if not session_registry:
            return {"error": "Session registry not available"}
        if not session_registry.is_valid_session_id(session_id):
            return {"error": "Invalid session ID format — expected DDDD-DDDD"}
        if not session_registry.session_exists(session_id):
            return {
                "error": (
                    f"Session '{session_id}' not found. "
                    "Call connect_to_visualization_session first to verify the session is open."
                )
            }
        visible, selected = _session_view_state(session_id)
        return {
            "session_id": session_id,
            "visible_node_ids": visible,
            "selected_node_ids": selected,
            "node_count": len(visible),
        }

    return tools_map


def _push_to_session(
    session_registry,
    session_id: Optional[str],
    tool_name: str,
    result: Dict[str, Any],
    session_manager=None,
) -> None:
    """Push *result* to a browser session if *session_id* is set.

    When the result has nodes but no explicit *action*, defaults to
    "add_to_visualization" so external AI tools add to the canvas rather
    than silently replacing it.

    The command goes to the legacy single-consumer registry (current frontend)
    and, when a *session_manager* is supplied, is also broadcast to the new
    shared-session hub so every connected collaborator receives it (design 3.8).
    """
    if not session_id:
        return
    command_result = dict(result)
    if "action" not in command_result and command_result.get("nodes"):
        command_result["action"] = "add_to_visualization"
    # A unique id lets the browser dedupe the legacy stream and the hub
    # broadcast delivering the same push during the handover between them
    # (design §8.1 R5) without mistaking a later, genuinely repeated command
    # for a duplicate of this one.
    command = {
        "type": "tool_result",
        "tool": tool_name,
        "result": command_result,
        "command_id": secrets.token_hex(8),
    }
    if session_registry and session_registry.is_valid_session_id(session_id):
        session_registry.push_command_sync(session_id, command)
    if session_manager is not None:
        try:
            session_manager.push_command(session_id, command)
        except Exception:
            # Best-effort mirror to the hub; never break the legacy push path.
            pass
