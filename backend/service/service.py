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

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from collections import Counter

from backend.core import (
    GraphStorage, Node, Edge, NodeType, RelationshipType,
    SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult, NODE_COLORS,
    get_node_type_names, get_relationship_type_names, get_node_color,
    EventContext, EventAttribution, EventActorAttribution, EventScopeAttribution,
)

from backend import config_loader
from backend.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    DefaultGraphAuthorizationHook,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
    GraphAuthorizationHook,
    build_graph_authorization_context,
)
from backend.federation import FederationManager

# Initialize logger
logger = logging.getLogger(__name__)

from .serializers import (
    serialize_node, serialize_nodes,
    serialize_edge, serialize_edges,
    serialize_similar_node, serialize_similar_nodes,
    serialize_graph_stats, serialize_add_result, serialize_delete_result,
    serialize_delete_edges_result,
    serialize_to_json
)


# Node type descriptions for metadata
NODE_TYPE_DESCRIPTIONS = {
    NodeType.ACTOR: "Government agencies, organizations",
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

    def __init__(
        self,
        storage: GraphStorage,
        federation_manager: Optional[FederationManager] = None,
        authorization_hook: Optional[GraphAuthorizationHook] = None,
    ):
        """
        Initialize GraphService with a GraphStorage instance.

        Args:
            storage: A GraphStorage instance for persistence
        """
        self._storage = storage
        self._federation_manager = federation_manager
        self._authorization_hook = authorization_hook or DefaultGraphAuthorizationHook()

    def _evaluate_graph_access(self, *, action: str, target: str) -> GraphAuthorizationDecision:
        """Evaluate the configured authorization seam for graph reads and mutations."""
        return self._authorization_hook.evaluate(
            build_graph_authorization_context(action=action, target=target)
        )

    def _build_access_denied_result(
        self,
        *,
        action: str,
        target: str,
        decision: GraphAuthorizationDecision,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "error": "Graph access denied",
            "message": decision.reason or "Graph access denied.",
            "error_code": "access_denied",
            "authorization": {
                "action": action,
                "target": target,
                "mode": decision.mode,
                "source": decision.source,
            },
        }

    @staticmethod
    def _node_graph_id(node: Node) -> str:
        return str(((node.metadata or {}).get("origin_graph_id") or "")).strip()

    def _is_node_visible(self, node: Optional[Node], graph_access: GraphAccessNarrowing) -> bool:
        if node is None:
            return False
        return graph_access.matches(graph_id=self._node_graph_id(node))

    def _filter_nodes_and_edges(
        self,
        *,
        nodes: List[Node],
        edges: List[Edge],
        graph_access: GraphAccessNarrowing,
    ) -> tuple[List[Node], List[Edge]]:
        visible_nodes = [node for node in nodes if self._is_node_visible(node, graph_access)]
        visible_node_ids = {node.id for node in visible_nodes}
        visible_edges = [
            edge for edge in edges
            if edge.source in visible_node_ids and edge.target in visible_node_ids
        ]
        return visible_nodes, visible_edges

    def _authorize_graph_access(self, *, action: str, target: str) -> Optional[Dict[str, Any]]:
        decision = self._evaluate_graph_access(action=action, target=target)
        if decision.allowed:
            return None
        return self._build_access_denied_result(action=action, target=target, decision=decision)

    def _get_visible_local_graph_stats(self, graph_access: GraphAccessNarrowing) -> GraphStats:
        visible_nodes, visible_edges = self._filter_nodes_and_edges(
            nodes=self._storage.get_all_nodes(),
            edges=self._storage.get_all_edges(),
            graph_access=graph_access,
        )
        nodes_by_type = Counter(
            node.type.value if hasattr(node.type, "value") else str(node.type)
            for node in visible_nodes
        )
        return GraphStats(
            total_nodes=len(visible_nodes),
            total_edges=len(visible_edges),
            nodes_by_type=dict(nodes_by_type),
            last_updated=datetime.now(timezone.utc),
        )

    def _get_visible_federation_graph_display_names(self, graph_access: GraphAccessNarrowing) -> Dict[str, str]:
        local_graph_name = self._storage.get_graph_name()
        graph_display_names: Dict[str, str] = {}
        if graph_access.matches(graph_id=""):
            graph_display_names["local"] = local_graph_name
        if self._federation_manager and self._federation_manager.enabled:
            for graph_id, display_name in self._federation_manager.get_graph_display_names().items():
                if graph_access.matches(graph_id=graph_id):
                    graph_display_names[graph_id] = display_name
        return graph_display_names

    def _get_federated_search_limit(self, minimum_limit: int, graph_access: GraphAccessNarrowing) -> int:
        if not self._federation_manager or not self._federation_manager.enabled or not graph_access.enabled:
            return minimum_limit
        return max(
            minimum_limit,
            sum(len(entry.nodes) for entry in self._federation_manager._cache.values()),
        )

    def _build_export_boundary_summary(
        self,
        *,
        target: str,
        decision: GraphAuthorizationDecision,
        visible_nodes: List[Node],
        visible_edges: List[Edge],
        total_nodes: int,
        total_edges: int,
    ) -> Dict[str, Any]:
        """Build a public-safe export boundary summary for restore/admin workflows."""
        request_context = build_graph_authorization_context(action=GRAPH_ACTION_READ, target=target)
        selection_summary = self.get_request_graph_selection_info(
            workspace_id=request_context.scope.get("workspace_id"),
            workspace_kind=request_context.scope.get("workspace_kind"),
            graph_id=request_context.scope.get("graph_id"),
        )

        narrowed = decision.graph_access.enabled
        local_graph_included = decision.graph_access.matches(graph_id="")
        included_graph_count = len(decision.graph_access.include_graph_ids)
        scope_kind = selection_summary.get("workspace_kind") or (
            "graph" if selection_summary.get("has_graph") else "standalone"
        )

        return {
            "contract_version": "1.0",
            "export_kind": "narrowed" if narrowed else "full",
            "is_narrowed": narrowed,
            "scope_kind": scope_kind,
            "selection_mode": selection_summary.get("selection_mode", "default"),
            "selection_source": selection_summary.get("selection_source", "default"),
            "has_workspace_selection": selection_summary.get("has_workspace", False),
            "has_graph_selection": selection_summary.get("has_graph", False),
            "graph_scope": {
                "local_graph_included": local_graph_included,
                "included_graph_count": included_graph_count,
            },
            "counts": {
                "nodes": len(visible_nodes),
                "edges": len(visible_edges),
                "omitted_nodes": max(total_nodes - len(visible_nodes), 0),
                "omitted_edges": max(total_edges - len(visible_edges), 0),
            },
        }

    def _build_mutation_attribution(self, *, target: str) -> Optional[EventAttribution]:
        """Build generic actor/scope attribution for mutation results and events."""
        context = build_graph_authorization_context(action=GRAPH_ACTION_MUTATE, target=target)
        attribution = EventAttribution(
            actor=EventActorAttribution(
                actor_id=context.actor.get("actor_id", ""),
                actor_type=context.actor.get("actor_type", ""),
                is_authenticated=bool(context.actor.get("is_authenticated", False)),
                auth_source=context.actor.get("auth_source", "anonymous"),
                source=context.actor.get("source", "default"),
            ),
            scope=EventScopeAttribution(
                workspace_id=context.scope.get("workspace_id", ""),
                workspace_kind=context.scope.get("workspace_kind", ""),
                graph_id=context.scope.get("graph_id", ""),
                source=context.scope.get("source", "default"),
            ),
        )
        return attribution if attribution.has_attribution() else None

    def _build_event_context(
        self,
        *,
        target: str,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Optional[EventContext]:
        """Build event context, enriching explicit event metadata with actor/scope attribution."""
        attribution = self._build_mutation_attribution(target=target)
        if not any((event_origin, event_session_id, event_correlation_id, attribution)):
            return None

        return EventContext(
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
            attribution=attribution,
        )

    @staticmethod
    def _attach_mutation_attribution(response: Dict[str, Any], event_context: Optional[EventContext]) -> Dict[str, Any]:
        """Attach audit-friendly attribution metadata to mutation responses when available."""
        if event_context and event_context.attribution:
            response["attribution"] = event_context.attribution.to_dict()
        return response

    @property
    def storage(self) -> GraphStorage:
        """Access the underlying storage (for advanced use cases)."""
        return self._storage

    # ==================== Search Operations ====================

    def search_graph(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Search for nodes in the graph based on text query.

        Args:
            query: Search text (matches against name, description, summary, tags)
            node_types: List of node types to filter on (Actor, Initiative, etc.)
            limit: Max number of results (default 50)
            action: Optional action for frontend ('add_to_visualization' or 'replace_visualization')

        Returns:
            Dict with matching nodes, connecting edges, and search metadata
        """
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="search_graph")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="search_graph",
                decision=decision,
            )

        # Log search request
        logger.info(f"SEARCH: query='{query}' types={node_types} limit={limit}")

        # Convert node_types to NodeType enum or keep as string for dynamic types
        type_filters = None
        if node_types:
            type_filters = [NodeType.from_string(t) for t in node_types]

        local_results = self._storage.search_nodes(
            query=query,
            node_types=type_filters,
            limit=max(limit, self._storage.get_stats().total_nodes) if decision.graph_access.enabled else limit,
        )

        visible_local_results = [
            node for node in local_results
            if self._is_node_visible(node, decision.graph_access)
        ][:limit]
        logger.info(f"SEARCH: Found {len(visible_local_results)} visible local results")

        # Get node IDs for edge filtering
        result_node_ids = set(node.id for node in visible_local_results)

        # Find edges connecting these nodes (either endpoint in results)
        connecting_edges = self._storage.get_incident_edges(list(result_node_ids))

        federated_nodes = []
        federated_edges = []

        if self._federation_manager and self._federation_manager.enabled:
            remaining = max(0, limit - len(visible_local_results))
            if remaining > 0:
                federated = self._federation_manager.search_nodes(
                    query=query,
                    node_types=node_types,
                    limit=self._get_federated_search_limit(remaining, decision.graph_access),
                    max_depth=federation_depth,
                )
                federated_nodes = federated["nodes"]
                federated_edges = federated["edges"]

        all_nodes = visible_local_results + federated_nodes
        all_edges = connecting_edges + federated_edges

        visible_nodes, visible_edges = self._filter_nodes_and_edges(
            nodes=all_nodes,
            edges=all_edges,
            graph_access=decision.graph_access,
        )
        if len(visible_nodes) > limit:
            visible_nodes = visible_nodes[:limit]
            visible_node_ids = {node.id for node in visible_nodes}
            visible_edges = [
                edge for edge in visible_edges
                if edge.source in visible_node_ids and edge.target in visible_node_ids
            ]

        # Deduplicate edges by ID when local/federated edge collections overlap in future extensions
        deduped_edges = []
        seen_edge_ids = set()
        for edge in visible_edges:
            if edge.id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge.id)
            deduped_edges.append(edge)

        visible_federated_nodes = [node for node in visible_nodes if self._node_graph_id(node)]

        result = {
            "nodes": serialize_nodes(visible_nodes),
            "edges": serialize_edges(deduped_edges),
            "total": len(visible_nodes),
            "query": query,
            "filters": {
                "node_types": node_types,
            },
            "federation": {
                "included": bool(self._federation_manager and self._federation_manager.enabled),
                "federated_nodes": len(visible_federated_nodes),
                "federated_edges": len([
                    edge for edge in deduped_edges
                    if (edge.metadata or {}).get("origin_graph_id")
                ]),
                "depth": federation_depth,
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
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_node_details")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_node_details",
                decision=decision,
            )

        node = self._storage.get_node(node_id)

        if not node or not self._is_node_visible(node, decision.graph_access):
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
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_related_nodes")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_related_nodes",
                decision=decision,
            )

        node = self._storage.get_node(node_id)
        if not self._is_node_visible(node, decision.graph_access):
            return {
                "success": False,
                "error": f"Node with ID {node_id} not found"
            }

        # Convert relationship_types to enum
        rel_filters = None
        if relationship_types:
            rel_filters = [RelationshipType(r) for r in relationship_types]

        result = self._storage.get_related_nodes(
            node_id=node_id,
            relationship_types=rel_filters,
            depth=depth
        )

        visible_nodes, visible_edges = self._filter_nodes_and_edges(
            nodes=result['nodes'],
            edges=result['edges'],
            graph_access=decision.graph_access,
        )

        return {
            "nodes": serialize_nodes(visible_nodes),
            "edges": serialize_edges(visible_edges),
            "total_nodes": len(visible_nodes),
            "total_edges": len(visible_edges),
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
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="add_nodes")
        if denied:
            denied.setdefault("added_node_ids", [])
            denied.setdefault("added_edge_ids", [])
            return denied

        # Convert dicts to Node and Edge objects.
        # Any key not in the Node model is a schema-defined extra field — fold it into metadata
        # so it is persisted without requiring model changes.
        _NODE_MODEL_FIELDS = {
            'id', 'type', 'name', 'description', 'summary',
            'tags', 'subtypes', 'aliases', 'metadata', 'embedding', 'created_at', 'updated_at',
        }
        try:
            node_objects = []
            for n in nodes:
                node_dict = dict(n)
                extra = {k: v for k, v in node_dict.items() if k not in _NODE_MODEL_FIELDS}
                if extra:
                    meta = dict(node_dict.get('metadata') or {})
                    meta.update(extra)
                    node_dict['metadata'] = meta
                    for k in extra:
                        node_dict.pop(k)
                node_objects.append(Node(**node_dict))
            edge_objects = [Edge(**e) for e in edges]
        except Exception as e:
            return {
                "success": False,
                "message": f"Error validating input: {str(e)}",
                "added_node_ids": [],
                "added_edge_ids": []
            }

        event_context = self._build_event_context(
            target="add_nodes",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        result = self._storage.add_nodes(node_objects, edge_objects, event_context=event_context)

        # Build response with serialized result and the actual nodes/edges for visualization
        response = serialize_add_result(result)

        # Include the added nodes and edges so frontend can add them to visualization
        if result.success:
            # Fetch the added nodes to get their full data (including generated IDs)
            added_nodes = [
                self._storage.get_node(node_id)
                for node_id in result.added_node_ids
            ]
            added_nodes = [n for n in added_nodes if n is not None]

            # Fetch the added edges
            added_edges = [
                self._storage.edges.get(edge_id)
                for edge_id in result.added_edge_ids
            ]
            added_edges = [e for e in added_edges if e is not None]

            response["nodes"] = serialize_nodes(added_nodes)
            response["edges"] = serialize_edges(added_edges)
            response["action"] = "add_to_visualization"

        return self._attach_mutation_attribution(response, event_context)

    def adopt_federated_node(
        self,
        federated_node_id: str,
        local_name: Optional[str] = None,
        relationship_type: str = "ADOPTED_FROM",
        create_new_copy: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Clone a federated cached node into the local graph and link lineage."""
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_MUTATE, target="adopt_federated_node")
        if not decision.allowed:
            denied = self._build_access_denied_result(
                action=GRAPH_ACTION_MUTATE,
                target="adopt_federated_node",
                decision=decision,
            )
            denied.setdefault("added_node_ids", [])
            denied.setdefault("added_edge_ids", [])
            return denied

        if not self._federation_manager or not self._federation_manager.enabled:
            return {
                "success": False,
                "message": "Federation is not enabled",
                "added_node_ids": [],
                "added_edge_ids": [],
            }

        source_node = self._federation_manager.get_cached_node(federated_node_id)
        if source_node is None:
            return {
                "success": False,
                "message": f"Federated node not found in cache: {federated_node_id}",
                "added_node_ids": [],
                "added_edge_ids": [],
            }
        if not self._is_node_visible(source_node, decision.graph_access):
            denied = self._build_access_denied_result(
                action=GRAPH_ACTION_MUTATE,
                target="adopt_federated_node",
                decision=GraphAuthorizationDecision(
                    allowed=False,
                    reason="Graph access denied for the selected graph scope.",
                    mode=decision.mode,
                    source=decision.source,
                    graph_access=decision.graph_access,
                ),
            )
            denied.setdefault("added_node_ids", [])
            denied.setdefault("added_edge_ids", [])
            return denied

        graph_cfg = self._federation_manager.get_graph_config_for_node(federated_node_id)
        if graph_cfg and not graph_cfg.capabilities.allow_adopt:
            return {
                "success": False,
                "message": f"Adoption is not allowed for source graph: {graph_cfg.graph_id}",
                "added_node_ids": [],
                "added_edge_ids": [],
            }

        if not create_new_copy:
            for local_node in self._storage.nodes.values():
                adopted_from = (local_node.metadata or {}).get("adopted_from") or {}
                if adopted_from.get("federated_node_id") == source_node.id:
                    return {
                        "success": True,
                        "message": "Federated node already adopted",
                        "adopted_node": serialize_node(local_node),
                        "source_node": serialize_node(source_node),
                        "lineage_edge": None,
                        "added_node_ids": [],
                        "added_edge_ids": [],
                        "action": "add_to_visualization",
                        "already_adopted": True,
                    }

        metadata = dict(source_node.metadata or {})
        metadata.update({
            "is_adopted": True,
            "adopted_from": {
                "federated_node_id": source_node.id,
                "origin_graph_id": source_node.metadata.get("origin_graph_id"),
                "origin_node_id": source_node.metadata.get("origin_node_id"),
            },
        })

        local_node = Node(
            type=source_node.type_str,
            name=local_name or source_node.name,
            description=source_node.description,
            summary=source_node.summary,
            tags=list(source_node.tags),
            metadata=metadata,
        )

        # Persist a local reference copy for the federated source if not already present,
        # so lineage edges are valid in local storage and visible in traversals.
        source_reference = self._storage.get_node(source_node.id)
        if source_reference is None:
            source_reference = Node(
                id=source_node.id,
                type=source_node.type_str,
                name=source_node.name,
                description=source_node.description,
                summary=source_node.summary,
                tags=list(source_node.tags),
                metadata={
                    **(source_node.metadata or {}),
                    "is_federated_reference": True,
                    "read_only": True,
                },
            )

        lineage_edge = Edge(
            source=local_node.id,
            target=source_reference.id,
            type=relationship_type,
            label="Adopted from federated source",
            metadata={
                "is_federated_lineage": True,
                "origin_graph_id": source_node.metadata.get("origin_graph_id"),
            },
        )

        event_context = self._build_event_context(
            target="adopt_federated_node",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        nodes_to_add = [local_node]
        if self._storage.get_node(source_reference.id) is None:
            nodes_to_add.append(source_reference)

        result = self._storage.add_nodes(nodes_to_add, [lineage_edge], event_context=event_context)

        if not result.success:
            return serialize_add_result(result)

        return self._attach_mutation_attribution({
            "success": True,
            "message": "Federated node adopted into local graph",
            "adopted_node": serialize_node(local_node),
            "source_node": serialize_node(source_node),
            "lineage_edge": serialize_edge(lineage_edge),
            "added_node_ids": result.added_node_ids,
            "added_edge_ids": result.added_edge_ids,
            "action": "add_to_visualization",
        }, event_context)

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
            updates: Dict with fields to update (name, description, summary, tags, aliases, metadata)
            event_origin: Source of the mutation (web-ui, mcp, system, agent:<id>)
            event_session_id: Unique session ID for loop prevention
            event_correlation_id: Correlation ID for chaining related events

        Returns:
            Dict with updated node or error
        """
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="update_node")
        if denied:
            return denied

        event_context = self._build_event_context(
            target="update_node",
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

        # Return the updated node with action for frontend to refresh visualization
        return self._attach_mutation_attribution({
            "success": True,
            "node": serialize_node(updated_node),
            "nodes": [serialize_node(updated_node)],
            "action": "update_in_visualization"
        }, event_context)

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
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="delete_nodes")
        if denied:
            denied.setdefault("deleted_node_ids", [])
            denied.setdefault("affected_edge_ids", [])
            return denied

        event_context = self._build_event_context(
            target="delete_nodes",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        result = self._storage.delete_nodes(node_ids, confirmed, event_context=event_context)
        return self._attach_mutation_attribution(serialize_delete_result(result), event_context)

    # ==================== Edge CRUD Operations ====================

    def add_edge(
        self,
        source: str,
        target: str,
        type: Optional[str] = None,
        label: Optional[str] = None,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add a single edge between existing nodes.

        Args:
            source: Source node ID
            target: Target node ID
            type: Relationship type (optional, defaults to RELATES_TO)
            label: Free-text label (optional)
            event_origin: Source of the mutation
            event_session_id: Session ID for loop prevention
            event_correlation_id: Correlation ID for chaining events

        Returns:
            Dict with added edge or error
        """
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="add_edge")
        if denied:
            return denied

        from backend.core.models import Edge

        edge_data = {"source": source, "target": target}
        if type:
            edge_data["type"] = type
        if label:
            edge_data["label"] = label

        try:
            edge = Edge(**edge_data)
        except Exception as e:
            return {"success": False, "message": f"Invalid edge data: {str(e)}"}

        event_context = self._build_event_context(
            target="add_edge",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        edge_id = self._storage.add_edge(edge, event_context=event_context)
        if not edge_id:
            return {"success": False, "message": "Could not add edge (source or target not found)"}

        return self._attach_mutation_attribution({
            "success": True,
            "edge": serialize_edge(edge),
            "edges": [serialize_edge(edge)],
            "action": "add_to_visualization",
        }, event_context)

    def update_edge(
        self,
        edge_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing edge.

        Args:
            edge_id: ID of the edge to update
            updates: Dict with fields to update (type, label, metadata)
            event_origin: Source of the mutation
            event_session_id: Session ID for loop prevention
            event_correlation_id: Correlation ID for chaining events

        Returns:
            Dict with updated edge or error
        """
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="update_edge")
        if denied:
            return denied

        event_context = self._build_event_context(
            target="update_edge",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        updated_edge = self._storage.update_edge(edge_id, updates, event_context=event_context)

        if not updated_edge:
            return {"success": False, "error": f"Edge with ID {edge_id} not found"}

        return self._attach_mutation_attribution({
            "success": True,
            "edge": serialize_edge(updated_edge),
        }, event_context)

    def delete_edge(
        self,
        edge_id: str,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete a single edge.

        Args:
            edge_id: ID of the edge to delete
            event_origin: Source of the mutation
            event_session_id: Session ID for loop prevention
            event_correlation_id: Correlation ID for chaining events

        Returns:
            Dict with success status
        """
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="delete_edge")
        if denied:
            return denied

        event_context = self._build_event_context(
            target="delete_edge",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        deleted = self._storage.delete_edge(edge_id, event_context=event_context)

        if not deleted:
            return {"success": False, "error": f"Edge with ID {edge_id} not found"}

        return self._attach_mutation_attribution({"success": True, "deleted_edge_id": edge_id}, event_context)

    def delete_edges(
        self,
        edge_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete up to 50 edges in a single operation. Requires confirmed=True."""
        denied = self._authorize_graph_access(action=GRAPH_ACTION_MUTATE, target="delete_edges")
        if denied:
            denied.setdefault("deleted_edge_ids", [])
            return denied

        if len(edge_ids) > 50:
            return {
                "deleted_edge_ids": [],
                "success": False,
                "message": "Max 50 edges can be deleted at a time.",
            }

        if not confirmed:
            return {
                "deleted_edge_ids": [],
                "success": False,
                "message": "Deletion requires confirmed=True. Please confirm before proceeding.",
            }

        event_context = self._build_event_context(
            target="delete_edges",
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

        result = self._storage.delete_edges(edge_ids, event_context=event_context)
        return self._attach_mutation_attribution(serialize_delete_edges_result(result), event_context)

    # ==================== Statistics & Metadata ====================

    def get_graph_stats(self) -> Dict[str, Any]:
        """
        Get statistics for the graph.

        Returns:
            Dict with statistics (total_nodes, total_edges, nodes_by_type)
        """
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_graph_stats")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_graph_stats",
                decision=decision,
            )

        stats = serialize_graph_stats(
            self._get_visible_local_graph_stats(decision.graph_access)
            if decision.graph_access.enabled
            else self._storage.get_stats()
        )

        local_graph_name = self._storage.get_graph_name()
        visible_graph_display_names = self._get_visible_federation_graph_display_names(decision.graph_access)
        federation_info: Dict[str, Any] = {
            "local_graph_name": local_graph_name if "local" in visible_graph_display_names else "",
            "max_selectable_depth": 1,
            "selectable_depth_levels": [1],
            "search_has_multiple_graphs": False,
            "graph_display_names": visible_graph_display_names,
        }

        if self._federation_manager and self._federation_manager.enabled:
            total_graph_count = len(visible_graph_display_names)
            federation_info = {
                "local_graph_name": local_graph_name if "local" in visible_graph_display_names else "",
                "max_selectable_depth": self._federation_manager.get_max_selectable_depth(),
                "selectable_depth_levels": self._federation_manager.get_selectable_depth_levels(),
                "search_has_multiple_graphs": total_graph_count > 1,
                "graph_display_names": visible_graph_display_names,
            }

        stats["federation"] = federation_info
        return stats

    # ==================== History Operations ====================

    def get_graph_history(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Return recent graph mutation history (newest first)."""
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_graph_history")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_graph_history",
                decision=decision,
            )

        entries = self._storage.get_recent_history(limit=limit, offset=offset)
        return {
            "success": True,
            "entries": entries,
            "count": len(entries),
            "limit": limit,
            "offset": offset,
        }

    def get_node_history(self, node_id: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Return mutation history for a single node (newest first)."""
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_node_history")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_node_history",
                decision=decision,
            )

        entries = self._storage.get_node_history(node_id, limit=limit, offset=offset)
        return {
            "success": True,
            "node_id": node_id,
            "entries": entries,
            "count": len(entries),
            "limit": limit,
            "offset": offset,
        }

    def get_edge_history(self, edge_id: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Return mutation history for a single edge (newest first)."""
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_edge_history")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_edge_history",
                decision=decision,
            )

        entries = self._storage.get_edge_history(edge_id, limit=limit, offset=offset)
        return {
            "success": True,
            "edge_id": edge_id,
            "entries": entries,
            "count": len(entries),
            "limit": limit,
            "offset": offset,
        }

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

    def get_subtypes(self, node_type: str = None) -> Dict[str, Any]:
        """
        Get existing subtypes used in the graph, grouped by node type.

        Args:
            node_type: Optional filter for a specific node type

        Returns:
            Dict with subtypes grouped by node type
        """
        subtypes = self._storage.get_subtypes_by_node_type(node_type)
        return {"subtypes": subtypes}

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

    def get_capabilities(self) -> Dict[str, Any]:
        """Get the public capability manifest for client discovery."""
        return config_loader.get_capabilities()

    def get_runtime_info(self) -> Dict[str, Any]:
        """Get the public runtime metadata for deployment introspection."""
        return config_loader.get_runtime_info()

    def get_tenant_context(self) -> Dict[str, Any]:
        """Get the tenant/deployment context metadata."""
        return config_loader.get_tenant_context()

    def get_config_context(self) -> Dict[str, Any]:
        """Get the effective public config scope and non-sensitive source metadata."""
        return config_loader.get_config_context()

    def get_request_actor_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        auth_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public request actor context with optional request-safe overrides."""
        return config_loader.get_request_actor_info() if not any([
            headers,
            actor_id,
            actor_type,
            auth_source,
        ]) else config_loader.get_public_request_actor_context(
            headers=headers,
            actor_id=actor_id,
            actor_type=actor_type,
            auth_source=auth_source,
        )

    def get_request_scope_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public request scope context with optional request-safe overrides."""
        return config_loader.get_request_scope_info() if not any([
            headers,
            workspace_id,
            workspace_kind,
            graph_id,
        ]) else config_loader.get_public_request_scope_context(
            headers=headers,
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    def get_request_graph_selection_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get the public graph/workspace selection summary with optional safe overrides."""
        return config_loader.get_request_graph_selection_info() if not any([
            headers,
            workspace_id,
            workspace_kind,
            graph_id,
        ]) else config_loader.get_public_request_graph_selection_context(
            headers=headers,
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

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

    def resolve_session_nodes(self, node_ids: List[str]) -> Dict[str, Any]:
        """Resolve a shared session's node references to node objects + edges.

        Sessions store node **references** only (design D4); this rehydrates them
        from the graph on load, mirroring ``get_saved_view``'s fetch logic and
        the same access/visibility filtering. ``group-*`` ids are session-local
        annotations, not graph nodes, and are skipped.
        """
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="resolve_session_nodes")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="resolve_session_nodes",
                decision=decision,
            )

        visible_node_ids: List[str] = []
        nodes = []
        for node_id in node_ids:
            if not isinstance(node_id, str) or node_id.startswith("group-"):
                continue
            node = self._storage.get_node(node_id)
            if node and self._is_node_visible(node, decision.graph_access):
                visible_node_ids.append(node_id)
                nodes.append(serialize_node(node))

        edges = serialize_edges(self._storage.get_edges_between_nodes(visible_node_ids))
        return {"success": True, "nodes": nodes, "edges": edges}

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
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="get_saved_view")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="get_saved_view",
                decision=decision,
            )

        # Search for SavedView node with the given name
        results = self._storage.search_nodes(
            query=name,
            node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
            limit=max(100, self._storage.get_stats().total_nodes) if decision.graph_access.enabled else 1,
        )

        visible_views = [
            view for view in results
            if view.name == name and self._is_node_visible(view, decision.graph_access)
        ]

        if not visible_views:
            return {
                "success": False,
                "error": f"View '{name}' not found."
            }

        view_node = visible_views[0]

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
        visible_node_ids = []
        nodes = []
        for node_id in actual_node_ids:
            node = self._storage.get_node(node_id)
            if node and self._is_node_visible(node, decision.graph_access):
                visible_node_ids.append(node_id)
                nodes.append(serialize_node(node))

        if not nodes:
            return {
                "success": False,
                "error": f"No nodes could be loaded from view '{name}'. The referenced nodes may have been deleted."
            }

        # Get all edges between these nodes
        edges = serialize_edges(
            self._storage.get_edges_between_nodes(visible_node_ids)
        )

        # Extract group data for frontend - prefer full metadata if available
        saved_groups = view_node.metadata.get('groups', [])
        if saved_groups:
            # Use full group metadata (includes label, color, style)
            group_data = saved_groups
        else:
            # Fall back to reconstructing from position map
            group_data = []
            for group_id in group_ids:
                group_position = position_map.get(group_id)
                if group_position:
                    group_data.append({
                        "id": group_id,
                        "position": group_position
                    })

        # Extract parentId mapping (which nodes belong to which groups)
        visible_node_id_set = set(visible_node_ids)
        filtered_position_map = {
            item_id: position
            for item_id, position in position_map.items()
            if item_id in visible_node_id_set or item_id.startswith('group-')
        }
        filtered_hidden_node_ids = [node_id for node_id in hidden_node_ids if node_id in visible_node_id_set]
        parent_ids = {
            node_id: group_id
            for node_id, group_id in view_node.metadata.get('parentIds', {}).items()
            if node_id in visible_node_id_set
        }

        return {
            "success": True,
            "nodes": nodes,
            "edges": edges,
            "positions": filtered_position_map,
            "hidden_node_ids": filtered_hidden_node_ids,
            "groups": group_data,
            "parentIds": parent_ids,
            "annotations": view_node.metadata.get('annotations', []),
            "action": "load_visualization"
        }

    def list_saved_views(self) -> Dict[str, Any]:
        """
        List all saved views.

        Returns:
            List of all SavedView nodes with their names and summaries
        """
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="list_saved_views")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="list_saved_views",
                decision=decision,
            )

        views = self._storage.search_nodes(
            query="",
            node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
            limit=100
        )

        view_list = []
        for view in views:
            if not self._is_node_visible(view, decision.graph_access):
                continue

            if 'node_ids' in view.metadata:
                referenced_node_ids = view.metadata.get('node_ids', [])
            else:
                referenced_node_ids = [
                    item.get('id')
                    for item in view.metadata.get('view_data', {}).get('nodes', [])
                    if isinstance(item, dict)
                ]
            node_count = len([
                node_id for node_id in referenced_node_ids
                if not str(node_id).startswith('group-')
                and self._is_node_visible(self._storage.get_node(node_id), decision.graph_access)
            ])
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
        Export graph data through the request-bound read visibility contract.

        Returns:
            Graph data plus a sanitized boundary summary for restore/admin workflows.
        """
        decision = self._evaluate_graph_access(action=GRAPH_ACTION_READ, target="export_graph")
        if not decision.allowed:
            return self._build_access_denied_result(
                action=GRAPH_ACTION_READ,
                target="export_graph",
                decision=decision,
            )

        source_nodes = self._storage.get_all_nodes()
        source_edges = self._storage.get_all_edges()
        visible_nodes, visible_edges = self._filter_nodes_and_edges(
            nodes=source_nodes,
            edges=source_edges,
            graph_access=decision.graph_access,
        )
        all_nodes = serialize_nodes(visible_nodes)
        all_edges = serialize_edges(visible_edges)
        export_boundary = self._build_export_boundary_summary(
            target="export_graph",
            decision=decision,
            visible_nodes=visible_nodes,
            visible_edges=visible_edges,
            total_nodes=len(source_nodes),
            total_edges=len(source_edges),
        )

        return {
            "version": "1.0",
            "exportDate": datetime.now(timezone.utc).isoformat(),
            "nodes": all_nodes,
            "edges": all_edges,
            "total_nodes": len(all_nodes),
            "total_edges": len(all_edges),
            "export_boundary": export_boundary,
        }
