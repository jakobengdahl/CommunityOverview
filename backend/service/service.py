"""
GraphService - thin facade over the service sub-modules.

All business logic lives in the four focused modules:
  access.py    — authorization and visibility helpers
  queries.py   — read-only operations
  mutations.py — write operations
  views.py     — saved views and export

Callers (rest_api.py, mcp_tools.py, …) continue to use this class unchanged;
public method signatures are preserved exactly.
"""

from typing import List, Optional, Dict, Any

from backend.core import GraphStorage
from backend.core.storage_search import MATCH_MODE_SUBSTRING
from backend.runtime.authorization import (
    GRAPH_ACTION_READ,
    DefaultGraphAuthorizationHook,
    GraphAuthorizationHook,
)
from backend.federation import FederationManager

from . import queries, mutations, views

import logging

logger = logging.getLogger(__name__)


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
        self._storage = storage
        self._federation_manager = federation_manager
        self._authorization_hook = authorization_hook or DefaultGraphAuthorizationHook()

    @property
    def storage(self) -> GraphStorage:
        """Access the underlying storage (for advanced use cases)."""
        return self._storage

    @property
    def authorization_hook(self) -> GraphAuthorizationHook:
        """The active authorization hook.

        Exposed so surfaces that authorize outside the node/edge CRUD path
        (e.g. the MCP visualization-session tools) evaluate against the *same*
        hook instance the hosted layer swaps in, rather than a fresh default.
        """
        return self._authorization_hook

    # ==================== Search Operations ====================

    def search_graph(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
        tags_any: Optional[List[str]] = None,
        tags_all: Optional[List[str]] = None,
        tags_none: Optional[List[str]] = None,
        metadata_filters: Optional[List[Dict[str, Any]]] = None,
        include_archived: bool = False,
        semantic: bool = False,
        match_mode: str = MATCH_MODE_SUBSTRING,
    ) -> Dict[str, Any]:
        return queries.search_graph(
            self._storage,
            self._federation_manager,
            self._authorization_hook,
            query,
            node_types=node_types,
            limit=limit,
            action=action,
            federation_depth=federation_depth,
            tags_any=tags_any,
            tags_all=tags_all,
            tags_none=tags_none,
            metadata_filters=metadata_filters,
            include_archived=include_archived,
            semantic=semantic,
            match_mode=match_mode,
        )

    def get_node_details(self, node_id: str) -> Dict[str, Any]:
        return queries.get_node_details(
            self._storage, self._authorization_hook, node_id
        )

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        depth: int = 1,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return queries.get_related_nodes(
            self._storage,
            self._authorization_hook,
            node_id,
            relationship_types=relationship_types,
            depth=depth,
            include_archived=include_archived,
        )

    def list_typed_nodes(
        self,
        node_type: str,
        tags_all: Optional[List[str]] = None,
        tags_any: Optional[List[str]] = None,
        subtypes_any: Optional[List[str]] = None,
        limit: int = 500,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return queries.list_typed_nodes(
            self._storage,
            self._authorization_hook,
            node_type=node_type,
            tags_all=tags_all,
            tags_any=tags_any,
            subtypes_any=subtypes_any,
            limit=limit,
            include_archived=include_archived,
        )

    def list_typed_edges(
        self,
        edge_type: str,
        tags_all: Optional[List[str]] = None,
        tags_any: Optional[List[str]] = None,
        limit: int = 500,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return queries.list_typed_edges(
            self._storage,
            self._authorization_hook,
            edge_type=edge_type,
            tags_all=tags_all,
            tags_any=tags_any,
            limit=limit,
            include_archived=include_archived,
        )

    # ==================== Similarity Operations ====================

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        return queries.find_similar_nodes(
            self._storage, name, node_type=node_type, threshold=threshold, limit=limit
        )

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        return queries.find_similar_nodes_batch(
            self._storage, names, node_type=node_type, threshold=threshold, limit=limit
        )

    # ==================== CRUD Operations ====================

    def add_nodes(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.add_nodes(
            self._storage,
            self._authorization_hook,
            nodes,
            edges,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

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
        return mutations.adopt_federated_node(
            self._storage,
            self._federation_manager,
            self._authorization_hook,
            federated_node_id,
            local_name=local_name,
            relationship_type=relationship_type,
            create_new_copy=create_new_copy,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def update_node(
        self,
        node_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        metadata_merge: bool = False,
        expected_updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.update_node(
            self._storage,
            self._authorization_hook,
            node_id,
            updates,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
            metadata_merge=metadata_merge,
            expected_updated_at=expected_updated_at,
        )

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.delete_nodes(
            self._storage,
            self._authorization_hook,
            node_ids,
            confirmed=confirmed,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def archive_nodes(
        self,
        node_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.set_nodes_archived(
            self._storage,
            self._authorization_hook,
            node_ids,
            True,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def unarchive_nodes(
        self,
        node_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.set_nodes_archived(
            self._storage,
            self._authorization_hook,
            node_ids,
            False,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

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
        return mutations.add_edge(
            self._storage,
            self._authorization_hook,
            source,
            target,
            type=type,
            label=label,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def update_edge(
        self,
        edge_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.update_edge(
            self._storage,
            self._authorization_hook,
            edge_id,
            updates,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def delete_edge(
        self,
        edge_id: str,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.delete_edge(
            self._storage,
            self._authorization_hook,
            edge_id,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def delete_edges(
        self,
        edge_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.delete_edges(
            self._storage,
            self._authorization_hook,
            edge_ids,
            confirmed=confirmed,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def archive_edges(
        self,
        edge_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.set_edges_archived(
            self._storage,
            self._authorization_hook,
            edge_ids,
            True,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def unarchive_edges(
        self,
        edge_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return mutations.set_edges_archived(
            self._storage,
            self._authorization_hook,
            edge_ids,
            False,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    # ==================== Statistics & Metadata ====================

    def get_graph_stats(self) -> Dict[str, Any]:
        return queries.get_graph_stats(
            self._storage, self._federation_manager, self._authorization_hook
        )

    # ==================== History Operations ====================

    def get_graph_history(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        return queries.get_graph_history(
            self._storage, self._authorization_hook, limit=limit, offset=offset
        )

    def get_node_history(
        self, node_id: str, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        return queries.get_node_history(
            self._storage, self._authorization_hook, node_id, limit=limit, offset=offset
        )

    def get_edge_history(
        self, edge_id: str, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        return queries.get_edge_history(
            self._storage, self._authorization_hook, edge_id, limit=limit, offset=offset
        )

    def list_node_types(self) -> Dict[str, Any]:
        return queries.list_node_types()

    def get_subtypes(self, node_type: str = None) -> Dict[str, Any]:
        return queries.get_subtypes(self._storage, node_type=node_type)

    def list_relationship_types(self) -> Dict[str, Any]:
        return queries.list_relationship_types()

    def audit_relationship_applicability(self) -> Dict[str, Any]:
        return queries.audit_relationship_applicability(self._storage)

    def get_schema(self) -> Dict[str, Any]:
        return queries.get_schema()

    def get_presentation(self) -> Dict[str, Any]:
        return queries.get_presentation()

    def get_capabilities(self) -> Dict[str, Any]:
        return queries.get_capabilities()

    def get_runtime_info(self) -> Dict[str, Any]:
        return queries.get_runtime_info()

    def get_tenant_context(self) -> Dict[str, Any]:
        return queries.get_tenant_context()

    def get_config_context(self) -> Dict[str, Any]:
        return queries.get_config_context()

    def get_request_actor_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        auth_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        return queries.get_request_actor_info(
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
        return queries.get_request_scope_info(
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
        return queries.get_request_graph_selection_info(
            headers=headers,
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    # ==================== Saved Views ====================

    def save_view(self, name: str) -> Dict[str, Any]:
        return views.save_view(name)

    def resolve_session_nodes(self, node_ids: List[str]) -> Dict[str, Any]:
        return views.resolve_session_nodes(
            self._storage, self._authorization_hook, node_ids
        )

    def resolve_session_node_semantics(
        self,
        node_ids: List[str],
        *,
        action: str = GRAPH_ACTION_READ,
        target: str = "resolve_session_node_semantics",
    ) -> Dict[str, Any]:
        """Resolve session node references to their type and status.

        ``action``/``target`` are required on the underlying helper, so that it
        can never pick a caller's authorization scope for it. They keep defaults
        *here* on purpose: this is a public method, and these two values are
        exactly what it evaluated before they were parameters, so an existing
        caller outside this repo is unaffected. Every caller in this repo passes
        both explicitly, and a new one should too.
        """
        return views.resolve_session_node_semantics(
            self._storage,
            self._authorization_hook,
            node_ids,
            action=action,
            target=target,
        )

    def get_saved_view(self, name: str) -> Dict[str, Any]:
        return views.get_saved_view(self._storage, self._authorization_hook, name)

    def list_saved_views(self) -> Dict[str, Any]:
        return views.list_saved_views(self._storage, self._authorization_hook)

    # ==================== Export ====================

    def export_graph(self) -> Dict[str, Any]:
        return views.export_graph(self._storage, self._authorization_hook)
