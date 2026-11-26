# covers: backend/service/templates.py, backend/core/session_manager.py
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core import Edge, GraphStorage, Node
from backend.core.session_manager import (
    CaptureRefused,
    SessionManager,
    SessionNotFound,
)
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service.service import GraphService
from backend.service.templates import capture_template


@pytest.fixture
def capture(tmp_path):
    storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    session = manager.create_session("source")
    a = Node(
        id="a", type="Actor", name="A", metadata={"color": "red", "private": "secret"}
    )
    b = Node(id="b", type="Actor", name="B")
    storage.nodes.update(a=a, b=b)
    storage.graph.add_edge(
        "a", "b", data=Edge(id="e", source="a", target="b", type="LINK")
    )
    session.state.update(
        node_refs=["a", "b", "missing", "group-old"],
        positions={"a": {"x": 3, "y": 4}},
        annotations=[{"id": "note", "type": "label", "attachment": {"target_id": "a"}}],
    )

    def run(**kwargs):
        return capture_template(
            service,
            manager,
            session.id,
            template_id="1234-5678",
            name="Example",
            created_by="author",
            expected_revision=session.seq,
            **kwargs,
        )

    return run, service, manager, session


def test_selection_edges_metadata_and_annotation_links(capture):
    run, _, _, _ = capture
    doc = run()["document"]
    assert [n["source_node_id"] for n in doc["nodes"]] == ["a", "b"]
    assert doc["nodes"][0]["snapshot"]["metadata"] == {"color": "red"}
    assert len(doc["edges"]) == 1
    selected = run(node_ids=["a"], annotation_ids=["note"])["document"]
    assert len(selected["nodes"]) == 1
    assert selected["edges"] == []
    assert (
        selected["annotation_document"]["annotations"][0]["attachment"]["target_id"]
        == selected["nodes"][0]["key"]
    )


@pytest.mark.parametrize("denied_id", ["node-0", "group-denied"])
def test_denials_and_missing_references(capture, denied_id):
    run, service, _, session = capture
    with patch(
        "backend.service.templates.access.authorize_graph_access", return_value=True
    ):
        assert run() == {"success": False, "error": "session_not_found"}
    for hook in ("authorize_graph_access", "evaluate_graph_access"):
        with patch(f"backend.service.templates.access.{hook}", side_effect=ValueError):
            assert run() == {"success": False, "error": "access_denied"}
    with patch("backend.service.templates.access.is_node_visible", return_value=False):
        assert run() == {"success": False, "error": "access_denied"}
    session.state["annotations"][0]["attachment"]["target_id"] = "missing"
    assert run()["success"]
    service._storage.nodes[denied_id] = Node(id=denied_id, type="Actor", name="Denied")
    session.state["node_refs"].append(denied_id)
    with patch(
        "backend.service.templates.access.is_node_visible",
        side_effect=lambda node, _: node.id != denied_id,
    ):
        if not denied_id.startswith("group-"):
            assert run(node_ids=[denied_id]) == {
                "success": False,
                "error": "access_denied",
            }
        session.state["annotations"][0]["content"] = {
            "attachment": {"target_id": denied_id, "target_type": "node"}
        }
        assert run(node_ids=["a"], annotation_ids=["note"]) == {
            "success": False,
            "error": "access_denied",
        }
        session.state["annotations"][0] = {
            "id": "note",
            "type": "reference",
            "target_kind": "resource",
            "content": {"target": denied_id},
        }
        assert run(node_ids=["a"], annotation_ids=["note"]) == {
            "success": False,
            "error": "access_denied",
        }
    assert run(node_ids=["missing", "group-old"])["document"]["nodes"] == []


def test_explicit_node_outside_session_is_not_captured(capture):
    run, service, _, _ = capture
    service._storage.nodes["outside"] = Node(id="outside", type="Actor", name="O")
    doc = run(node_ids=["a", "outside"])["document"]
    assert [n["source_node_id"] for n in doc["nodes"]] == ["a"]


