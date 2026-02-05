"""
GraphService - Business logic layer for graph operations.

This module provides a unified service interface for graph operations,
independent of the transport protocol (REST, MCP, WebSocket, etc.).

Key design principles:
- No LLM calls - this layer only handles client/LLM requests
- Stateless operations - all state is managed by graph_core
- Consistent response format across all methods
- Thread-safe operations through graph_core
- Schema and presentation config are loaded from config_loader
"""

from typing import List, Optional, Dict, Any
from datetime import datetime

from backend.core import (
    GraphStorage, Node, Edge, NodeType, RelationshipType,
    SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult, NODE_COLORS,
    get_node_type_names, get_relationship_type_names, get_node_color,
    EventContext,
)

from backend import config_loader

from .serializers import (
    serialize_node, serialize_nodes,
    serialize_edge, serialize_edges,
    serialize_similar_node, serialize_similar_nodes,
    serialize_graph_stats, serialize_add_result, serialize_delete_result,
    serialize_to_json
)


# Node type descriptions for metadata
NODE_TYPE_DESCRIPTIONS = {
    NodeType.ACTOR: "Government agencies, organizations",
    NodeType.COMMUNITY: "Communities (eSam, Myndigheter, etc.)",
    NodeType.INITIATIVE: "Projects, collaborative activities",
    NodeType.CAPABILITY: "Capabilities (procurement, IT development, etc.)",
    NodeType.RESOURCE: "Outputs (reports, software, etc.)",
    NodeType.LEGISLATION: "Laws and directives (NIS2, GDPR, etc.)",
    NodeType.THEME: "Themes (AI, data strategies, etc.)",
    NodeType.SAVED_VIEW: "Saved graph view snapshots for quick navigation",
    NodeType.VISUALIZATION_VIEW: "Saved graph view snapshots (legacy)"
}

# Relationship type descriptions
RELATIONSHIP_TYPE_DESCRIPTIONS = {
    RelationshipType.BELONGS_TO: "Belongs to (actor belongs to community, initiative belongs to actor)",
    RelationshipType.IMPLEMENTS: "Implements (initiative implements legislation)",
    RelationshipType.PRODUCES: "Produces (initiative produces resource/capability)",
    RelationshipType.GOVERNED_BY: "Governed by (initiative governed by legislation)",
    RelationshipType.RELATES_TO: "Relates to (general connection)",
    RelationshipType.PART_OF: "Part of (component is part of larger whole)"
}


