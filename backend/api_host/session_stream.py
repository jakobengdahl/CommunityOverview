"""Visualization-session registry lifecycle and legacy SSE stream endpoint.

The browser opens the SSE stream on load so external AI (MCP) clients can push
visualization commands into the window (design §3.8). Session *state* is no
longer uploaded here; MCP query tools read it from the shared-session store.
"""

import asyncio
import json
import logging
import re
import secrets
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from backend.core.session_registry import SessionRegistry
from backend.runtime.authorization import GRAPH_ACTION_MUTATE, use_request_authorization
from backend.service.access import authorize_graph_access
from backend.service.rest_api import _lookup_rate_key

logger = logging.getLogger(__name__)


SESSION_MAX_COUNT = 10_000  # cap auto-created sessions to limit unauthenticated DoS

# Visual reaction styles a triggered node can play (client renders these).
PULSE_STYLES = ("glow", "grow", "marker")
_DEFAULT_PULSE_STYLE = "glow"

# Conservative allow-list for a caller-supplied CSS colour: hex, rgb()/rgba(),
# or a plain colour keyword. Anything else is dropped and the node's own colour
# is used, so an external caller can never push an arbitrary style string.
_PULSE_COLOR_RE = re.compile(
    r"^#[0-9a-fA-F]{3,8}$|^rgba?\([0-9.,%\s]+\)$|^[a-zA-Z]{1,20}$"
)


class PulseTriggerRequest(BaseModel):
    """Body of an external pulse-trigger call."""

    # node_id is validated at the boundary to the id charset this graph actually
    # uses (UUIDs and slug/namespaced ids), which excludes HTML/JS/whitespace
    # metacharacters — so the value is safe everywhere it flows downstream (the
    # SSE command stream and any response echo).
    node_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:@/-]+$")
    style: str = _DEFAULT_PULSE_STYLE
    color: Optional[str] = Field(default=None, max_length=32)
    duration_ms: int = Field(default=1500, ge=200, le=15000)


def _safe_pulse_color(color: Optional[str]) -> Optional[str]:
    if not color:
        return None
    candidate = color.strip()
    return candidate if _PULSE_COLOR_RE.match(candidate) else None


def _extract_trigger_token(
    request: Request, query_token: Optional[str]
) -> Optional[str]:
    """Prefer an ``Authorization: Bearer`` header, fall back to a ``?token=`` param.

    The query-param form lets simple webhook senders that can only configure a
    URL still authenticate; header form is preferred for anything that can set
    one.
    """
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return query_token


class AutoAddAgentRequest(BaseModel):
    """Body of a create-auto-add-agent call."""

    node_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


# Fixed, safe messages for auto-add validation failures, keyed by the stable
# AutoAddRuleError.code. The endpoint returns these instead of the raw exception
# text so no exception detail is exposed to an external caller (CodeQL).
_AUTO_ADD_ERROR_MESSAGES = {
    "empty_pattern": "an auto-add agent needs at least one node type or keyword",
    "capacity_reached": "auto-add agent capacity reached for this session",
}