@pytest.mark.parametrize("narrowed", [False, True])
def test_missing_ref_denied_only_under_narrowing(capture, narrowed):
    """A missing node ref is skipped, but denies the capture under narrowing."""
    run, service, _, _ = capture

    class Hook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True, graph_access=GraphAccessNarrowing(enabled=narrowed)
            )

    service._authorization_hook = Hook()
    result = run(node_ids=["a", "missing"])
    if narrowed:
        assert result == {"success": False, "error": "access_denied"}
    else:
        assert [n["source_node_id"] for n in result["document"]["nodes"]] == ["a"]


@pytest.mark.parametrize(
    "geometry, bounds",
    [
        ({"x": 9, "y": 8}, {"x": 0, "y": 0, "width": 9, "height": 8}),
        ({"w": 5, "h": 6}, {"x": 0, "y": 0, "width": 3, "height": 4}),
    ],
)
def test_bounds_use_geometry_when_position_is_none(capture, geometry, bounds):
    """Geometry stands in for a missing position; an absent coordinate is 0."""
    run, _, _, session = capture
    session.state["annotations"] = [
        {"id": "box", "type": "label", "position": None, "geometry": geometry}
    ]
    assert run()["document"]["bounds"] == bounds


@pytest.mark.parametrize("field", ["attachment", "member_node_ids"])
def test_annotation_id_in_node_only_field_is_dropped(capture, field):
    """An annotation id where the template allows only node keys is dropped."""
    run, _, _, session = capture
    link = (
        {"target_id": "note", "target_type": "node"}
        if field == "attachment"
        else ["note", "a"]
    )
    session.state["annotations"].append({"id": "box", "type": "group", field: link})
    doc = run()["document"]
    box = doc["annotation_document"]["annotations"][1]
    if field == "attachment":
        assert "attachment" not in box
    else:
        assert box["member_node_ids"] == [doc["nodes"][0]["key"]]


@pytest.mark.parametrize("target", ["b", "missing"])
@pytest.mark.parametrize("nested", [False, True])
def test_uncaptured_resource_reference_is_omitted(capture, target, nested):
    run, _, _, session = capture
    reference = {"id": "ref", "type": "reference"}
    payload = {"target_kind": "resource", "target": target}
    reference.update({"content": payload} if nested else payload)
    session.state["annotations"] = [
        reference,
        {"id": "note", "type": "label", "parentId": "ref"},
    ]
    doc = run(node_ids=["a"], annotation_ids=["ref", "note"])["document"]
    assert doc["annotation_document"]["annotations"] == [
        {"id": "note", "type": "label"}
    ]
    if target == "b":
        doc = run(node_ids=["b"], annotation_ids=["ref"])["document"]
        captured = doc["annotation_document"]["annotations"][0]
        assert (captured["content"] if nested else captured)["target"] == (
            doc["nodes"][0]["key"]
        )


@pytest.mark.parametrize("endpoint", ["start", "endEndpoint"])
def test_dropped_line_endpoint_attachment_is_removed(capture, endpoint):
    run, _, _, session = capture
    point = {"x": 1, "y": 2}
    session.state["annotations"] = [
        {
            "id": "line",
            "type": "line",
            endpoint: {
                "point": point,
                "attachment": {"target_id": "b", "target_type": "node"},
            },
        }
    ]
    doc = run(node_ids=["a"], annotation_ids=["line"])["document"]
    assert doc["annotation_document"]["annotations"][0][endpoint] == {"point": point}


@pytest.mark.parametrize("alias", ["target_id", "targetId", "node_id", "nodeId"])
@pytest.mark.parametrize("field", ["anchor", "attachment"])
@pytest.mark.parametrize("nested", [False, True])
def test_annotation_link_aliases(capture, alias, field, nested):
    run, _, _, session = capture
    annotation = session.state["annotations"][0]
    annotation.pop("attachment")
    payload = annotation.setdefault("content", {}) if nested else annotation
    payload[field] = {alias: "a"}
    if field == "attachment":
        payload[field]["target_id"] = "a"
    doc = run(node_ids=["a"], annotation_ids=["note"])["document"]
    captured = doc["annotation_document"]["annotations"][0]
    payload = captured["content"] if nested else captured
    assert payload[field][alias] == doc["nodes"][0]["key"]


