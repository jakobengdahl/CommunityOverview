# covers: backend/service/rest_api.py, backend/service/mcp_tools.py
"""Copy guarantees across the REST and MCP boundaries."""

import copy
from unittest.mock import Mock

import pytest

from backend.core import session_manager as manager_module
from backend.core.session_annotations import image_annotation_error
from backend.core.write_modes import STAGED_UNSUPPORTED
from backend.runtime.authorization import GraphAuthorizationDecision


def invoke(test_app, source, transport):
    if transport == "rest":
        response = test_app.post(f"/api/sessions/{source.id}/copy")
        return response.status_code, response.json()
    result = test_app.app.state.tools_map["copy_visualization_session"](source.id)
    return (200 if result["success"] else 403), result


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_copy_preserves_canvas_and_isolates_identity_and_history(test_app, transport):
    """Every stored canvas field survives, with new IDs and no shared mutable state.

    Every reference resolved as an annotation id follows the copied annotation.
    """
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
        shape_overrides={"node": {"shape": "hexagon"}},
        type_shapes={"Actor": {"shape": "diamond"}},
        shape_revisions={"node:node": 2, "type:Actor": 1},
        shape_operation_ids={'["op-1"]': True},
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
            "created_by": "other",
            "updated_by": "other",
        }
        for kind in kinds
    ]
    source.state["annotations"][4]["content"]["start"] = {
        "attachment": {"target_type": "annotation", "target_id": "shape"}
    }
    source.state["annotations"][4].update(startAnchor="text", endAnchor="node")
    source.state["annotations"][5]["attachment"] = {"target_id": "note"}
    source.state["annotations"][6]["attachment"] = {
        "target_type": "node",
        "target_id": "shape",
    }
    source.state["annotations"][7]["image"] = {"url": "data:image/webp;base64,AAAA"}
    source.state["annotations"][8]["member_node_ids"] = ["node"]
    source.state["annotations"][9].update(target_kind="session", target=source.id)
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
    assert copied.name == "Copy of Design"
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
    expected["shape_operation_ids"] = {}
    for original, actual in zip(expected["annotations"], annotations):
        original["id"] = actual["id"]
        original["version"] = 1
        original["field_versions"] = {}
        del original["created_by"], original["updated_by"]
    expected["annotations"][4]["content"]["start"]["attachment"]["target_id"] = (
        annotations[0]["id"]
    )
    expected["annotations"][4]["startAnchor"] = annotations[1]["id"]
    expected["annotations"][5]["attachment"]["target_id"] = annotations[2]["id"]
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
    status, result = invoke(test_app, source, transport)
    assert status == 403
    if transport == "mcp":
        assert result["error_code"] == "access_denied"
        assert "session" not in result
    assert test_app.app.state.session_manager.store.session_count() == 1


