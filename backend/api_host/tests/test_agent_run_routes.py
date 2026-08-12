"""Tests for the /agents/runs read endpoints."""

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api_host.agent_routes import register_agent_routes


def _client(registry):
    app = FastAPI()
    register_agent_routes(
        app,
        agent_registry=registry,
        graph_storage=MagicMock(),
        federation_manager=MagicMock(),
    )
    return TestClient(app)


def test_list_runs_passes_filters_and_returns_payload():
    registry = MagicMock()
    registry.list_runs.return_value = [
        {"id": "job-1", "agent_id": "a1", "status": "succeeded"}
    ]
    client = _client(registry)

    resp = client.get(
        "/agents/runs",
        params={"agent_id": "a1", "kind": "event", "status": "succeeded", "limit": 25},
    )

    assert resp.status_code == 200
    assert resp.json() == [{"id": "job-1", "agent_id": "a1", "status": "succeeded"}]
    registry.list_runs.assert_called_once_with(
        agent_id="a1", kind="event", status="succeeded", limit=25
    )


def test_list_runs_defaults():
    registry = MagicMock()
    registry.list_runs.return_value = []
    client = _client(registry)

    resp = client.get("/agents/runs")
    assert resp.status_code == 200
    assert resp.json() == []
    registry.list_runs.assert_called_once_with(
        agent_id=None, kind=None, status=None, limit=100
    )


def test_list_runs_rejects_out_of_range_limit():
    registry = MagicMock()
    client = _client(registry)
    assert client.get("/agents/runs", params={"limit": 0}).status_code == 422
    assert client.get("/agents/runs", params={"limit": 501}).status_code == 422


def test_run_detail_found():
    registry = MagicMock()
    registry.get_run.return_value = {"id": "job-1", "status": "running"}
    client = _client(registry)

    resp = client.get("/agents/runs/job-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    registry.get_run.assert_called_once_with("job-1")


def test_run_detail_missing_returns_404():
    registry = MagicMock()
    registry.get_run.return_value = None
    client = _client(registry)

    resp = client.get("/agents/runs/nope")
    assert resp.status_code == 404
