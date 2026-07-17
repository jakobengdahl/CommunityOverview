"""Visualization-session registry lifecycle and legacy SSE stream endpoint.

The browser opens the SSE stream on load so external AI (MCP) clients can push
visualization commands into the window (design §3.8). Session *state* is no
longer uploaded here; MCP query tools read it from the shared-session store.
"""

import asyncio
import json
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

from backend.core.session_registry import SessionRegistry
from backend.service.rest_api import _lookup_rate_key

logger = logging.getLogger(__name__)


SESSION_MAX_COUNT = 10_000  # cap auto-created sessions to limit unauthenticated DoS


def register_session_stream(
    app: FastAPI,
    session_registry: SessionRegistry,
    session_manager=None,
) -> None:
    """Register the session-registry startup lifecycle and the SSE stream route."""

    @app.on_event("startup")
    async def _session_registry_startup():
        session_registry.set_event_loop(asyncio.get_running_loop())

        async def _periodic_cleanup():
            while True:
                await asyncio.sleep(300)  # 5-minute eviction cycle
                evicted = session_registry.cleanup_stale()
                if evicted:
                    logger.debug("session_registry: evicted %d stale sessions", evicted)

        app.state.session_cleanup_task = asyncio.create_task(_periodic_cleanup())

    @app.get("/sessions/{session_id}/stream")
    async def session_stream(session_id: str, request: Request):
        """SSE stream — browser connects here to receive MCP visualization commands.

        The browser opens this on load so external AI clients can push commands
        into the window (design §3.8). Session *state* is no longer uploaded here;
        MCP query tools read it from the shared-session store instead.
        """
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)

        if session_manager is not None:
            from backend.core.session_manager import RateLimited

            try:
                session_manager.check_lookup_rate(_lookup_rate_key(request))
            except RateLimited:
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

        if (
            session_id not in session_registry._sessions
            and session_registry.session_count >= SESSION_MAX_COUNT
        ):
            return JSONResponse({"error": "too many sessions"}, status_code=503)

        async def event_generator():
            yield f"data: {json.dumps({'type': 'connected', 'session_id': session_id})}\n\n"
            try:
                async for command in session_registry.stream(session_id):
                    if command.get("type") == "ping":
                        # SSE comment — keeps the connection alive, browsers ignore it
                        yield ": ping\n\n"
                    else:
                        yield f"data: {json.dumps(command)}\n\n"
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
