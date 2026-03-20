"""
Reverse-proxy helpers for forwarding MCP traffic to the upstream service.

Supports:
- SSE (Server-Sent Events) streaming via GET /sse
- Regular HTTP POST via POST /messages
"""

import logging
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, Response

import config

logger = logging.getLogger(__name__)

# Shared async HTTP client (re-used across requests for connection pooling)
_client = httpx.AsyncClient(timeout=None)  # SSE streams are long-lived


async def proxy_sse(request: Request) -> StreamingResponse:
    """Forward a GET /sse request to the upstream and stream the response back."""
    upstream_url = config.UPSTREAM_MCP_BASE_URL + "/mcp/sse"

    # Forward all incoming query parameters (e.g. sessionId)
    params = dict(request.query_params)

    # Forward a safe subset of request headers
    headers = _forward_headers(request)

    logger.info("Proxying SSE to %s params=%s", upstream_url, params)

    async def event_stream() -> AsyncIterator[bytes]:
        async with _client.stream(
            "GET",
            upstream_url,
            params=params,
            headers=headers,
        ) as upstream_resp:
            if upstream_resp.status_code != 200:
                logger.warning(
                    "Upstream SSE returned %s", upstream_resp.status_code
                )
                yield b"event: error\ndata: upstream error\n\n"
                return
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def proxy_post_mcp_sse(request: Request) -> Response:
    """Forward a POST /mcp/sse request to the upstream and return the response."""
    # Build upstream URL preserving the exact path (including any sub-paths)
    path = request.url.path  # e.g. /mcp/sse or /mcp/sse/messages
    upstream_url = config.UPSTREAM_MCP_BASE_URL + path

    params = dict(request.query_params)
    headers = _forward_headers(request)
    body = await request.body()

    logger.info("Proxying POST to %s", upstream_url)

    resp = await _client.post(
        upstream_url,
        params=params,
        headers=headers,
        content=body,
    )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


async def proxy_post(request: Request) -> Response:
    """Forward a POST /messages request to the upstream and return the response."""
    upstream_url = config.UPSTREAM_MCP_BASE_URL + "/mcp/messages"

    params = dict(request.query_params)
    headers = _forward_headers(request)
    body = await request.body()

    logger.info("Proxying POST to %s", upstream_url)

    resp = await _client.post(
        upstream_url,
        params=params,
        headers=headers,
        content=body,
    )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOP_BY_HOP = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # Host must match the upstream, not the gateway
        "host",
    ]
)


def _forward_headers(request: Request) -> dict:
    """Return a filtered copy of the incoming headers suitable for forwarding."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
