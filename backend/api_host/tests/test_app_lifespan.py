"""The app's lifespan starts the session work and stops the background work."""

import asyncio
import contextlib
import threading

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.server import AppRuntime
from backend.api_host.tests.conftest import _BUILT_APPS

_BACKGROUND_THREADS = ("event-delivery-worker", "merge-record-retention")


def _background_threads():
    return sorted(
        t.name for t in threading.enumerate() if t.name in _BACKGROUND_THREADS
    )


def test_closing_the_client_stops_the_work_the_app_started(app_config):
    before = _background_threads()
    app = create_app(app_config)

    with TestClient(app) as client:
        cleanup_task = app.state.session_cleanup_task
        assert client.get("/health").status_code == 200
        assert _background_threads() != before

    assert cleanup_task.cancelled()
    assert _background_threads() == before


def test_the_streamable_transport_is_served_only_inside_the_lifespan(app_config):
    app = create_app(app_config)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(app) as client:
        started = client.post("/mcp", json=initialize, headers=headers)

    stopped = TestClient(app).post("/mcp", json=initialize, headers=headers)

    assert started.status_code == 200
    assert stopped.status_code != 200


def _record_lifecycle(app, monkeypatch):
    events = []
    registry = app.state.session_registry
    manager = app.state.mcp.session_manager
    bind_loop, run_manager = registry.set_event_loop, manager.run
    stop_runtime = AppRuntime.stop

    def recording_bind_loop(loop):
        events.append("registry started")
        bind_loop(loop)

    @contextlib.asynccontextmanager
    async def recording_run():
        async with run_manager():
            events.append("manager started")
            yield
            events.append("manager stopping")

    # AppRuntime is frozen, so the class is patched; other apps pass through.
    def recording_stop(runtime):
        if runtime is app.state.runtime:
            task = getattr(app.state, "session_cleanup_task", None)
            cancelled = task is not None and task.cancelling() > 0
            events.append(f"runtime stopped, cleanup cancelled={cancelled}")
        stop_runtime(runtime)

    monkeypatch.setattr(registry, "set_event_loop", recording_bind_loop)
    monkeypatch.setattr(manager, "run", recording_run)
    monkeypatch.setattr(AppRuntime, "stop", recording_stop)
    return events


def test_startup_and_shutdown_run_in_order(app_config, monkeypatch):
    app = create_app(app_config)
    events = _record_lifecycle(app, monkeypatch)

    with TestClient(app):
        pass

    assert events == [
        "registry started",
        "manager started",
        "manager stopping",
        "runtime stopped, cleanup cancelled=True",
    ]


def test_an_error_while_serving_still_shuts_down(app_config, monkeypatch):
    app = create_app(app_config)
    events = _record_lifecycle(app, monkeypatch)

    # Starlette throws into the lifespan when serving ends abnormally; the
    # TestClient never does, so the lifespan is driven directly.
    async def serve():
        async with app.router.lifespan_context(app):
            raise RuntimeError("while serving")

    # The session manager's task group wraps what leaves its scope.
    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(serve())

    assert raised.group_contains(RuntimeError, match="while serving")

    assert events[-1] == "runtime stopped, cleanup cancelled=True"


def test_a_failed_session_registry_startup_skips_the_rest(
    app_config, monkeypatch, request
):
    app = create_app(app_config)
    # The teardown would run the failing startup again; this test stops it.
    request.node.stash[_BUILT_APPS].clear()
    events = _record_lifecycle(app, monkeypatch)

    def broken(loop):
        raise RuntimeError("registry broke")

    monkeypatch.setattr(app.state.session_registry, "set_event_loop", broken)

    with pytest.raises(RuntimeError, match="registry broke"):
        with TestClient(app):
            pass

    assert events == []
    app.state.runtime.stop()
