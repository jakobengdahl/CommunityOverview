"""
Write (mutation) operations for GraphService.

All functions are standalone; they receive storage, the authorization hook,
and (where required) the federation manager as explicit parameters.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.core import Edge, Node
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAuthorizationDecision,
)

from . import access
from .serializers import (
    serialize_add_result,
    serialize_delete_edges_result,
    serialize_delete_result,
    serialize_edge,
    serialize_edges,
    serialize_node,
    serialize_nodes,
)

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.federation import FederationManager
    from backend.runtime.authorization import GraphAuthorizationHook


# ---------------------------------------------------------------------------
# Node mutations
# ---------------------------------------------------------------------------

_NODE_MODEL_FIELDS = {
    "id",
    "type",
    "name",
    "description",
    "summary",
    "tags",
    "subtypes",
    "aliases",
    "metadata",
    "embedding",
    "created_at",
    "updated_at",
}


def add_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    denied = access.authorize_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="add_nodes"
    )
    if denied:
        denied.setdefault("added_node_ids", [])
        denied.setdefault("added_edge_ids", [])
        return denied

    # Convert dicts to Node/Edge objects; fold unknown keys into metadata
    try:
        node_objects = []
        for n in nodes:
            node_dict = dict(n)
            extra = {k: v for k, v in node_dict.items() if k not in _NODE_MODEL_FIELDS}
            if extra:
                meta = dict(node_dict.get("metadata") or {})
                meta.update(extra)
                node_dict["metadata"] = meta
                for k in extra:
                    node_dict.pop(k)
            node_objects.append(Node(**node_dict))
        edge_objects = [Edge(**e) for e in edges]
    except Exception as e:
        return {
            "success": False,
            "message": f"Error validating input: {str(e)}",
            "added_node_ids": [],
            "added_edge_ids": [],
        }

    event_context = access.build_event_context(
        target="add_nodes",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    result = storage.add_nodes(node_objects, edge_objects, event_context=event_context)
    response = serialize_add_result(result)

    if result.success:
        added_nodes = [storage.get_node(nid) for nid in result.added_node_ids]
        added_nodes = [n for n in added_nodes if n is not None]
        added_edges = [storage.edges.get(eid) for eid in result.added_edge_ids]
        added_edges = [e for e in added_edges if e is not None]
        response["nodes"] = serialize_nodes(added_nodes)
        response["edges"] = serialize_edges(added_edges)
        response["action"] = "add_to_visualization"

    return access.attach_mutation_attribution(response, event_context)


def update_node(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_id: str,
    updates: Dict[str, Any],
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="update_node"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="update_node", decision=decision
        )
    if decision.graph_access.enabled and not access.is_node_visible(
        storage.get_node(node_id), decision.graph_access
    ):
        return {"success": False, "error": f"Node with ID {node_id} not found"}

    event_context = access.build_event_context(
        target="update_node",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    try:
        updated_node = storage.update_node(
            node_id, updates, event_context=event_context
        )
    except ValueError as e:
        return {"success": False, "error": f"Error validating input: {e}"}
    if not updated_node:
        return {"success": False, "error": f"Node with ID {node_id} not found"}

    return access.attach_mutation_attribution(
        {
            "success": True,
            "node": serialize_node(updated_node),
            "nodes": [serialize_node(updated_node)],
            "action": "update_in_visualization",
        },
        event_context,
    )


def delete_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_ids: List[str],
    confirmed: bool = False,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="delete_nodes"
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="delete_nodes", decision=decision
        )
        denied.setdefault("deleted_node_ids", [])
        denied.setdefault("affected_edge_ids", [])
        return denied

    if decision.graph_access.enabled:
        node_ids = [
            nid
            for nid in node_ids
            if access.is_node_visible(storage.get_node(nid), decision.graph_access)
        ]

    event_context = access.build_event_context(
        target="delete_nodes",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    result = storage.delete_nodes(node_ids, confirmed, event_context=event_context)
    return access.attach_mutation_attribution(
        serialize_delete_result(result), event_context
    )


# ---------------------------------------------------------------------------
# Edge mutations
# ---------------------------------------------------------------------------


def add_edge(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    source: str,
    target: str,
    type: Optional[str] = None,
    label: Optional[str] = None,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="add_edge"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="add_edge", decision=decision
        )
    if decision.graph_access.enabled and not (
        access.is_node_visible(storage.get_node(source), decision.graph_access)
        and access.is_node_visible(storage.get_node(target), decision.graph_access)
    ):
        return {
            "success": False,
            "message": "Could not add edge (source or target not found)",
        }

    from backend.core.models import Edge as EdgeModel

    edge_data: Dict[str, Any] = {"source": source, "target": target}
    if type:
        edge_data["type"] = type
    if label:
        edge_data["label"] = label

    try:
        edge = EdgeModel(**edge_data)
    except Exception as e:
        return {"success": False, "message": f"Invalid edge data: {str(e)}"}

    event_context = access.build_event_context(
        target="add_edge",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    edge_id = storage.add_edge(edge, event_context=event_context)
    if not edge_id:
        return {
            "success": False,
            "message": "Could not add edge (source or target not found)",
        }

    return access.attach_mutation_attribution(
        {
            "success": True,
            "edge": serialize_edge(edge),
            "edges": [serialize_edge(edge)],
            "action": "add_to_visualization",
        },
        event_context,
    )


def update_edge(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    edge_id: str,
    updates: Dict[str, Any],
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="update_edge"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="update_edge", decision=decision
        )
    if decision.graph_access.enabled and not access.is_edge_visible(
        storage.edges.get(edge_id), storage, decision.graph_access
    ):
        return {"success": False, "error": f"Edge with ID {edge_id} not found"}

    event_context = access.build_event_context(
        target="update_edge",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    updated_edge = storage.update_edge(edge_id, updates, event_context=event_context)
    if not updated_edge:
        return {"success": False, "error": f"Edge with ID {edge_id} not found"}

    return access.attach_mutation_attribution(
        {"success": True, "edge": serialize_edge(updated_edge)},
        event_context,
    )


def delete_edge(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    edge_id: str,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="delete_edge"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="delete_edge", decision=decision
        )
    if decision.graph_access.enabled and not access.is_edge_visible(
        storage.edges.get(edge_id), storage, decision.graph_access
    ):
        return {"success": False, "error": f"Edge with ID {edge_id} not found"}

    event_context = access.build_event_context(
        target="delete_edge",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    deleted = storage.delete_edge(edge_id, event_context=event_context)
    if not deleted:
        return {"success": False, "error": f"Edge with ID {edge_id} not found"}

    return access.attach_mutation_attribution(
        {"success": True, "deleted_edge_id": edge_id}, event_context
    )


def delete_edges(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    edge_ids: List[str],
    confirmed: bool = False,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="delete_edges"
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="delete_edges", decision=decision
        )
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

    if decision.graph_access.enabled:
        edge_ids = [
            eid
            for eid in edge_ids
            if access.is_edge_visible(
                storage.edges.get(eid), storage, decision.graph_access
            )
        ]

    event_context = access.build_event_context(
        target="delete_edges",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    result = storage.delete_edges(edge_ids, event_context=event_context)
    return access.attach_mutation_attribution(
        serialize_delete_edges_result(result), event_context
    )


# ---------------------------------------------------------------------------
# Federation adoption
# ---------------------------------------------------------------------------


def adopt_federated_node(
    storage: "GraphStorage",
    federation_manager: Optional["FederationManager"],
    hook: "GraphAuthorizationHook",
    federated_node_id: str,
    local_name: Optional[str] = None,
    relationship_type: str = "ADOPTED_FROM",
    create_new_copy: bool = False,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="adopt_federated_node"
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE,
            target="adopt_federated_node",
            decision=decision,
        )
        denied.setdefault("added_node_ids", [])
        denied.setdefault("added_edge_ids", [])
        return denied

    if not federation_manager or not federation_manager.enabled:
        return {
            "success": False,
            "message": "Federation is not enabled",
            "added_node_ids": [],
            "added_edge_ids": [],
        }

    source_node = federation_manager.get_cached_node(federated_node_id)
    if source_node is None:
        return {
            "success": False,
            "message": f"Federated node not found in cache: {federated_node_id}",
            "added_node_ids": [],
            "added_edge_ids": [],
        }
    if not access.is_node_visible(source_node, decision.graph_access):
        denied = access.build_access_denied_result(
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

    graph_cfg = federation_manager.get_graph_config_for_node(federated_node_id)
    if graph_cfg and not graph_cfg.capabilities.allow_adopt:
        return {
            "success": False,
            "message": f"Adoption is not allowed for source graph: {graph_cfg.graph_id}",
            "added_node_ids": [],
            "added_edge_ids": [],
        }

    if not create_new_copy:
        for local_node in storage.nodes.values():
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
    metadata.update(
        {
            "is_adopted": True,
            "adopted_from": {
                "federated_node_id": source_node.id,
                "origin_graph_id": source_node.metadata.get("origin_graph_id"),
                "origin_node_id": source_node.metadata.get("origin_node_id"),
            },
        }
    )

    local_node = Node(
        type=source_node.type_str,
        name=local_name or source_node.name,
        description=source_node.description,
        summary=source_node.summary,
        tags=list(source_node.tags),
        metadata=metadata,
    )

    source_reference = storage.get_node(source_node.id)
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

    from backend.core.models import Edge as EdgeModel

    lineage_edge = EdgeModel(
        source=local_node.id,
        target=source_reference.id,
        type=relationship_type,
        label="Adopted from federated source",
        metadata={
            "is_federated_lineage": True,
            "origin_graph_id": source_node.metadata.get("origin_graph_id"),
        },
    )

    event_context = access.build_event_context(
        target="adopt_federated_node",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    nodes_to_add = [local_node]
    if storage.get_node(source_reference.id) is None:
        nodes_to_add.append(source_reference)

    result = storage.add_nodes(
        nodes_to_add, [lineage_edge], event_context=event_context
    )

    if not result.success:
        return serialize_add_result(result)

    return access.attach_mutation_attribution(
        {
            "success": True,
            "message": "Federated node adopted into local graph",
            "adopted_node": serialize_node(local_node),
            "source_node": serialize_node(source_node),
            "lineage_edge": serialize_edge(lineage_edge),
            "added_node_ids": result.added_node_ids,
            "added_edge_ids": result.added_edge_ids,
            "action": "add_to_visualization",
        },
        event_context,
    )
