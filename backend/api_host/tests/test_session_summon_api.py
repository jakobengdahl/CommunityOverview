"""
HTTP contract of ``POST /api/sessions/{id}/summon`` and the staged-session
case of the ``summon_session_viewport`` MCP tool: the route asks the hook under
its MCP twin's name, validates the target and never creates a session; in a
staged session the tool frames nodes of the composed view.
"""

import dataclasses
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host.server import create_app
from backend.service import access

BOUNDS = {"x": 0, "y": 0, "width": 400, "height": 300}


def _listen(client, session_id, client_id="viewer", name="Viewer"):
    manager = client.app.state.session_manager
    sub, _ = manager.connect(session_id, client_id, name)
    while not sub.queue.empty():
        sub.queue.get_nowait()
    return sub


def _summons(sub):
    out = []
    while not sub.queue.empty():
        event = sub.queue.get_nowait()
        if event.get("type") == "viewport_summoned":
            out.append(event)
    return out


class TestSummonRoute:
    def test_summons_every_connected_client_naming_the_summoner(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        _listen(test_app, sid, "summoner", "Alice")
        viewer = _listen(test_app, sid)

        resp = test_app.post(
            f"/api/sessions/{sid}/summon",
            json={"client_id": "summoner", "bounds": BOUNDS},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["connected_clients"] == 2
        (event,) = _summons(viewer)
        assert event["summoned_by"] == "Alice"
        assert event["client_id"] == "summoner"

    @pytest.mark.parametrize(
        "bounds",
        [
            {**BOUNDS, "width": 0},
            {"x": 0, "y": 0, "width": 1},
            {**BOUNDS, "x": 1e30},
            "everywhere",
            [0, 0, 1, 1],
            None,
        ],
    )
    def test_invalid_bounds_are_400(self, test_app: TestClient, bounds):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        viewer = _listen(test_app, sid)

        resp = test_app.post(
            f"/api/sessions/{sid}/summon", json={"client_id": "c", "bounds": bounds}
        )

        assert resp.status_code == 400
        assert _summons(viewer) == []

    def test_unknown_session_is_404_and_not_created(self, test_app: TestClient):
        resp = test_app.post(
            "/api/sessions/9999-9991/summon", json={"client_id": "c", "bounds": BOUNDS}
        )
        assert resp.status_code == 404
        assert test_app.get("/api/sessions/9999-9991").status_code == 404

    def test_invalid_session_id_is_400(self, test_app: TestClient):
        resp = test_app.post(
            "/api/sessions/nope/summon", json={"client_id": "c", "bounds": BOUNDS}
        )
        assert resp.status_code == 400

    def test_the_route_asks_the_hook_under_its_mcp_twins_name(
        self, test_app: TestClient, monkeypatch
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        asked = []
        original = access.authorize_graph_access

        def spy(hook, **kwargs):
            asked.append(kwargs)
            return original(hook, **kwargs)

        monkeypatch.setattr(access, "authorize_graph_access", spy)
        test_app.post(
            f"/api/sessions/{sid}/summon", json={"client_id": "c", "bounds": BOUNDS}
        )

        seen = [c for c in asked if c["target"] == "summon_session_viewport"]
        assert seen and seen[0]["session_id"] == sid

    def test_a_caller_that_may_not_act_in_the_session_cannot_summon(
        self, test_app: TestClient, monkeypatch
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        viewer = _listen(test_app, sid)
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")

        resp = test_app.post(
            f"/api/sessions/{sid}/summon", json={"client_id": "c", "bounds": BOUNDS}
        )

        assert resp.status_code == 403
        assert _summons(viewer) == []


@pytest.fixture
def staged_client(app_config, mock_llm_provider):
    config = dataclasses.replace(app_config, graph_staged_sessions="true")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        yield TestClient(create_app(config))


def test_mcp_summon_in_a_staged_session_frames_a_staged_node(staged_client):
    tools = staged_client.app.state.tools_map
    sid = tools["create_visualization_session"](name="s", write_mode="staged")[
        "session"
    ]["session_id"]
    staged = tools["add_nodes"](
        nodes=[{"id": "staged-1", "type": "Actor", "name": "staged"}],
        edges=[],
        visualization_session_id=sid,
    )
    assert staged["staged"] is True
    assert staged_client.app.state.graph_storage.get_node("staged-1") is None
    added = tools["add_nodes_to_session"](session_id=sid, node_ids=["staged-1"])
    assert added.get("success") is True, added
    tools["apply_visualization_layout"](
        session_id=sid, positions={"staged-1": {"x": 40, "y": 60}}
    )
    viewer = _listen(staged_client, sid)

    result = tools["summon_session_viewport"](session_id=sid, node_ids=["staged-1"])

    assert result["success"] is True, result
    (event,) = _summons(viewer)
    assert (event["bounds"]["x"], event["bounds"]["y"]) == (40.0, 60.0)
