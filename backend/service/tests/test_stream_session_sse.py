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
from starlette.requests import ClientDisconnect

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


# More queue waits than any test here drives. A generator that times out
# without yielding (a dropped ping) never suspends, so without this bound it
# would spin until the pytest timeout instead of failing at once.
_MAX_QUEUE_WAITS = 10


def _time_out_queue_waits(monkeypatch):
    """Make every queue wait in the generator time out at once."""
    waits = 0

    async def _timeout(awaitable, timeout):
        nonlocal waits
        awaitable.close()
        waits += 1
        if waits > _MAX_QUEUE_WAITS:
            raise AssertionError(
                f"{waits} queue waits timed out without the stream yielding"
            )
        raise asyncio.TimeoutError

    # Swap only the module's view of asyncio so the real wait_for elsewhere
    # (the event loop, pytest-asyncio) is untouched.
    fake_asyncio = SimpleNamespace(
        wait_for=_timeout,
        TimeoutError=asyncio.TimeoutError,
        CancelledError=asyncio.CancelledError,
        QueueEmpty=asyncio.QueueEmpty,
    )
    monkeypatch.setattr(rest_api_module, "asyncio", fake_asyncio)


@pytest.mark.asyncio
async def test_queue_wait_timeout_emits_ping_comment_and_keeps_streaming(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    assert (await anext(body)).startswith("data: ")  # catch-up snapshot
    assert '"presence_joined"' in await anext(body)

    _time_out_queue_waits(monkeypatch)

    assert await anext(body) == ": ping\n\n"
    # The loop continues after a ping rather than ending the stream.
    assert await anext(body) == ": ping\n\n"
    assert manager.bus.subscriber_count(session_id) == 1

    await body.aclose()
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_resync_after_unannounced_delete_synthesises_notice_and_stays_open(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    await anext(body)  # catch-up snapshot
    await anext(body)  # own presence_joined echo

    # The overflow sentinel is queued, then the session vanishes (with any
    # notice drained by the overflow) before the generator translates it into
    # a fresh catch_up snapshot.
    manager.bus.publish(session_id, {"type": "resync"})
    assert manager.store.delete(session_id)

    chunk = await anext(body)
    assert chunk == 'data: {"type": "session_deleted", "deleted_by": null}\n\n'
    # Like the first catch_up path, the stream stays open until the client
    # leaves, so the browser does not reconnect into an empty session.
    _time_out_queue_waits(monkeypatch)
    assert await anext(body) == ": ping\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_resync_after_delete_forwards_the_notice_queued_behind_it(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    await anext(body)  # catch-up snapshot
    await anext(body)  # own presence_joined echo

    # The overflow sentinel is queued, then delete_session's own notice lands
    # behind it; the resync translation finds the session gone.
    manager.bus.publish(session_id, {"type": "resync"})
    assert await manager.delete_session(session_id, deleted_by="bob")

    chunk = await anext(body)
    assert chunk.startswith("data: ")
    assert json.loads(chunk[len("data: ") :]) == {
        "type": "session_deleted",
        "deleted_by": "bob",
        "write_mode": "direct",
    }
    _time_out_queue_waits(monkeypatch)
    assert await anext(body) == ": ping\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_delete_before_first_catch_up_forwards_the_queued_notice(
    stream_env, monkeypatch
):
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
    _time_out_queue_waits(monkeypatch)
    assert await anext(body) == ": ping\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(body)
    _assert_disconnected(manager, session_id)


@pytest.mark.asyncio
async def test_unannounced_delete_before_first_catch_up_synthesises_notice(
    stream_env, monkeypatch
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, body = await _open_stream(endpoint, manager, request)

    # A removal that published nothing still must not abort the stream.
    assert manager.store.delete(session_id)

    chunk = await anext(body)
    assert chunk == 'data: {"type": "session_deleted", "deleted_by": null}\n\n'
    _time_out_queue_waits(monkeypatch)
    assert await anext(body) == ": ping\n\n"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "raised"),
    [(OSError, ClientDisconnect), (asyncio.CancelledError, asyncio.CancelledError)],
)
async def test_failed_response_start_unsubscribes_without_iterating_the_body(
    stream_env, failure, raised
):
    manager, endpoint = stream_env
    session = manager.create_session()
    response = await endpoint(
        session_id=session.id,
        request=_FakeRequest(),
        client_id="c1",
        name="Alice",
        since_seq=None,
    )
    assert manager.bus.subscriber_count(session.id) == 1

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        # The client is gone before the status line is written.
        assert message["type"] == "http.response.start"
        raise failure

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    with pytest.raises(raised):
        await response(scope, receive, send)
    _assert_disconnected(manager, session.id)


@pytest.mark.asyncio
async def test_completed_response_releases_once_and_keeps_sibling_presence(
    stream_env,
):
    manager, endpoint = stream_env
    request = _FakeRequest()
    session_id, sibling = await _open_stream(endpoint, manager, _FakeRequest())
    await anext(sibling)  # the sibling connection's generator is running
    response = await endpoint(
        session_id=session_id,
        request=request,
        client_id="c1",
        name="Alice",
        since_seq=None,
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        # The client leaves after the first chunk, so the generator's finally
        # and the response's both run.
        if message["type"] == "http.response.body":
            request.disconnected = True

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    assert manager.bus.subscriber_count(session_id) == 1
    assert [m["client_id"] for m in manager.roster(session_id)] == ["c1"]
    await sibling.aclose()
    _assert_disconnected(manager, session_id)
