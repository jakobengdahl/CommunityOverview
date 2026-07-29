"""
Reverse-proxy helpers for forwarding MCP traffic to the upstream service.

Supports:
- SSE (Server-Sent Events) streaming via GET /sse
- Regular HTTP POST via POST /messages and /mcp/messages
- Endpoint URL rewriting so MCP clients POST back through the gateway
"""

import logging
import urllib.parse
from typing import AsyncIterator

import httpx2 as httpx
from fastapi import Request
from fastapi.responses import StreamingResponse, Response

import config

logger = logging.getLogger(__name__)

# Shared async HTTP client (re-used across requests for connection pooling).
# http2=False is explicit: Cloud Run HTTP/2 multiplexing can cause 421 errors
# when the proxied Host doesn't match the upstream's certificate.
_client = httpx.AsyncClient(timeout=None, http2=False)


async def proxy_sse(request: Request) -> StreamingResponse:
    """Forward a GET /sse request to the upstream and stream the response back.

    The SSE stream from the upstream contains an ``event: endpoint`` message
    whose ``data:`` field carries the URL that MCP clients must POST messages
    to.  Because the gateway sits between the client and the upstream, that
    URL would otherwise point at the upstream host.  We rewrite it on the fly
    so that clients always POST back through the gateway.
    """
    upstream_url = config.UPSTREAM_MCP_BASE_URL + "/mcp/sse"

    # Forward all incoming query parameters (e.g. sessionId)
    params = dict(request.query_params)

    # Forward a safe subset of request headers and ensure the upstream
    # MCPBrowserHandler sees the SSE accept type (otherwise it returns JSON).
    headers = _forward_headers(request)
    headers["accept"] = "text/event-stream"

    logger.info("Proxying SSE to %s params=%s", upstream_url, params)

    # Encode the upstream base URL once for byte-level replacement in SSE data lines.
    # This rewrites any absolute upstream URL in "endpoint" events to a root-relative
    # path so clients always POST back through the gateway rather than bypassing it.
    upstream_prefix = config.UPSTREAM_MCP_BASE_URL.encode()

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
            buf = b""
            async for chunk in upstream_resp.aiter_bytes():
                buf += chunk
                # Emit complete lines, rewriting upstream base URL → root-relative path
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.replace(upstream_prefix, b"") + b"\n"
            if buf:
                yield buf.replace(upstream_prefix, b"")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def proxy_post_mcp(request: Request) -> Response:
    """Forward a POST to an /mcp/* path to the upstream and return the response.

    Handles both ``/mcp/messages`` and ``/mcp/sse/{subpath}`` paths.
    """
    path = request.url.path  # e.g. /mcp/messages or /mcp/sse/messages
    upstream_url = config.UPSTREAM_MCP_BASE_URL + path

    params = dict(request.query_params)
    headers = _forward_headers(request)
    body = await request.body()

    logger.info("Proxying POST to %s params=%s", upstream_url, params)

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


async def proxy_streamable_http(request: Request) -> Response:
    """Forward a Streamable HTTP (MCP spec ≥2025-03-26) request to the upstream.

    The upstream answers POST /mcp either with a single JSON body or with an SSE
    stream, depending on the request. Both are relayed as-is; the SSE case is
    streamed so a long-running tool call is not buffered. ``Mcp-Session-Id`` is
    forwarded in both directions so the client keeps its session.

    The trailing slash matters: the upstream mounts the MCP app at ``/mcp``, and
    Starlette answers a request for the bare mount path with a 307 to ``/mcp/``.
    That redirect carries the upstream's own host, which would send the client
    straight past the gateway, so the gateway asks for ``/mcp/`` up front.
    """
    upstream_url = config.UPSTREAM_MCP_BASE_URL + "/mcp/"
    params = dict(request.query_params)
    headers = _forward_headers(request)
    body = await request.body()

    logger.info("Proxying %s /mcp (streamable http) to %s", request.method, upstream_url)

    if request.method in ("GET", "DELETE"):
        upstream_req = _client.build_request(
            request.method, upstream_url, params=params, headers=headers
        )
    else:
        upstream_req = _client.build_request(
            "POST", upstream_url, params=params, headers=headers, content=body
        )

    upstream_resp = await _client.send(upstream_req, stream=True)
    content_type = upstream_resp.headers.get("content-type", "")

    if "text/event-stream" not in content_type:
        payload = await upstream_resp.aread()
        await upstream_resp.aclose()
        return Response(
            content=payload,
            status_code=upstream_resp.status_code,
            headers=_response_headers(upstream_resp.headers),
        )

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream_resp.status_code,
        media_type="text/event-stream",
        headers=_response_headers(upstream_resp.headers),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOP_BY_HOP = frozenset({
    "host",
    "authorization",      # gateway JWT – must not leak to upstream
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "trailers",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
})


