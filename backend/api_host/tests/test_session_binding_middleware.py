"""SessionBindingMiddleware driven as bare ASGI: OPTIONS passes, an empty header
is refused by the middleware itself, and header values are stripped."""

import asyncio

from backend.api_host.session_binding import SessionBindingMiddleware
from backend.runtime import session_context

HDR = session_context.SESSION_ID_HEADER.lower().encode()
SID = "1111-2222-3333-4444"


def _run(method, path, header_values):
    seen = {}
    sent = []

    async def app(scope, receive, send):
        seen["bound"] = session_context.current_session_id()
        seen["reached"] = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = SessionBindingMiddleware(app, api_prefix="/api", is_retired=lambda s: False)
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(HDR, v.encode()) for v in header_values],
    }

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(mw(scope, receive, send))
    return seen, sent


def test_an_options_request_with_the_header_passes_through_on_a_non_aware_route():
    seen, sent = _run("OPTIONS", "/api/schema", [SID])
    assert seen.get("reached") is True
    assert sent[0]["status"] == 204


def test_an_empty_header_is_refused_by_the_middleware_itself():
    seen, sent = _run("POST", "/api/nodes", [""])
    assert not seen.get("reached")
    assert sent[0]["status"] == 404


def test_header_values_are_stripped_before_binding_and_deduping():
    seen, sent = _run("POST", "/api/nodes", [SID, " " + SID + " "])
    assert seen.get("bound") == SID


def test_a_request_naming_a_session_is_refused_where_records_are_unreadable():
    asked = []

    def unreadable():
        asked.append(1)
        return True

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = SessionBindingMiddleware(
        app,
        api_prefix="/api",
        is_retired=lambda s: False,
        sessions_unreadable=unreadable,
    )

    def status(path, values):
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(HDR, v.encode()) for v in values],
        }
        asyncio.run(mw(scope, receive, send))
        return sent[0]["status"]

    assert status("/api/search", [SID]) == 422
    assert status(f"/api/sessions/{SID}", []) == 422
    asked.clear()
    assert status("/api/schema", []) == 204
    assert asked == []
