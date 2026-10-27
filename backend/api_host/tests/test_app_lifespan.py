"""The app's lifespan starts the session work and stops the background work."""

import threading

from fastapi.testclient import TestClient

from backend.api_host import create_app

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
