"""
Write (mutation) operations for GraphService.

All functions are standalone; they receive storage, the authorization hook,
and (where required) the federation manager as explicit parameters.
"""

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from backend.core import (
    Edge,
    EntityRevisionConflict,
    GraphWriteFailed,
    GraphWriteOutcomeUnknown,
    Node,
    NodeType,
    StaleUpdateError,
)
from backend.core.models import metadata_depth_error
from backend.core.session_annotations import (
    saved_view_annotation_error,
    saved_view_write_error,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_OPERATION_STAGE,
    GraphAuthorizationDecision,
)

from . import access
from .staging import sent_metadata_keys
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

    from .staging import StagedWrite


def _operation(staging: Optional["StagedWrite"]) -> str:
    """What the hook is told the write does (§14): staging, or nothing new."""
    return GRAPH_OPERATION_STAGE if staging is not None else ""


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

# Server-owned (docs/SESSION_OVERLAY_CONTRACT.md §4.1): a caller-supplied
# revision is dropped, never folded into metadata like other unknown keys.
_SERVER_OWNED_FIELDS = frozenset({"revision"})

# What a direct write can be refused or fail with, beyond validation.
_WRITE_ERRORS = (EntityRevisionConflict, GraphWriteFailed, GraphWriteOutcomeUnknown)


# Marks a refusal of the request's content, so a transport can tell it from a
# missing entity without matching message text.
VALIDATION_ERROR_CODE = "validation_error"


def write_error_result(exc: Exception, **extra: Any) -> Dict[str, Any]:
    """The refusal a direct write answers with (§16.3): an `error` code, a
    `message`, and for a conflict the entity and its `current_revision`."""
    result: Dict[str, Any] = {
        "success": False,
        "error": getattr(exc, "code", "write_failed"),
        "message": str(exc),
        **extra,
    }
    if isinstance(exc, EntityRevisionConflict):
        result.update(
            conflict=True,
            entity_kind=exc.kind,
            entity_id=exc.entity_id,
            current_revision=exc.current_revision,
        )
    return result


def _create_conflict_result(
    conflict: EntityRevisionConflict, **extra: Any
) -> Dict[str, Any]:
    """A refused create: the caller needs only to know the id is taken. The
    taken entity may be one this caller cannot see - another instance's
    create on a shared store it has not applied yet - so nothing about it,
    its revision included, goes back."""
    return write_error_result(
        EntityRevisionConflict(conflict.kind, conflict.entity_id, None, None),
        message=f"{conflict.kind} {conflict.entity_id!r} already exists",
        current_revision=None,
        **extra,
    )


# add_nodes/update_node are the only write paths a SavedView's
# annotation_document/annotations ever go through (there is no dedicated
# "save view" mutation — the frontend posts a plain node through add_nodes).
# Neither path is annotation-aware otherwise, so a SavedView node is the one
# case where this generic layer must apply the same image-annotation rule
# SessionStore enforces for live ops (see saved_view_annotation_error).
_SAVED_VIEW_NODE_TYPES = frozenset(
    {NodeType.SAVED_VIEW.value, NodeType.VISUALIZATION_VIEW.value}
)


