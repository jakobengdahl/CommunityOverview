"""MCP HTTP transport mounting for the api_host application.

Builds the dynamic MCP instructions from the loaded schema, wraps the FastMCP
SSE / Streamable-HTTP transports so browser requests are routed correctly, and
mounts them at ``/mcp``.
"""

import contextlib
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from backend.config import config_loader
from backend.llm.language_policy import format_language_policy_for_prompt
from backend.runtime.authorization import use_request_authorization

logger = logging.getLogger(__name__)


def build_mcp_instructions() -> str:
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

    language_policy_section = format_language_policy_for_prompt(
        presentation, external_agent=True
    )

    instructions = f"""You are a helpful knowledge agent for: {presentation.get("title", "Knowledge Graph")}.
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


def bind_request_authorization_to_asgi_app(asgi_app):
    async def request_bound_app(scope, receive, send):
        if scope.get("type") != "http":
            await asgi_app(scope, receive, send)
            return

        with use_request_authorization(headers=Headers(scope=scope)):
            await asgi_app(scope, receive, send)

    return request_bound_app


def _mount_relative_path(scope) -> str:
    """Return the request path relative to the mount this handler is served from.

    Starlette keeps the full path in ``scope["path"]`` and the mount prefix in
    ``scope["root_path"]``; sub-applications derive their own route path the same
    way. A bare mount hit (``/mcp``) normalises to ``/`` so it is treated as the
    transport root rather than as an empty sub-path.
    """
    path = scope.get("path", "") or ""
    root_path = scope.get("root_path", "") or ""
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        path = path[len(root_path) :]
    return path or "/"


class MCPBrowserHandler:
    """ASGI shim routing /mcp requests to the correct MCP transport.

    Regular browser GETs would otherwise hang waiting for SSE. This handler
    dispatches between the legacy SSE transport and the Streamable HTTP
    transport, and returns a helpful info payload for plain browser GETs.
    """

    def __init__(
        self, sse_app, streamable_app=None, tools_map=None, streamable_ready=None
    ):
        self.sse_app = sse_app
        self.streamable_app = streamable_app
        self.tools_map = tools_map or {}
        # Streamable HTTP needs its session manager running. Until it is, route
        # to the legacy transport instead of raising "Task group is not
        # initialized" at the client.
        self._streamable_ready = streamable_ready or (lambda: True)

    def _streamable(self):
        if self.streamable_app is not None and self._streamable_ready():
            return self.streamable_app
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.sse_app(scope, receive, send)
            return

        # Starlette leaves scope["path"] as the full request path and records the
        # mount prefix in scope["root_path"], so the path relative to this mount
        # has to be derived — comparing scope["path"] against "/" would never
        # match and every Streamable HTTP request would fall through to the
        # legacy SSE app, which answers 404 for them.
        path = _mount_relative_path(scope)
        method = scope.get("method", "GET")
        is_root = path == "/"

        mcp_logger = logging.getLogger("mcp.server")
        if not mcp_logger.handlers:
            logging.basicConfig()
        mcp_logger.info(f"MCP Request: {method} {scope.get('path', '')}")

        headers = dict(scope.get("headers", []))
        accept_header = headers.get(b"accept", b"").decode("utf-8", errors="ignore")

        streamable = self._streamable()

        # POST to the mount root (/mcp) → Streamable HTTP transport
        if method == "POST" and is_root and streamable:
            await streamable(scope, receive, send)
            return

        # DELETE for session termination (Streamable HTTP)
        if method == "DELETE" and streamable:
            await streamable(scope, receive, send)
            return

        # GET /mcp with Accept: text/event-stream → Streamable HTTP SSE channel
        # (new spec: server opens SSE stream for server-initiated messages)
        if method == "GET" and is_root and "text/event-stream" in accept_header:
            if streamable:
                await streamable(scope, receive, send)
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
            response = JSONResponse(
                {
                    "endpoint": "/mcp/sse",
                    "type": "MCP (Model Context Protocol) Server",
                    "description": "This endpoint is for MCP clients, not direct browser access.",
                    "usage": "Use an MCP-compatible client (like Claude Desktop or ChatGPT) to connect to this endpoint.",
                    "protocol": "MCP supports SSE and Streamable HTTP transports.",
                    "transports": {
                        "sse_legacy": "/mcp/sse",
                        "streamable_http": "/mcp"
                        if self.streamable_app
                        else "not available",
                    },
                    "streamable_http_endpoints": {
                        "POST /mcp": "send JSON-RPC message; respond inline or as SSE stream",
                        "GET /mcp": "open SSE stream for server-initiated messages (Accept: text/event-stream)",
                    }
                    if self.streamable_app
                    else {},
                    "documentation": "https://modelcontextprotocol.io/",
                    "available_tools": list(self.tools_map.keys()),
                }
            )
            await response(scope, receive, send)
            return

        await self.sse_app(scope, receive, send)


def mount_mcp(app: FastAPI, mcp, tools_map) -> None:
    """Mount the MCP HTTP endpoints at /mcp.

    Two transports are supported:
      1. Legacy SSE  (GET /mcp/sse + POST /mcp/messages) – for older clients
      2. Streamable HTTP (POST /mcp) – for ChatGPT, Claude, and MCP spec ≥2025-03-26
    """
    mcp_sse_app = bind_request_authorization_to_asgi_app(mcp.sse_app())

    # Try to create Streamable HTTP app (requires mcp ≥ 1.8).
    # If the installed version doesn't support it, fall back to SSE-only.
    try:
        # FastMCP mounts its Streamable HTTP handler at settings.streamable_http_path
        # ("/mcp" by default). This whole app is itself mounted at /mcp, so the
        # handler sees the already-stripped path "/" and the default would never
        # match — every POST /mcp answered 404. Serve it from the mount root.
        try:
            mcp.settings.streamable_http_path = "/"
        except (AttributeError, ValueError):
            logger.warning(
                "Could not set streamable_http_path; Streamable HTTP may not respond "
                "on /mcp with this mcp version."
            )
        mcp_streamable_app = bind_request_authorization_to_asgi_app(
            mcp.streamable_http_app()
        )
    except (AttributeError, TypeError):
        mcp_streamable_app = None

    streamable_state = {"started": False}
    if mcp_streamable_app is not None:
        _attach_streamable_session_lifecycle(app, mcp, streamable_state)

    app.mount(
        "/mcp",
        MCPBrowserHandler(
            mcp_sse_app,
            mcp_streamable_app,
            tools_map,
            streamable_ready=lambda: streamable_state["started"],
        ),
    )


def _attach_streamable_session_lifecycle(app: FastAPI, mcp, state: dict) -> None:
    """Run the Streamable HTTP session manager for the lifetime of *app*.

    FastMCP starts that manager from its own app's lifespan. We mount the app
    instead of running it, so the lifespan never fires and the first request
    fails with "Task group is not initialized". Tie it to this app's lifecycle.
    """
    stack = contextlib.AsyncExitStack()

    async def _start() -> None:
        try:
            await stack.enter_async_context(mcp.session_manager.run())
            state["started"] = True
            logger.info("Streamable HTTP session manager started")
        except Exception:  # noqa: BLE001 - never block startup over a transport
            logger.exception(
                "Could not start the Streamable HTTP session manager; "
                "only the legacy SSE transport will work."
            )

    async def _stop() -> None:
        state["started"] = False
        await stack.aclose()

    app.router.on_startup.append(_start)
    app.router.on_shutdown.append(_stop)