def test_staged_source_refused(capture):
    run, _, manager, _ = capture
    with patch.object(
        manager, "capture_source", side_effect=CaptureRefused("staged_changes")
    ):
        assert run() == {"success": False, "error": "staged_changes"}


def test_shape_selection_and_tombstone(capture):
    run, _, manager, session = capture
    definition = {
        "version": 1,
        "base": "rectangle",
        "scale": 1.0,
        "placement": "top-left",
        "areas": {"first": "name", "second": None, "third": None},
    }
    session.state["type_shapes"] = {"Actor": {"ref": "saved:card"}}
    session.state["shape_overrides"] = {"b": {"inline": definition}}
    manager.shape_library = SimpleNamespace(
        snapshot=lambda: {"entries": {"card": {"definition": definition}}}
    )
    nodes = run()["document"]["nodes"]
    assert nodes[0]["shape"] == {
        "ref": "saved:card",
        "inline": definition,
        "builtin": False,
    }
    assert nodes[1]["shape"] == {"ref": None, "inline": definition, "builtin": False}
    manager.shape_library = SimpleNamespace(snapshot=lambda: {"entries": {}})
    assert run()["document"]["nodes"][0]["shape"] == {
        "ref": None,
        "inline": None,
        "builtin": True,
    }


@pytest.mark.parametrize(
    "field, limit, code",
    [
        ("node_refs", 501, "nodes_limit"),
        ("annotations", 2001, "annotations_limit"),
        ("references", 2001, "annotations_limit"),
    ],
)
def test_limits_fail_without_truncation(capture, field, limit, code):
    """A limit counts the selection, before uncaptured references are omitted."""
    run, service, _, session = capture
    if field == "node_refs":
        for i in range(limit):
            service._storage.nodes[str(i)] = Node(id=str(i), type="Actor", name="N")
        session.state[field] = [str(i) for i in range(limit)]
    elif field == "annotations":
        session.state[field] = [{"id": str(i), "type": "label"} for i in range(limit)]
    else:
        session.state["annotations"] = [
            {
                "id": str(i),
                "type": "reference",
                "target_kind": "resource",
                "target": "x",
            }
            for i in range(limit)
        ]
    assert run() == {"success": False, "error": code}
    if code == "annotations_limit":
        with patch(
            "backend.service.templates.access.is_node_visible", return_value=False
        ):
            assert run() == {"success": False, "error": "access_denied"}


def test_edge_limit_and_excluded_system_types(capture):
    run, service, _, session = capture
    service._storage.nodes["system"] = Node(
        id="system", type="SavedView", name="System"
    )
    session.state["node_refs"].append("system")
    edge = Edge(id="e", source="a", target="b", type="LINK")
    with patch.object(
        service._storage, "get_edges_between_nodes", return_value=[edge] * 2001
    ):
        assert run() == {"success": False, "error": "edges_limit"}
    assert "system" not in {n["source_node_id"] for n in run()["document"]["nodes"]}


