"""
backend.core - Core graph storage layer for Community Knowledge Graph

This package provides the foundational graph storage, persistence, and search
functionality without any dependencies on MCP, HTTP APIs, or external services.

Main components:
- GraphStorage: Main class for persisting and querying the graph
- VectorStore: Manages embeddings for semantic search
- Models: Data models for nodes, edges, and result types
- Events: Event system for graph mutation webhooks

Usage:
    from backend.core import GraphStorage, Node, Edge, NodeType, RelationshipType

    # Initialize storage
    storage = GraphStorage("path/to/graph.json")

    # Enable event system for webhooks
    storage.setup_events(enabled=True)

    # Search nodes
    results = storage.search_nodes("query text")

    # Add nodes with event context
    from backend.core import EventContext
    ctx = EventContext(event_origin="web-ui", event_session_id="abc123")
    node = Node(type=NodeType.ACTOR, name="Test Actor")
    result = storage.add_nodes([node], [], event_context=ctx)
"""

# Core storage
from .storage import GraphStorage

# Vector store (for direct access if needed)
from .vector_store import VectorStore

# Data models
from .models import (
    # Enums (legacy, kept for backward compatibility)
    NodeType,
    RelationshipType,
    NODE_COLORS,

    # Dynamic type functions (new, config-based)
    get_node_type_names,
    get_relationship_type_names,
    is_valid_node_type,
    is_valid_relationship_type,
    get_node_color,
    NODE_COLORS_LOOKUP,

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

# Event system
from .events import EventContext, EventOrigin

__all__ = [
    # Core storage
    "GraphStorage",
    "VectorStore",

    # Enums (legacy)
    "NodeType",
    "RelationshipType",
    "NODE_COLORS",

    # Dynamic type functions (new)
    "get_node_type_names",
    "get_relationship_type_names",
    "is_valid_node_type",
    "is_valid_relationship_type",
    "get_node_color",
    "NODE_COLORS_LOOKUP",

    # Core models
    "Node",
    "Edge",

    # Result models
    "SimilarNode",
    "GraphStats",
    "ProposedNodesResult",
    "AddNodesResult",
    "DeleteNodesResult",

    # Event system
    "EventContext",
    "EventOrigin",
]

__version__ = "1.0.0"
