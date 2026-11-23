"""The federation read route: the published graph, read-only, for other instances."""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend.federation.config import FederationSettings
from backend.federation.publish import (
    FEDERATION_GRAPH_PATH,
    REFUSED,
    FederationKeyProvider,
    log_fetch,
    presented_key,
)
from backend.service.serializers import serialize_edges, serialize_nodes

logger = logging.getLogger(__name__)

# One fixed body for every refusal, so it never says whether a key was
# unknown, disabled or expired.
_REFUSED_BODY = {"detail": "Federation access refused"}


def published_graph(graph_storage) -> Dict[str, Any]:
    """The graph as an external reader sees it; the one place a filter belongs."""
    return {
        "nodes": serialize_nodes(graph_storage.get_all_nodes()),
        "edges": serialize_edges(graph_storage.get_all_edges()),
    }


def register_federation_routes(
    app: FastAPI,
    graph_storage,
    settings: FederationSettings,
    key_provider: Optional[FederationKeyProvider],
) -> None:
    """Register the federation read route, only when publishing is on.

    Without a provider the route does not exist, so it answers like any unknown
    path. Only GET and HEAD are registered: nothing reachable here writes.
    """
    if key_provider is None:
        return
    # The auth middleware lets exactly this route through to its own key check.
    app.state.federation_route_active = True
    graph_id = settings.publish.graph_id

    @app.api_route(FEDERATION_GRAPH_PATH, methods=["GET", "HEAD"])
    async def federation_graph(request: Request) -> JSONResponse:
        request_id = request.headers.get("x-request-id", "")
        presented, consistent = presented_key(request.headers)
        result = REFUSED
        if consistent:
            try:
                result = key_provider.authenticate(presented)
            except Exception:
                # A failing (external) provider admits nothing.
                logger.exception("Federation key provider failed")
        if not result.served:
            log_fetch(result, graph_id=graph_id, request_id=request_id)
            return JSONResponse(status_code=401, content=_REFUSED_BODY)
        payload = published_graph(graph_storage)
        log_fetch(
            result,
            graph_id=graph_id,
            request_id=request_id,
            nodes=len(payload["nodes"]),
            edges=len(payload["edges"]),
        )
        return JSONResponse(payload)
