"""Direct tool-execution and graph-export endpoints for the api_host application."""

import json
import logging
import traceback
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend.service import GraphService, json_serializer
from backend.runtime.authorization import use_request_authorization

logger = logging.getLogger(__name__)


# Tools safe for unauthenticated access (read-only).
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
    # Visualization session tools — read-only, safe without auth
    "connect_to_visualization_session",
    "get_visualization_session_state",
}


def access_denied_json_response(result: Any) -> Optional[JSONResponse]:
    if isinstance(result, dict) and result.get("error_code") == "access_denied":
        return JSONResponse(result, status_code=403)
    return None


def register_tool_routes(
    app: FastAPI,
    graph_service: GraphService,
    tools_map: dict,
    auth_active: bool,
) -> None:
    """Register /execute_tool and /export_graph."""

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
            # auth_active is the canonical condition for whether the auth
            # middleware is active. Use it here so bearer-only deployments are
            # also covered (no password required).
            if not auth_active:
                if tool_name not in SAFE_TOOLS:
                    return JSONResponse(
                        {
                            "error": f"Tool '{tool_name}' requires authentication. Please enable AUTH_ENABLED or use a safe tool."
                        },
                        status_code=403,
                    )

            if tool_name not in tools_map:
                return JSONResponse(
                    {"error": f"Tool {tool_name} not found"}, status_code=404
                )

            func = tools_map[tool_name]
            with use_request_authorization(headers=request.headers):
                result = func(**arguments)

            access_denied_response = access_denied_json_response(result)
            if access_denied_response is not None:
                return access_denied_response

            return JSONResponse(json.loads(json.dumps(result, default=json_serializer)))
        except Exception as e:
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/export_graph")
    async def export_graph_endpoint(request: Request) -> JSONResponse:
        """Export the entire graph (all nodes and edges)."""
        try:
            with use_request_authorization(headers=request.headers):
                result = graph_service.export_graph()

            access_denied_response = access_denied_json_response(result)
            if access_denied_response is not None:
                return access_denied_response

            return JSONResponse(result)
        except Exception as e:
            error_trace = traceback.format_exc()
            return JSONResponse(
                {"error": str(e), "traceback": error_trace}, status_code=500
            )
