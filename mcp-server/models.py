"""
Backward-compatible wrapper for graph_core.models

This module re-exports all models from graph_core for backward compatibility.
New code should import directly from graph_core instead.

Example:
    # Old way (still works)
    from models import Node, Edge, NodeType

    # New way (preferred)
    from graph_core import Node, Edge, NodeType
"""

# Re-export everything from graph_core.models
from graph_core.models import (
    NodeType,
    RelationshipType,
    NODE_COLORS,
    Node,
    Edge,
    SimilarNode,
    GraphStats,
    ProposedNodesResult,
    AddNodesResult,
    DeleteNodesResult,
)

__all__ = [
    "NodeType",
    "RelationshipType",
    "NODE_COLORS",
    "Node",
    "Edge",
    "SimilarNode",
    "GraphStats",
    "ProposedNodesResult",
    "AddNodesResult",
    "DeleteNodesResult",
]
