"""
Saved-view and export operations for GraphService.

All functions are standalone; they receive storage and the authorization hook
as explicit parameters.
"""

from typing import TYPE_CHECKING, Any, Dict, List

from backend.core import NodeType
from backend.runtime.authorization import GRAPH_ACTION_READ

from . import access
from .serializers import serialize_edges, serialize_node

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.runtime.authorization import GraphAuthorizationHook


def save_view(name: str) -> Dict[str, Any]:
    """Signal the frontend to capture and save the current visualization state."""
    return {
        "action": "save_view",
        "name": name,
        "message": f"Ready to save view '{name}'. Client will capture current visualization state.",
    }


def resolve_session_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_ids: List[str],
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="resolve_session_nodes"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ,
            target="resolve_session_nodes",
            decision=decision,
        )

    visible_node_ids: List[str] = []
    nodes = []
    for node_id in node_ids:
        if not isinstance(node_id, str) or node_id.startswith("group-"):
            continue
        node = storage.get_node(node_id)
        if node and access.is_node_visible(node, decision.graph_access):
            visible_node_ids.append(node_id)
            nodes.append(serialize_node(node))

    edges = serialize_edges(storage.get_edges_between_nodes(visible_node_ids))
    return {"success": True, "nodes": nodes, "edges": edges}


def resolve_session_node_semantics(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_ids: List[str],
    *,
    action: str,
    target: str,
) -> Dict[str, Any]:
    """Resolve the *meaning* of session node references: type and status only.

    The layout tools need this projection so an agent can arrange a session by
    node type or status without parsing id strings or issuing one
    ``get_node_details`` call per node. It deliberately does not reuse
    ``resolve_session_nodes``: that one serializes whole nodes and scans for the
    edges between them, which a geometry read never uses.

    ``status`` is not a schema field — it is whatever the deployment stores
    under ``metadata["status"]`` — so it is reported only when that value is a
    non-blank string, and is ``None`` otherwise. Blank normalises to ``None``
    (as ``node_graph_id`` does for its own metadata key) so an agent building
    status lanes never gets an unnamed one.

    ``action`` and ``target`` are required rather than defaulted, so the scope a
    caller gets is never one this helper picked for it. A caller that only reads
    the projection passes ``GRAPH_ACTION_READ``; one that turns it into a write
    must pass ``GRAPH_ACTION_MUTATE``, so the ids it keeps are the ones it may
    *write* and not merely the ones it may read — a hook is free to narrow the
    two differently. ``target`` names the operation the decision is really for:
    a caller that has already gated its own tool name passes that same name, so
    a target-aware hook is asked about the operation the user invoked rather than
    about this helper, and both evaluations of one call agree.
    """
    decision = access.evaluate_graph_access(hook, action=action, target=target)
    if not decision.allowed:
        return access.build_access_denied_result(
            action=action,
            target=target,
            decision=decision,
        )

    semantics: Dict[str, Dict[str, Any]] = {}
    for node_id in node_ids:
        if not isinstance(node_id, str):
            continue
        node = storage.get_node(node_id)
        if node is None or not access.is_node_visible(node, decision.graph_access):
            continue
        status = node.metadata.get("status")
        semantics[node_id] = {
            "type": node.type_str,
            "status": status.strip() or None if isinstance(status, str) else None,
        }

    return {"success": True, "nodes": semantics}