def test_template_crud_conflicts_and_authorization(capture):
    from backend.service import templates

    _, service, manager, session = capture
    saved = templates.save_template(
        service, manager, session.id, "Example", session.seq
    )
    doc = saved["template"]
    tid = doc["template_id"]
    assert templates.list_templates(service, manager, tags=["missing"])["count"] == 0
    assert templates.get_template(service, manager, tid)["template"] == doc
    assert (
        templates.update_template(service, manager, tid, 0, name="Changed")["error"]
        == "revision_conflict"
    )
    refused = templates.update_template(
        service,
        manager,
        tid,
        doc["revision"],
        replace_from={"session_id": session.id, "expected_revision": session.seq + 1},
    )
    assert (
        refused["error"] == "revision_conflict" and refused["field"] == "replace_from"
    )
    assert templates.get_template(service, manager, tid)["template"] == doc
    updated = templates.update_template(
        service,
        manager,
        tid,
        doc["revision"],
        tags=["x"],
        replace_from={"session_id": session.id, "expected_revision": session.seq},
    )["template"]
    assert templates.list_templates(service, manager, tags=["x"])["count"] == 1
    with patch(
        "backend.service.templates.access.authorize_graph_access",
        return_value={"error_code": "access_denied"},
    ) as auth:
        assert (
            templates.delete_template(service, manager, tid, updated["revision"])[
                "error"
            ]
            == "access_denied"
        )
        assert auth.call_args.kwargs["target"] == "delete_visualization_session"
        assert auth.call_args.kwargs["session_id"] == tid
    assert templates.delete_template(service, manager, tid, doc["revision"]) == {
        "success": False,
        "error": "revision_conflict",
        "current_revision": updated["revision"],
    }
    assert templates.delete_template(service, manager, tid, updated["revision"])[
        "success"
    ]
    assert (
        templates.get_template(service, manager, tid)["error"] == "template_not_found"
    )
    assert templates.list_templates(service, manager)["count"] == 0
    from backend.core.template_store import TemplateError

    with pytest.raises(TemplateError) as exc:
        manager.template_store.save(doc, doc["revision"])
    assert exc.value.code == "deleted"


def test_list_filters_templates_the_caller_cannot_read(capture):
    from backend.service import templates

    _, service, manager, session = capture
    doc = templates.save_template(service, manager, session.id, "Example", session.seq)[
        "template"
    ]
    with patch(
        "backend.service.templates.access.authorize_graph_access",
        side_effect=lambda hook, **kw: (
            {"error": "access_denied"}
            if kw["target"] == "get_visualization_session"
            else None
        ),
    ):
        assert (
            templates.get_template(service, manager, doc["template_id"])["error"]
            == "access_denied"
        )
        assert templates.list_templates(service, manager)["templates"] == []


def test_collection_authorization_ignores_bound_session(capture):
    from backend.runtime.session_context import bind_session
    from backend.service import templates

    _, service, manager, session = capture
    seen = []

    class Hook:
        def evaluate(self, context):
            seen.append((context.target, context.session_id))
            return GraphAuthorizationDecision(
                allowed=not (
                    context.target
                    in {"create_visualization_session", "list_visualization_sessions"}
                    and context.session_id
                )
            )

    service._authorization_hook = Hook()
    with bind_session("9999-9999"):
        saved = templates.save_template(
            service, manager, session.id, "Example", session.seq
        )
        assert saved["success"]
        assert templates.list_templates(service, manager)["count"] == 1
    assert ("create_visualization_session", "") in seen
    assert ("list_visualization_sessions", "") in seen
    assert ("get_visualization_session", saved["template"]["template_id"]) in seen


@pytest.mark.parametrize("allowed", [True, False])
def test_save_assigns_creator_access_to_a_fresh_non_session_id(capture, allowed):
    from backend.service import templates

    _, service, manager, session = capture
    assigned = []

    class Hook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(allowed=True)

        def assign_copied_session_access(self, context, *, copy_session_id):
            assigned.append((context.target, context.session_id, copy_session_id))
            return GraphAuthorizationDecision(allowed=allowed)

    service._authorization_hook = Hook()
    # The first candidate is the session's own id and must be skipped.
    parts = iter([int(p) for p in session.id.split("-")] + [1111, 2222, 3333, 4444])
    with patch("secrets.randbelow", side_effect=lambda _: next(parts)):
        saved = templates.save_template(
            service, manager, session.id, "Example", session.seq
        )
    assert assigned == [("save_template", session.id, "1111-2222-3333-4444")]
    assert saved["success"] is allowed
    if not allowed:
        assert saved["error"] == "access_denied"
    assert templates.list_templates(service, manager)["count"] == int(allowed)
    if allowed:
        # Nor does a session id ever name a template, deleted or not.
        tid = saved["template"]["template_id"]
        templates.delete_template(service, manager, tid, 1)
        with pytest.raises(SessionNotFound):
            manager.get_or_create(tid)
        parts = iter([1111, 2222, 3333, 4444, 5555, 6666, 7777, 8888])
        with patch("secrets.randbelow", side_effect=lambda _: next(parts)):
            assert manager.create_session("new").id == "5555-6666-7777-8888"
        assert not manager.store.exists(tid)


