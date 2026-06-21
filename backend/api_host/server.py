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

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.requests import Request
from mcp.server.fastmcp import FastMCP

from backend.core import GraphStorage
from backend.llm_providers import get_llm_availability
from backend.service import GraphService, create_rest_router, register_mcp_tools, json_serializer
from backend.ui import ChatService, DocumentService, create_ui_router
from backend.agents import AgentRegistry, AgentsSettings
from backend.federation import FederationManager, load_federation_config, summarize_federation_config
from backend import config_loader
from backend.authorization import use_request_authorization
from backend.language_policy import format_language_policy_for_prompt

from .config import AppConfig


logger = logging.getLogger(__name__)


_PUBLIC_STARTUP_DIAGNOSTICS_PATH = "/diagnostics/startup"
_PUBLIC_READINESS_PATH = "/ready"


def _count_enabled_capabilities(capabilities: Dict[str, Any]) -> Dict[str, int]:
    manifest = capabilities.get("capabilities", [])
    return {
        "configured": len(manifest),
        "enabled": sum(1 for capability in manifest if capability.get("enabled", True)),
        "disabled": sum(1 for capability in manifest if not capability.get("enabled", True)),
    }


def _collect_graph_integrity_diagnostics(graph_storage: GraphStorage) -> Dict[str, Any]:
    dangling_edges = 0
    self_referencing_edges = 0

    for edge in graph_storage.edges.values():
        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)

        if source == target:
            self_referencing_edges += 1

        if source not in graph_storage.nodes or target not in graph_storage.nodes:
            dangling_edges += 1

    return {
        "status": "ok" if dangling_edges == 0 else "degraded",
        "node_count": len(graph_storage.nodes),
        "edge_count": len(graph_storage.edges),
        "dangling_edge_count": dangling_edges,
        "self_referencing_edge_count": self_referencing_edges,
    }


