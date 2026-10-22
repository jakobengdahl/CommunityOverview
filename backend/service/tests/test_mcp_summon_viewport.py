"""
Tests for the ``summon_session_viewport`` MCP tool: it asks the hook as a
session mutation, validates its target (finite bounds, or node ids on the
session's canvas that the caller may read) and publishes nothing on refusal.
The broadcast semantics themselves are covered in
``backend/core/tests/test_session_summon.py``.
"""

import math
import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, Node
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, register_mcp_tools
from backend.service.mcp_tools import _ASSUMED_NODE_SIZE, _INVALID_SESSION_ID_ERROR
from backend.service.tests.test_authorization import FixedNarrowingHook
from backend.service.tests.test_mcp_auto_add_tools import DenyAllRecordingHook

BOUNDS = {"x": 0, "y": 0, "width": 400, "height": 300}


def _tools(tmp_path, hook=None):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage, authorization_hook=hook)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager, storage


def _listen(manager, session_id):
    sub, _ = manager.connect(session_id, "viewer", "Viewer")
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


def _canvas(manager, storage, placed):
    """A session whose canvas holds *placed* ``{id: (x, y)}`` as graph nodes."""
    for node_id in placed:
        storage.add_nodes([Node(id=node_id, type="Actor", name=node_id)], [])
    session = manager.create_session()
    manager.add_node_refs(session.id, "mcp-agent", list(placed))
    manager.apply_layout(
        session.id,
        "mcp-agent",
        positions={n: {"x": x, "y": y} for n, (x, y) in placed.items()},
    )
    return session