def add_nodes(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
    expect_absent: bool = False,
    on_nodes_landed: Optional[Callable[[List[str]], None]] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    """``on_nodes_landed`` gets the ids of the nodes this call left in the
    graph, including those a failed call keeps. With ``staging`` the write
    goes to that session's layer instead (§9), and nothing lands."""
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="add_nodes",
        graph_operation=_operation(staging),
    )
    denied = (
        None
        if decision.allowed
        else access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="add_nodes", decision=decision
        )
    )
    if denied:
        denied.setdefault("added_node_ids", [])
        denied.setdefault("added_edge_ids", [])
        return denied

    # Convert dicts to Node/Edge objects; fold unknown keys into metadata
    try:
        node_objects = []
        for n in nodes:
            node_dict = {
                k: v for k, v in dict(n).items() if k not in _SERVER_OWNED_FIELDS
            }
            extra = {k: v for k, v in node_dict.items() if k not in _NODE_MODEL_FIELDS}
            if extra:
                meta = dict(node_dict.get("metadata") or {})
                meta.update(extra)
                node_dict["metadata"] = meta
                for k in extra:
                    node_dict.pop(k)
            node = Node(**node_dict)
            depth_error = metadata_depth_error(node.metadata)
            if depth_error:
                raise ValueError(depth_error)
            if node.type_str in _SAVED_VIEW_NODE_TYPES:
                annotation_error = saved_view_annotation_error(node.metadata)
                if annotation_error:
                    return {
                        "success": False,
                        "message": f"Error validating input: {annotation_error}",
                        "added_node_ids": [],
                        "added_edge_ids": [],
                    }
            node_objects.append(node)
        edge_objects = []
        for e in edges:
            edge = Edge(
                **{k: v for k, v in dict(e).items() if k not in _SERVER_OWNED_FIELDS}
            )
            depth_error = metadata_depth_error(edge.metadata)
            if depth_error:
                raise ValueError(depth_error)
            edge_objects.append(edge)
    except Exception as e:
        return {
            "success": False,
            "message": f"Error validating input: {str(e)}",
            "added_node_ids": [],
            "added_edge_ids": [],
        }

    if staging is not None:
        try:
            return staging.add_nodes(
                node_objects,
                edge_objects,
                expect_absent=expect_absent,
                graph_access=decision.graph_access,
            )
        except EntityRevisionConflict as e:
            return write_error_result(
                EntityRevisionConflict(e.kind, e.entity_id, None, None),
                message=f"{e.kind} {e.entity_id!r} already exists",
                current_revision=None,
                added_node_ids=[],
                added_edge_ids=[],
            )

    event_context = access.build_event_context(
        target="add_nodes",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    try:
        result = storage.add_nodes(
            node_objects,
            edge_objects,
            event_context=event_context,
            expect_absent=expect_absent,
        )
    except EntityRevisionConflict as e:
        return _create_conflict_result(e, added_node_ids=[], added_edge_ids=[])
    except _WRITE_ERRORS as e:
        return write_error_result(e, added_node_ids=[], added_edge_ids=[])
    if on_nodes_landed is not None:
        on_nodes_landed(
            result.added_node_ids if result.success else result.landed_node_ids
        )
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
    metadata_merge: bool = False,
    expected_updated_at: Optional[str] = None,
    expected_revision: Optional[int] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="update_node",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="update_node", decision=decision
        )
    if staging is not None:
        try:
            return staging.update_node(
                node_id,
                updates,
                metadata_merge=metadata_merge,
                expected_updated_at=expected_updated_at,
                expected_revision=expected_revision,
                graph_access=decision.graph_access,
            )
        except EntityRevisionConflict as e:
            return write_error_result(e)
        except StaleUpdateError as e:
            return {
                "success": False,
                "conflict": True,
                "error": str(e),
                "current_updated_at": e.current_updated_at.isoformat(),
            }
        except ValueError as e:
            return {
                "success": False,
                "error": f"Error validating input: {e}",
                "error_code": VALIDATION_ERROR_CODE,
            }
    target_node = storage.get_node(node_id)
    if decision.graph_access.enabled and not access.is_node_visible(
        target_node, decision.graph_access
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
            node_id,
            updates,
            event_context=event_context,
            metadata_merge=metadata_merge,
            expected_updated_at=expected_updated_at,
            expected_revision=expected_revision,
            # Under the storage lock, on the node the update replaces.
            validate=lambda before, after: saved_view_write_error(
                before, after, sent_metadata_keys(updates)
            ),
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e)
    except StaleUpdateError as e:
        return {
            "success": False,
            "conflict": True,
            "error": str(e),
            "current_updated_at": e.current_updated_at.isoformat(),
        }
    except ValueError as e:
        return {
            "success": False,
            "error": f"Error validating input: {e}",
            "error_code": VALIDATION_ERROR_CODE,
        }
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
    expected_revisions: Optional[Dict[str, int]] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="delete_nodes",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="delete_nodes", decision=decision
        )
        denied.setdefault("deleted_node_ids", [])
        denied.setdefault("affected_edge_ids", [])
        return denied

    # An expectation is a condition on a node this call deletes; one on any
    # other node would be checked against memory and never by the store.
    stray = sorted(set(expected_revisions or {}) - set(node_ids))
    if stray:
        return {
            "success": False,
            "message": (
                f"expected_revisions names nodes this call does not delete: {stray}"
            ),
            "deleted_node_ids": [],
            "affected_edge_ids": [],
        }

    if staging is not None:
        # The limits a direct delete enforces in the storage, before anything.
        if len(node_ids) > 10:
            return {
                "success": False,
                "message": "Max 10 nodes can be deleted at a time. Contact admin for bulk deletion.",
                "deleted_node_ids": [],
                "affected_edge_ids": [],
            }
        if not confirmed:
            return {
                "success": False,
                "message": "Deletion requires confirmed=True parameter",
                "deleted_node_ids": [],
                "affected_edge_ids": [],
            }
        try:
            return staging.delete_nodes(
                node_ids,
                expected_revisions=expected_revisions,
                graph_access=decision.graph_access,
            )
        except EntityRevisionConflict as e:
            return write_error_result(e, deleted_node_ids=[], affected_edge_ids=[])

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

    if expected_revisions and decision.graph_access.enabled:
        # An expectation on a node this caller may not see is answered as if
        # it were absent, never with its revision.
        visible = set(node_ids)
        hidden = [nid for nid in expected_revisions if nid not in visible]
        if hidden:
            return write_error_result(
                EntityRevisionConflict(
                    "node", hidden[0], expected_revisions[hidden[0]], None
                ),
                deleted_node_ids=[],
                affected_edge_ids=[],
            )
    try:
        result = storage.delete_nodes(
            node_ids,
            confirmed,
            event_context=event_context,
            expected_revisions=expected_revisions,
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e, deleted_node_ids=[], affected_edge_ids=[])
    return access.attach_mutation_attribution(
        serialize_delete_result(result), event_context
    )


