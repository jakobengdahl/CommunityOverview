"""
graph_services - Service layer for Community Knowledge Graph

This package provides a unified service interface for graph operations,
supporting multiple client protocols (REST API, MCP, WebSocket, etc.).

Architecture:
- GraphService: Business logic layer wrapping graph_core
- REST API: FastAPI router for HTTP endpoints
- MCP Tools: Model Context Protocol tool registration

Key design principles:
- No LLM calls in this layer (handled separately by chat_logic)
- Consistent response format across all protocols
- Stateless operations (state managed by graph_core)
- Thread-safe through graph_core

Usage:
    from graph_services import GraphService, create_rest_router, register_mcp_tools
    from graph_core import GraphStorage
    from mcp.server.fastmcp import FastMCP
    from fastapi import FastAPI

    # Initialize storage and service
    storage = GraphStorage("graph.json")
    service = GraphService(storage)

    # REST API setup
    app = FastAPI()
    router = create_rest_router(service)
    app.include_router(router, prefix="/api/graph")

    # MCP setup
    mcp = FastMCP("community-knowledge-graph")
    tools_map = register_mcp_tools(mcp, service)
"""

from .service import GraphService
from .rest_api import create_rest_router
from .mcp_tools import register_mcp_tools
from .serializers import (
    json_serializer,
    serialize_to_json,
    serialize_node,
    serialize_nodes,
    serialize_edge,
    serialize_edges,
    serialize_similar_node,
    serialize_similar_nodes,
    serialize_graph_stats,
    serialize_add_result,
    serialize_delete_result,
)

__all__ = [
    # Core service
    "GraphService",

    # Protocol adapters
    "create_rest_router",
    "register_mcp_tools",

    # Serialization utilities
    "json_serializer",
    "serialize_to_json",
    "serialize_node",
    "serialize_nodes",
    "serialize_edge",
    "serialize_edges",
    "serialize_similar_node",
    "serialize_similar_nodes",
    "serialize_graph_stats",
    "serialize_add_result",
    "serialize_delete_result",
]

__version__ = "1.0.0"