def test_update_losing_the_store_cas_reports_current_revision(capture):
    from backend.service import templates

    _, service, manager, session = capture
    doc = templates.save_template(service, manager, session.id, "Example", session.seq)[
        "template"
    ]
    store = manager.template_store
    real_save = store.save

    def concurrent_save(document, expected_revision):
        real_save(dict(doc), doc["revision"])
        return real_save(document, expected_revision)

    with patch.object(store, "save", side_effect=concurrent_save):
        result = templates.update_template(
            service, manager, doc["template_id"], doc["revision"], name="New"
        )
    assert result == {
        "success": False,
        "error": "revision_conflict",
        "current_revision": doc["revision"] + 1,
    }


def test_failed_creator_assignment_leaves_no_grant(capture):
    from backend.service import templates

    _, service, manager, session = capture
    grants = set()

    class Hook:
        def evaluate(self, context):
            return GraphAuthorizationDecision(allowed=True)

        def assign_copied_session_access(self, context, *, copy_session_id):
            grants.add(copy_session_id)
            raise RuntimeError("assignment backend down")

        def revoke_copied_session_access(self, context, *, copy_session_id):
            grants.discard(copy_session_id)

    service._authorization_hook = Hook()
    saved = templates.save_template(
        service, manager, session.id, "Example", session.seq
    )
    assert saved == {"success": False, "error": "access_denied"}
    assert grants == set()
    assert templates.list_templates(service, manager)["count"] == 0


def test_unreadable_stored_template_is_a_storage_error(capture):
    from backend.service import templates

    _, service, manager, session = capture
    doc = templates.save_template(service, manager, session.id, "Example", session.seq)[
        "template"
    ]
    record = manager.template_store._backend.load(doc["template_id"])
    record["document"]["nodes"] = "broken"
    manager.template_store._backend.save(record)
    for result in (
        templates.get_template(service, manager, doc["template_id"]),
        templates.update_template(
            service, manager, doc["template_id"], doc["revision"], name="New"
        ),
    ):
        assert result == {"success": False, "error": "storage_error"}


def test_insert_reference_is_atomic_and_preserves_existing_nodes(capture):
    from backend.service import templates

    _, service, manager, source = capture
    saved = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    target.state.update(
        node_refs=["a"],
        positions={"a": {"x": 500, "y": 20}},
        hidden_node_ids=["a"],
        shape_overrides={"a": {"builtin": True}},
    )
    before = __import__("copy").deepcopy(target.state)
    tid = saved["template_id"]
    for x, y in ((10**400, 0), (0, -(10**400))):
        assert templates.insert_template(
            service, manager, target.id, tid, x=x, y=y
        ) == {
            "success": False,
            "error": "invalid_request",
        }
    assert (
        templates.insert_template(
            service, manager, target.id, tid, expected_revision=99
        )["error"]
        == "revision_conflict"
    )
    assert (
        templates.insert_template(
            service, manager, target.id, tid, template_revision=99
        )["error"]
        == "revision_conflict"
    )
    assert (
        templates.insert_template(service, manager, target.id, tid, mode="copy")[
            "error"
        ]
        == "unsupported_mode"
    )
    assert target.state == before
    result = templates.insert_template(service, manager, target.id, tid, x=100, y=200)
    assert result["success"] and result["added"] == ["b"]
    assert result["skipped"] == [{"node_id": "a", "reason": "already_present"}]
    assert target.state["positions"]["a"] == before["positions"]["a"]
    assert target.state["hidden_node_ids"] == ["a"]
    assert target.state["shape_overrides"]["a"] == {"builtin": True}
    annotation = target.state["annotations"][0]
    assert annotation["id"] == result["annotation_id_map"]["note"] != "note"
    assert annotation["attachment"]["target_id"] == "a"
    assert target.state["positions"]["b"] == {"x": 100, "y": 200}