def set_nodes_archived(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    node_ids: List[str],
    archived: bool,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    """Archive or unarchive nodes (hide-by-default vs. permanent delete)."""
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="archive_nodes",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="archive_nodes", decision=decision
        )
        denied.setdefault("node_ids", [])
        return denied
    if staging is not None:
        return staging.set_nodes_archived(
            node_ids, archived, graph_access=decision.graph_access
        )

    if decision.graph_access.enabled:
        node_ids = [
            nid
            for nid in node_ids
            if access.is_node_visible(storage.get_node(nid), decision.graph_access)
        ]

    event_context = access.build_event_context(
        target="archive_nodes",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    try:
        nodes = storage.set_nodes_archived(
            node_ids, archived, event_context=event_context
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e, node_ids=[])
    return access.attach_mutation_attribution(
        {
            "success": True,
            "archived": archived,
            "node_ids": [n.id for n in nodes],
            "nodes": serialize_nodes(nodes),
            "action": "update_in_visualization",
        },
        event_context,
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
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="add_edge",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="add_edge", decision=decision
        )
    if (
        staging is None
        and decision.graph_access.enabled
        and not (
            access.is_node_visible(storage.get_node(source), decision.graph_access)
            and access.is_node_visible(storage.get_node(target), decision.graph_access)
        )
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

    if staging is not None:
        return staging.add_edge(edge, graph_access=decision.graph_access)

    event_context = access.build_event_context(
        target="add_edge",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    try:
        edge_id = storage.add_edge(edge, event_context=event_context)
    except EntityRevisionConflict as e:
        return _create_conflict_result(e)
    except _WRITE_ERRORS as e:
        return write_error_result(e)
    except ValueError as e:
        return {"success": False, "message": str(e)}
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
    expected_revision: Optional[int] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="update_edge",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="update_edge", decision=decision
        )
    if staging is not None:
        try:
            return staging.update_edge(
                edge_id,
                updates,
                expected_revision=expected_revision,
                graph_access=decision.graph_access,
            )
        except EntityRevisionConflict as e:
            return write_error_result(e)
        except ValueError as e:
            return {
                "success": False,
                "message": str(e),
                "error_code": VALIDATION_ERROR_CODE,
            }
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

    try:
        updated_edge = storage.update_edge(
            edge_id,
            updates,
            event_context=event_context,
            expected_revision=expected_revision,
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e)
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
            "error_code": VALIDATION_ERROR_CODE,
        }
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
    expected_revision: Optional[int] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="delete_edge",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="delete_edge", decision=decision
        )
    if staging is not None:
        try:
            return staging.delete_edge(
                edge_id,
                expected_revision=expected_revision,
                graph_access=decision.graph_access,
            )
        except EntityRevisionConflict as e:
            return write_error_result(e)
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

    try:
        deleted = storage.delete_edge(
            edge_id, event_context=event_context, expected_revision=expected_revision
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e)
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
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="delete_edges",
        graph_operation=_operation(staging),
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

    if staging is not None:
        return staging.delete_edges(edge_ids, graph_access=decision.graph_access)

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

    try:
        result = storage.delete_edges(edge_ids, event_context=event_context)
    except _WRITE_ERRORS as e:
        return write_error_result(e, deleted_edge_ids=[])
    return access.attach_mutation_attribution(
        serialize_delete_edges_result(result), event_context
    )


