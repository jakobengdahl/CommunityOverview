"""
JSON serialization utilities for graph_services.

Provides consistent serialization of graph objects for API responses.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Union

from graph_core import Node, Edge, SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult


def json_serializer(obj: Any) -> Any:
    """
    Custom JSON serializer for objects not serializable by default.

    Handles:
    - datetime objects -> ISO format strings
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def serialize_to_json(data: Any) -> Any:
    """
    Serialize data to JSON-compatible format.

    Uses custom serializer for datetime objects.
    Returns a JSON-safe dict/list structure.
    """
    return json.loads(json.dumps(data, default=json_serializer))


def serialize_node(node: Node) -> Dict[str, Any]:
    """Serialize a Node to a dictionary."""
    return serialize_to_json(node.model_dump())


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
        "match_reason": similar.match_reason
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