class GraphService:
    """
    Central service class for all graph operations.

    Wraps GraphStorage and provides a clean API for:
    - Searching and querying
    - CRUD operations
    - Similarity detection
    - Statistics
    - Saved views management

    This class does NOT make any LLM calls - it only handles
    requests from clients and LLMs through various protocols.
    """

    def __init__(self, storage: GraphStorage):
        """
        Initialize GraphService with a GraphStorage instance.

        Args:
            storage: A GraphStorage instance for persistence
        """
        self._storage = storage

    @property
    def storage(self) -> GraphStorage:
        """Access the underlying storage (for advanced use cases)."""
        return self._storage

    # ==================== Search Operations ====================

    def search_graph(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        communities: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Search for nodes in the graph based on text query.

        Args:
            query: Search text (matches against name, description, summary, tags)
            node_types: List of node types to filter on (Actor, Initiative, etc.)
            communities: List of communities to filter on
            limit: Max number of results (default 50)
            action: Optional action for frontend ('add_to_visualization' or 'replace_visualization')

        Returns:
            Dict with matching nodes, connecting edges, and search metadata
        """
        # Log search request
        print(f"SEARCH: query='{query}' types={node_types} communities={communities} limit={limit}")

        # Convert node_types to NodeType enum or keep as string for dynamic types
        type_filters = None
        if node_types:
            type_filters = [NodeType.from_string(t) for t in node_types]

        results = self._storage.search_nodes(
            query=query,
            node_types=type_filters,
            communities=communities,
            limit=limit
        )
        print(f"SEARCH: Found {len(results)} results")

        # Get node IDs for edge filtering
        result_node_ids = set(node.id for node in results)

        # Find edges connecting these nodes (either endpoint in results)
        connecting_edges = [
            edge for edge in self._storage.edges.values()
            if edge.source in result_node_ids or edge.target in result_node_ids
        ]

        result = {
            "nodes": serialize_nodes(results),
            "edges": serialize_edges(connecting_edges),
            "total": len(results),
            "query": query,
            "filters": {
                "node_types": node_types,
                "communities": communities
            }
        }

        # Include action if specified (for frontend to know how to display results)
        if action:
            result["action"] = action

        return result

    def get_node_details(self, node_id: str) -> Dict[str, Any]:
        """
        Get complete information about a specific node.

        Args:
            node_id: ID of the node

        Returns:
            Dict with node data or error
        """
        node = self._storage.get_node(node_id)

        if not node:
            return {
                "success": False,
                "error": f"Node with ID {node_id} not found"
            }

        return {
            "success": True,
            "node": serialize_node(node)
        }

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        depth: int = 1
    ) -> Dict[str, Any]:
        """
        Get nodes connected to the given node.

        Args:
            node_id: ID of the starting node
            relationship_types: List of relationship types to filter on
            depth: How many hops from the starting node (default 1)

        Returns:
            Dict with nodes and edges
        """
        # Convert relationship_types to enum
        rel_filters = None
        if relationship_types:
            rel_filters = [RelationshipType(r) for r in relationship_types]

        result = self._storage.get_related_nodes(
            node_id=node_id,
            relationship_types=rel_filters,
            depth=depth
        )

        return {
            "nodes": serialize_nodes(result['nodes']),
            "edges": serialize_edges(result['edges']),
            "total_nodes": len(result['nodes']),
            "total_edges": len(result['edges']),
            "depth": depth
        }

    # ==================== Similarity Operations ====================

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        Find similar nodes based on name (for duplicate detection).

        Args:
            name: The name to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results (default 5)

        Returns:
            Dict with similar nodes and similarity scores
        """
        type_filter = NodeType.from_string(node_type) if node_type else None

        similar = self._storage.find_similar_nodes(
            name=name,
            node_type=type_filter,
            threshold=threshold,
            limit=limit
        )

        return {
            "similar_nodes": serialize_similar_nodes(similar),
            "total": len(similar),
            "search_name": name
        }

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        Find similar nodes for multiple names at once (batch processing).

        Much more efficient than calling find_similar_nodes multiple times.

        Args:
            names: List of names to search for similar nodes
            node_type: Optional node type to filter on
            threshold: Similarity threshold 0.0-1.0 (default 0.7)
            limit: Max number of results per name (default 5)

        Returns:
            Dict with results for each name
        """
        type_filter = NodeType.from_string(node_type) if node_type else None

        results = self._storage.find_similar_nodes_batch(
            names=names,
            node_type=type_filter,
            threshold=threshold,
            limit=limit
        )

        # Format results for JSON
        formatted_results = {}
        for name, similar_list in results.items():
            formatted_results[name] = {
                "similar_nodes": serialize_similar_nodes(similar_list),
                "total": len(similar_list)
            }

        return {
            "results": formatted_results,
            "total_searched": len(names),
            "message": f"Searched for {len(names)} names"
        }

    # ==================== CRUD Operations ====================

    def add_nodes(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add new nodes and edges to the graph.

        Args:
            nodes: List of node dictionaries to add
            edges: List of edge dictionaries to add
            event_origin: Source of the mutation (web-ui, mcp, system, agent:<id>)
            event_session_id: Unique session ID for loop prevention
            event_correlation_id: Correlation ID for chaining related events

        Returns:
            Dict with result (added_node_ids, added_edge_ids, success, message)
        """
        # Convert dicts to Node and Edge objects
        try:
            node_objects = [Node(**n) for n in nodes]
            edge_objects = [Edge(**e) for e in edges]
        except Exception as e:
            return {
                "success": False,
                "message": f"Error validating input: {str(e)}",
                "added_node_ids": [],
                "added_edge_ids": []
            }

        # Create event context if any event parameters provided
        event_context = None
        if event_origin or event_session_id or event_correlation_id:
            event_context = EventContext(
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
            )

        result = self._storage.add_nodes(node_objects, edge_objects, event_context=event_context)
        return serialize_add_result(result)

    def update_node(
        self,
        node_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing node.

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update (name, description, summary, communities, tags, metadata)
            event_origin: Source of the mutation (web-ui, mcp, system, agent:<id>)
            event_session_id: Unique session ID for loop prevention
            event_correlation_id: Correlation ID for chaining related events

        Returns:
            Dict with updated node or error
        """
        # Create event context if any event parameters provided
        event_context = None
        if event_origin or event_session_id or event_correlation_id:
            event_context = EventContext(
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
            )

        updated_node = self._storage.update_node(node_id, updates, event_context=event_context)

        if not updated_node:
            return {
                "success": False,
                "error": f"Node with ID {node_id} not found"
            }

        return {
            "success": True,
            "node": serialize_node(updated_node)
        }

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete nodes from the graph (max 10 at a time).

        SECURITY: Requires confirmed=True and max 10 nodes per call.

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion
            event_origin: Source of the mutation (web-ui, mcp, system, agent:<id>)
            event_session_id: Unique session ID for loop prevention
            event_correlation_id: Correlation ID for chaining related events

        Returns:
            Dict with result (deleted_node_ids, affected_edge_ids, success, message)
        """
        # Create event context if any event parameters provided
        event_context = None
        if event_origin or event_session_id or event_correlation_id:
            event_context = EventContext(
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
            )

        result = self._storage.delete_nodes(node_ids, confirmed, event_context=event_context)
        return serialize_delete_result(result)

    # ==================== Statistics & Metadata ====================

    def get_graph_stats(self, communities: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get statistics for the graph.

        Args:
            communities: Optional list of communities to filter on

        Returns:
            Dict with statistics (total_nodes, total_edges, nodes_by_type, nodes_by_community)
        """
        stats = self._storage.get_stats(communities)
        return serialize_graph_stats(stats)

    def list_node_types(self) -> Dict[str, Any]:
        """
        List all allowed node types according to the schema config.

        Returns:
            Dict with node types and their color coding
        """
        schema = config_loader.get_schema()
        node_types = []

        for type_name, type_config in schema.get("node_types", {}).items():
            node_types.append({
                "type": type_name,
                "color": type_config.get("color", "#9CA3AF"),
                "description": type_config.get("description", ""),
                "fields": type_config.get("fields", []),
                "static": type_config.get("static", False)
            })

        return {"node_types": node_types}

    def list_relationship_types(self) -> Dict[str, Any]:
        """
        List all allowed relationship types according to schema config.

        Returns:
            Dict with relationship types
        """
        schema = config_loader.get_schema()
        relationship_types = []

        for type_name, type_config in schema.get("relationship_types", {}).items():
            relationship_types.append({
                "type": type_name,
                "description": type_config.get("description", "")
            })

        return {"relationship_types": relationship_types}

    def get_schema(self) -> Dict[str, Any]:
        """
        Get the complete schema configuration.

        Returns:
            Dict with node_types and relationship_types
        """
        return config_loader.get_schema()

    def get_presentation(self) -> Dict[str, Any]:
        """
        Get the presentation configuration.

        Returns:
            Dict with title, introduction, colors, prompt_prefix, prompt_suffix
        """
        return config_loader.get_presentation()

    # ==================== Saved Views ====================

    def save_view(self, name: str) -> Dict[str, Any]:
        """
        Signal intent to save the current view state.

        This does NOT save the view data itself because the backend
        does not know the client state. It acts as a signal for the
        frontend to capture and save the current visualization state.

        Args:
            name: Name of the view to save

        Returns:
            A signal object that the frontend will intercept
        """
        return {
            "action": "save_view",
            "name": name,
            "message": f"Ready to save view '{name}'. Client will capture current visualization state."
        }

    def get_saved_view(self, name: str) -> Dict[str, Any]:
        """
        Get a saved view by name and load its content for display.

        Returns the actual nodes and edges that are part of the saved view,
        NOT the SavedView node itself.

        Args:
            name: Name of the saved view

        Returns:
            The nodes and edges to display with position data
        """
        # Search for SavedView node with the given name
        results = self._storage.search_nodes(
            query=name,
            node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
            limit=1
        )

        if not results:
            return {
                "success": False,
                "error": f"View '{name}' not found."
            }

        view_node = results[0]

        # Support both old and new formats
        position_map = {}
        node_ids = []
        hidden_node_ids = []

        # Try new format first
        view_data = view_node.metadata.get('view_data', {})
        if view_data and 'nodes' in view_data:
            node_position_data = view_data.get('nodes', [])
            hidden_node_ids = view_data.get('hidden_nodes', [])
            position_map = {
                item['id']: item.get('position')
                for item in node_position_data
                if isinstance(item, dict)
            }
            node_ids = list(position_map.keys())
        # Fall back to old format
        elif 'node_ids' in view_node.metadata:
            node_ids = view_node.metadata.get('node_ids', [])
            position_map = view_node.metadata.get('positions', {})
            hidden_node_ids = view_node.metadata.get('hidden_nodes', [])
        else:
            return {
                "success": False,
                "error": f"View '{name}' contains no nodes."
            }

        # Filter out group IDs (frontend-only concept)
        actual_node_ids = [nid for nid in node_ids if not nid.startswith('group-')]
        group_ids = [nid for nid in node_ids if nid.startswith('group-')]

        # Fetch all the actual nodes
        nodes = []
        for node_id in actual_node_ids:
            node = self._storage.get_node(node_id)
            if node:
                nodes.append(serialize_node(node))

        if not nodes:
            return {
                "success": False,
                "error": f"No nodes could be loaded from view '{name}'. The referenced nodes may have been deleted."
            }

        # Get all edges between these nodes
        edges = serialize_edges(
            self._storage.get_edges_between_nodes(actual_node_ids)
        )

        # Extract group positions for frontend
        group_data = []
        for group_id in group_ids:
            group_position = position_map.get(group_id)
            if group_position:
                group_data.append({
                    "id": group_id,
                    "position": group_position
                })

        return {
            "success": True,
            "nodes": nodes,
            "edges": edges,
            "positions": position_map,
            "hidden_node_ids": hidden_node_ids,
            "groups": group_data,
            "action": "load_visualization"
        }

    def list_saved_views(self) -> Dict[str, Any]:
        """
        List all saved views.

        Returns:
            List of all SavedView nodes with their names and summaries
        """
        views = self._storage.search_nodes(
            query="",
            node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
            limit=100
        )

        view_list = []
        for view in views:
            node_count = (
                len(view.metadata.get('node_ids', []))
                if 'node_ids' in view.metadata
                else len(view.metadata.get('view_data', {}).get('nodes', []))
            )
            view_info = {
                "name": view.name,
                "description": view.description,
                "summary": view.summary,
                "created_at": view.created_at.isoformat() if view.created_at else None,
                "node_count": node_count
            }
            view_list.append(view_info)

        return {
            "success": True,
            "views": view_list,
            "total": len(view_list)
        }

    # ==================== Export ====================

    def export_graph(self) -> Dict[str, Any]:
        """
        Export the entire graph (all nodes and edges).

        Returns:
            Complete graph data in JSON format
        """
        all_nodes = serialize_nodes(list(self._storage.nodes.values()))
        all_edges = serialize_edges(list(self._storage.edges.values()))

        return {
            "version": "1.0",
            "exportDate": datetime.utcnow().isoformat(),
            "nodes": all_nodes,
            "edges": all_edges,
            "total_nodes": len(all_nodes),
            "total_edges": len(all_edges)
        }