def _build_public_tenant_context(tenant_context: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize tenant configuration without exposing tenant identifiers."""
    return {
        "environment": tenant_context["environment"],
        "tenant_id_configured": bool(tenant_context["tenant_id"]),
        "tenant_name_configured": bool(tenant_context["tenant_name"]),
        "tenant_context_configured": bool(
            tenant_context["tenant_id"] or tenant_context["tenant_name"]
        ),
    }


def _build_public_config_context(config_context: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize config resolution context without exposing tenant identifiers."""
    return {
        "environment": config_context["environment"],
        "tenant_context_configured": bool(
            config_context["tenant_id"] or config_context["tenant_name"]
        ),
        "tenant_config_dir_configured": config_context["tenant_config_dir_configured"],
        "schema_config_source": config_context["schema_config_source"],
        "federation_config_source": config_context["federation_config_source"],
    }


def _build_startup_diagnostics(
    *,
    config: AppConfig,
    graph_storage: GraphStorage,
    federation_summary: Dict[str, Any],
    federation_manager: FederationManager,
    agent_registry: AgentRegistry,
) -> Dict[str, Any]:
    runtime_info = config_loader.get_runtime_info()
    config_context = config_loader.get_config_context()
    tenant_context = config_loader.get_tenant_context()
    request_actor = config_loader.get_request_actor_info()
    request_scope = config_loader.get_request_scope_info()
    request_selection = config_loader.get_request_graph_selection_info()
    capabilities = config_loader.get_capabilities()
    capability_summary = _count_enabled_capabilities(capabilities)
    graph_integrity = _collect_graph_integrity_diagnostics(graph_storage)
    federation_runtime = federation_manager.get_status()
    agent_status = agent_registry.get_all_status()
    public_tenant_context = _build_public_tenant_context(tenant_context)
    public_config_context = _build_public_config_context(config_context)

    llm_availability = get_llm_availability()

    checks = {
        "config": {
            "status": "ok",
            "schema_config_source": config_context["schema_config_source"],
            "federation_config_source": config_context["federation_config_source"],
            "tenant_config_dir_configured": config_context["tenant_config_dir_configured"],
        },
        "graph_storage": {
            "status": graph_integrity["status"],
            "graph_nodes": len(graph_storage.nodes),
            "graph_edges": len(graph_storage.edges),
            "integrity": graph_integrity,
        },
        "event_delivery": {
            "status": "ok" if getattr(graph_storage, "_events_enabled", False) else "disabled",
        },
        "llm": {
            "status": "ok" if llm_availability["available"] else "no_key",
            "available": llm_availability["available"],
            "provider": llm_availability["provider"],
        },
        "agents": {
            "status": "ok" if agent_status["enabled"] else "disabled",
            "enabled": agent_status["enabled"],
            "worker_count": agent_status["worker_count"],
            "subscription_count": agent_status["subscription_count"],
            "configured_integrations": len(agent_status.get("mcp_integrations", [])),
        },
        "federation": {
            "status": federation_runtime.get("status", "disabled" if not federation_summary.get("enabled") else "ok"),
            "enabled": federation_summary.get("enabled", False),
            "configured_graphs": federation_summary.get("configured_graphs", 0),
            "active_graphs": federation_summary.get("active_graphs", 0),
        },
    }

    blocking_failures = [
        name for name, check in checks.items()
        if name in {"config", "graph_storage"} and check.get("status") not in {"ok", "disabled"}
    ]
    readiness_status = "ready" if not blocking_failures else "not_ready"

    diagnostics = {
        "status": readiness_status,
        "config_profile": config.config_profile,
        "runtime": runtime_info,
        "tenant_context": public_tenant_context,
        "config_context": public_config_context,
        "request_context_defaults": {
            "actor": request_actor,
            "scope": request_scope,
            "selection": request_selection,
        },
        "capabilities": capability_summary,
        "checks": checks,
        "warnings": [],
    }

    if graph_integrity["status"] != "ok":
        diagnostics["warnings"].append("graph_integrity_degraded")
    federation_check_status = checks["federation"]["status"]
    if federation_check_status not in {"ok", "disabled", "healthy"}:
        diagnostics["warnings"].append("federation_runtime_degraded")

    return diagnostics


def _emit_startup_diagnostics_log(diagnostics: Dict[str, Any]) -> None:
    logger.info(
        "startup_diagnostics %s",
        json.dumps(diagnostics, sort_keys=True),
    )


def _build_mcp_instructions() -> str:
    """Build MCP instructions dynamically from the loaded schema configuration."""
    schema = config_loader.get_schema()
    presentation = config_loader.get_presentation()
    node_types = schema.get("node_types", {})
    relationship_types = schema.get("relationship_types", {})

    # Build node type descriptions
    domain_lines = []
    system_lines = []
    for name, cfg in node_types.items():
        category = cfg.get("category", "domain")
        desc = cfg.get("description", "")
        entry = f"  - {name}: {desc}" if desc else f"  - {name}"
        if category == "system":
            system_lines.append(entry)
        else:
            domain_lines.append(entry)

    # Build relationship type descriptions
    rel_lines = []
    for name, cfg in relationship_types.items():
        desc = cfg.get("description", "")
        entry = f"  - {name}: {desc}" if desc else f"  - {name}"
        rel_lines.append(entry)

    # Include prompt_prefix if configured (contains domain-specific context)
    prompt_context = ""
    if presentation.get("prompt_prefix"):
        prompt_context = f"\nDOMAIN CONTEXT:\n{presentation['prompt_prefix']}\n"

    language_policy_section = format_language_policy_for_prompt(presentation, external_agent=True)

    instructions = f"""You are a helpful knowledge agent for: {presentation.get('title', 'Knowledge Graph')}.
{prompt_context}
METADATA MODEL — Node Types available in this graph:

Domain types (the core concepts users work with):
{chr(10).join(domain_lines)}

System types (infrastructure, not usually queried directly):
{chr(10).join(system_lines)}

Relationship types:
{chr(10).join(rel_lines)}

SEARCH STRATEGY:
- Start with broad search terms (e.g., "AI" instead of "AI projects in Sweden").
- If a search yields no results, try broader terms or synonyms.
- An empty query or "*" returns a list of nodes (limited by 'limit').
- Use the node_types parameter to filter by type (e.g., node_types=["Klassifikation"]).
- When users ask about a concept that matches a node type name, search for that type.

DATA MANAGEMENT:
- ALWAYS check for existing nodes using 'find_similar_nodes' before creating new ones.
- When adding nodes, use the correct type from the metadata model above.
- Use 'get_subtypes' to find existing sub-classifications before adding new ones.
- Follow the graph language policy below for any new or updated graph content.

{language_policy_section}
VISUALIZATION:
- If the user asks to see the graph visually or mentions "widget", "canvas", or "visualize",
  provide them with the Widget URL (available via 'get_presentation').
"""
    return instructions


def _access_denied_json_response(result: Any) -> Optional[JSONResponse]:
    if isinstance(result, dict) and result.get("error_code") == "access_denied":
        return JSONResponse(result, status_code=403)
    return None


def _bind_request_authorization_to_asgi_app(asgi_app):
    async def request_bound_app(scope, receive, send):
        if scope.get("type") != "http":
            await asgi_app(scope, receive, send)
            return

        with use_request_authorization(headers=Headers(scope=scope)):
            await asgi_app(scope, receive, send)

    return request_bound_app


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

    # Add Basic Auth Middleware if enabled
    # Two modes:
    #   1. auth_enabled=True: Basic Auth on ALL endpoints (except /health, /info)
    #   2. mcp_basic_auth=True: Basic Auth ONLY on /mcp and /execute_tool endpoints
    #      (for deployments where the rest is protected by Cloud Run/IAP)
    if config.auth_password and (config.auth_enabled or config.mcp_basic_auth):
        import base64
        import secrets

        @app.middleware("http")
        async def basic_auth_middleware(request: Request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)

            # Allow public liveness/readiness/diagnostics and logout-related
            # routes without auth. /auth/logout and /logged-out must be
            # reachable without a valid session, otherwise the user ends up in
            # an auth loop.
            if request.url.path in [
                "/health",
                _PUBLIC_READINESS_PATH,
                "/info",
                _PUBLIC_STARTUP_DIAGNOSTICS_PATH,
                "/auth/logout",
                "/logged-out",
            ]:
                return await call_next(request)

            # In MCP-only mode, only require auth for MCP and execute_tool paths
            if config.mcp_basic_auth and not config.auth_enabled:
                path = request.url.path
                if not (path.startswith("/mcp") or path.startswith("/execute_tool")):
                    return await call_next(request)

            # Check for Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                    headers={"WWW-Authenticate": "Basic"},
                )

            try:
                scheme, credentials = auth_header.split()
                if scheme.lower() != 'basic':
                    raise ValueError

                decoded = base64.b64decode(credentials).decode("utf-8")
                username, _, password = decoded.partition(":")

                is_correct_username = secrets.compare_digest(
                    username, config.auth_username
                )
                is_correct_password = secrets.compare_digest(
                    password, config.auth_password or ""
                )


                if not (is_correct_username and is_correct_password):
                    raise ValueError
            except (ValueError, Exception):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid credentials"},
                    headers={"WWW-Authenticate": "Basic"},
                )

            return await call_next(request)

    # Add CORS middleware to allow external clients (like ChatGPT MCP connector)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins for MCP clients
        allow_credentials=True,
        allow_methods=["*"],  # Allow all methods (GET, POST, OPTIONS, etc.)
        allow_headers=["*"],  # Allow all headers
    )

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
            subscription_id,
            event.to_webhook_payload()
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
    app.state.startup_diagnostics = _build_startup_diagnostics(
        config=config,
        graph_storage=graph_storage,
        federation_summary=federation_summary,
        federation_manager=federation_manager,
        agent_registry=agent_registry,
    )
    _emit_startup_diagnostics_log(app.state.startup_diagnostics)

    # Create and mount REST API router
    rest_router = create_rest_router(graph_service)
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

    # Initialize FastMCP with dynamic instructions built from schema configuration
    instructions = _build_mcp_instructions()

    mcp = FastMCP(
        config.mcp_name,
        instructions=instructions,
        # Disable DNS rebinding protection: the default host="127.0.0.1"
        # auto-enables it, which rejects any Host header that isn't localhost.
        # In production (Cloud Run) the Host header is the public URL, so
        # requests get a 421 "Invalid Host header". Authentication is handled
        # by the gateway / Cloud Run IAP, so this check is not needed.
        host="0.0.0.0",
    )
    tools_map = register_mcp_tools(mcp, graph_service)

    # Store MCP instance and tools map on app state
    app.state.mcp = mcp
    app.state.tools_map = tools_map

    # Mount MCP HTTP endpoints.
    # Two transports are supported:
    #   1. Legacy SSE  (GET /mcp/sse + POST /mcp/messages) – for older clients
    #   2. Streamable HTTP (POST /mcp) – for ChatGPT, Claude, and MCP spec ≥2025-03-26
    mcp_sse_app = _bind_request_authorization_to_asgi_app(mcp.sse_app())

    # Try to create Streamable HTTP app (requires mcp ≥ 1.8).
    # If the installed version doesn't support it, fall back to SSE-only.
    try:
        mcp_streamable_app = _bind_request_authorization_to_asgi_app(mcp.streamable_http_app())
        _has_streamable = True
    except (AttributeError, TypeError):
        mcp_streamable_app = None
        _has_streamable = False

    # Wrap the MCP apps with a handler for browser requests.
    # Regular browser GETs would otherwise hang waiting for SSE.
    # This ASGI middleware routes requests to the correct transport.
    class MCPBrowserHandler:
        def __init__(self, sse_app, streamable_app=None):
            self.sse_app = sse_app
            self.streamable_app = streamable_app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.sse_app(scope, receive, send)
                return

            path = scope.get("path", "")
            method = scope.get("method", "GET")
            is_root = path in ("", "/")

            import logging
            mcp_logger = logging.getLogger("mcp.server")
            if not mcp_logger.handlers:
                logging.basicConfig()
            mcp_logger.info(f"MCP Request: {method} /mcp{path}")

            headers = dict(scope.get("headers", []))
            accept_header = headers.get(b"accept", b"").decode("utf-8", errors="ignore")

            # POST to the mount root (/mcp) → Streamable HTTP transport
            if method == "POST" and is_root and self.streamable_app:
                await self.streamable_app(scope, receive, send)
                return

            # DELETE for session termination (Streamable HTTP)
            if method == "DELETE" and self.streamable_app:
                await self.streamable_app(scope, receive, send)
                return

            # GET /mcp with Accept: text/event-stream → Streamable HTTP SSE channel
            # (new spec: server opens SSE stream for server-initiated messages)
            if method == "GET" and is_root and "text/event-stream" in accept_header:
                if self.streamable_app:
                    await self.streamable_app(scope, receive, send)
                else:
                    await self.sse_app(scope, receive, send)
                return

            # Sub-path requests with SSE accept or POST → legacy SSE transport
            # (GET /mcp/sse, POST /mcp/messages/)
            if "text/event-stream" in accept_header or method == "POST":
                await self.sse_app(scope, receive, send)
                return

            # Regular browser GET → return helpful info
            if method == "GET":
                response = JSONResponse({
                    "endpoint": "/mcp/sse",
                    "type": "MCP (Model Context Protocol) Server",
                    "description": "This endpoint is for MCP clients, not direct browser access.",
                    "usage": "Use an MCP-compatible client (like Claude Desktop or ChatGPT) to connect to this endpoint.",
                    "protocol": "MCP supports SSE and Streamable HTTP transports.",
                    "transports": {
                        "sse_legacy": "/mcp/sse",
                        "streamable_http": "/mcp" if self.streamable_app else "not available",
                    },
                    "streamable_http_endpoints": {
                        "POST /mcp": "send JSON-RPC message; respond inline or as SSE stream",
                        "GET /mcp": "open SSE stream for server-initiated messages (Accept: text/event-stream)",
                    } if self.streamable_app else {},
                    "documentation": "https://modelcontextprotocol.io/",
                    "available_tools": list(tools_map.keys()),
                })
                await response(scope, receive, send)
                return

            await self.sse_app(scope, receive, send)

    app.mount("/mcp", MCPBrowserHandler(mcp_sse_app, mcp_streamable_app))

    # Define safe tools for unauthenticated access
    SAFE_TOOLS = {
        "search_graph",
        "get_node_details",
        "get_related_nodes",
        "find_similar_nodes",
        "find_similar_nodes_batch",
        "get_graph_stats",
        "get_capabilities",
        "get_runtime_info",
        "get_tenant_context",
        "get_config_context",
        "get_request_actor",
        "get_request_scope",
        "get_request_selection",
        "list_node_types",
        "list_relationship_types",
        "get_schema",
        "get_presentation",
        "list_saved_views",
        "get_saved_view",
    }

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

            # Security Check: Enforce authentication for unsafe tools.
            # The BasicAuth middleware only activates when auth_password is set AND at least
            # one auth mode (auth_enabled or mcp_basic_auth) is on. Mirror that condition
            # exactly so a misconfigured deployment (auth enabled but no password set, or
            # neither auth mode on) still gets SAFE_TOOLS enforcement.
            auth_middleware_active = bool(config.auth_password) and (config.auth_enabled or config.mcp_basic_auth)
            if not auth_middleware_active:
                if tool_name not in SAFE_TOOLS:
                    return JSONResponse(
                        {"error": f"Tool '{tool_name}' requires authentication. Please enable AUTH_ENABLED or use a safe tool."},
                        status_code=403
                    )

            if tool_name not in tools_map:
                return JSONResponse({"error": f"Tool {tool_name} not found"}, status_code=404)

            func = tools_map[tool_name]
            with use_request_authorization(headers=request.headers):
                result = func(**arguments)

            access_denied_response = _access_denied_json_response(result)
            if access_denied_response is not None:
                return access_denied_response

            import json
            return JSONResponse(json.loads(json.dumps(result, default=json_serializer)))
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, status_code=500)

    # Add export_graph endpoint (convenience route)
    @app.get("/export_graph")
    async def export_graph_endpoint(request: Request) -> JSONResponse:
        """Export the entire graph (all nodes and edges)."""
        try:
            with use_request_authorization(headers=request.headers):
                result = graph_service.export_graph()

            access_denied_response = _access_denied_json_response(result)
            if access_denied_response is not None:
                return access_denied_response

            return JSONResponse(result)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            return JSONResponse({"error": str(e), "traceback": error_trace}, status_code=500)

    # Serve favicon/graph-icon from web static path to prevent 404 noise
    _favicon_path = Path(config.web_static_path) / "graph-icon.svg"

    @app.get("/graph-icon.svg")
    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    @app.get("/favicon.png")
    async def favicon():
        if _favicon_path.exists():
            return FileResponse(str(_favicon_path), media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    # Mount static files for web app
    _mount_static_files(app, config)

    # Add health check endpoint
    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        """Liveness endpoint that confirms the host process is serving requests."""
        return {
            "status": "healthy",
            "kind": "liveness",
            "graph_nodes": len(graph_storage.nodes),
            "graph_edges": len(graph_storage.edges),
            "readiness_endpoint": _PUBLIC_READINESS_PATH,
        }

    @app.get(_PUBLIC_READINESS_PATH)
    async def readiness_check() -> Dict[str, Any]:
        """Readiness endpoint with safe startup and dependency diagnostics."""
        return {
            "status": app.state.startup_diagnostics["status"],
            "kind": "readiness",
            "checks": app.state.startup_diagnostics["checks"],
            "warnings": app.state.startup_diagnostics["warnings"],
            "startup_diagnostics_endpoint": _PUBLIC_STARTUP_DIAGNOSTICS_PATH,
        }

    @app.get(_PUBLIC_STARTUP_DIAGNOSTICS_PATH)
    async def startup_diagnostics() -> Dict[str, Any]:
        """Structured startup diagnostics safe for public operability introspection."""
        return app.state.startup_diagnostics

    # Root endpoint - redirect to web app
    @app.get("/")
    async def root() -> RedirectResponse:
        """Redirect root to web application."""
        return RedirectResponse(url="/web/", status_code=302)

    # Logout endpoint - cloud-agnostic.
    # If LOGOUT_REDIRECT_URL is set, redirect there (e.g. an IAP / OAuth
    # proxy sign-out URL). Otherwise, fall back to a local /logged-out page.
    # This endpoint is exempt from auth middleware so it never loops.
    logout_redirect_url = os.environ.get("LOGOUT_REDIRECT_URL", "/logged-out")

    @app.get("/auth/logout")
    async def logout() -> RedirectResponse:
        """Log the user out by redirecting to the configured target."""
        return RedirectResponse(url=logout_redirect_url, status_code=302)

    # Fallback logged-out page, used when no external auth layer is present.
    # Must not require auth — this page is where users land after logout.
    @app.get("/logged-out")
    async def logged_out():
        """Simple standalone page shown after logout."""
        from fastapi.responses import HTMLResponse

        html = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Logged out</title>
  <link rel="icon" href="/favicon.svg" />
  <style>
    :root { color-scheme: dark; }
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
      background: #121212;
      color: #eaeaea;
    }
    body {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .logged-out-card {
      background: rgba(26, 26, 26, 0.95);
      border: 1px solid #333;
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
      padding: 32px 40px;
      max-width: 420px;
      text-align: center;
    }
    .logged-out-card h1 {
      margin: 0 0 12px 0;
      font-size: 1.4rem;
      font-weight: 600;
      color: #fff;
    }
    .logged-out-card p {
      margin: 0 0 20px 0;
      color: #bbb;
      font-size: 0.95rem;
      line-height: 1.5;
    }
    .logged-out-card a {
      display: inline-block;
      padding: 10px 18px;
      background: #646cff;
      color: #fff;
      text-decoration: none;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 500;
      transition: background 0.15s;
    }
    .logged-out-card a:hover {
      background: #535bf2;
    }
  </style>
</head>
<body>
  <div class="logged-out-card">
    <h1>You have been logged out</h1>
    <p>Your session has ended. You can return to the application using the link below.</p>
    <a href="/">Back to start</a>
  </div>
</body>
</html>
'''
        return HTMLResponse(content=html)

    # API info endpoint
    @app.get("/info")
    async def info() -> Dict[str, Any]:
        """API information endpoint."""
        llm = get_llm_availability()
        return {
            "name": "Community Knowledge Graph",
            "version": "1.0.0",
            "config_profile": config.config_profile,
            "endpoints": {
                "api": config.api_prefix,
                "ui": "/ui",
                "mcp": "/mcp",
                "web": "/web",
                "widget": "/widget",
                "health": "/health",
                "ready": _PUBLIC_READINESS_PATH,
                "startup_diagnostics": _PUBLIC_STARTUP_DIAGNOSTICS_PATH,
            },
            "graph_stats": {
                "nodes": len(graph_storage.nodes),
                "edges": len(graph_storage.edges),
            },
            "llm_provider": chat_service.provider_type,
            "llm_available": llm["available"],
            "operability": {
                "startup_status": app.state.startup_diagnostics["status"],
                "warnings": app.state.startup_diagnostics["warnings"],
                "capabilities": app.state.startup_diagnostics["capabilities"],
                "config_context": app.state.startup_diagnostics["config_context"],
            },
            "federation": {
                **federation_summary,
                "runtime": federation_manager.get_status(),
            },
        }

    @app.get("/federation/status")
    async def federation_status() -> Dict[str, Any]:
        """Get federation cache and connectivity status."""
        return federation_manager.get_status()

    @app.post("/federation/sync")
    async def federation_sync() -> Dict[str, Any]:
        """Trigger best-effort sync for all enabled federated graph sources."""
        return await federation_manager.sync_all()

    # Agent system endpoints
    @app.get("/agents/status")
    async def agents_status() -> Dict[str, Any]:
        """Get agent system status and all worker statuses."""
        return agent_registry.get_all_status()

    @app.get("/agents/integrations")
    async def agents_integrations():
        """Get available MCP integrations for agent configuration."""
        return agent_registry.get_available_mcp_integrations()

    # Shutdown handler for graceful cleanup
    @app.on_event("shutdown")
    async def shutdown_event():
        """Gracefully shutdown agent registry and event system."""
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
        uvicorn backend.api_host.server:get_app --factory
    """
    return create_app()
