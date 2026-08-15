"""
Tests for the MCP visualization-layout tools (``get_visualization_layout`` and
``apply_visualization_layout``) registered in ``backend/service/mcp_tools.py``.

These tools let an external AI agent read node geometry from a shared
visualization session and move nodes back into it. They are thin wrappers over
``SessionManager.apply_layout`` / ``get_session``; the op semantics themselves
are covered in ``backend/core/tests/test_session_manager.py``.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, Node
from backend.core.session_manager import SessionManager
from backend.core.session_registry import SessionRegistry
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools
from backend.service.tests.test_authorization import FixedNarrowingHook


@pytest.fixture
def layout_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


@pytest.fixture
def authz_tools(tmp_path):
    """tools_map wired with both a session registry and a manager.

    ``get_visualization_session_state`` reads through the registry while the two
    geometry tools read/write through the manager, so the authorization-gating
    tests need both wired to exercise all three.
    """
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    registry = SessionRegistry()

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(
        mock_mcp, service, session_registry=registry, session_manager=manager
    )
    return tools_map, manager, registry


def _session_with_nodes(manager, node_ids):
    session = manager.create_session()
    manager.store.apply_state_op(session, {"op": "nodes_added", "node_ids": node_ids})
    manager.store.persist(session)
    return session


class TestGetVisualizationLayout:
    def test_returns_positions_and_revision(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a", "b"])
        manager.apply_layout(session.id, "mcp-agent", positions={"a": {"x": 5, "y": 6}})

        result = tools_map["get_visualization_layout"](session_id=session.id)

        assert result["session_id"] == session.id
        assert result["revision"] == session.seq
        assert result["node_count"] == 2
        by_id = {n["id"]: n for n in result["nodes"]}
        assert by_id["a"]["x"] == 5.0 and by_id["a"]["y"] == 6.0
        # A node with no recorded position reports null coordinates.
        assert by_id["b"]["x"] is None and by_id["b"]["y"] is None
        assert "assumed_node_size" in result

    def test_reports_hidden_state(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a", "b"])
        manager.store.apply_state_op(session, {"op": "nodes_hidden", "node_ids": ["b"]})

        result = tools_map["get_visualization_layout"](session_id=session.id)
        by_id = {n["id"]: n for n in result["nodes"]}
        assert by_id["b"]["hidden"] is True
        assert by_id["a"]["hidden"] is False

    def test_reports_node_type_and_status_for_semantic_arrangement(self, layout_tools):
        """An agent must be able to lay out by type/status without parsing ids."""
        tools_map, manager = layout_tools
        tools_map["add_nodes"](
            nodes=[
                {
                    "id": "alpha",
                    "type": "Initiative",
                    "name": "Alpha",
                    "metadata": {"status": "in_progress"},
                },
                {"id": "beta", "type": "Actor", "name": "Beta"},
            ],
            edges=[],
        )
        session = _session_with_nodes(manager, ["alpha", "beta"])

        result = tools_map["get_visualization_layout"](session_id=session.id)
        by_id = {n["id"]: n for n in result["nodes"]}

        assert by_id["alpha"]["type"] == "Initiative"
        assert by_id["alpha"]["status"] == "in_progress"
        assert by_id["beta"]["type"] == "Actor"
        # A deployment that does not use metadata["status"] reports null, meaning
        # "unknown" rather than a fabricated value.
        assert by_id["beta"]["status"] is None
        # The pre-existing geometry projection is unchanged alongside the new fields.
        assert by_id["beta"]["x"] is None and by_id["beta"]["hidden"] is False
        assert result["assumed_node_size"] == {"width": 220, "height": 120}

    def test_unresolvable_node_ref_keeps_geometry_with_null_semantics(
        self, layout_tools
    ):
        """A reference with no readable node must still report x/y/hidden."""
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["ghost"])
        manager.apply_layout(
            session.id, "mcp-agent", positions={"ghost": {"x": 3, "y": 4}}
        )

        result = tools_map["get_visualization_layout"](session_id=session.id)

        assert result["node_count"] == 1
        node = result["nodes"][0]
        assert node["id"] == "ghost"
        assert node["x"] == 3.0 and node["y"] == 4.0
        assert node["hidden"] is False
        assert node["type"] is None and node["status"] is None

    def test_non_string_status_metadata_is_not_reported(self, layout_tools):
        """Only a string status is a lane label; anything else reads as unknown."""
        tools_map, manager = layout_tools
        tools_map["add_nodes"](
            nodes=[
                {
                    "id": "gamma",
                    "type": "Actor",
                    "name": "Gamma",
                    "metadata": {"status": {"phase": 1}},
                },
                {
                    "id": "delta",
                    "type": "Actor",
                    "name": "Delta",
                    "metadata": {"status": "   "},
                },
                {
                    "id": "epsilon",
                    "type": "Actor",
                    "name": "Epsilon",
                    "metadata": {"status": "  done  "},
                },
            ],
            edges=[],
        )
        session = _session_with_nodes(manager, ["gamma", "delta", "epsilon"])

        by_id = {
            n["id"]: n
            for n in tools_map["get_visualization_layout"](session_id=session.id)[
                "nodes"
            ]
        }

        assert by_id["gamma"]["type"] == "Actor"
        assert by_id["gamma"]["status"] is None
        # A blank status would otherwise become an unnamed swimlane.
        assert by_id["delta"]["status"] is None
        # Padding must not split one lane into two.
        assert by_id["epsilon"]["status"] == "done"

    def test_out_of_scope_node_keeps_geometry_but_leaks_no_semantics(self, tmp_path):
        """Graph-scope narrowing must hide meaning without hiding the node.

        A node outside the caller's ``include_graph_ids`` must still be laid
        out — dropping it would leave an agent unable to place something the
        session references — but its type/status must not leak.
        """
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.add_nodes(
            [
                Node(
                    id="alpha",
                    type="Initiative",
                    name="Alpha",
                    metadata={
                        "origin_graph_id": "graph-alpha",
                        "status": "in_progress",
                    },
                ),
                Node(
                    id="beta",
                    type="Actor",
                    name="Beta",
                    metadata={"origin_graph_id": "graph-beta", "status": "done"},
                ),
            ],
            [],
        )
        service = GraphService(
            storage,
            authorization_hook=FixedNarrowingHook(
                allow_local_graph=False, include_graph_ids=("graph-alpha",)
            ),
        )
        manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
        session = _session_with_nodes(manager, ["alpha", "beta"])
        manager.apply_layout(
            session.id, "mcp-agent", positions={"beta": {"x": 7, "y": 8}}
        )

        by_id = {
            n["id"]: n
            for n in tools_map["get_visualization_layout"](session_id=session.id)[
                "nodes"
            ]
        }

        assert by_id["alpha"]["type"] == "Initiative"
        assert by_id["alpha"]["status"] == "in_progress"
        assert by_id["beta"]["x"] == 7.0 and by_id["beta"]["y"] == 8.0
        assert by_id["beta"]["type"] is None and by_id["beta"]["status"] is None

    def test_invalid_session_id(self, layout_tools):
        tools_map, _ = layout_tools
        assert "error" in tools_map["get_visualization_layout"](session_id="nope")

    def test_unknown_session(self, layout_tools):
        tools_map, _ = layout_tools
        result = tools_map["get_visualization_layout"](session_id="9999-9999")
        assert "not found" in result["error"]

    def test_reports_selection_so_one_call_answers_what_and_where(self, authz_tools):
        """Selection is merged in and agrees with the dedicated state tool.

        The visible set is deliberately *not* merged: it is already this
        response's nodes with ``hidden`` false, so repeating it would duplicate
        the same fact in two shapes.
        """
        tools_map, manager, registry = authz_tools
        session = _session_with_nodes(manager, ["a", "b"])
        registry.get_or_create(session.id)
        manager.claims.claim(session.id, "client-1", ["a"])

        result = tools_map["get_visualization_layout"](session_id=session.id)
        state = tools_map["get_visualization_session_state"](session_id=session.id)

        assert result["selected_node_ids"] == ["a"]
        assert result["selected_node_ids"] == state["selected_node_ids"]
        assert "visible_node_ids" not in result
        assert [n["id"] for n in result["nodes"] if not n["hidden"]] == state[
            "visible_node_ids"
        ]

    def test_empty_selection_is_reported_as_an_empty_list(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])

        result = tools_map["get_visualization_layout"](session_id=session.id)

        assert result["selected_node_ids"] == []

    def test_missing_manager_is_reported(self):
        storage = GraphStorage(json_path="/tmp/does-not-matter.json")
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service)  # no session_manager
        assert "error" in tools_map["get_visualization_layout"](session_id="1111-2222")


class TestApplyVisualizationLayout:
    def test_absolute_move_succeeds(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a", "b"])

        result = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 1, "y": 2}, "b": {"x": 3, "y": 4}},
        )
        assert result["success"] is True
        assert result["moved"] == 2
        assert result["revision"] == session.seq
        assert session.state["positions"]["a"] == {"x": 1.0, "y": 2.0}

    def test_delta_move_succeeds(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 10, "y": 10}}
        )
        tools_map["apply_visualization_layout"](
            session_id=session.id, deltas={"a": {"dx": -4, "dy": 6}}
        )
        assert session.state["positions"]["a"] == {"x": 6.0, "y": 16.0}

    def test_revision_conflict_is_reported(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 1}}
        )
        result = tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 2, "y": 2}},
            expected_revision=0,
        )
        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == session.seq
        # The stale write did not move the node.
        assert session.state["positions"]["a"] == {"x": 1.0, "y": 1.0}

    def test_busy_when_lock_held(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])

        # Simulate an in-flight apply_ops batch holding the per-session lock by
        # returning a lock that reports itself locked (avoids depending on
        # asyncio.Lock internals or a running loop).
        class _HeldLock:
            def locked(self):
                return True

        manager._lock = lambda _sid: _HeldLock()
        result = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 1}}
        )
        assert result["success"] is False
        assert result["error"] == "busy"

    def test_neither_positions_nor_deltas_is_error(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        result = tools_map["apply_visualization_layout"](session_id=session.id)
        assert result["success"] is False

    def test_invalid_session_id(self, layout_tools):
        tools_map, _ = layout_tools
        result = tools_map["apply_visualization_layout"](
            session_id="nope", positions={"a": {"x": 1, "y": 1}}
        )
        assert result["success"] is False

    def test_unknown_session(self, layout_tools):
        tools_map, _ = layout_tools
        result = tools_map["apply_visualization_layout"](
            session_id="9999-9999", positions={"a": {"x": 1, "y": 1}}
        )
        assert result["success"] is False

    def test_animation_hint_reaches_the_broadcast(self, layout_tools):
        tools_map, manager = layout_tools
        session = _session_with_nodes(manager, ["a"])
        sub = manager.bus.subscribe(session.id)
        tools_map["apply_visualization_layout"](
            session_id=session.id,
            positions={"a": {"x": 1, "y": 1}},
            animate=True,
            duration_ms=300,
            easing="linear",
        )
        event = sub.queue.get_nowait()
        assert event["op"]["animation"] == {
            "animate": True,
            "duration_ms": 300,
            "easing": "linear",
        }


class TestVisualizationToolsAuthorization:
    """The geometry/state tools must gate through the same authorization seam as
    the session CRUD tools.

    Regression for the bypass where ``get_visualization_layout``,
    ``get_visualization_session_state`` (READ) and ``apply_visualization_layout``
    (MUTATE) skipped ``_authorize_session`` entirely: under the permissive
    open-core default nothing changed, but in a read-only or deny-all mode (the
    seam the hosted layer swaps in for per-tenant enforcement) an actor denied
    ``get_visualization_session`` could still read node geometry and MOVE nodes.
    """

    def test_deny_all_blocks_layout_read(self, authz_tools, monkeypatch):
        tools_map, manager, _ = authz_tools
        session = _session_with_nodes(manager, ["a", "b"])
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["get_visualization_layout"](session_id=session.id)

        assert result.get("error_code") == "access_denied"

    def test_deny_all_blocks_session_state_read(self, authz_tools, monkeypatch):
        tools_map, manager, _ = authz_tools
        session = _session_with_nodes(manager, ["a"])
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["get_visualization_session_state"](session_id=session.id)

        assert result.get("error_code") == "access_denied"

    def test_read_only_blocks_layout_mutation_and_moves_nothing(
        self, authz_tools, monkeypatch
    ):
        tools_map, manager, _ = authz_tools
        session = _session_with_nodes(manager, ["a", "b"])
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 10, "y": 20}}
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        # The denied write must not have moved the node.
        assert "a" not in session.state.get("positions", {})

    def test_read_only_still_allows_the_getters(self, authz_tools, monkeypatch):
        # read-only denies only mutations; both READ tools must keep working.
        tools_map, manager, registry = authz_tools
        session = _session_with_nodes(manager, ["a"])
        registry.get_or_create(session.id)  # state tool checks the registry
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        layout = tools_map["get_visualization_layout"](session_id=session.id)
        state = tools_map["get_visualization_session_state"](session_id=session.id)

        assert layout["session_id"] == session.id
        assert "error_code" not in layout
        assert state["session_id"] == session.id
        assert "error_code" not in state

    def test_permissive_default_allows_read_and_mutation(self, authz_tools):
        tools_map, manager, _ = authz_tools
        session = _session_with_nodes(manager, ["a"])

        layout = tools_map["get_visualization_layout"](session_id=session.id)
        moved = tools_map["apply_visualization_layout"](
            session_id=session.id, positions={"a": {"x": 1, "y": 2}}
        )

        assert "error_code" not in layout
        assert moved["success"] is True
        assert moved["moved"] == 1
