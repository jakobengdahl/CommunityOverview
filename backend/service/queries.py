"""
Read-only query operations for GraphService.

All functions are standalone; they receive storage, the authorization hook,
and the federation manager as explicit parameters.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.core import NodeType, RelationshipType
from backend.runtime.authorization import GRAPH_ACTION_READ

from . import access
from .serializers import (
    serialize_edges,
    serialize_graph_stats,
    serialize_node,
    serialize_nodes,
    serialize_similar_nodes,
)

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.federation import FederationManager
    from backend.runtime.authorization import GraphAuthorizationHook

import logging

logger = logging.getLogger(__name__)


def search_graph(
    storage: "GraphStorage",
    federation_manager: Optional["FederationManager"],
    hook: "GraphAuthorizationHook",
    query: str,
    node_types: Optional[List[str]] = None,
    limit: int = 50,
    action: Optional[str] = None,
    federation_depth: Optional[int] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    metadata_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="search_graph"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="search_graph", decision=decision
        )

    logger.info(f"SEARCH: query='{query}' types={node_types} limit={limit}")

    type_filters = None
    if node_types:
        type_filters = [NodeType.from_string(t) for t in node_types]

    # Accept the documented list-of-filters shape; tolerate a single bare dict by
    # normalising it once here so matching and the echoed `filters` agree.
    if isinstance(metadata_filters, dict):
        metadata_filters = [metadata_filters]

    has_generic_filters = bool(tags_all or tags_any or tags_none or metadata_filters)

    def _passes_filters(node) -> bool:
        return _node_matches_search_filters(
            node,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            metadata_filters=metadata_filters,
        )

    # Widen the text-search window when a graph-scope narrowing or a generic
    # tag/metadata filter is active, so post-fetch filtering sees the full
    # candidate set instead of dropping matches that fell outside a `limit`-sized
    # text window.
    local_results = storage.search_nodes(
        query=query,
        node_types=type_filters,
        limit=max(limit, storage.get_stats().total_nodes)
        if decision.graph_access.enabled or has_generic_filters
        else limit,
    )

    visible_local_results = [
        node
        for node in local_results
        if access.is_node_visible(node, decision.graph_access) and _passes_filters(node)
    ][:limit]
    logger.info(f"SEARCH: Found {len(visible_local_results)} visible local results")

    result_node_ids = set(node.id for node in visible_local_results)
    connecting_edges = storage.get_incident_edges(list(result_node_ids))

    federated_nodes: List = []
    federated_edges: List = []
    if federation_manager and federation_manager.enabled:
        remaining = max(0, limit - len(visible_local_results))
        if remaining > 0:
            federated = federation_manager.search_nodes(
                query=query,
                node_types=node_types,
                limit=access.get_federated_search_limit(
                    federation_manager,
                    remaining,
                    decision.graph_access,
                    widen=has_generic_filters,
                ),
                max_depth=federation_depth,
            )
            federated_nodes = [
                node for node in federated["nodes"] if _passes_filters(node)
            ]
            federated_edges = federated["edges"]

    all_nodes = visible_local_results + federated_nodes
    all_edges = connecting_edges + federated_edges

    visible_nodes, visible_edges = access.filter_nodes_and_edges(
        nodes=all_nodes,
        edges=all_edges,
        graph_access=decision.graph_access,
    )
    if len(visible_nodes) > limit:
        visible_nodes = visible_nodes[:limit]
        visible_node_ids = {node.id for node in visible_nodes}
        visible_edges = [
            edge
            for edge in visible_edges
            if edge.source in visible_node_ids and edge.target in visible_node_ids
        ]

    deduped_edges = []
    seen_edge_ids: set = set()
    for edge in visible_edges:
        if edge.id in seen_edge_ids:
            continue
        seen_edge_ids.add(edge.id)
        deduped_edges.append(edge)

    visible_federated_nodes = [
        node for node in visible_nodes if access.node_graph_id(node)
    ]

    result: Dict[str, Any] = {
        "nodes": serialize_nodes(visible_nodes),
        "edges": serialize_edges(deduped_edges),
        "total": len(visible_nodes),
        "query": query,
        "filters": {
            "node_types": node_types,
            "tags_any": list(tags_any or []),
            "tags_all": list(tags_all or []),
            "tags_none": list(tags_none or []),
            "metadata_filters": list(metadata_filters or []),
        },
        "federation": {
            "included": bool(federation_manager and federation_manager.enabled),
            "federated_nodes": len(visible_federated_nodes),
            "federated_edges": len(
                [
                    edge
                    for edge in deduped_edges
                    if (edge.metadata or {}).get("origin_graph_id")
                ]
            ),
            "depth": federation_depth,
        },
    }
    if action:
        result["action"] = action
    return result


def get_node_details(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_id: str,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_node_details"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_node_details", decision=decision
        )

    node = storage.get_node(node_id)
    if not node or not access.is_node_visible(node, decision.graph_access):
        return {"success": False, "error": f"Node with ID {node_id} not found"}

    return {"success": True, "node": serialize_node(node)}


def get_related_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_id: str,
    relationship_types: Optional[List[str]] = None,
    depth: int = 1,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_related_nodes"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_related_nodes", decision=decision
        )

    node = storage.get_node(node_id)
    if not access.is_node_visible(node, decision.graph_access):
        return {"success": False, "error": f"Node with ID {node_id} not found"}

    rel_filters = None
    if relationship_types:
        rel_filters = [RelationshipType(r) for r in relationship_types]

    result = storage.get_related_nodes(
        node_id=node_id, relationship_types=rel_filters, depth=depth
    )

    visible_nodes, visible_edges = access.filter_nodes_and_edges(
        nodes=result["nodes"],
        edges=result["edges"],
        graph_access=decision.graph_access,
    )

    return {
        "nodes": serialize_nodes(visible_nodes),
        "edges": serialize_edges(visible_edges),
        "total_nodes": len(visible_nodes),
        "total_edges": len(visible_edges),
        "depth": depth,
    }


def _matches_tag_filters(
    values: List[str],
    *,
    tags_all: Optional[List[str]] = None,
    tags_any: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
) -> bool:
    """Return True when ``values`` satisfies the AND/OR/NONE tag filter.

    ``tags_all`` (AND): every listed tag must be present.
    ``tags_any`` (OR): at least one listed tag must be present.
    ``tags_none`` (NONE): none of the listed tags may be present.
    Empty/omitted lists disable that dimension. All empty → always True.
    The dimensions combine with AND (a value set must pass every configured one).
    """
    present = set(values or [])
    if tags_all and not set(tags_all).issubset(present):
        return False
    if tags_any and present.isdisjoint(set(tags_any)):
        return False
    if tags_none and not present.isdisjoint(set(tags_none)):
        return False
    return True


def _as_value_set(raw: Any) -> set:
    """Normalize a metadata value or a filter's requested values to a set of
    strings, so config-neutral JSON scalars (str/int/bool) compare uniformly.

    A list/tuple/set contributes each of its items; a scalar contributes itself;
    ``None`` contributes nothing.
    """
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(v) for v in raw}
    return {str(raw)}


def _matches_metadata_filters(
    metadata: Optional[Dict[str, Any]],
    metadata_filters: Optional[List[Dict[str, Any]]],
) -> bool:
    """Return True when ``metadata`` satisfies every generic metadata filter.

    Each filter is ``{"key": str, "values": [...], "match": "any"|"all"|"none"}``:
      - ``any`` (default): the node's value(s) at ``key`` intersect the requested
        values.
      - ``all``: every requested value is present in the node's value(s) at
        ``key`` (meaningful when the stored value is itself a list).
      - ``none``: the node's value(s) at ``key`` share nothing with the requested
        values (exclusion).

    Values compare as strings so heterogeneous scalar types match uniformly. A
    filter with no ``key`` or no ``values`` is ignored. Filters combine with AND
    (the node must satisfy all of them). The mechanism is deliberately
    key-agnostic — it hardcodes no field names or values.
    """
    if not metadata_filters:
        return True
    if isinstance(metadata_filters, dict):
        metadata_filters = [metadata_filters]
    meta = metadata or {}
    for spec in metadata_filters:
        if not isinstance(spec, dict):
            continue
        key = spec.get("key")
        requested = _as_value_set(spec.get("values"))
        if not key or not requested:
            continue
        match = str(spec.get("match") or "any").lower()
        present = _as_value_set(meta.get(key))
        if match == "all":
            if not requested.issubset(present):
                return False
        elif match == "none":
            if requested & present:
                return False
        else:  # "any" (and any unrecognised mode)
            if present.isdisjoint(requested):
                return False
    return True


def _node_matches_search_filters(
    node: Any,
    *,
    tags_all: Optional[List[str]] = None,
    tags_any: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    metadata_filters: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Combined generic tag + metadata predicate for a single node."""
    return _matches_tag_filters(
        node.tags, tags_all=tags_all, tags_any=tags_any, tags_none=tags_none
    ) and _matches_metadata_filters(node.metadata, metadata_filters)