def _forward_headers(request: Request) -> dict:
    """Return a filtered copy of the incoming headers suitable for forwarding."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _response_headers(headers) -> dict:
    """Copy upstream response headers, dropping the ones the ASGI server owns.

    ``Mcp-Session-Id`` is deliberately kept: Streamable HTTP clients read it
    from the initialize response and send it back on every later request.

    A ``Location`` pointing at the upstream is rewritten to the gateway's own
    origin. Following such a redirect would take the client around the gateway
    to an origin it cannot authenticate against, and would disclose the internal
    service URL.
    """
    dropped = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    result = {k: v for k, v in headers.items() if k.lower() not in dropped}

    location = next((v for k, v in result.items() if k.lower() == "location"), None)
    if location:
        upstream_host = urllib.parse.urlparse(config.UPSTREAM_MCP_BASE_URL).hostname
        parsed = urllib.parse.urlparse(location)
        if parsed.hostname and parsed.hostname == upstream_host:
            rewritten = config.PUBLIC_BASE_URL.rstrip("/") + parsed.path
            if parsed.query:
                rewritten += "?" + parsed.query
            logger.info("Rewrote upstream redirect %s → %s", location, rewritten)
            for key in list(result):
                if key.lower() == "location":
                    result[key] = rewritten
    return result


def _rewrite_endpoint_event(raw: bytes) -> bytes:
    """If *raw* is an ``event: endpoint`` SSE frame, rewrite the data URL.

    The upstream MCP server sends the message-posting URL (e.g.
    ``http://upstream-host/mcp/messages/?session_id=…``).  We replace
    the scheme+host with the gateway's PUBLIC_BASE_URL so clients POST
    back through the gateway.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw

    # Fast path: most events are not "endpoint".
    if "event: endpoint" not in text and "event:endpoint" not in text:
        return raw

    upstream_base = config.UPSTREAM_MCP_BASE_URL.rstrip("/")
    gateway_base = config.PUBLIC_BASE_URL.rstrip("/")

    lines = text.split("\n")
    rewritten = False
    for i, line in enumerate(lines):
        if not line.startswith("data:"):
            continue
        url = line[len("data:"):].strip()
        if not url:
            continue

        # Case 1: absolute URL pointing at the upstream – replace origin.
        if url.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(url)
            upstream_parsed = urllib.parse.urlparse(upstream_base)
            if parsed.hostname == upstream_parsed.hostname:
                new_url = gateway_base + parsed.path
                if parsed.query:
                    new_url += "?" + parsed.query
                lines[i] = f"data: {new_url}"
                rewritten = True
                logger.info("Rewrote endpoint URL %s → %s", url, new_url)

        # Case 2: root-relative path like /messages/?session_id=…
        # MCP clients resolve this with urljoin(sse_url, data).
        # Because data starts with "/", urljoin discards the /mcp prefix.
        # E.g. urljoin("https://gw/mcp/sse", "/messages/?s=1")
        #    → "https://gw/messages/?s=1"
        # The gateway has POST /messages/ to handle this, but we also
        # prepend /mcp so that clients doing correct relative resolution
        # (without the leading /) also work.
        elif url.startswith("/") and not url.startswith("/mcp"):
            parsed = urllib.parse.urlparse(url)
            new_url = "/mcp" + parsed.path
            if parsed.query:
                new_url += "?" + parsed.query
            lines[i] = f"data: {new_url}"
            rewritten = True
            logger.info("Rewrote relative endpoint URL %s → %s", url, new_url)

    if rewritten:
        return "\n".join(lines).encode("utf-8")
    return raw