def register_session_stream(
    app: FastAPI,
    session_registry: SessionRegistry,
    session_manager=None,
    graph_service=None,
    auto_add_registry=None,
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
                # Drop auto-add agents whose session is no longer live, so a
                # session-scoped agent dies with its session (bounded memory).
                if auto_add_registry is not None:
                    auto_add_registry.prune_to_sessions(
                        session_registry._sessions.keys()
                    )

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

    def _rate_limited(request: Request) -> bool:
        """Consume one lookup-rate token; return True when the source is throttled."""
        if session_manager is None:
            return False
        from backend.core.session_manager import RateLimited

        try:
            session_manager.check_lookup_rate(_lookup_rate_key(request))
            return False
        except RateLimited:
            return True

    @app.post("/sessions/{session_id}/trigger-token")
    async def mint_pulse_trigger_token(session_id: str, request: Request):
        """Mint (or rotate) the pulse-trigger token for a live session.

        Called by the browser that owns the session to obtain the secret it
        embeds in the external trigger URL. Runs under the graph authorization
        seam so the hosted layer can bind minting to a real actor; the open-core
        default is permissive, consistent with the rest of the platform.

        Threat model (open core): the session id is not itself a secret — it is
        shared in the ``?session=`` URL — and already grants canvas-push via the
        MCP session tools, so this endpoint does not widen the "push to a known
        session" surface. It does add one griefing vector: because minting
        rotates the token, anyone who can reach this route and knows a session
        id can revoke a legitimate user's already-configured trigger URL. Gating
        minting to an authenticated session owner is the hosted layer's job via
        the authorization hook above.
        """
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)

        if graph_service is not None:
            with use_request_authorization(headers=request.headers):
                denied = authorize_graph_access(
                    graph_service.authorization_hook,
                    action=GRAPH_ACTION_MUTATE,
                    target="mint_pulse_trigger_token",
                )
            if denied:
                return JSONResponse(denied, status_code=403)

        if _rate_limited(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

        if (
            session_id not in session_registry._sessions
            and session_registry.session_count >= SESSION_MAX_COUNT
        ):
            return JSONResponse({"error": "too many sessions"}, status_code=503)

        token = session_registry.mint_trigger_token(session_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "trigger_token": token,
                "pulse_path": f"/sessions/{session_id}/pulse",
            }
        )

    @app.post("/sessions/{session_id}/pulse")
    async def pulse_node(
        session_id: str,
        body: PulseTriggerRequest,
        request: Request,
        token: Optional[str] = Query(default=None),
    ):
        """External trigger: play a visual pulse on a node in the live session.

        Authenticated with the session's pulse-trigger token (``Authorization:
        Bearer`` header, or ``?token=`` for simple webhook senders). Emits a
        ``node_pulse`` command over the existing SSE session-push channel; the
        browser reacts by animating the targeted node.
        """
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)

        # Rate-limit before the token check so the short token cannot be
        # brute-forced through this endpoint.
        if _rate_limited(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

        supplied = _extract_trigger_token(request, token)
        if not session_registry.verify_trigger_token(session_id, supplied):
            return JSONResponse(
                {"error": "invalid or missing trigger token"}, status_code=401
            )

        style = body.style if body.style in PULSE_STYLES else _DEFAULT_PULSE_STYLE
        command = {
            "type": "node_pulse",
            "node_id": body.node_id,
            "pulse": {
                "style": style,
                "color": _safe_pulse_color(body.color),
                "duration_ms": body.duration_ms,
            },
            "command_id": secrets.token_hex(8),
        }

        # Dispatch over the same best-effort channels as MCP tool pushes
        # (_push_to_session): the legacy single-consumer registry and the
        # shared-session hub, whichever the browser is currently on. Neither
        # confirms a live subscriber — a pulse for a session no one is watching
        # is simply unseen — so this reports *dispatch*, not receipt, matching
        # the rest of the push architecture.
        await session_registry.push_command(session_id, command)
        if session_manager is not None:
            try:
                session_manager.push_command(session_id, command)
            except Exception:
                pass

        return JSONResponse({"success": True, "command_id": command["command_id"]})

    # ------------------------------------------------------------------
    # Session-scoped auto-add agents
    # ------------------------------------------------------------------
    #
    # An auto-add agent watches for newly created nodes matching a pattern and
    # adds each match to this session's live view (additively). It is bound to
    # this one session: it only pushes here and is pruned when the session ends.
    # Creation goes through the graph authorization/mutate seam like the pulse
    # trigger-token endpoint, so the hosted layer can bind it to a real actor;
    # the open-core default is permissive. It never mutates the graph.

    def _auto_add_authorize_mutate(request: Request):
        """Return a denial JSONResponse if a mutating auto-add call is refused."""
        if graph_service is None:
            return None
        with use_request_authorization(headers=request.headers):
            denied = authorize_graph_access(
                graph_service.authorization_hook,
                action=GRAPH_ACTION_MUTATE,
                target="session_auto_add_agent",
            )
        if denied:
            return JSONResponse(denied, status_code=403)
        return None

    @app.post("/sessions/{session_id}/auto-add-agents")
    async def create_auto_add_agent(
        session_id: str, body: AutoAddAgentRequest, request: Request
    ):
        """Create a session-scoped auto-add agent on a live session."""
        if auto_add_registry is None:
            return JSONResponse(
                {"error": "auto-add agents are not available"}, status_code=503
            )
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)

        denied = _auto_add_authorize_mutate(request)
        if denied is not None:
            return denied
        if _rate_limited(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        if (
            session_id not in session_registry._sessions
            and session_registry.session_count >= SESSION_MAX_COUNT
        ):
            return JSONResponse({"error": "too many sessions"}, status_code=503)

        from backend.core.session_auto_add import AutoAddRuleError

        try:
            rule = auto_add_registry.add_rule(
                session_id, node_types=body.node_types, keywords=body.keywords
            )
        except AutoAddRuleError as exc:
            # Map to a fixed, safe message keyed by the exception's stable code —
            # never echo the exception text back to an external caller (CodeQL
            # information-exposure). The full exception detail is kept only in a
            # structured server log line, tagged with a correlation id the client
            # can quote to support to tie its 400 back to that log entry.
            correlation_id = secrets.token_hex(8)
            logger.warning(
                "auto-add rule rejected (session=%s code=%s correlation_id=%s): %s",
                session_id,
                exc.code,
                correlation_id,
                exc,
            )
            message = _AUTO_ADD_ERROR_MESSAGES.get(
                exc.code, "invalid auto-add agent pattern"
            )
            return JSONResponse(
                {
                    "error": message,
                    "code": exc.code,
                    "correlation_id": correlation_id,
                },
                status_code=400,
            )
        # Materialise the push session so the prune keeps this agent while live.
        session_registry.get_or_create(session_id)
        return JSONResponse({"success": True, "agent": rule.to_dict()})

    @app.get("/sessions/{session_id}/auto-add-agents")
    async def list_auto_add_agents(session_id: str, request: Request):
        """List the auto-add agents configured on a session."""
        if auto_add_registry is None:
            return JSONResponse(
                {"error": "auto-add agents are not available"}, status_code=503
            )
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)
        if _rate_limited(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        agents = [r.to_dict() for r in auto_add_registry.list_rules(session_id)]
        return JSONResponse({"success": True, "agents": agents})

    @app.delete("/sessions/{session_id}/auto-add-agents/{agent_id}")
    async def delete_auto_add_agent(session_id: str, agent_id: str, request: Request):
        """Remove an auto-add agent from a session."""
        if auto_add_registry is None:
            return JSONResponse(
                {"error": "auto-add agents are not available"}, status_code=503
            )
        if not session_registry.is_valid_session_id(session_id):
            return JSONResponse({"error": "invalid session_id format"}, status_code=400)

        denied = _auto_add_authorize_mutate(request)
        if denied is not None:
            return denied
        if _rate_limited(request):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

        if not auto_add_registry.remove_rule(session_id, agent_id):
            return JSONResponse({"error": "auto-add agent not found"}, status_code=404)
        return JSONResponse({"success": True})
