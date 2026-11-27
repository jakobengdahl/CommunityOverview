# covers: backend/service/rest_api.py, backend/service/mcp_tools.py
"""Copy guarantees across the REST and MCP boundaries."""

import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.core import session_manager as manager_module
from backend.core.layer_store import LayerStoreError
from backend.core.session_annotations import image_annotation_error
from backend.core.write_modes import STAGED_UNSUPPORTED
from backend.service.staging import HookFailed
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
@pytest.mark.parametrize("denied_action", ["read", "mutate", "assign"])
def test_copy_requires_source_read_access(test_app, transport, denied_action):
    """Permission to create alone cannot copy an unreadable source, nor a copy
    whose access assignments the hook refuses."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    before = copy.deepcopy(source.to_dict())
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=not (
            ctx.action == denied_action
            and (denied_action == "mutate" or ctx.session_id == source.id)
        )
    )
    hook.assign_copied_session_access.return_value = GraphAuthorizationDecision(
        allowed=denied_action != "assign"
    )
    test_app.app.state.graph_service._authorization_hook = hook
    status, result = invoke(test_app, source, transport)
    assert status == 403
    if transport == "mcp":
        assert result["error_code"] == "access_denied"
        assert "session" not in result
    assert manager.store.session_count() == 1
    assert source.to_dict() == before


def test_capture_contract_describes_visibility_on_failure():
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "docs/SESSION_SAVEDVIEW_COMPAT_CONTRACT.md"
    )
    contract = contract_path.read_text()
    text = " ".join(contract.split())
    assert "A destination may be briefly visible before access assignment" in text
    assert "if assignment fails, it is removed before failure is reported" in text
    assert "no partial copy is visible on every failure" not in text


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
    the graph unchanged; a copier the hook refuses staging, or whose copy's
    access assignments it refuses, is refused (REST 403, MCP success false),
    as is a source over the layer cap (layer_full), and no session is created.
    The hook assigns access as the copier, from the source to the copy it
    returns; copied entries carry a fresh stage token."""
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
            create_app(
                replace(
                    app_config,
                    graph_staged_sessions="true",
                    session_layer_max_entries="1",
                )
            )
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

    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(allowed=True)
    assign = hook.assign_copied_session_access
    assign.return_value = GraphAuthorizationDecision(allowed=False)
    succeeded, _ = copy_as(source, "copier")
    assert not succeeded
    assert manager.store.session_count() == count
    refused_id = assign.call_args.kwargs["copy_session_id"]
    assert manager.write_modes.is_retired(refused_id)
    assign.return_value = GraphAuthorizationDecision(allowed=True)
    succeeded, result = copy_as(source, "copier")
    assert succeeded
    copied = result if transport == "rest" else result["session"]
    (context,), kwargs = assign.call_args
    assert (context.actor["actor_id"], context.session_id) == ("copier", source)
    assert kwargs["copy_session_id"] == copied.get("id", copied.get("session_id"))

    # A hook's own OSError is no storage failure; a failed document save is
    # storage_error and leaves no staged record or entries behind.
    layer = manager.write_modes.layer_store
    count = manager.store.session_count()
    assign.side_effect = ConnectionError()
    with pytest.raises(HookFailed):
        copy_as(source, "copier")
    assign.side_effect = None
    assert manager.store.session_count() == count
    with patch.object(manager.store._backend, "save", side_effect=OSError("x")):
        succeeded, result = copy_as(source, "copier")
    assert not succeeded
    refusal = result["detail"] if transport == "rest" else result
    assert refusal["error"] == "storage_error"
    failed_id = assign.call_args.kwargs["copy_session_id"]
    assert manager.write_modes.is_retired(failed_id)
    assert layer.load_layer(failed_id) == []
    assert manager.store.session_count() == count

    (staged,) = layer.load_layer(source)
    assert staged["stage_token"] not in {
        e["stage_token"] for e in layer.load_layer(copied_id)
    }

    # A source over the cap (staged before it was lowered) is refused as full.
    layer.stage(source, [{**staged, "entity_id": "node-2"}])
    count = manager.store.session_count()
    succeeded, result = copy_as(source, "copier")
    assert not succeeded
    refusal = result["detail"] if transport == "rest" else result
    assert (refusal["error"], refusal["limit"]) == ("layer_full", 1)
    assert manager.store.session_count() == count


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