def test_insert_skips_missing_and_remaps_links(capture):
    from backend.service import templates

    _, service, manager, source = capture
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    del service._storage.nodes["a"]
    target = manager.create_session("target")
    result = templates.insert_template(service, manager, target.id, doc["template_id"])
    assert result["success"] and result["added"] == ["b"]
    assert result["skipped"] == [{"node_id": "a", "reason": "missing"}]
    assert "attachment" not in target.state["annotations"][0]
    assert target.state["positions"]["b"] == {"x": 0, "y": 0}


@pytest.mark.parametrize(
    "entry,ref,expected,reason",
    [
        ({"deleted": False}, "saved:card", {"ref": "saved:card"}, None),
        ({"deleted": True}, "saved:card", {"builtin": True}, "tombstoned"),
        (None, "saved:card", None, "missing_shape"),
        (None, "config:unknown", None, "missing_shape"),
        (None, None, None, None),
    ],
)
def test_insert_resolves_captured_shapes(capture, entry, ref, expected, reason):
    from backend.service import templates

    _, service, manager, source = capture
    definition = {
        "version": 1,
        "base": "rectangle",
        "scale": 1.0,
        "placement": "top-left",
        "areas": {"first": "name", "second": None, "third": None},
    }
    source.state["shape_overrides"] = {"a": {"inline": definition}}
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    doc["nodes"][0]["shape"]["ref"] = ref
    doc = manager.template_store.save(doc, doc["revision"])
    manager.shape_library = SimpleNamespace(
        backend=SimpleNamespace(
            read=lambda: {"entries": {"card": entry} if entry else {}}
        )
    )
    target = manager.create_session("target")
    result = templates.insert_template(service, manager, target.id, doc["template_id"])
    assert result["success"]
    assert target.state["shape_overrides"]["a"] == (expected or {"inline": definition})
    assert target.state["type_shapes"] == {}
    assert [item["reason"] for item in result["shapes"]] == ([reason] if reason else [])


