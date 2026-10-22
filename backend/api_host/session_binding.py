"""Bind ``x-communityoverview-session-id`` for every HTTP request (§16.1).

docs/SESSION_OVERLAY_CONTRACT.md §16.1: the header is bound into the request
context once, by middleware, not route by route, so no route can miss it - the
session routes, the agent routes, proposal approval and the host's own routes
included. A route that neither stages nor composes refuses a request carrying
it with ``session_not_supported``: the header is never ignored (I2), because a
caller that believes it is staging must never write to the graph by mistake.

What the header resolves to is decided where the request is handled - the
graph service resolves the session's write mode on every write - so this layer
answers nothing about whether a session exists; it only binds, and refuses the
requests that could not honour a session at all.

A route that names its own session in its path (the session routes) may carry
the header only for that same session (``session_mismatch`` otherwise). A
retired session id in such a path is answered 404 here, for every one of them:
a retired id is never recreated (§5.3), and several of those routes would
otherwise create it.

Over MCP the header binds here too, but a tool call runs in the MCP server's
own task, which this binding does not reach; the tool wrapper in
``service/mcp_tools.py`` reads the header of each message's own request.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Collection, Optional, Pattern, Sequence, Tuple

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.routing import Match

from backend.runtime import session_context

# (method, path) pairs whose handlers stage or compose (§8.2, §9.1), or hand
# the request to tools that do and refuse the session themselves. Paths under
# the API prefix are given without it.
_API_SESSION_AWARE: Sequence[Tuple[str, str]] = (
    ("POST", r"/search"),
    ("GET", r"/nodes/[^/]+"),
    ("POST", r"/nodes/[^/]+/related"),
    ("POST", r"/similar"),
    ("POST", r"/similar/batch"),
    ("POST", r"/federation/adopt"),
    ("POST", r"/nodes"),
    ("PATCH", r"/nodes/[^/]+"),
    ("DELETE", r"/nodes"),
    ("POST", r"/nodes/archive"),
    ("POST", r"/edges"),
    ("PATCH", r"/edges/[^/]+"),
    ("DELETE", r"/edges/[^/]+"),
    ("POST", r"/edges/archive"),
    ("GET", r"/stats"),
    ("GET", r"/meta/subtypes"),
    ("GET", r"/meta/relationship-applicability/audit"),
    ("GET", r"/views"),
    ("GET", r"/views/[^/]+"),
    ("GET", r"/export"),
)
_HOST_SESSION_AWARE: Sequence[Tuple[str, str]] = (
    ("POST", r"/execute_tool"),
    ("GET", r"/export_graph"),
    ("POST", r"/ui/chat"),
    ("POST", r"/ui/chat/simple"),
    ("POST", r"/agents/proposals/[^/]+/approve"),
    ("*", r"/mcp(/.*)?"),
)

_SESSION_ID = r"(?P<session_id>[^/]+)"


def _compile(prefix: str, routes: Sequence[Tuple[str, str]]):
    return [(method, re.compile(re.escape(prefix) + path)) for method, path in routes]


class SessionBindingMiddleware:
    """Pure ASGI, so the binding is visible to the endpoint and to everything
    it calls, streaming responses included."""

    def __init__(
        self,
        app,
        *,
        api_prefix: str,
        is_retired: Callable[[str], bool],
        sessions_unreadable: Callable[[], bool] = lambda: False,
        typed_paths: Callable[[], Collection[str]] = frozenset,
    ) -> None:
        self.app = app
        self._is_retired = is_retired
        self._sessions_unreadable = sessions_unreadable
        self._typed_paths = typed_paths
        self._aware = _compile(api_prefix, _API_SESSION_AWARE) + _compile(
            "", _HOST_SESSION_AWARE
        )
        self._session_routes: Sequence[Pattern[str]] = (
            re.compile(re.escape(api_prefix) + r"/sessions/" + _SESSION_ID + r"(/.*)?"),
            re.compile(r"/sessions/" + _SESSION_ID + r"(/.*)?"),
        )

    def _is_aware(self, method: str, path: str) -> bool:
        if method == "GET" and path in self._typed_paths():
            return True
        return any(
            (m == "*" or m == method) and pattern.fullmatch(path)
            for m, pattern in self._aware
        )

    def _path_session(self, path: str) -> Optional[str]:
        for pattern in self._session_routes:
            match = pattern.fullmatch(path)
            if match:
                return match.group("session_id")
        return None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        path_session = self._path_session(path)
        if path_session is not None and self._is_retired(path_session):
            # Answered as the session routes answer an id they do not hold.
            # A route that would create an unknown id still tells the two
            # apart, by refusing the retired one: that is never recreating it.
            await _send_json(send, 404, {"detail": "session not found"})
            return

        values = Headers(scope=scope).getlist(session_context.SESSION_ID_HEADER)
        if (
            (path_session is not None or values)
            and method != "OPTIONS"
            and await run_in_threadpool(self._sessions_unreadable)
        ):
            # Asked only for a request naming a session, so others cost nothing.
            await _refuse(send, session_context.sessions_unreadable())
            return
        if not values or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        named = {value.strip() for value in values}
        if len(named) > 1:
            first, second = sorted(named)[:2]
            await _refuse(send, session_context.mismatch(first, second))
            return
        session_id = named.pop()
        if not session_id:
            # An empty header names no session that could resolve; reading it
            # as "none" would turn a templated-out id into a direct write.
            await _refuse(send, session_context.not_found(session_id))
            return

        if path_session is not None:
            if path_session != session_id:
                await _refuse(send, session_context.mismatch(session_id, path_session))
                return
        elif not self._is_aware(method, path):
            await _refuse(send, session_context.not_supported(f"{method} {path}"))
            return

        with session_context.bind_session(session_id):
            await self.app(scope, receive, send)


async def _refuse(send, refusal: session_context.SessionRefused) -> None:
    await _send_json(
        send,
        refusal.status,
        {"detail": {"error": refusal.code, "message": refusal.message}},
    )


async def _send_json(send, status: int, payload) -> None:
    # FastAPI's own encoding, so a refusal here reads byte for byte like the
    # routes' answers.
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def add_session_binding_middleware(
    app,
    *,
    api_prefix: str,
    is_retired: Callable[[str], bool],
    sessions_unreadable: Callable[[], bool] = lambda: False,
) -> None:
    app.add_middleware(
        SessionBindingMiddleware,
        api_prefix=api_prefix,
        is_retired=is_retired,
        sessions_unreadable=sessions_unreadable,
        typed_paths=lambda: _typed_interface_paths(app),
    )


def _typed_interface_paths(app) -> Collection[str]:
    """The paths of the config-driven typed interfaces (``list_typed_nodes``,
    ``list_typed_edges``), which compose (§8.2).

    Read off the app's own routes once they are all registered, and only
    where the typed route is the one a GET of that path reaches: a fixed
    route registered first wins such a path, and it may not compose.
    """
    cached = getattr(app.state, "session_aware_typed_paths", None)
    if cached is not None:
        return cached
    paths = set()
    routes = list(app.routes)
    for route in routes:
        if not (getattr(route, "name", "") or "").startswith("custom_interface_"):
            continue
        scope = {"type": "http", "path": route.path, "method": "GET"}
        first = next(
            (r for r in routes if r.matches(scope)[0] == Match.FULL),
            None,
        )
        if first is route:
            paths.add(route.path)
    app.state.session_aware_typed_paths = frozenset(paths)
    return app.state.session_aware_typed_paths