def get_saved_view(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    name: str,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="get_saved_view"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="get_saved_view", decision=decision
        )

    results = storage.search_nodes(
        query=name,
        node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
        limit=max(100, storage.get_stats().total_nodes)
        if decision.graph_access.enabled
        else 1,
    )

    visible_views = [
        view
        for view in results
        if view.name == name and access.is_node_visible(view, decision.graph_access)
    ]

    if not visible_views:
        return {"success": False, "error": f"View '{name}' not found."}

    view_node = visible_views[0]

    position_map: Dict[str, Any] = {}
    node_ids: List[str] = []
    hidden_node_ids: List[str] = []

    view_data = view_node.metadata.get("view_data", {})
    if view_data and "nodes" in view_data:
        node_position_data = view_data.get("nodes", [])
        hidden_node_ids = view_data.get("hidden_nodes", [])
        position_map = {
            item["id"]: item.get("position")
            for item in node_position_data
            if isinstance(item, dict)
        }
        node_ids = list(position_map.keys())
    elif "node_ids" in view_node.metadata:
        node_ids = view_node.metadata.get("node_ids", [])
        position_map = view_node.metadata.get("positions", {})
        hidden_node_ids = view_node.metadata.get("hidden_nodes", [])
    else:
        return {"success": False, "error": f"View '{name}' contains no nodes."}

    actual_node_ids = [nid for nid in node_ids if not nid.startswith("group-")]
    group_ids = [nid for nid in node_ids if nid.startswith("group-")]

    visible_node_ids = []
    nodes = []
    for node_id in actual_node_ids:
        node = storage.get_node(node_id)
        if node and access.is_node_visible(node, decision.graph_access):
            visible_node_ids.append(node_id)
            nodes.append(serialize_node(node))

    if not nodes:
        return {
            "success": False,
            "error": f"No nodes could be loaded from view '{name}'. The referenced nodes may have been deleted.",
        }

    edges = serialize_edges(storage.get_edges_between_nodes(visible_node_ids))

    saved_groups = view_node.metadata.get("groups", [])
    if saved_groups:
        group_data = saved_groups
    else:
        group_data = []
        for group_id in group_ids:
            group_position = position_map.get(group_id)
            if group_position:
                group_data.append({"id": group_id, "position": group_position})

    visible_node_id_set = set(visible_node_ids)
    filtered_position_map = {
        item_id: position
        for item_id, position in position_map.items()
        if item_id in visible_node_id_set or item_id.startswith("group-")
    }
    filtered_hidden_node_ids = [
        node_id for node_id in hidden_node_ids if node_id in visible_node_id_set
    ]
    parent_ids = {
        node_id: group_id
        for node_id, group_id in view_node.metadata.get("parentIds", {}).items()
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
        "annotations": view_node.metadata.get("annotations", []),
        "action": "load_visualization",
    }


def list_saved_views(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="list_saved_views"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="list_saved_views", decision=decision
        )

    views = storage.search_nodes(
        query="",
        node_types=[NodeType.SAVED_VIEW, NodeType.VISUALIZATION_VIEW],
        limit=100,
    )

    view_list = []
    for view in views:
        if not access.is_node_visible(view, decision.graph_access):
            continue

        if "node_ids" in view.metadata:
            referenced_node_ids = view.metadata.get("node_ids", [])
        else:
            referenced_node_ids = [
                item.get("id")
                for item in view.metadata.get("view_data", {}).get("nodes", [])
                if isinstance(item, dict)
            ]
        node_count = len(
            [
                node_id
                for node_id in referenced_node_ids
                if not str(node_id).startswith("group-")
                and access.is_node_visible(
                    storage.get_node(node_id), decision.graph_access
                )
            ]
        )
        view_list.append(
            {
                "name": view.name,
                "description": view.description,
                "summary": view.summary,
                "created_at": view.created_at.isoformat() if view.created_at else None,
                "node_count": node_count,
            }
        )

    return {"success": True, "views": view_list, "total": len(view_list)}


def export_graph(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
) -> Dict[str, Any]:
    from datetime import datetime, timezone

    from .serializers import serialize_edges as ser_edges, serialize_nodes as ser_nodes

    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="export_graph"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="export_graph", decision=decision
        )

    source_nodes = storage.get_all_nodes()
    source_edges = storage.get_all_edges()
    visible_nodes, visible_edges = access.filter_nodes_and_edges(
        nodes=source_nodes,
        edges=source_edges,
        graph_access=decision.graph_access,
    )
    all_nodes = ser_nodes(visible_nodes)
    all_edges = ser_edges(visible_edges)
    export_boundary = access.build_export_boundary_summary(
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