def test_insert_rest_matches_service_and_maps_errors(capture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.service import templates
    from backend.service.rest_api import create_rest_router

    _, service, manager, source = capture
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    app = FastAPI()
    app.include_router(
        create_rest_router(service, session_manager=manager), prefix="/api"
    )
    client = TestClient(app)
    url = f"/api/sessions/{target.id}/insert-template"
    conflict = client.post(
        url, json={"template_id": doc["template_id"], "expected_revision": 1}
    )
    assert (
        conflict.status_code == 409 and conflict.json()["error"] == "revision_conflict"
    )
    result = client.post(
        url, json={"template_id": doc["template_id"], "x": 20, "y": 30}
    )
    assert result.status_code == 200 and result.json()["added"] == ["a", "b"]


def test_insert_denial_and_persistence_failure_leave_session_unchanged(capture):
    import copy
    from backend.service import templates

    _, service, manager, source = capture
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    before = copy.deepcopy(target.state)
    with patch(
        "backend.service.templates.access.authorize_graph_access", return_value=True
    ):
        assert (
            templates.insert_template(service, manager, target.id, doc["template_id"])[
                "error"
            ]
            == "access_denied"
        )
    with patch.object(manager.store, "persist", side_effect=OSError):
        assert (
            templates.insert_template(service, manager, target.id, doc["template_id"])[
                "error"
            ]
            == "storage_error"
        )
    assert target.state == before and target.seq == 0


def test_insert_remaps_groups_and_line_endpoints(capture):
    from backend.service import templates

    _, service, manager, source = capture
    source.state["annotations"] = [
        {"id": "group", "type": "group", "member_node_ids": ["a", "b"]},
        {
            "id": "line",
            "type": "line",
            "from": {"x": 1, "y": 2},
            "start": {
                "point": {"x": 1, "y": 2},
                "attachment": {"target_id": "group", "target_type": "annotation"},
            },
            "end": {
                "point": {"x": 4, "y": 5},
                "attachment": {"target_id": "b", "target_type": "node"},
            },
        },
        {
            "id": "stroke",
            "type": "freehand",
            "points": [{"x": 5, "y": 6}, {"x": 7, "y": 8}],
        },
    ]
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    result = templates.insert_template(
        service, manager, target.id, doc["template_id"], x=100, y=200
    )
    assert result["success"]
    group, line, stroke = target.state["annotations"]
    assert group["member_node_ids"] == ["a", "b"]
    assert (
        line["start"]["attachment"]["target_id"]
        == group["id"]
        == result["annotation_id_map"]["group"]
    )
    assert line["end"]["attachment"]["target_id"] == "b"
    assert line["from"] == line["start"]["point"] == {"x": 101, "y": 202}
    assert line["end"]["point"] == {"x": 104, "y": 205}
    assert stroke["points"] == [{"x": 105, "y": 206}, {"x": 107, "y": 208}]


@pytest.mark.parametrize("nested", [False, True])
def test_default_origin_is_right_of_existing_annotation(capture, nested):
    from backend.service import templates

    _, service, manager, source = capture
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    target.state["annotations"] = [
        {
            "id": "wide",
            "type": "label",
            "position": {"x": 100, "y": 0},
            "size": {"w": 600, "h": 20},
        },
        {
            "id": "line",
            "type": "line",
            "from": {"x": 10, "y": 0},
            "end": {"point": {"x": 1500, "y": 0}},
        },
        {
            "id": "stroke",
            "type": "freehand",
            **(
                {"content": {"points": [{"x": 2500, "y": 0}]}}
                if nested
                else {"points": [{"x": 2500, "y": 0}]}
            ),
        },
    ]
    result = templates.insert_template(service, manager, target.id, doc["template_id"])
    assert result["success"]
    assert target.state["positions"]["b"]["x"] == 2700


def test_insert_uses_rendered_bounds_and_moves_implicit_annotations(capture):
    from backend.service import templates

    _, service, manager, source = capture
    source.state["annotations"] = [
        {"id": "implicit", "type": "label"},
        {"id": "stroke", "type": "freehand", "points": [{"x": -50, "y": -30}]},
    ]
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    target.state.update(
        node_refs=["a"],
        positions={},
        annotations=[
            {"id": "existing", "type": "reference", "position": {"x": 400, "y": 0}}
        ],
    )
    result = templates.insert_template(service, manager, target.id, doc["template_id"])
    assert result["success"]
    # The reference ends at 620; the source stroke starts at -50.
    assert target.state["annotations"][1]["position"] == {"x": 870, "y": 30}
    assert target.state["annotations"][2]["points"] == [{"x": 820, "y": 0}]
    assert target.state["positions"]["b"] == {"x": 870, "y": 30}


def test_insert_default_origin_accounts_for_unpositioned_node(capture):
    from backend.service import templates

    _, service, manager, source = capture
    source.state["annotations"] = []
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    target.state.update(node_refs=["a"], positions={})
    assert templates.insert_template(service, manager, target.id, doc["template_id"])[
        "success"
    ]
    assert target.state["positions"]["b"] == {"x": 400, "y": 0}


def test_insert_rejects_overflow_without_writing(capture):
    from backend.service import templates
    import copy

    _, service, manager, source = capture
    source.state["annotations"] = [
        {"id": "far", "type": "label", "position": {"x": -1e308, "y": 0}}
    ]
    doc = templates.save_template(service, manager, source.id, "Example", source.seq)[
        "template"
    ]
    target = manager.create_session("target")
    before = copy.deepcopy(target.state)
    result = templates.insert_template(
        service, manager, target.id, doc["template_id"], x=1e308, y=0
    )
    assert result == {"success": False, "error": "invalid_request"}
    assert target.state == before and target.seq == 0
