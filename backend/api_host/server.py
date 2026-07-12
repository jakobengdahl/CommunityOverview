"""
App Host Server - Unified FastAPI application exposing GraphService.

This module provides create_app() which builds a FastAPI application that:
- Exposes GraphService via REST API endpoints
- Registers MCP tools via FastMCP
- Serves static files for web app and widget
- Does NOT include LLM calls or chat logic (handled in later steps)

create_app() is composition only: the auth/CORS middleware, startup
diagnostics, MCP mount shim, legacy session SSE, direct tool routes, system
routes and agent routes each live in their own module under this package.

Usage:
    from backend.api_host import create_app

    # Default configuration
    app = create_app()

    # Custom configuration
    from backend.api_host.config import AppConfig
    config = AppConfig(graph_file="custom_graph.json")
    app = create_app(config)
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# FastMCP is imported here (not only in mcp_mount) so it stays patchable as
# backend.api_host.server.FastMCP in tests that stub the MCP transport.
from mcp.server.fastmcp import FastMCP

from backend.core.session_registry import SessionRegistry
from backend.core.session_store import FileSessionPersistenceBackend, SessionStore
from backend.core.session_manager import SessionManager

from backend.core import GraphStorage
from backend.service import GraphService, create_rest_router, register_mcp_tools
from backend.ui import ChatService, DocumentService, create_ui_router
from backend.agents import AgentRegistry, AgentsSettings
from backend.federation import (
    FederationManager,
    load_federation_config,
    summarize_federation_config,
)

from .config import AppConfig
from .diagnostics import build_startup_diagnostics, emit_startup_diagnostics_log
from .middleware import add_auth_middleware, add_cors_middleware, compute_auth_active
from .mcp_mount import build_mcp_instructions, mount_mcp
from .session_stream import register_session_stream
from .tool_routes import register_tool_routes
from .system_routes import register_system_routes
from .agent_routes import register_agent_routes

logger = logging.getLogger(__name__)


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
    if config is None:
        config = AppConfig.from_env()

    app = FastAPI(
        title="Community Knowledge Graph",
        description="REST API and MCP server for community knowledge graph operations",
        version="1.0.0",
    )

    # Middleware order matters: auth is installed first so CORS (added next)
    # wraps it as the outermost layer, letting preflight requests through.
    _auth_active = compute_auth_active(config)
    add_auth_middleware(app, config)
    add_cors_middleware(app, config)

    federation_config = load_federation_config()
    federation_summary = summarize_federation_config(federation_config)

    # Initialize graph storage if not provided
    if graph_storage is None:
        graph_path = config.get_graph_path()
        graph_storage = GraphStorage(str(graph_path))

    def _on_federated_node_event(operation, before_node, after_node):
        graph_storage.emit_federated_node_event(
            operation=operation,
            node_before=before_node,
            node_after=after_node,
            event_origin="federation-sync",
        )

    def _on_federated_edge_event(operation, before_edge, after_edge):
        graph_storage.emit_federated_edge_event(
            operation=operation,
            edge_before=before_edge,
            edge_after=after_edge,
            event_origin="federation-sync",
        )

    federation_manager = FederationManager(
        federation_config,
        on_node_event=_on_federated_node_event,
        on_edge_event=_on_federated_edge_event,
    )

    # Initialize event system for webhook delivery
    graph_storage.setup_events(enabled=True)

    # Initialize GraphService
    graph_service = GraphService(graph_storage, federation_manager=federation_manager)

    # Run federation startup sync (best effort, never blocks startup on failures)
    federation_manager.sync_on_startup()
    federation_manager.start()

    # Initialize Agent Registry for background agent workers
    agent_settings = AgentsSettings.from_env()
    agent_registry = AgentRegistry(
        settings=agent_settings,
        graph_storage=graph_storage,
        graph_service=graph_service,
    )

    # Connect agent delivery callback to event system
    # This allows agent-linked subscriptions to route events internally
    def agent_delivery_callback(event, subscription_id: str) -> bool:
        """Route events to agent queues for agent-linked subscriptions."""
        if not agent_registry.is_enabled:
            return False
        if not agent_registry.is_agent_subscription(subscription_id):
            return False
        return agent_registry.enqueue_for_subscription(
            subscription_id, event.to_webhook_payload()
        )

    graph_storage.set_agent_delivery_callback(agent_delivery_callback)

    # Start agent registry (loads agents and starts workers)
    agent_registry.start()

    # Register system listener to update agent registry on Agent node changes
    def agent_lifecycle_listener(event):
        if event.entity.kind != "node" or event.entity.type != "Agent":
            return

        node_id = event.entity.id
        if event.event_type == "node.create":
            agent_registry.handle_agent_created(node_id)
        elif event.event_type == "node.update":
            agent_registry.handle_agent_updated(node_id)
        elif event.event_type == "node.delete":
            agent_registry.handle_agent_deleted(node_id)

    graph_storage.add_system_listener(agent_lifecycle_listener)

    # Store service on app state for access in routes
    app.state.graph_service = graph_service
    app.state.graph_storage = graph_storage
    app.state.agent_registry = agent_registry
    app.state.config = config
    app.state.federation_config = federation_config
    app.state.federation_summary = federation_summary
    app.state.federation_manager = federation_manager
    app.state.startup_diagnostics = build_startup_diagnostics(
        config=config,
        graph_storage=graph_storage,
        federation_summary=federation_summary,
        federation_manager=federation_manager,
        agent_registry=agent_registry,
    )
    emit_startup_diagnostics_log(app.state.startup_diagnostics)

    # Initialize server-side shared-session store + manager (multi-user sessions).
    # Sessions live outside the graph, one JSON file per session under a
    # directory next to the graph data (design D4/D5). The op-driven store is now
    # the single source of truth for session state; the legacy /sessions/{id}/stream
    # channel below is kept only to deliver MCP visualization pushes to the browser
    # (design §3.8) — the browser no longer uploads canvas state, MCP tools read it
    # from this store.
    sessions_dir = config.sessions_dir or str(
        config.get_graph_path().parent / "sessions"
    )
    session_store = SessionStore(FileSessionPersistenceBackend(sessions_dir))
    session_manager = SessionManager(session_store)
    app.state.session_store = session_store
    app.state.session_manager = session_manager

    # Create and mount REST API router
    rest_router = create_rest_router(graph_service, session_manager=session_manager)
    app.include_router(rest_router, prefix=config.api_prefix)

    # Create UI Backend services (ChatService and DocumentService)
    chat_service = ChatService(graph_service)
    document_service = DocumentService()

    # Load skills for expert agents that have skills_urls configured.
    # Runs once synchronously at startup (a new event loop is created so this
    # is safe regardless of whether the caller is inside an async context).
    try:
        from backend.config_loader import get_expert_agent_configs, get_skills_config

        _expert_configs = get_expert_agent_configs()
        if any(e.skills_urls for e in _expert_configs):
            chat_service.load_expert_skills_sync(_expert_configs, get_skills_config())
            logger.info(
                "Expert agent skills loaded for %d expert(s) with skills_urls",
                sum(1 for e in _expert_configs if e.skills_urls),
            )
    except Exception as _exc:
        logger.warning("Expert agent skills startup load failed (non-fatal): %s", _exc)

    # Store chat service on app state for access in routes
    app.state.chat_service = chat_service
    app.state.document_service = document_service

    # Create and mount UI Backend router
    ui_router = create_ui_router(chat_service, document_service)
    app.include_router(ui_router, prefix="/ui")

    # Initialize visualization session registry.
    # The asyncio event loop reference is injected in the startup handler
    # (registered below) so that sync MCP tools can push commands thread-safely.
    session_registry = SessionRegistry()
    app.state.session_registry = session_registry
    register_session_stream(app, session_registry)

    # Initialize FastMCP with dynamic instructions built from schema configuration
    mcp = FastMCP(
        config.mcp_name,
        instructions=build_mcp_instructions(),
        # Disable DNS rebinding protection: the default host="127.0.0.1"
        # auto-enables it, which rejects any Host header that isn't localhost.
        # In production (Cloud Run) the Host header is the public URL, so
        # requests get a 421 "Invalid Host header". Authentication is handled
        # by the gateway / Cloud Run IAP, so this check is not needed.
        host="0.0.0.0",
    )
    tools_map = register_mcp_tools(
        mcp,
        graph_service,
        session_registry=session_registry,
        session_manager=session_manager,
    )

    # Store MCP instance and tools map on app state
    app.state.mcp = mcp
    app.state.tools_map = tools_map

    mount_mcp(app, mcp, tools_map)

    # Direct tool-execution and export endpoints
    register_tool_routes(app, graph_service, tools_map, _auth_active)

    # System / operability routes (favicon, health, info, logout, …).
    # Their paths are disjoint from the /web and /widget static mounts, so the
    # registration order relative to those mounts does not affect resolution.
    register_system_routes(
        app,
        config,
        graph_storage,
        chat_service,
        federation_summary,
        federation_manager,
    )

    # Mount static files for web app and widget
    _mount_static_files(app, config)

    # Federation + agent-system status endpoints
    register_agent_routes(app, agent_registry, graph_storage, federation_manager)

    # Shutdown handler for graceful cleanup
    @app.on_event("shutdown")
    async def shutdown_event():
        """Gracefully shutdown agent registry and event system."""
        if hasattr(app.state, "session_cleanup_task"):
            app.state.session_cleanup_task.cancel()
        federation_manager.stop()
        agent_registry.stop()
        graph_storage.shutdown_events()

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
                {"error": "Web app not built", "path": str(web_path)}, status_code=404
            )

    # Mount widget static files
    widget_path = Path(config.widget_static_path)
    if widget_path.exists() and widget_path.is_dir():
        app.mount(
            "/widget", StaticFiles(directory=str(widget_path), html=True), name="widget"
        )
    else:
        # Create fallback route that returns a placeholder
        @app.get("/widget/{path:path}")
        async def widget_placeholder(path: str) -> JSONResponse:
            return JSONResponse(
                {"error": "Widget not built", "path": str(widget_path)}, status_code=404
            )


def get_app() -> FastAPI:
    """
    Factory function for uvicorn.

    Usage:
        uvicorn backend.api_host.server:get_app --factory
    """
    return create_app()