def list_typed_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    *,
    node_type: str,
    tags_all: Optional[List[str]] = None,
    tags_any: Optional[List[str]] = None,
    subtypes_any: Optional[List[str]] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """List local nodes of one type, filtered by tag/subtype, for a dedicated
    REST interface.

    Applies the same read authorization and graph-scope narrowing as
    ``search_graph`` so a dedicated endpoint never returns more than the generic
    interface would. Federated nodes are out of scope for these endpoints — they
    expose the local graph's own configured types only.
    """
    target = f"list_typed_nodes:{node_type}"
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target=target
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target=target, decision=decision
        )

    matched = [
        node
        for node in storage.get_all_nodes()
        if node.type_str == node_type
        and access.is_node_visible(node, decision.graph_access)
        and _matches_tag_filters(node.tags, tags_all=tags_all, tags_any=tags_any)
        and (not subtypes_any or not set(node.subtypes).isdisjoint(set(subtypes_any)))
    ]
    matched = matched[:limit]

    matched_ids = {node.id for node in matched}
    incident = storage.get_incident_edges(list(matched_ids))
    connecting_edges = [
        edge
        for edge in incident
        if edge.source in matched_ids and edge.target in matched_ids
    ]

    return {
        "nodes": serialize_nodes(matched),
        "edges": serialize_edges(connecting_edges),
        "total": len(matched),
        "node_type": node_type,
        "filters": {
            "tags_all": list(tags_all or []),
            "tags_any": list(tags_any or []),
            "subtypes_any": list(subtypes_any or []),
        },
    }


