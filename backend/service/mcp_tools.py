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

from typing import List, Optional, Dict, Any, Callable

from .service import GraphService


def register_mcp_tools(mcp, service: GraphService) -> Dict[str, Callable]:
    """
    Register all GraphService methods as MCP tools.

    Args:
        mcp: FastMCP instance to register tools with
        service: GraphService instance to use for operations

    Returns:
        Dict mapping tool names to their functions (for ChatProcessor)
    """
    tools_map = {}

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
        communities: Optional[List[str]] = None,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        Search for nodes in the graph based on text query

        Args:
            query: Search text (matches against name, description, summary)
            node_types: List of node types to filter on (Actor, Initiative, etc.)
            communities: List of communities to filter on
            limit: Max number of results (default 50)

        Returns:
            Dict with matching nodes and edges connecting them
        """
        return service.search_graph(
            query=query,
            node_types=node_types,
            communities=communities,
            limit=limit
        )

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
        depth: int = 1
    ) -> Dict[str, Any]:
        """
        Get nodes connected to the given node

        Args:
            node_id: ID of the starting node
            relationship_types: List of relationship types to filter on
            depth: How many hops from the starting node (default 1)

        Returns:
            Dict with nodes and edges
        """
        return service.get_related_nodes(
            node_id=node_id,
            relationship_types=relationship_types,
            depth=depth
        )

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
        edges: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Add new nodes and edges to the graph

        Args:
            nodes: List of node objects to add
            edges: List of edge objects to add

        Returns:
            Dict with result (added_node_ids, added_edge_ids, success, message)
        """
        return service.add_nodes(nodes=nodes, edges=edges)

    @register_tool
    def update_node(node_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing node

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update (name, description, summary, communities, metadata)

        Returns:
            Dict with updated node or error
        """
        return service.update_node(node_id, updates)

    @register_tool
    def delete_nodes(
        node_ids: List[str],
        confirmed: bool = False
    ) -> Dict[str, Any]:
        """
        Delete nodes from the graph (max 10 at a time)

        SECURITY: Requires confirmed=True and max 10 nodes per call

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion

        Returns:
            Dict with result (deleted_node_ids, affected_edge_ids, success, message)
        """
        return service.delete_nodes(node_ids=node_ids, confirmed=confirmed)

    # ==================== Statistics & Metadata Tools ====================

    @register_tool
    def get_graph_stats(communities: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get statistics for the graph

        Args:
            communities: Optional list of communities to filter on

        Returns:
            Dict with statistics (total_nodes, total_edges, nodes_by_type, nodes_by_community)
        """
        return service.get_graph_stats(communities)

    @register_tool
    def list_node_types() -> Dict[str, Any]:
        """
        List all allowed node types according to the metamodel

        Returns:
            Dict with node types and their color coding
        """
        return service.list_node_types()

    @register_tool
    def list_relationship_types() -> Dict[str, Any]:
        """
        List all allowed relationship types

        Returns:
            Dict with relationship types
        """
        return service.list_relationship_types()

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
    def get_saved_view(name: str) -> Dict[str, Any]:
        """
        Get a saved view by name and load its content for display.

        This returns the actual nodes and edges that are part of the saved view,
        NOT the SavedView node itself. The SavedView node is just metadata
        storage - what the user wants to see is the content it references.

        Note: "Saved view" = a snapshot of nodes/edges/positions saved in the graph.
              "Current visualization" = what is currently displayed in the GUI.

        Args:
            name: Name of the saved view

        Returns:
            The nodes and edges to display in the visualization, with position and hidden node data
        """
        return service.get_saved_view(name)

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

    return tools_map