def set_edges_archived(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    edge_ids: List[str],
    archived: bool,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    """Archive or unarchive edges (hide-by-default vs. permanent delete)."""
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="archive_edges",
        graph_operation=_operation(staging),
    )
    if not decision.allowed:
        denied = access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="archive_edges", decision=decision
        )
        denied.setdefault("edge_ids", [])
        return denied
    if staging is not None:
        return staging.set_edges_archived(
            edge_ids, archived, graph_access=decision.graph_access
        )

    if decision.graph_access.enabled:
        edge_ids = [
            eid
            for eid in edge_ids
            if access.is_edge_visible(
                storage.edges.get(eid), storage, decision.graph_access
            )
        ]

    event_context = access.build_event_context(
        target="archive_edges",
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )

    try:
        edges = storage.set_edges_archived(
            edge_ids, archived, event_context=event_context
        )
    except _WRITE_ERRORS as e:
        return write_error_result(e, edge_ids=[])
    return access.attach_mutation_attribution(
        {
            "success": True,
            "archived": archived,
            "edge_ids": [e.id for e in edges],
            "edges": serialize_edges(edges),
            "action": "update_in_visualization",
        },
        event_context,
    )


# ---------------------------------------------------------------------------
# Federation adoption
# ---------------------------------------------------------------------------

# Bookkeeping keys FederationManager._build_cache stamps onto a cached
# federated node's metadata (see backend/federation/manager.py). adopt_federated_node
# starts the new local node's metadata as a copy of the cached node's metadata, so
# these must be stripped: left in place, they make the freshly adopted node -- a
# full local copy the user now owns -- keep matching access.node_graph_id and
# reporting as federated (e.g. inflating search_graph's federated_nodes count),
# and could make graph-scope narrowing hide it as if it still lived in the
# origin graph. The node's federation lineage is preserved separately, nested
# under metadata["adopted_from"].
_FEDERATION_BOOKKEEPING_METADATA_KEYS = (
    "origin_graph_id",
    "origin_graph_name",
    "origin_node_id",
    "federation_distance",
    "federation_path",
    "sync_state",
    "is_federated",
)


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
    staging: Optional["StagedWrite"] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook,
        action=GRAPH_ACTION_MUTATE,
        target="adopt_federated_node",
        graph_operation=_operation(staging),
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
        if staging is not None:
            already = staging.find_adopted(source_node.id)
            adopted_nodes = [already] if already is not None else []
        else:
            adopted_nodes = list(storage.nodes.values())
        for local_node in adopted_nodes:
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
    for key in _FEDERATION_BOOKKEEPING_METADATA_KEYS:
        metadata.pop(key, None)
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
        subtypes=list(source_node.subtypes),
        aliases=list(source_node.aliases),
        metadata=metadata,
    )

    # The source reference below copies the same metadata, so this covers it.
    depth_error = metadata_depth_error(local_node.metadata)
    if depth_error:
        return {
            "success": False,
            "message": f"Error validating input: {depth_error}",
            "added_node_ids": [],
            "added_edge_ids": [],
        }

    if local_node.type_str in _SAVED_VIEW_NODE_TYPES:
        annotation_error = saved_view_annotation_error(local_node.metadata)
        if annotation_error:
            return {
                "success": False,
                "message": f"Error validating input: {annotation_error}",
                "added_node_ids": [],
                "added_edge_ids": [],
            }

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
    if staging is not None:
        if not staging.node_exists(source_reference.id):
            nodes_to_add.append(source_reference)
        staged = staging.add_nodes(
            nodes_to_add,
            [lineage_edge],
            expect_absent=False,
            graph_access=decision.graph_access,
        )
        if not staged.get("success"):
            return staged
        return {
            **staged,
            "message": "Federated node adoption staged in the session",
            "adopted_node": serialize_node(local_node),
            "source_node": serialize_node(source_node),
            "lineage_edge": serialize_edge(lineage_edge),
        }
    if storage.get_node(source_reference.id) is None:
        nodes_to_add.append(source_reference)

    try:
        result = storage.add_nodes(
            nodes_to_add, [lineage_edge], event_context=event_context
        )
    except EntityRevisionConflict as e:
        return _create_conflict_result(e, added_node_ids=[], added_edge_ids=[])
    except _WRITE_ERRORS as e:
        return write_error_result(e, added_node_ids=[], added_edge_ids=[])

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