def list_typed_edges(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    *,
    edge_type: str,
    tags_all: Optional[List[str]] = None,
    tags_any: Optional[List[str]] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """List local edges of one type for a dedicated REST interface.

    An edge is only returned when *both* of its endpoint nodes are visible under
    the request's graph-scope narrowing — the same rule the generic search path
    applies (``access.filter_nodes_and_edges``) — so a dedicated edge endpoint
    cannot leak an edge into or out of a graph the caller may not see.

    Edges carry no ``tags`` field; when tag filters are configured they are
    matched against ``edge.metadata['tags']`` (a list, if present), so operators
    can tag edges through metadata without a schema change.
    """
    target = f"list_typed_edges:{edge_type}"
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target=target
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target=target, decision=decision
        )

    matched_edges = []
    endpoint_ids: set = set()
    for edge in storage.get_all_edges():
        if edge.type_str != edge_type:
            continue
        source = storage.get_node(edge.source)
        targ = storage.get_node(edge.target)
        if not access.is_node_visible(source, decision.graph_access):
            continue
        if not access.is_node_visible(targ, decision.graph_access):
            continue
        edge_tags = (edge.metadata or {}).get("tags") or []
        if not isinstance(edge_tags, list):
            edge_tags = []
        if not _matches_tag_filters(edge_tags, tags_all=tags_all, tags_any=tags_any):
            continue
        matched_edges.append(edge)
        endpoint_ids.add(edge.source)
        endpoint_ids.add(edge.target)
        if len(matched_edges) >= limit:
            break

    endpoint_nodes = [
        node for node in (storage.get_node(nid) for nid in endpoint_ids) if node
    ]

    return {
        "edges": serialize_edges(matched_edges),
        "nodes": serialize_nodes(endpoint_nodes),
        "total": len(matched_edges),
        "edge_type": edge_type,
        "filters": {
            "tags_all": list(tags_all or []),
            "tags_any": list(tags_any or []),
        },
    }