class TestSummonByBounds:
    def test_publishes_the_bounds_under_the_given_display_name(self, tmp_path):
        tools, manager, _ = _tools(tmp_path)
        session = manager.create_session()
        sub = _listen(manager, session.id)

        result = tools["summon_session_viewport"](
            session_id=session.id, bounds=BOUNDS, display_name="  Planner  "
        )

        assert result["success"] is True
        assert result["connected_clients"] == 1
        (event,) = _summons(sub)
        assert event["summoned_by"] == "Planner"
        assert event["client_id"] == "mcp-agent"
        assert event["bounds"] == {k: float(v) for k, v in BOUNDS.items()}

    def test_default_display_name_and_length_cap(self, tmp_path):
        tools, manager, _ = _tools(tmp_path)
        session = manager.create_session()
        sub = _listen(manager, session.id)

        tools["summon_session_viewport"](session_id=session.id, bounds=BOUNDS)
        tools["summon_session_viewport"](
            session_id=session.id, bounds=BOUNDS, display_name="x" * 500
        )

        first, second = _summons(sub)
        assert first["summoned_by"] == "AI assistant"
        assert second["summoned_by"] == "x" * 100

    @pytest.mark.parametrize(
        "bounds",
        [
            {**BOUNDS, "x": math.nan},
            {**BOUNDS, "width": math.inf},
            {**BOUNDS, "height": 0},
            {"x": 0, "y": 0},
        ],
    )
    def test_invalid_bounds_publish_nothing(self, tmp_path, bounds):
        tools, manager, _ = _tools(tmp_path)
        session = manager.create_session()
        sub = _listen(manager, session.id)

        result = tools["summon_session_viewport"](session_id=session.id, bounds=bounds)

        assert result["success"] is False
        assert "bounds" in result["error"]
        assert _summons(sub) == []

    def test_needs_exactly_one_target(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        # "a" is a valid target on its own, so only exclusivity can refuse it.
        session = _canvas(manager, storage, {"a": (0, 0)})
        sub = _listen(manager, session.id)

        neither = tools["summon_session_viewport"](session_id=session.id)
        both = tools["summon_session_viewport"](
            session_id=session.id, bounds=BOUNDS, node_ids=["a"]
        )

        assert neither["success"] is False and both["success"] is False
        assert _summons(sub) == []

    def test_unknown_and_invalid_sessions(self, tmp_path):
        tools, manager, _ = _tools(tmp_path)

        unknown = tools["summon_session_viewport"](
            session_id="1234-5678", bounds=BOUNDS
        )
        unknown_by_ids = tools["summon_session_viewport"](
            session_id="1234-5678", node_ids=["a"]
        )
        invalid = tools["summon_session_viewport"](session_id="nope", bounds=BOUNDS)

        assert unknown["success"] is False and "not found" in unknown["error"]
        assert unknown_by_ids["success"] is False
        assert "not found" in unknown_by_ids["error"]
        assert invalid == {"success": False, "error": _INVALID_SESSION_ID_ERROR}
        assert manager.get_session("1234-5678") is None


class TestSummonByNodeIds:
    def test_frames_the_bounding_box_of_the_named_nodes(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(
            manager, storage, {"a": (100, 50), "b": (400, 250), "c": (0, 0)}
        )
        sub = _listen(manager, session.id)

        result = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["a", "b", "a"]
        )

        assert result["success"] is True, result
        expected = {
            "x": 100.0,
            "y": 50.0,
            "width": 300.0 + _ASSUMED_NODE_SIZE["width"],
            "height": 200.0 + _ASSUMED_NODE_SIZE["height"],
        }
        assert result["bounds"] == expected
        assert _summons(sub)[0]["bounds"] == expected

    def test_a_grouped_node_is_framed_where_the_canvas_draws_it(self, tmp_path):
        # A group member's stored position is relative to its group.
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (40, 60), "b": (10, 10)})
        manager.upsert_annotation(
            session.id,
            "mcp-agent",
            {
                "id": "g1",
                "type": "group",
                "position": {"x": 2000, "y": 1500},
                "member_node_ids": ["a"],
            },
        )

        grouped = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["a"]
        )
        loose = tools["summon_session_viewport"](session_id=session.id, node_ids=["b"])

        assert (grouped["bounds"]["x"], grouped["bounds"]["y"]) == (2040.0, 1560.0)
        assert (loose["bounds"]["x"], loose["bounds"]["y"]) == (10.0, 10.0)

    @pytest.mark.parametrize(
        "group, origin",
        [
            # The canvas draws a group at its geometry when the two disagree.
            (
                {"position": {"x": 1, "y": 2}, "geometry": {"x": 300, "y": 400}},
                (300, 400),
            ),
            ({"geometry": {"x": 300, "y": 400}}, (300, 400)),
            # Per coordinate: an unusable geometry value falls back to position.
            (
                {"position": {"x": 1, "y": 2}, "geometry": {"x": "5", "y": 400}},
                (1, 400),
            ),
            ({"position": {"x": True, "y": math.inf}}, (0, 0)),
            ({"geometry": {"x": 10**400, "y": 7}}, (0, 7)),
        ],
    )
    def test_the_group_origin_is_read_the_way_the_canvas_draws_it(
        self, tmp_path, group, origin
    ):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (40, 60)})
        session.state["annotations"] = [
            None,
            {
                "id": "note",
                "type": "note",
                "position": {"x": 9e3, "y": 9e3},
                "member_node_ids": ["a"],
            },
            {"id": "g1", "type": "group", "member_node_ids": [None, "a"], **group},
        ]

        result = tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert (result["bounds"]["x"], result["bounds"]["y"]) == (
            40.0 + origin[0],
            60.0 + origin[1],
        )

    def test_a_group_known_only_by_kind_still_offsets_its_members(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (40, 60)})
        session.state["annotations"] = [
            {
                "id": "g1",
                "kind": "group",
                "position": {"x": 100, "y": 200},
                "member_node_ids": ["a"],
            },
        ]

        result = tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert (result["bounds"]["x"], result["bounds"]["y"]) == (140.0, 260.0)

    @pytest.mark.parametrize("members", [5, "a", None, {"a": 1}, [["a"], {"b": 1}]])
    def test_a_group_whose_members_are_not_a_list_offsets_nothing(
        self, tmp_path, members
    ):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (40, 60)})
        session.state["annotations"] = [
            {
                "id": "g1",
                "type": "group",
                "position": {"x": 100, "y": 200},
                "member_node_ids": members,
            },
            # Only a group makes its members relative to it.
            {
                "id": "n1",
                "type": "note",
                "position": {"x": 500, "y": 500},
                "member_node_ids": ["a"],
            },
        ]

        result = tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert result["success"] is True, result
        assert (result["bounds"]["x"], result["bounds"]["y"]) == (40.0, 60.0)

    def test_annotations_that_are_not_a_list_add_no_offset(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (40, 60)})
        session.state["annotations"] = None

        result = tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert (result["bounds"]["x"], result["bounds"]["y"]) == (40.0, 60.0)

    def test_a_stale_position_for_a_node_off_the_canvas_is_refused(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (0, 0)})
        storage.add_nodes([Node(id="gone", type="Actor", name="gone")], [])
        session.state["positions"]["gone"] = {"x": 5.0, "y": 5.0}

        result = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["gone"]
        )

        assert result["error"] == "unknown_node_ids"

    def test_ids_not_on_the_canvas_are_refused_and_named(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (0, 0)})
        # On the graph but not on this session's canvas.
        storage.add_nodes([Node(id="elsewhere", type="Actor", name="e")], [])
        sub = _listen(manager, session.id)

        result = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["a", "elsewhere", "ghost"]
        )

        assert result["success"] is False
        assert result["error"] == "unknown_node_ids"
        assert result["node_ids"] == ["elsewhere", "ghost"]
        assert _summons(sub) == []

    @pytest.mark.parametrize("axis", ["x", "y"])
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_node_with_a_non_finite_position_is_refused_in_any_order(
        self, tmp_path, bad, axis
    ):
        tools, manager, storage = _tools(tmp_path)
        session = _canvas(manager, storage, {"a": (0, 0), "b": (300, 400)})
        # What a raw browser node_moved can store: json accepts NaN/Infinity.
        session.state["positions"]["a"] = {"x": 0.0, "y": 0.0, axis: bad}
        sub = _listen(manager, session.id)

        results = [
            tools["summon_session_viewport"](session_id=session.id, node_ids=ids)
            for ids in (["a", "b"], ["b", "a"])
        ]

        for result in results:
            assert result["error"] == "unknown_node_ids"
            assert result["node_ids"] == ["a"]
        assert _summons(sub) == []

    def test_a_node_on_the_canvas_without_a_position_is_refused(self, tmp_path):
        tools, manager, storage = _tools(tmp_path)
        storage.add_nodes([Node(id="a", type="Actor", name="a")], [])
        session = manager.create_session()
        manager.add_node_refs(session.id, "mcp-agent", ["a"])

        result = tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert result["error"] == "unknown_node_ids"

    def test_a_node_the_caller_may_not_read_is_reported_as_unknown(self, tmp_path):
        hook = FixedNarrowingHook(allow_local_graph=False, include_graph_ids=("g2",))
        tools, manager, storage = _tools(tmp_path, hook)
        session = _canvas(manager, storage, {"local": (0, 0)})
        storage.add_nodes(
            [
                Node(
                    id="remote",
                    type="Actor",
                    name="remote",
                    metadata={"origin_graph_id": "g2"},
                )
            ],
            [],
        )
        manager.add_node_refs(session.id, "mcp-agent", ["remote"])
        manager.apply_layout(
            session.id, "mcp-agent", positions={"remote": {"x": 5, "y": 5}}
        )

        hidden = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["local"]
        )
        visible = tools["summon_session_viewport"](
            session_id=session.id, node_ids=["remote"]
        )

        assert hidden["error"] == "unknown_node_ids"
        assert visible["success"] is True

    @pytest.mark.parametrize("node_ids", [[], [""], [1], "a"])
    def test_malformed_node_ids_are_refused(self, tmp_path, node_ids):
        tools, manager, _ = _tools(tmp_path)
        session = manager.create_session()

        result = tools["summon_session_viewport"](
            session_id=session.id, node_ids=node_ids
        )

        assert result["success"] is False

    def test_too_many_node_ids(self, tmp_path):
        tools, manager, _ = _tools(tmp_path)
        session = manager.create_session()

        result = tools["summon_session_viewport"](
            session_id=session.id, node_ids=[f"n{i}" for i in range(501)]
        )

        assert result["error"] == "too_large"


class TestAuthorization:
    def test_node_ids_are_resolved_with_the_read_scope(self, tmp_path):
        seen = []

        class RecordingHook:
            def evaluate(self, context):
                seen.append((context.action, context.target))
                return GraphAuthorizationDecision(
                    allowed=True, mode="custom", source="test"
                )

        tools, manager, storage = _tools(tmp_path, RecordingHook())
        session = _canvas(manager, storage, {"a": (0, 0)})

        tools["summon_session_viewport"](session_id=session.id, node_ids=["a"])

        assert (GRAPH_ACTION_READ, "summon_session_viewport") in seen

    def test_a_denied_caller_cannot_summon(self, tmp_path):
        hook = DenyAllRecordingHook()
        tools, manager, _ = _tools(tmp_path, hook)
        session = manager.create_session()
        sub = _listen(manager, session.id)

        result = tools["summon_session_viewport"](session_id=session.id, bounds=BOUNDS)

        assert result.get("success") is not True
        assert (GRAPH_ACTION_MUTATE, "summon_session_viewport") in hook.seen
        assert _summons(sub) == []
