"""
JSON serialization utilities for graph_services.

Provides consistent serialization of graph objects for API responses.
"""

import json
from datetime import datetime
from typing import Any, Dict, List

from backend.core import (
    Node,
    NodeType,
    Edge,
    SimilarNode,
    GraphStats,
    AddNodesResult,
    DeleteNodesResult,
    DeleteEdgesResult,
)
from backend.core.session_annotations import sanitize_saved_view_metadata

# Every generic node read (search, get_node_details, add_nodes/update_node
# responses, ...) funnels through serialize_node, including the "double-click
# a SavedView node to open it" flow in App.jsx — which reads
# metadata.annotations straight off an already-serialized node rather than
# calling get_saved_view again. Sanitizing here, not only in
# views.get_saved_view, is what makes that second read path safe too.
_SAVED_VIEW_NODE_TYPES = frozenset(
    {NodeType.SAVED_VIEW.value, NodeType.VISUALIZATION_VIEW.value}
)


def json_serializer(obj: Any) -> Any:
    """
    Custom JSON serializer for objects not serializable by default.

    Handles:
    - datetime objects -> ISO format strings
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def json_key_text(key: Any) -> str:
    """Convert a dict key the way json.dumps does (skipkeys=False)."""
    if isinstance(key, str):
        return key
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, int):
        return int.__repr__(key)
    if isinstance(key, float):
        return float.__repr__(key)
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key)}")


def _to_json_native(data: Any) -> Any:
    """The json.dumps/json.loads round trip of serialize_to_json, iteratively.

    Containers are copied once each (memoized by id), so shared references
    stay shared and a circular one terminates.
    """
    copies: Dict[int, Any] = {}
    pending: List[Any] = []

    def convert(value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        # Subclasses (an enum member, say) come back as their JSON base type.
        if isinstance(value, str):
            return str.__str__(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return float(value)
        if isinstance(value, (dict, list, tuple)):
            if id(value) not in copies:
                copy: Any = dict(value) if isinstance(value, dict) else list(value)
                copies[id(value)] = copy
                pending.append(copy)
            return copies[id(value)]
        return convert(json_serializer(value))

    root = convert(data)
    while pending:
        container = pending.pop()
        if isinstance(container, dict):
            items = list(container.items())
            container.clear()
            for key, value in items:
                container[json_key_text(key)] = convert(value)
        else:
            for index, value in enumerate(container):
                container[index] = convert(value)
    return root


def serialize_to_json(data: Any) -> Any:
    """
    Serialize data to JSON-compatible format.

    Uses custom serializer for datetime objects.
    Returns a JSON-safe dict/list structure.
    """
    try:
        return json.loads(json.dumps(data, default=json_serializer))
    except RecursionError:
        # Nesting past the C json recursion limit, which Python 3.11 ties to
        # the interpreter's; a graph may still hold such a node, so convert
        # without recursing rather than fail its read and every export.
        return _to_json_native(data)


def serialize_node(node: Node) -> Dict[str, Any]:
    """
    Serialize a Node to a dictionary.
    Excludes large internal fields like 'embedding'.

    A SavedView/VisualizationView node additionally has its annotation
    metadata passed through ``sanitize_saved_view_metadata`` — see
    ``_SAVED_VIEW_NODE_TYPES`` above for why every generic read needs this,
    not only ``get_saved_view``.
    """
    data = node.model_dump(exclude={"embedding"})
    if node.type_str in _SAVED_VIEW_NODE_TYPES and isinstance(
        data.get("metadata"), dict
    ):
        data["metadata"] = sanitize_saved_view_metadata(data["metadata"])
    return serialize_to_json(data)


def serialize_edge(edge: Edge) -> Dict[str, Any]:
    """Serialize an Edge to a dictionary."""
    return serialize_to_json(edge.model_dump())


def serialize_nodes(nodes: List[Node]) -> List[Dict[str, Any]]:
    """Serialize a list of Nodes to dictionaries."""
    return [serialize_node(node) for node in nodes]


def serialize_edges(edges: List[Edge]) -> List[Dict[str, Any]]:
    """Serialize a list of Edges to dictionaries."""
    return [serialize_edge(edge) for edge in edges]


def serialize_similar_node(similar: SimilarNode) -> Dict[str, Any]:
    """Serialize a SimilarNode to a dictionary."""
    return {
        "node": serialize_node(similar.node),
        "similarity_score": similar.similarity_score,
        "match_reason": similar.match_reason,
    }


def serialize_similar_nodes(similar_nodes: List[SimilarNode]) -> List[Dict[str, Any]]:
    """Serialize a list of SimilarNodes to dictionaries."""
    return [serialize_similar_node(s) for s in similar_nodes]


def serialize_graph_stats(stats: GraphStats) -> Dict[str, Any]:
    """Serialize GraphStats to a dictionary."""
    return serialize_to_json(stats.model_dump())


def serialize_add_result(result: AddNodesResult) -> Dict[str, Any]:
    """Serialize AddNodesResult to a dictionary."""
    return result.model_dump()


def serialize_delete_result(result: DeleteNodesResult) -> Dict[str, Any]:
    """Serialize DeleteNodesResult to a dictionary."""
    return result.model_dump()


def serialize_delete_edges_result(result: DeleteEdgesResult) -> Dict[str, Any]:
    """Serialize DeleteEdgesResult to a dictionary."""
    return result.model_dump()