def find_similar_nodes(
    storage: "GraphStorage",
    name: str,
    node_type: Optional[str] = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> Dict[str, Any]:
    type_filter = NodeType.from_string(node_type) if node_type else None
    similar = storage.find_similar_nodes(
        name=name, node_type=type_filter, threshold=threshold, limit=limit
    )
    return {
        "similar_nodes": serialize_similar_nodes(similar),
        "total": len(similar),
        "search_name": name,
    }


def find_similar_nodes_batch(
    storage: "GraphStorage",
    names: List[str],
    node_type: Optional[str] = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> Dict[str, Any]:
    type_filter = NodeType.from_string(node_type) if node_type else None
    results = storage.find_similar_nodes_batch(
        names=names, node_type=type_filter, threshold=threshold, limit=limit
    )
    formatted: Dict[str, Any] = {}
    for name, similar_list in results.items():
        formatted[name] = {
            "similar_nodes": serialize_similar_nodes(similar_list),
            "total": len(similar_list),
        }
    return {
        "results": formatted,
        "total_searched": len(names),
        "message": f"Searched for {len(names)} names",
    }


def get_graph_stats(
    storage: "GraphStorage",
    federation_manager: Optional["FederationManager"],
    hook: "GraphAuthorizationHook",
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_graph_stats"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_graph_stats", decision=decision
        )

    stats = serialize_graph_stats(
        access.get_visible_local_graph_stats(storage, decision.graph_access)
        if decision.graph_access.enabled
        else storage.get_stats()
    )

    local_graph_name = storage.get_graph_name()
    visible_graph_display_names = access.get_visible_federation_graph_display_names(
        storage, federation_manager, decision.graph_access
    )
    federation_info: Dict[str, Any] = {
        "local_graph_name": local_graph_name
        if "local" in visible_graph_display_names
        else "",
        "max_selectable_depth": 1,
        "selectable_depth_levels": [1],
        "search_has_multiple_graphs": False,
        "graph_display_names": visible_graph_display_names,
    }

    if federation_manager and federation_manager.enabled:
        total_graph_count = len(visible_graph_display_names)
        federation_info = {
            "local_graph_name": local_graph_name
            if "local" in visible_graph_display_names
            else "",
            "max_selectable_depth": federation_manager.get_max_selectable_depth(),
            "selectable_depth_levels": federation_manager.get_selectable_depth_levels(),
            "search_has_multiple_graphs": total_graph_count > 1,
            "graph_display_names": visible_graph_display_names,
        }

    stats["federation"] = federation_info
    return stats


def get_graph_history(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_graph_history"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_graph_history", decision=decision
        )
    entries = storage.get_recent_history(limit=limit, offset=offset)
    return {
        "success": True,
        "entries": entries,
        "count": len(entries),
        "limit": limit,
        "offset": offset,
    }


def get_node_history(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_node_history"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_node_history", decision=decision
        )
    entries = storage.get_node_history(node_id, limit=limit, offset=offset)
    return {
        "success": True,
        "node_id": node_id,
        "entries": entries,
        "count": len(entries),
        "limit": limit,
        "offset": offset,
    }


def get_edge_history(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    edge_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_edge_history"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_edge_history", decision=decision
        )
    entries = storage.get_edge_history(edge_id, limit=limit, offset=offset)
    return {
        "success": True,
        "edge_id": edge_id,
        "entries": entries,
        "count": len(entries),
        "limit": limit,
        "offset": offset,
    }


def list_node_types() -> Dict[str, Any]:
    from backend.config import config_loader

    schema = config_loader.get_schema()
    node_types = []
    for type_name, type_config in schema.get("node_types", {}).items():
        node_types.append(
            {
                "type": type_name,
                "color": type_config.get("color", "#9CA3AF"),
                "description": type_config.get("description", ""),
                "fields": type_config.get("fields", []),
                "static": type_config.get("static", False),
            }
        )
    return {"node_types": node_types}


def get_subtypes(
    storage: "GraphStorage", node_type: Optional[str] = None
) -> Dict[str, Any]:
    return {"subtypes": storage.get_subtypes_by_node_type(node_type)}


def list_relationship_types() -> Dict[str, Any]:
    from backend.config import config_loader

    schema = config_loader.get_schema()
    relationship_types = []
    for type_name, type_config in schema.get("relationship_types", {}).items():
        relationship_types.append(
            {"type": type_name, "description": type_config.get("description", "")}
        )
    return {"relationship_types": relationship_types}


def get_schema() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_schema()


def get_presentation() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_presentation()


def get_capabilities() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_capabilities()


def get_runtime_info() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_runtime_info()


def get_tenant_context() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_tenant_context()


def get_config_context() -> Dict[str, Any]:
    from backend.config import config_loader

    return config_loader.get_config_context()


def get_request_actor_info(
    *,
    headers: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
    actor_type: Optional[str] = None,
    auth_source: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.config import config_loader

    if not any([headers, actor_id, actor_type, auth_source]):
        return config_loader.get_request_actor_info()
    return config_loader.get_public_request_actor_context(
        headers=headers,
        actor_id=actor_id,
        actor_type=actor_type,
        auth_source=auth_source,
    )


def get_request_scope_info(
    *,
    headers: Optional[Dict[str, Any]] = None,
    workspace_id: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    graph_id: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.config import config_loader

    if not any([headers, workspace_id, workspace_kind, graph_id]):
        return config_loader.get_request_scope_info()
    return config_loader.get_public_request_scope_context(
        headers=headers,
        workspace_id=workspace_id,
        workspace_kind=workspace_kind,
        graph_id=graph_id,
    )


def get_request_graph_selection_info(
    *,
    headers: Optional[Dict[str, Any]] = None,
    workspace_id: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    graph_id: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.config import config_loader

    if not any([headers, workspace_id, workspace_kind, graph_id]):
        return config_loader.get_request_graph_selection_info()
    return config_loader.get_public_request_graph_selection_context(
        headers=headers,
        workspace_id=workspace_id,
        workspace_kind=workspace_kind,
        graph_id=graph_id,
    )
