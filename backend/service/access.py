"""
Authorization and access-control helpers for GraphService.

All functions are pure helpers (no I/O, no state) and receive the
authorization hook and/or the storage/federation manager instances they
need as explicit parameters.  GraphService methods delegate to these.
"""

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from backend.core import (
    Edge,
    EventAttribution,
    EventActorAttribution,
    EventContext,
    EventScopeAttribution,
    GraphStats,
    Node,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_READ,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
    GraphAuthorizationHook,
    build_graph_authorization_context,
)

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.federation import FederationManager


# ---------------------------------------------------------------------------
# Authorization evaluation
# ---------------------------------------------------------------------------


def evaluate_graph_access(
    hook: GraphAuthorizationHook, *, action: str, target: str
) -> GraphAuthorizationDecision:
    return hook.evaluate(
        build_graph_authorization_context(action=action, target=target)
    )


def build_access_denied_result(
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


def authorize_graph_access(
    hook: GraphAuthorizationHook, *, action: str, target: str
) -> Optional[Dict[str, Any]]:
    """Return None when access is allowed; return error dict when denied."""
    decision = evaluate_graph_access(hook, action=action, target=target)
    if decision.allowed:
        return None
    return build_access_denied_result(action=action, target=target, decision=decision)


# ---------------------------------------------------------------------------
# Visibility helpers
# ---------------------------------------------------------------------------


def node_graph_id(node: Node) -> str:
    return str(((node.metadata or {}).get("origin_graph_id") or "")).strip()


def is_node_visible(node: Optional[Node], graph_access: GraphAccessNarrowing) -> bool:
    if node is None:
        return False
    return graph_access.matches(graph_id=node_graph_id(node))


def is_edge_visible(
    edge: Optional[Edge],
    storage: "GraphStorage",
    graph_access: GraphAccessNarrowing,
) -> bool:
    """An edge is visible only when both endpoint nodes are visible.

    Mirrors ``filter_nodes_and_edges`` and ``list_typed_edges``: graph-scope
    narrowing for an edge is derived from the visibility of its endpoints, so a
    caller can never reach an edge into or out of a graph they may not see.
    """
    if edge is None:
        return False
    return is_node_visible(
        storage.get_node(edge.source), graph_access
    ) and is_node_visible(storage.get_node(edge.target), graph_access)


def filter_nodes_and_edges(
    *,
    nodes: List[Node],
    edges: List[Edge],
    graph_access: GraphAccessNarrowing,
) -> Tuple[List[Node], List[Edge]]:
    visible_nodes = [n for n in nodes if is_node_visible(n, graph_access)]
    visible_node_ids = {n.id for n in visible_nodes}
    visible_edges = [
        e
        for e in edges
        if e.source in visible_node_ids and e.target in visible_node_ids
    ]
    return visible_nodes, visible_edges


# ---------------------------------------------------------------------------
# Stats / federation display names
# ---------------------------------------------------------------------------


def get_visible_local_graph_stats(
    storage: "GraphStorage", graph_access: GraphAccessNarrowing
) -> GraphStats:
    visible_nodes, visible_edges = filter_nodes_and_edges(
        nodes=storage.get_all_nodes(),
        edges=storage.get_all_edges(),
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


def get_visible_federation_graph_display_names(
    storage: "GraphStorage",
    federation_manager: Optional["FederationManager"],
    graph_access: GraphAccessNarrowing,
) -> Dict[str, str]:
    local_graph_name = storage.get_graph_name()
    graph_display_names: Dict[str, str] = {}
    if graph_access.matches(graph_id=""):
        graph_display_names["local"] = local_graph_name
    if federation_manager and federation_manager.enabled:
        for gid, display_name in federation_manager.get_graph_display_names().items():
            if graph_access.matches(graph_id=gid):
                graph_display_names[gid] = display_name
    return graph_display_names


def get_federated_search_limit(
    federation_manager: Optional["FederationManager"],
    minimum_limit: int,
    graph_access: GraphAccessNarrowing,
    widen: bool = False,
) -> int:
    """Return how many federated nodes to fetch before post-fetch filtering.

    The window is widened to the full federation cache when graph-scope narrowing
    is active, or when ``widen`` is set (a generic tag/metadata filter is active),
    so post-fetch filtering sees the full candidate set instead of dropping
    matches that ranked below a ``minimum_limit``-sized text window — mirroring
    the local widening in ``queries.search_graph``.
    """
    if not federation_manager or not federation_manager.enabled:
        return minimum_limit
    if not graph_access.enabled and not widen:
        return minimum_limit
    return max(
        minimum_limit,
        sum(len(entry.nodes) for entry in federation_manager._cache.values()),
    )


# ---------------------------------------------------------------------------
# Export boundary summary
# ---------------------------------------------------------------------------


def build_export_boundary_summary(
    *,
    target: str,
    decision: GraphAuthorizationDecision,
    visible_nodes: List[Node],
    visible_edges: List[Edge],
    total_nodes: int,
    total_edges: int,
) -> Dict[str, Any]:
    from backend.config import config_loader

    request_context = build_graph_authorization_context(
        action=GRAPH_ACTION_READ, target=target
    )
    scope = request_context.scope
    # Replicate the conditional logic from GraphService.get_request_graph_selection_info:
    # when scope values are present the public-context variant is used.
    workspace_id = scope.get("workspace_id")
    workspace_kind = scope.get("workspace_kind")
    graph_id = scope.get("graph_id")
    if any([workspace_id, workspace_kind, graph_id]):
        selection_summary = config_loader.get_public_request_graph_selection_context(
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )
    else:
        selection_summary = config_loader.get_request_graph_selection_info()

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


# ---------------------------------------------------------------------------
# Mutation attribution / event context
# ---------------------------------------------------------------------------


def build_mutation_attribution(*, target: str) -> Optional[EventAttribution]:
    from backend.runtime.authorization import GRAPH_ACTION_MUTATE

    context = build_graph_authorization_context(
        action=GRAPH_ACTION_MUTATE, target=target
    )
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


def build_event_context(
    *,
    target: str,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Optional[EventContext]:
    attribution = build_mutation_attribution(target=target)
    if not any((event_origin, event_session_id, event_correlation_id, attribution)):
        return None
    return EventContext(
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
        attribution=attribution,
    )


def attach_mutation_attribution(
    response: Dict[str, Any], event_context: Optional[EventContext]
) -> Dict[str, Any]:
    if event_context and event_context.attribution:
        response["attribution"] = event_context.attribution.to_dict()
    return response
