# covers: backend/service/templates.py
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core import Edge, GraphStorage, Node
from backend.core.session_manager import CaptureRefused, SessionManager
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
