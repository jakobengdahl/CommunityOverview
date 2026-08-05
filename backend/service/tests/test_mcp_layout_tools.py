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

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.service import GraphService, register_mcp_tools


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

    def test_invalid_session_id(self, layout_tools):
        tools_map, _ = layout_tools
        assert "error" in tools_map["get_visualization_layout"](session_id="nope")

    def test_unknown_session(self, layout_tools):
        tools_map, _ = layout_tools
        result = tools_map["get_visualization_layout"](session_id="9999-9999")
        assert "not found" in result["error"]

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