@pytest.mark.parametrize("description", ["x" * 2000, "å" * 200])
def test_copy_rejects_large_state_before_creating(test_app, monkeypatch, description):
    """An over-budget copy, measured as op batches are, leaves no destination."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.description = description
    monkeypatch.setattr(manager_module, "DEFAULT_MAX_SESSION_DOCUMENT_BYTES", 2048)
    response = test_app.post(f"/api/sessions/{source.id}/copy")
    assert response.status_code == 413
    assert manager.store.session_count() == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_copy_carries_staged_changes_only_for_a_copier_who_may_stage(
    app_config, mock_llm_provider, transport
):
    """A staged copy holds the source's staged changes, staged by the copier,
    the graph unchanged; a copier the hook refuses staging is refused (REST
    403, MCP success false) and no session is created."""
    from dataclasses import replace
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from backend.api_host.server import create_app
    from backend.runtime.authorization import use_request_authorization
    from backend.runtime.session_context import SESSION_ID_HEADER

    def actor(actor_id):
        return {
            "x-communityoverview-actor-id": actor_id,
            "x-communityoverview-actor-type": "member",
            "x-communityoverview-auth-source": "gateway",
        }

    def copy_as(source, actor_id, name=None):
        if transport == "rest":
            response = client.post(
                f"/api/sessions/{source}/copy",
                headers=actor(actor_id),
                json={"name": name} if name else None,
            )
            return response.status_code == 200, response.json()
        with use_request_authorization(headers=actor(actor_id)):
            result = client.app.state.tools_map["copy_visualization_session"](source)
        return result["success"], result

    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(
            create_app(replace(app_config, graph_staged_sessions="true"))
        )
    source = client.post("/api/sessions", json={"write_mode": "staged"}).json()["id"]
    assert (
        client.patch(
            "/api/nodes/node-1",
            headers={SESSION_ID_HEADER: source, **actor("author")},
            json={"updates": {"description": "staged"}},
        ).status_code
        == 200
    )
    before = client.get(f"/api/sessions/{source}/staged").json()["items"]
    assert [i["staged_by"] for i in before] == ["author"]
    graph_before = client.get("/api/nodes/node-1").json()
    succeeded, result = copy_as(source, "copier", name="Kopia")
    assert succeeded
    copied = result if transport == "rest" else result["session"]
    copied_id = copied["id"] if transport == "rest" else copied["session_id"]
    assert copied["write_mode"] == "staged"
    if transport == "rest":
        assert copied["name"] == "Kopia"
    copied_items = client.get(f"/api/sessions/{copied_id}/staged").json()["items"]
    assert all(i["staged_at"] > before[0]["staged_at"] for i in copied_items)
    assert [i["staged_by"] for i in copied_items] == ["copier"]
    own = ("version", "session_seq", "staged_at", "staged_by")
    assert [{k: v for k, v in i.items() if k not in own} for i in copied_items] == [
        {k: v for k, v in i.items() if k not in own} for i in before
    ]
    assert client.get(f"/api/sessions/{source}/staged").json()["items"] == before
    assert client.get("/api/nodes/node-1").json() == graph_before

    manager = client.app.state.session_manager
    count = manager.store.session_count()
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=ctx.graph_operation != "stage"
    )
    client.app.state.graph_service._authorization_hook = hook
    succeeded, result = copy_as(source, "copier")
    assert not succeeded
    if transport == "mcp":
        assert result["success"] is False and "session" not in result
    assert manager.store.session_count() == count
    asked = [call.args[0] for call in hook.evaluate.call_args_list]
    assert [
        (c.target, c.session_id) for c in asked if c.graph_operation == "stage"
    ] == [("copy_visualization_session", source)]


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_copy_of_staged_source_without_staged_support_is_refused(
    test_app, monkeypatch, transport
):
    """A staged source on a deployment without staged sessions yields its refusal code."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    monkeypatch.setattr(manager, "write_mode_of", lambda s: {"write_mode": "staged"})
    if transport == "rest":
        response = test_app.post(f"/api/sessions/{source.id}/copy")
        assert response.status_code == 422
    else:
        result = invoke(test_app, source, transport)[1]
        assert result["error"] == STAGED_UNSUPPORTED
    assert manager.store.session_count() == 1


def test_copy_obeys_session_count_limit(test_app):
    """Copying cannot bypass the session allocation cap."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    manager._max_sessions = 1
    assert test_app.post(f"/api/sessions/{source.id}/copy").status_code == 503
    assert manager.store.session_count() == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_copy_refuses_legacy_remote_image_url_before_creating(test_app, transport):
    """A non-embedded image URL counts as a new reference; nothing is allocated."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state["annotations"] = [
        {
            "id": "legacy",
            "type": "image",
            "x": 0,
            "y": 0,
            "image": {"url": "https://example.com/picture.png"},
        }
    ]
    before = copy.deepcopy(source.to_dict())
    status, result = invoke(test_app, source, transport)
    assert status == (400 if transport == "rest" else 403)
    assert result.get("success") is not True
    if transport == "mcp":
        assert result["error"] == image_annotation_error(source.state["annotations"][0])
    assert manager.store.session_count() == 1
    assert source.to_dict() == before
