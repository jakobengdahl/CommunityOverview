"""
Edge paths of the shared-session SSE generator (``GET /sessions/{id}/stream``).

The endpoint is called directly (not through TestClient, whose stream never
returns) with a real in-memory ``SessionManager``, and its ``StreamingResponse``
body iterator is driven by hand. That keeps the keep-alive ping, the
resync-after-delete race and the unsubscribe-on-exit guarantees testable
without waiting on the real 25-second queue timeout.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.service import GraphService
from backend.service import rest_api as rest_api_module
from backend.service.rest_api import create_rest_router


class _FakeRequest:
    """The slice of ``starlette.requests.Request`` the stream route reads."""

    def __init__(self) -> None:
        self.client = SimpleNamespace(host="127.0.0.1")
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.headers = {}
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.fixture
def stream_env(tmp_path):
    service = GraphService(GraphStorage(json_path=os.path.join(tmp_path, "g.json")))
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    router = create_rest_router(service, session_manager=manager)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/sessions/{session_id}/stream"
    )
    return manager, endpoint


async def _open_stream(endpoint, manager, request):
    session = manager.create_session()
    response = await endpoint(
        session_id=session.id,
        request=request,
        client_id="c1",
        name="Alice",
        since_seq=None,
    )
    return session.id, response.body_iterator


def _assert_disconnected(manager, session_id):
    assert manager.bus.subscriber_count(session_id) == 0
    assert manager.roster(session_id) == []


@pytest.mark.asyncio
async def test_queue_wait_timeout_emits_ping_comment_and_keeps_streaming(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    assert (await anext(body)).startswith("data: ")  # catch-up snapshot
    assert '"presence_joined"' in await anext(body)

    async def _timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    # Swap only the module's view of asyncio so the real wait_for elsewhere
    # (the event loop, pytest-asyncio) is untouched.
    fake_asyncio = SimpleNamespace(
        wait_for=_timeout,
        TimeoutError=asyncio.TimeoutError,
        CancelledError=asyncio.CancelledError,
    )
    monkeypatch.setattr(rest_api_module, "asyncio", fake_asyncio)

    assert await anext(body) == ": ping\n\n"
    # The loop continues after a ping rather than ending the stream.
    assert await anext(body) == ": ping\n\n"
    assert manager.bus.subscriber_count(session_id) == 1

    await body.aclose()
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_resync_after_session_deleted_becomes_session_deleted_and_ends(
    stream_env,
):
    manager, endpoint = stream_env
    session_id, body = await _open_stream(endpoint, manager, _FakeRequest())

    await anext(body)  # catch-up snapshot
    await anext(body)  # own presence_joined echo

    # The overflow sentinel is queued, then the session vanishes before the
    # generator translates it into a fresh catch_up snapshot.
    manager.bus.publish(session_id, {"type": "resync"})
    assert manager.store.delete(session_id)

    chunk = await anext(body)
    assert chunk == 'data: {"type": "session_deleted", "deleted_by": null}\n\n'
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_delete_before_first_catch_up_forwards_the_queued_notice(stream_env):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    # The handshake passed; the session goes before the body is first iterated.
    assert await manager.delete_session(session_id, deleted_by="bob")

    chunk = await anext(body)
    assert chunk.startswith("data: ")
    assert json.loads(chunk[len("data: ") :]) == {
        "type": "session_deleted",
        "deleted_by": "bob",
        "write_mode": "direct",
    }
    # Like a mid-stream delete, the stream stays open until the client leaves.
    assert manager.bus.subscriber_count(session_id) == 1
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_unannounced_delete_before_first_catch_up_synthesises_notice(
    stream_env,
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    # A removal that published nothing still must not abort the stream.
    assert manager.store.delete(session_id)

    chunk = await anext(body)
    assert chunk == 'data: {"type": "session_deleted", "deleted_by": null}\n\n'
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_client_disconnect_ends_stream_and_unsubscribes(stream_env):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    await anext(body)
    request.disconnected = True

    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_an_event_unsubscribes(stream_env):
    manager, endpoint = stream_env
    session_id, body = await _open_stream(endpoint, manager, _FakeRequest())

    await anext(body)  # catch-up snapshot
    await anext(body)  # own presence_joined echo

    pending = asyncio.ensure_future(anext(body))
    # Let the generator reach its await on the empty subscription queue.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pending.done()
    pending.cancel()

    # Whether the generator swallows or re-raises the cancellation is not the
    # invariant here; releasing the subscription and presence is.
    with pytest.raises((StopAsyncIteration, asyncio.CancelledError)):
        await pending
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_unexpected_error_propagates_and_still_unsubscribes(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    session_id, body = await _open_stream(endpoint, manager, _FakeRequest())

    await anext(body)  # catch-up snapshot

    def _boom(event, session_manager, sid):
        raise RuntimeError("translation failed")

    monkeypatch.setattr(rest_api_module, "_resolve_stream_event", _boom)

    with pytest.raises(RuntimeError, match="translation failed"):
        await anext(body)
    _assert_disconnected(manager, session_id)