@pytest.mark.parametrize("copy", [False, True])
def test_session_capacity_refusals_reach_rest_and_mcp(test_app, copy):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    manager._max_sessions = 1
    if copy:
        response = test_app.post(f"/api/sessions/{source.id}/copy")
        result = test_app.app.state.tools_map["copy_visualization_session"](source.id)
    else:
        response = test_app.post("/api/sessions", json={})
        result = test_app.app.state.tools_map["create_visualization_session"]()
    assert response.status_code == 503
    assert result["success"] is False
    assert result["error"] == "too_many_sessions"
    assert manager.store.session_count() == 1


@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "copy",
        "rename",
        "stream",
        "mcp_create",
        "mcp_copy",
        "mcp_rename",
        "mcp_describe",
    ],
)
@pytest.mark.parametrize("failure_point", ["_replace_locked", "load"])
def test_creation_storage_failure_is_safe(
    test_app, monkeypatch, operation, failure_point
):
    manager = test_app.app.state.session_manager
    source = manager.create_session("source")
    manager._max_sessions = 2
    backend = manager.store._backend

    def fail(*args, **kwargs):
        raise OSError("private storage detail")

    with monkeypatch.context() as patch:
        patch.setattr(backend, failure_point, fail)
        if operation == "create":
            response = test_app.post("/api/sessions", json={})
        elif operation == "copy":
            response = test_app.post(f"/api/sessions/{source.id}/copy")
        elif operation == "rename":
            response = test_app.patch("/api/sessions/1111-2222", json={"name": "name"})
        elif operation == "stream":
            response = test_app.get("/api/sessions/1111-2222/stream?client_id=c")
        else:
            tools = test_app.app.state.tools_map
            result = {
                "mcp_create": lambda: tools["create_visualization_session"](),
                "mcp_copy": lambda: tools["copy_visualization_session"](source.id),
                "mcp_rename": lambda: tools["rename_visualization_session"](
                    "1111-2222", "name"
                ),
                "mcp_describe": lambda: tools["describe_visualization_session"](
                    "1111-2222", "description"
                ),
            }[operation]()
        if operation.startswith("mcp_"):
            assert result == {"success": False, "error": "storage_error"}
        else:
            assert response.status_code == 503
            assert response.json()["detail"] == {"error": "storage_error"}
    assert manager.store.session_count() == 1
    assert manager.create_session()


@pytest.mark.parametrize("operation", ["rename", "mcp_rename", "mcp_describe"])
def test_update_storage_failure_is_storage_error(test_app, monkeypatch, operation):
    """A failed op write on an existing session is ``storage_error`` and keeps
    the session."""
    manager = test_app.app.state.session_manager
    sid = "1111-2222"
    manager.get_or_create(sid)

    def fail(*args, **kwargs):
        raise OSError("private storage detail")

    with monkeypatch.context() as patch:
        patch.setattr(manager.store._backend, "_replace_locked", fail)
        if operation == "rename":
            response = test_app.patch(f"/api/sessions/{sid}", json={"name": "name"})
            assert response.status_code == 503
            assert response.json()["detail"] == {"error": "storage_error"}
        else:
            tool = test_app.app.state.tools_map[
                operation.replace("mcp_", "") + "_visualization_session"
            ]
            assert tool(sid, "value") == {"success": False, "error": "storage_error"}
    assert manager.store.get(sid) is not None
    assert manager.store.session_count() == 1


def save_view(test_app, session_id, body, transport):
    """POST .../save-view, or the MCP tool through FastMCP's argument layer,
    with the same raw arguments."""
    import asyncio

    if transport == "rest":
        response = test_app.post(f"/api/sessions/{session_id}/save-view", json=body)
        return response.status_code, response.json()
    response = asyncio.run(
        test_app.app.state.mcp.call_tool(
            "save_session_view", {"session_id": session_id, **body}
        )
    )
    result = response.structured_content["result"]
    return 200 if result["success"] else None, result


