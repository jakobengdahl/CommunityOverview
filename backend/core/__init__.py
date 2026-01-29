"""
backend.core - Core graph storage layer for Community Knowledge Graph

This package provides the foundational graph storage, persistence, and search
functionality without any dependencies on MCP, HTTP APIs, or external services.

Main components:
- GraphStorage: Main class for persisting and querying the graph
- VectorStore: Manages embeddings for semantic search
- Models: Data models for nodes, edges, and result types

Usage:
    from backend.core import GraphStorage, Node, Edge, NodeType, RelationshipType

    # Initialize storage
    storage = GraphStorage("path/to/graph.json")

    # Search nodes
    results = storage.search_nodes("query text")

    # Add nodes
    node = Node(type=NodeType.ACTOR, name="Test Actor")
    result = storage.add_nodes([node], [])
"""

# Core storage
from .storage import GraphStorage

# Vector store (for direct access if needed)
from .vector_store import VectorStore

# Data models
from .models import (
    # Enums
    NodeType,
    RelationshipType,
    NODE_COLORS,

    # Core models
    Node,
    Edge,

    # Result models
    SimilarNode,
    GraphStats,
    ProposedNodesResult,
    AddNodesResult,
    DeleteNodesResult,
)

__all__ = [
    # Core storage
    "GraphStorage",
    "VectorStore",

    # Enums
    "NodeType",
    "RelationshipType",
    "NODE_COLORS",

    # Core models
    "Node",
    "Edge",

    # Result models
    "SimilarNode",
    "GraphStats",
    "ProposedNodesResult",
    "AddNodesResult",
    "DeleteNodesResult",
]

__version__ = "1.0.0"
