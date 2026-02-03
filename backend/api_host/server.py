"""
App Host Server - Unified FastAPI application exposing GraphService.

This module provides create_app() which builds a FastAPI application that:
- Exposes GraphService via REST API endpoints
- Registers MCP tools via FastMCP
- Serves static files for web app and widget
- Does NOT include LLM calls or chat logic (handled in later steps)

Usage:
    from backend.api_host import create_app

    # Default configuration
    app = create_app()

    # Custom configuration
    from backend.api_host.config import AppConfig
    config = AppConfig(graph_file="custom_graph.json")
    app = create_app(config)
"""

import os
from pathlib import Path
from typing import Optional, Dict, Any, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from mcp.server.fastmcp import FastMCP

from backend.core import GraphStorage
from backend.service import GraphService, create_rest_router, register_mcp_tools, json_serializer
from backend.ui import ChatService, DocumentService, create_ui_router

from .config import AppConfig


def create_app(
    config: Optional[AppConfig] = None,
    graph_storage: Optional[GraphStorage] = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Optional configuration object. If None, uses defaults from environment.
        graph_storage: Optional pre-configured GraphStorage instance.
                      If None, creates one based on config.

    Returns:
        Configured FastAPI application with REST API, MCP tools, and static file serving.
    """
    # Use default config if not provided
    if config is None:
        config = AppConfig.from_env()

    # Create FastAPI app
    app = FastAPI(
        title="Community Knowledge Graph",
        description="REST API and MCP server for community knowledge graph operations",
        version="1.0.0",
    )

    # Add CORS middleware to allow external clients (like ChatGPT MCP connector)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins for MCP clients
        allow_credentials=True,
        allow_methods=["*"],  # Allow all methods (GET, POST, OPTIONS, etc.)
        allow_headers=["*"],  # Allow all headers
    )

    # Initialize graph storage if not provided
    if graph_storage is None:
        graph_path = config.get_graph_path()
        graph_storage = GraphStorage(str(graph_path))

    # Initialize GraphService
    graph_service = GraphService(graph_storage)

    # Store service on app state for access in routes
    app.state.graph_service = graph_service
    app.state.graph_storage = graph_storage
    app.state.config = config

    # Create and mount REST API router
    rest_router = create_rest_router(graph_service)
    app.include_router(rest_router, prefix=config.api_prefix)

    # Create UI Backend services (ChatService and DocumentService)
    chat_service = ChatService(graph_service)
    document_service = DocumentService()

    # Store chat service on app state for access in routes
    app.state.chat_service = chat_service
    app.state.document_service = document_service

    # Create and mount UI Backend router
    ui_router = create_ui_router(chat_service, document_service)
    app.include_router(ui_router, prefix="/ui")

    # Initialize FastMCP and register tools
    # We add custom instructions to guide the LLM on how to use the tools effectively.
    instructions = """
    You are a helpful knowledge agent assisting users with the Community Knowledge Graph.

    SEARCH STRATEGY:
    - Start with broad search terms (e.g., "AI" instead of "AI projects in Sweden").
    - If a search yields no results, try broader terms or synonyms.
    - An empty query or "*" returns a list of nodes (limited by 'limit').

    DATA MANAGEMENT:
    - ALWAYS check for existing nodes/actors using 'find_similar_nodes' before creating new ones.
    - Avoid creating generic actor nodes like "Universities" or "Research Institutes". Be specific.
    - When adding initiatives, try to link them to existing actors and communities.

    VISUALIZATION:
    - If the user asks to see the graph visually or mentions "widget", "canvas", or "visualize",
      provide them with the Widget URL (available via 'get_presentation').
      Normally this URL is: https://{host}/widget/
    """

    mcp = FastMCP(config.mcp_name, instructions=instructions)
    tools_map = register_mcp_tools(mcp, graph_service)

    # Store MCP instance and tools map on app state
    app.state.mcp = mcp
    app.state.tools_map = tools_map

    # Mount MCP HTTP endpoint
    # Use sse_app to provide standard /sse and /messages endpoints
    mcp_app = mcp.sse_app()

    # Add a middleware to handle browser requests to /mcp
    # The MCP endpoint expects MCP protocol requests (GET with Accept: text/event-stream for SSE),
    # not regular browser GET requests which would hang waiting for SSE.
    # We use a pure ASGI middleware class to avoid BaseHTTPMiddleware limitations with streaming responses.
    class MCPBrowserHandlerMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            path = scope.get("path", "")
            method = scope.get("method", "GET")

            # Simple request logging for MCP endpoints
            if path.startswith("/mcp"):
                import logging
                logging.getLogger("uvicorn.access").info(f"MCP Request: {method} {path}")

            # Only intercept specific paths if needed, here we check startswith /mcp
            # Note: scope['path'] does not include root_path, but here we assume standard mounting
            if path.startswith("/mcp"):
                headers = dict(scope.get("headers", []))
                # Headers are bytes
                accept_header = headers.get(b"accept", b"").decode("utf-8", errors="ignore")

                # If client expects SSE or is POST request, let it through to MCP app
                if "text/event-stream" in accept_header or method == "POST":
                    await self.app(scope, receive, send)
                    return

                # For regular browser GET requests, return helpful info
                if method == "GET":
                    response = JSONResponse({
                        "endpoint": "/mcp/sse",
                        "type": "MCP (Model Context Protocol) Server",
                        "description": "This endpoint is for MCP clients, not direct browser access.",
                        "usage": "Use an MCP-compatible client (like Claude Desktop or ChatGPT) to connect to this endpoint: /mcp/sse",
                        "protocol": "MCP uses Server-Sent Events (SSE) for streaming communication.",
                        "documentation": "https://modelcontextprotocol.io/",
                        "available_tools": list(tools_map.keys()),
                    })
                    await response(scope, receive, send)
                    return

            await self.app(scope, receive, send)

    app.add_middleware(MCPBrowserHandlerMiddleware)

    app.mount("/mcp", mcp_app)

    # Add execute_tool endpoint for direct tool execution
    @app.post("/execute_tool")
    async def execute_tool_endpoint(request: Request) -> JSONResponse:
        """Execute a graph tool directly by name."""
        try:
            body = await request.json()
            tool_name = body.get("tool_name")
            arguments = body.get("arguments", {})

            if not tool_name:
                return JSONResponse({"error": "No tool_name provided"}, status_code=400)

            if tool_name not in tools_map:
                return JSONResponse({"error": f"Tool {tool_name} not found"}, status_code=404)

            func = tools_map[tool_name]
            result = func(**arguments)

            import json
            return JSONResponse(json.loads(json.dumps(result, default=json_serializer)))
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, status_code=500)

    # Add export_graph endpoint (convenience route)
    @app.get("/export_graph")
    async def export_graph_endpoint() -> JSONResponse:
        """Export the entire graph (all nodes and edges)."""
        try:
            result = graph_service.export_graph()
            return JSONResponse(result)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            return JSONResponse({"error": str(e), "traceback": error_trace}, status_code=500)

    # Mount static files for web app
    _mount_static_files(app, config)

    # Add health check endpoint
    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint."""
        return {
            "status": "healthy",
            "graph_nodes": len(graph_storage.nodes),
            "graph_edges": len(graph_storage.edges),
        }

    # Root endpoint - redirect to web app
    @app.get("/")
    async def root() -> RedirectResponse:
        """Redirect root to web application."""
        return RedirectResponse(url="/web/", status_code=302)

    # API info endpoint
    @app.get("/info")
    async def info() -> Dict[str, Any]:
        """API information endpoint."""
        return {
            "name": "Community Knowledge Graph",
            "version": "1.0.0",
            "endpoints": {
                "api": config.api_prefix,
                "ui": "/ui",
                "mcp": "/mcp",
                "web": "/web",
                "widget": "/widget",
                "health": "/health",
            },
            "graph_stats": {
                "nodes": len(graph_storage.nodes),
                "edges": len(graph_storage.edges),
            },
            "llm_provider": chat_service.provider_type
        }

    return app


def _mount_static_files(app: FastAPI, config: AppConfig) -> None:
    """
    Mount static file directories for web app and widget.

    Only mounts directories that exist.
    """
    # Mount web app static files
    web_path = Path(config.web_static_path)
    if web_path.exists() and web_path.is_dir():
        app.mount("/web", StaticFiles(directory=str(web_path), html=True), name="web")
    else:
        # Create fallback route that returns a placeholder
        @app.get("/web/{path:path}")
        async def web_placeholder(path: str) -> JSONResponse:
            return JSONResponse(
                {"error": "Web app not built", "path": str(web_path)},
                status_code=404
            )

    # Mount widget static files
    widget_path = Path(config.widget_static_path)
    if widget_path.exists() and widget_path.is_dir():
        app.mount("/widget", StaticFiles(directory=str(widget_path), html=True), name="widget")
    else:
        # Create fallback route that returns a placeholder
        @app.get("/widget/{path:path}")
        async def widget_placeholder(path: str) -> JSONResponse:
            return JSONResponse(
                {"error": "Widget not built", "path": str(widget_path)},
                status_code=404
            )


def get_app() -> FastAPI:
    """
    Factory function for uvicorn.

    Usage:
        uvicorn app_host.server:get_app --factory
    """
    return create_app()