def test_save_view_captures_presentation_as_a_new_session(test_app):
    """Presentation, hidden refs and description are copied into a new session
    at its own revision; transient state is not; missing refs are kept and
    reported; both surfaces give the same result for the same input."""
    from backend.config.config_loader import build_session_url

    manager = test_app.app.state.session_manager
    source = manager.create_session("Design")
    source.description = "Working canvas"
    presentation = dict(
        node_refs=["node-1", "gone"],
        positions={"node-1": {"x": 20, "y": 30}},
        hidden_node_ids=["gone"],
        hidden_edge_ids=["edge-1"],
        dimmed_node_ids=["node-1"],
        dimmed_edge_ids=["edge-2"],
        edge_intensity=0.4,
        shape_overrides={"node-1": {"shape": "hexagon"}},
        type_shapes={"Actor": {"shape": "diamond"}},
        shape_revisions={"node:node-1": 2},
    )
    source.state.update(presentation, shape_operation_ids={'["op-1"]': True})
    source.seq = 7
    source.activity_log = [{"id": "history"}]
    manager.presence.join(source.id, "other", "Participant")
    before = copy.deepcopy(source.to_dict())
    body = {"name": "n" * 200, "expected_revision": 7}
    results = {t: save_view(test_app, source.id, body, t) for t in ("rest", "mcp")}
    ids = set()
    for status, result in results.values():
        assert status == 200
        saved = manager.get_session(result.pop("session_id"))
        ids.add(saved.id)
        assert result.pop("session_url") == build_session_url(saved.id)
        assert result == {
            "success": True,
            "kind": "saved_session",
            "source_session_id": source.id,
            "source_revision": 7,
            "revision": 0,
            "missing_node_ids": ["gone"],
        }
        assert (saved.name, saved.description) == ("n" * 200, "Working canvas")
        assert saved.seq == 0 and saved.activity_log == []
        assert manager.roster(saved.id) == []
        assert saved.write_mode == "direct"
        assert {k: saved.state[k] for k in presentation} == presentation
        assert saved.state["shape_operation_ids"] == {}
    assert len(ids | {source.id}) == 3
    assert source.to_dict() == before

    empty = manager.create_session()
    status, result = save_view(
        test_app, empty.id, {"name": "Empty", "expected_revision": 0}, "mcp"
    )
    assert status == 200 and result["missing_node_ids"] == []


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_copies_annotations_and_groups(test_app, transport):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state.update(
        node_refs=["node-1"],
        annotations=[
            {
                "id": "note",
                "type": "note",
                "version": 5,
                "field_versions": {"text": 5},
                "updated_at": "2000-01-01T00:00:00+00:00",
                "parentId": "group-a",
            },
            {"id": "group-a", "type": "group", "member_node_ids": ["node-1", "note"]},
            {
                "id": "line",
                "type": "line",
                "startAnchor": "note",
                "attachment": {"target_type": "annotation", "target_id": "group-a"},
                "start": {"target_id": "note"},
                "endEndpoint": {"attachment": {"targetId": "group-a"}},
            },
        ],
        shape_overrides={"group-a": {"shape": "box"}},
    )
    before = copy.deepcopy(source.to_dict())
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status == 200 and result["missing_node_ids"] == []
    saved = manager.get_session(result["session_id"])
    note, group, line = saved.state["annotations"]
    assert {note["id"], group["id"], line["id"]}.isdisjoint({"note", "group-a", "line"})
    assert note["parentId"] == group["id"]
    assert note["updated_at"] != "2000-01-01T00:00:00+00:00"
    assert group["member_node_ids"] == ["node-1", note["id"]]
    assert line["startAnchor"] == note["id"]
    assert line["attachment"]["target_id"] == group["id"]
    assert line["start"]["target_id"] == note["id"]
    assert line["endEndpoint"]["attachment"]["targetId"] == group["id"]
    assert saved.state["shape_overrides"] == {group["id"]: {"shape": "box"}}
    assert all(
        a["version"] == 1 and a["field_versions"] == {} for a in (note, group, line)
    )
    assert saved.seq == 0 and source.to_dict() == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_reads_graph_ids_only_from_explicit_link_fields(test_app, transport):
    """An id is an annotation wherever the source stores one under it, even
    in a node-typed attachment; free-form payload names nothing, and legacy
    group- canvas keys name no node."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    free_form = {"target_id": "free", "member_node_ids": ["free"], "parentId": "node-1"}
    source.state.update(
        node_refs=["node-1"],
        positions={"node-1": {"x": 1, "y": 2}, "group-1": {"x": 0, "y": 0}},
        shape_overrides={"node-1": {"shape": "box"}, "group-1": {"shape": "box"}},
        annotations=[
            {"id": "node-1", "type": "note", "data": free_form},
            {"id": "g", "type": "group", "member_node_ids": ["node-1"]},
            {
                "id": "l",
                "type": "line",
                "start": {"attachment": {"target_type": "node", "target_id": "node-1"}},
                "parentId": "group-1",
            },
            {
                "id": "r",
                "kind": "reference",
                "target_kind": "resource",
                "target": "gone",
            },
        ],
    )
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status == 200 and result["missing_node_ids"] == ["gone"]
    state = manager.get_session(result["session_id"]).state
    note, group, line, reference = state["annotations"]
    assert state["node_refs"] == ["node-1"] and note["data"] == free_form
    assert state["positions"].keys() == {"node-1", "group-1"}
    assert state["shape_overrides"].keys() == {note["id"], "group-1"}
    assert group["member_node_ids"] == [note["id"]]
    assert line["start"]["attachment"]["target_id"] == note["id"]
    assert reference["target"] == "gone"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_enforces_annotation_limit(test_app, transport):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state["annotations"] = [{"id": "a", "type": "note"}]
    before = copy.deepcopy(source.to_dict())
    manager.store._max_annotations = 0
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status in (413, None)
    assert result == {"success": False, "error": "session_too_large"}
    assert manager.store.session_count() == 1 and source.to_dict() == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "annotation",
    [
        {"id": "a", "type": "image", "image": {"url": "https://example.org/image.png"}},
        {
            "id": "a",
            "type": "reference",
            "target_kind": "url",
            "target": "javascript:alert(1)",
        },
        {"id": "a", "type": "unknown"},
        {"id": "a", "type": "line", "start": {"target_id": 1.5}},
        {"id": "a", "type": "line", "start": {"target_id": 2**53}},
        {"id": "a", "type": "line", "start": {"target_id": -(10**21)}},
    ],
)
def test_save_view_rejects_unsafe_annotations(test_app, transport, annotation):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state["annotations"] = [annotation]
    before = copy.deepcopy(source.to_dict())
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status in (422, None)
    assert result == {"success": False, "error": "invalid_presentation"}
    assert manager.store.session_count() == 1 and source.to_dict() == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "annotation",
    [
        {"id": "a", "type": "reference", "target_kind": "resource", "target": "node-1"},
        {"id": "a", "kind": "reference", "target_kind": "resource", "target": "node-1"},
        {"id": "a", "type": "group", "member_node_ids": ["node-1"]},
        {
            "id": "a",
            "type": "line",
            "start": {"attachment": {"target_type": "node", "target_id": "node-1"}},
        },
        {"id": "a", "type": "line", "start": {"target_id": "node-1"}},
        {"id": "a", "type": "line", "endEndpoint": {"attachment": {"nodeId": 7}}},
        {"id": "a", "type": "text", "anchor": {"targetId": "node-1"}},
    ],
)
def test_save_view_denies_inaccessible_annotation_targets(
    test_app, transport, annotation
):
    from backend.runtime.authorization import GraphAccessNarrowing

    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state.update(annotations=[annotation])
    before = copy.deepcopy(source.to_dict())
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=True,
        graph_access=GraphAccessNarrowing(enabled=True, allow_local_graph=False)
        if ctx.target == "save_session_view"
        else GraphAccessNarrowing(),
    )
    test_app.app.state.graph_service._authorization_hook = hook
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status in (403, None)
    assert result == {"success": False, "error": "access_denied"}
    assert manager.store.session_count() == 1 and source.to_dict() == before
    assert not hook.assign_copied_session_access.called


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_node_typed_link_to_an_annotation_is_not_a_graph_ref(
    test_app, transport
):
    from backend.runtime.authorization import GraphAccessNarrowing

    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state.update(
        annotations=[
            {"id": "a", "type": "note"},
            {
                "id": "l",
                "type": "line",
                "start": {"attachment": {"target_type": "node", "target_id": "a"}},
            },
        ]
    )
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=True,
        graph_access=GraphAccessNarrowing(enabled=True, allow_local_graph=False)
        if ctx.target == "save_session_view"
        else GraphAccessNarrowing(),
    )
    test_app.app.state.graph_service._authorization_hook = hook
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status == 200 and result["missing_node_ids"] == []
    note, line = manager.get_session(result["session_id"]).state["annotations"]
    assert line["start"]["attachment"]["target_id"] == note["id"] != "a"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_drops_off_canvas_group_members_and_does_not_report_them(
    test_app, transport
):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state.update(
        node_refs=["node-1"],
        annotations=[
            {
                "id": "g",
                "type": "group",
                "member_node_ids": ["node-1", "node-2", "gone"],
            }
        ],
    )
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status == 200 and result["missing_node_ids"] == []
    (group,) = manager.get_session(result["session_id"]).state["annotations"]
    assert group["member_node_ids"] == ["node-1"]


def test_save_view_mcp_arguments_reach_the_tool_raw_as_rest_does(test_app):
    """FastMCP neither decodes nor type-checks the arguments: a JSON-looking
    name is saved verbatim, as REST saves it, and a non-string session id is
    `invalid_request`, not a ToolError. The schema still requires all three."""
    import asyncio

    (tool,) = [
        t
        for t in asyncio.run(test_app.app.state.mcp.list_tools())
        if t.name == "save_session_view"
    ]
    assert set(tool.input_schema["required"]) == {
        "session_id",
        "name",
        "expected_revision",
    }
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    for name in ("null", "[1]", '{"a": 1}'):
        body = {"name": name, "expected_revision": 0}
        results = [save_view(test_app, source.id, body, t) for t in ("rest", "mcp")]
        saved = [manager.get_session(r["session_id"]) for _, r in results]
        assert [s.name for s in saved] == [name, name]
    _, result = save_view(
        test_app, 123, {"name": "View", "expected_revision": 0}, "mcp"
    )
    assert (result["success"], result["error"]) == (False, "invalid_request")


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "body",
    [
        {"name": "n" * 201, "expected_revision": 0},
        {"name": " ", "expected_revision": 0},
        {"name": 5, "expected_revision": 0},
        {"name": None, "expected_revision": 0},
        {"expected_revision": 0},
        {"name": "View", "expected_revision": "0"},
        {"name": "View", "expected_revision": -1},
        {"name": "View", "expected_revision": True},
        {"name": "View"},
    ],
)
def test_save_view_rejects_invalid_raw_arguments(test_app, transport, body):
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    status, result = save_view(test_app, source.id, body, transport)
    assert status in (400, None)
    assert result == {"success": False, "error": "invalid_request"}
    assert manager.store.session_count() == 1


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_save_view_refuses_stale_or_staged_sources(test_app, monkeypatch, transport):
    """A stale revision, staged changes, an oversized capture, a
    full session store or a failing staged-layer store publish nothing; the
    current revision is withheld when the source read is denied."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.seq = 3
    status_of = {"rest": lambda s: s, "mcp": lambda s: None}[transport]

    def attempt(revision=3):
        return save_view(
            test_app, source.id, {"name": "V", "expected_revision": revision}, transport
        )

    assert attempt(2) == (
        status_of(409),
        {"success": False, "error": "revision_conflict", "current_revision": 3},
    )
    hook = Mock()
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=ctx.session_id != source.id
    )
    test_app.app.state.graph_service._authorization_hook = hook
    assert attempt(2) == (
        status_of(404),
        {"success": False, "error": "session_not_found"},
    )
    hook.evaluate.side_effect = None
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=True)

    writes = manager.write_modes
    monkeypatch.setattr(writes, "choose", lambda mode: ("staged", "explicit"))
    monkeypatch.setattr(writes, "_store", Mock(load_layer=lambda sid: [{}]))
    assert attempt() == (status_of(409), {"success": False, "error": "staged_changes"})
    driver_error = type("OperationalError", (Exception,), {})
    for error in (LayerStoreError, driver_error):
        writes._store.load_layer = Mock(side_effect=error("db at /srv/x"))
        assert attempt() == (
            status_of(503),
            {"success": False, "error": "storage_error"},
        )
    monkeypatch.undo()
    manager._max_sessions = 1
    assert attempt() == (
        status_of(503),
        {"success": False, "error": "too_many_sessions"},
    )
    manager._max_sessions = 10
    source.description = "x" * 2000
    source.state["node_refs"] = ["node-1"]
    hook.evaluate.side_effect = lambda ctx: GraphAuthorizationDecision(
        allowed=ctx.target != "save_session_view"
    )
    monkeypatch.setattr(manager_module, "DEFAULT_MAX_SESSION_DOCUMENT_BYTES", 2048)
    assert attempt() == (
        status_of(413),
        {"success": False, "error": "session_too_large"},
    )
    assert manager.store.session_count() == 1
    assert not hook.assign_copied_session_access.called


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "refusal, refs",
    [
        ("create", {"node_refs": ["node-1"]}),
        ("narrowed", {"node_refs": ["node-1"]}),
        (
            "narrowed",
            {
                "positions": {"node-1": {"x": 4, "y": 8}},
                "annotations": [{"id": "node-1", "type": "note"}],
            },
        ),
        ("narrowed", {"node_refs": ["gone"]}),
        ("narrowed", {"hidden_edge_ids": ["edge-1"]}),
        ("narrowed", {"dimmed_edge_ids": ["edge-1"]}),
        ("removed", {"shape_revisions": {"node:node-1": 1, "type:Actor": 2}}),
        ("hook_raises", {}),
    ],
)
def test_save_view_denial_publishes_nothing_and_discloses_nothing(
    test_app, transport, refusal, refs
):
    """A denied destination create, a node or edge ref the caller cannot read
    (or, under narrowing, cannot be shown missing), or a raising hook is one
    generic access_denied. A node only left in shape_revisions is off the
    canvas: neither checked, reported missing nor copied."""
    from backend.runtime.authorization import GraphAccessNarrowing

    manager = test_app.app.state.session_manager
    source = manager.create_session()
    source.state.update(refs)
    narrowed = GraphAccessNarrowing(enabled=True, allow_local_graph=False)
    hook = Mock()

    def evaluate(ctx):
        if refusal == "hook_raises":
            raise RuntimeError("hook down at /srv/secret")
        return GraphAuthorizationDecision(
            allowed=not (refusal == "create" and ctx.action == "mutate"),
            reason="node-1 is secret",
            graph_access=narrowed
            if refusal in ("narrowed", "removed") and ctx.target == "save_session_view"
            else GraphAccessNarrowing(),
        )

    hook.evaluate.side_effect = evaluate
    test_app.app.state.graph_service._authorization_hook = hook
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    if refusal == "removed":
        assert status == 200 and result["missing_node_ids"] == []
        saved = manager.get_session(result["session_id"])
        assert saved.state["shape_revisions"] == {"type:Actor": 2}
        return
    assert status in (403, None)
    assert result == {"success": False, "error": "access_denied"}
    assert manager.store.session_count() == 1
    assert not hook.assign_copied_session_access.called


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("failure", [None, ConnectionError, ValueError])
def test_save_view_grants_after_publishing_and_revokes_on_failure(
    test_app, transport, failure
):
    """Access is assigned to the published copy; a refused or raising
    assignment revokes the grants made for it (a raising revoke does not
    replace the denial) and removes the copy before failing."""
    manager = test_app.app.state.session_manager
    source = manager.create_session()
    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=True)
    published = []

    def assign(context, *, copy_session_id):
        published.append(manager.store._backend.load(copy_session_id) is not None)
        if failure:
            raise failure()
        return GraphAuthorizationDecision(allowed=False)

    hook.assign_copied_session_access.side_effect = assign
    if failure is ValueError:
        hook.revoke_copied_session_access.side_effect = RuntimeError()
    test_app.app.state.graph_service._authorization_hook = hook
    status, result = save_view(
        test_app, source.id, {"name": "V", "expected_revision": 0}, transport
    )
    assert status in (403, None)
    assert result == {"success": False, "error": "access_denied"}
    assert published == [True]
    copy_id = hook.assign_copied_session_access.call_args.kwargs["copy_session_id"]
    (context,), kwargs = hook.revoke_copied_session_access.call_args
    assert (context.session_id, kwargs) == (
        source.id,
        {"copy_session_id": copy_id},
    )
    assert manager.get_session(copy_id) is None
    assert manager.store.session_count() == 1
