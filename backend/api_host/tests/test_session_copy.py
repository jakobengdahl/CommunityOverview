# covers: backend/service/rest_api.py, backend/service/mcp_tools.py
"""Copy guarantees across the REST and MCP boundaries."""

import copy
from unittest.mock import Mock

import pytest

from backend.core import session_manager as manager_module
from backend.runtime.authorization import GraphAuthorizationDecision


def invoke(test_app, source, transport):
    if transport == "rest":
        response = test_app.post(f"/api/sessions/{source.id}/copy")
        return response.status_code, response.json()
    result = test_app.app.state.tools_map["copy_visualization_session"](source.id)
    return (200 if result["success"] else 403), result


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_copy_preserves_canvas_and_isolates_identity_and_history(test_app, transport):
    """Every stored canvas field survives, with new IDs and no shared mutable state."""
    manager = test_app.app.state.session_manager
    source = manager.create_session("Design")
    source.description = "Working canvas"
    source.state.update(
        node_refs=["node"],
        positions={"node": {"x": 20, "y": 30}},
        hidden_node_ids=["hidden"],
        hidden_edge_ids=["edge"],
        dimmed_node_ids=["dimmed"],
        dimmed_edge_ids=["dim-edge"],
        edge_intensity=0.4,
    )
    kinds = [
        "shape",
        "text",
        "note",
        "freehand",
        "line",
        "icon",
        "vote_dot",
        "image",
        "group",
        "reference",
        "label",
        "heatmap",
    ]
    source.state["annotations"] = [
        {
            "id": kind,
            "type": kind,
            "x": 1,
            "y": 2,
            "content": {"text": kind},
            "version": 7,
            "field_versions": {"content": 7},
        }
        for kind in kinds
    ]
    source.state["annotations"][4]["content"]["start"] = {
        "attachment": {"target_type": "annotation", "target_id": "shape"}
    }
    source.state["annotations"][7]["content"]["image"] = {
        "url": "data:image/webp;base64,AAAA"
    }
    source.state["annotations"][8]["member_node_ids"] = ["node"]
    source.state["annotations"][9]["content"]["target"] = source.id
    source.seq = 19
    source.activity_log = [{"id": "history"}]
    manager.presence.join(source.id, "other", "Participant")
    manager.leases.acquire(source.id, "other", ["shape"])
    before = copy.deepcopy(source.to_dict())
    status, result = invoke(test_app, source, transport)
    assert status == 200
    copied_id = result["id"] if transport == "rest" else result["session"]["session_id"]
    copied = manager.get_session(copied_id)
    assert copied.id != source.id
    assert copied.name == "Design (copy)"
    assert copied.description == source.description
    assert copied.seq == 0 and copied.activity_log == []
    assert manager.roster(copied.id) == []
    assert manager.leases.snapshot(copied.id) == {}
    assert manager.leases.snapshot(source.id) == {"shape": "other"}
    annotations = copied.state["annotations"]
    assert {a["id"] for a in annotations}.isdisjoint(kinds)
    assert (
        annotations[4]["content"]["start"]["attachment"]["target_id"]
        == annotations[0]["id"]
    )
    expected = copy.deepcopy(before["state"])
    for original, actual in zip(expected["annotations"], annotations):
        original["id"] = actual["id"]
        original["version"] = 1
        original["field_versions"] = {}
    expected["annotations"][4]["content"]["start"]["attachment"]["target_id"] = (
        annotations[0]["id"]
    )
    assert copied.state == expected
    annotations[0]["content"]["text"] = "changed"
    assert source.to_dict() == before
    manager.store.persist(copied)
    assert manager.store._backend.load(copied.id)["state"] == copied.state


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("denied_action", ["read", "mutate"])
def test_copy_requires_source_read_access(test_app, transport, denied_action):
    """Permission to create alone cannot copy an unreadable source."""
    source = test_app.app.state.session_manager.create_session()
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=not (
            ctx.action == denied_action
            and (denied_action == "mutate" or ctx.session_id == source.id)
        )
    )
    test_app.app.state.graph_service._authorization_hook = hook
    assert invoke(test_app, source, transport)[0] == 403
    assert test_app.app.state.session_manager.store.session_count() == 1


def test_copy_rejects_large_state_before_creating(test_app, monkeypatch):
    """An over-budget copy leaves no empty destination behind."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.description = "x" * 2000
    monkeypatch.setattr(manager_module, "DEFAULT_MAX_SESSION_DOCUMENT_BYTES", 2048)
    response = test_app.post(f"/api/sessions/{source.id}/copy")
    assert response.status_code == 413
    assert manager.store.session_count() == 1


def test_copy_preserves_staged_mode_without_staged_changes(
    app_config, mock_llm_provider
):
    """A staged copy starts an empty layer and leaves the source layer intact."""
    from dataclasses import replace
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from backend.api_host.server import create_app
    from backend.runtime.session_context import SESSION_ID_HEADER

    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(
            create_app(replace(app_config, graph_staged_sessions="true"))
        )
    source = client.post("/api/sessions", json={"write_mode": "staged"}).json()["id"]
    assert (
        client.patch(
            "/api/nodes/node-1",
            headers={SESSION_ID_HEADER: source},
            json={"updates": {"description": "staged"}},
        ).status_code
        == 200
    )
    before = client.get(f"/api/sessions/{source}/staged").json()["items"]
    assert len(before) == 1
    response = client.post(f"/api/sessions/{source}/copy")
    assert response.status_code == 200
    copied = response.json()
    assert copied["write_mode"] == "staged"
    assert client.get(f"/api/sessions/{copied['id']}/staged").json()["items"] == []
    assert client.get(f"/api/sessions/{source}/staged").json()["items"] == before


def test_copy_obeys_session_count_limit(test_app):
    """Copying cannot bypass the session allocation cap."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    manager._max_sessions = 1
    assert test_app.post(f"/api/sessions/{source.id}/copy").status_code == 503
    assert manager.store.session_count() == 1
