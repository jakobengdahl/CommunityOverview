"""Tests for the /agents/proposals governance endpoints."""

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


def test_list_proposals_passes_filters():
    registry = MagicMock()
    registry.list_proposals.return_value = [{"id": "prop-1", "status": "pending"}]
    client = _client(registry)

    resp = client.get(
        "/agents/proposals",
        params={"agent_id": "a1", "status": "pending", "limit": 10},
    )
    assert resp.status_code == 200
    assert resp.json() == [{"id": "prop-1", "status": "pending"}]
    registry.list_proposals.assert_called_once_with(
        agent_id="a1", status="pending", limit=10
    )


def test_list_proposals_limit_bounds():
    registry = MagicMock()
    client = _client(registry)
    assert client.get("/agents/proposals", params={"limit": 0}).status_code == 422
    assert client.get("/agents/proposals", params={"limit": 501}).status_code == 422


def test_proposal_detail_404():
    registry = MagicMock()
    registry.get_proposal.return_value = None
    client = _client(registry)
    assert client.get("/agents/proposals/nope").status_code == 404


def test_approve_applies_and_returns_result():
    registry = MagicMock()
    registry.approve_proposal.return_value = {
        "id": "prop-1",
        "status": "applied",
        "apply_result": {"added_node_ids": ["n1"]},
    }
    client = _client(registry)

    resp = client.post(
        "/agents/proposals/prop-1/approve", params={"decided_by": "jakob"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    registry.approve_proposal.assert_called_once_with("prop-1", decided_by="jakob")


def test_approve_missing_returns_404():
    registry = MagicMock()
    registry.approve_proposal.return_value = None
    client = _client(registry)
    assert client.post("/agents/proposals/nope/approve").status_code == 404


def test_reject_returns_result():
    registry = MagicMock()
    registry.reject_proposal.return_value = {"id": "prop-1", "status": "rejected"}
    client = _client(registry)

    resp = client.post("/agents/proposals/prop-1/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    registry.reject_proposal.assert_called_once_with("prop-1", decided_by=None)
