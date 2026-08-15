"""
Tests for the ``add_nodes_to_session`` MCP tool.

The tool places a known set of nodes on a session's canvas by id, instead of
requiring a search whose results happen to be exactly that set. It is a thin
wrapper over ``SessionManager.add_node_refs``; the op semantics themselves are
covered in ``backend/core/tests/test_session_manager.py``.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage, Node
from backend.core.session_manager import SessionManager
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools
from backend.service.tests.test_authorization import FixedNarrowingHook


def _wire(storage, service):
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


@pytest.fixture
def tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    tools_map, manager = _wire(storage, service)
    tools_map["add_nodes"](
        nodes=[
            {"id": "alpha", "type": "Initiative", "name": "Alpha"},
            {"id": "beta", "type": "Actor", "name": "Beta"},
            {"id": "gamma", "type": "Actor", "name": "Gamma"},
        ],
        edges=[],
    )
    return tools_map, manager


def _session(manager):
    return manager.create_session().id


class TestAddNodesToSession:
    def test_named_nodes_become_the_sessions_nodes(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha", "beta"]
        assert result["node_count"] == 2
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_adding_is_additive_and_never_duplicates(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )

        assert result["added"] == ["beta"]
        assert manager.get_session(sid).state["node_refs"] == ["alpha", "beta"]

    def test_adding_only_known_nodes_does_not_advance_the_revision(self, tools):
        """A no-op write must not make every other collaborator's revision stale."""
        tools_map, manager = tools
        sid = _session(manager)
        first = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        again = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert again["success"] is True
        assert again["added"] == []
        assert again["revision"] == first["revision"]

    def test_the_new_nodes_are_broadcast_to_connected_clients(self, tools):
        """Session state is server-owned: connected canvases learn about the add."""
        tools_map, manager = tools
        sid = _session(manager)
        published = []
        manager.bus.publish = lambda session_id, event: published.append(
            (session_id, event)
        )

        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert [sid for sid, _ in published] == [sid]
        op = published[0][1]["op"]
        assert op["op"] == "nodes_added"
        assert op["node_ids"] == ["alpha"]

    def test_unknown_ids_are_skipped_rather_than_referenced(self, tools):
        """A stale id must not leave a phantom reference in session state."""
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "ghost"]
        )

        assert result["success"] is True
        assert result["added"] == ["alpha"]
        assert result["skipped"] == ["ghost"]
        assert manager.get_session(sid).state["node_refs"] == ["alpha"]

    def test_all_ids_unknown_is_an_error(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["ghost"])

        assert result["success"] is False
        assert result["error"] == "no_resolvable_nodes"
        assert result["skipped"] == ["ghost"]

    def test_stale_expected_revision_is_rejected(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["beta"], expected_revision=0
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == manager.get_session(sid).seq
        assert manager.get_session(sid).state["node_refs"] == ["alpha"]

    def test_current_revision_is_accepted(self, tools):
        tools_map, manager = tools
        sid = _session(manager)
        first = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        result = tools_map["add_nodes_to_session"](
            session_id=sid,
            node_ids=["beta"],
            expected_revision=first["revision"],
        )

        assert result["success"] is True
        assert result["revision"] > first["revision"]

    def test_returned_revision_threads_into_a_layout_write(self, tools):
        """The point of the tool: populate then arrange, without a search."""
        tools_map, manager = tools
        sid = _session(manager)

        added = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["alpha", "beta"]
        )
        moved = tools_map["apply_visualization_layout"](
            session_id=sid,
            positions={"alpha": {"x": 0, "y": 0}, "beta": {"x": 300, "y": 0}},
            expected_revision=added["revision"],
        )

        assert moved["success"] is True
        layout = tools_map["get_visualization_layout"](session_id=sid)
        assert {n["id"] for n in layout["nodes"]} == {"alpha", "beta"}

    def test_invalid_session_id(self, tools):
        tools_map, _ = tools
        result = tools_map["add_nodes_to_session"](
            session_id="nope", node_ids=["alpha"]
        )
        assert result["success"] is False
        assert "Invalid session ID" in result["error"]

    def test_unknown_session(self, tools):
        tools_map, _ = tools
        result = tools_map["add_nodes_to_session"](
            session_id="9999-9999", node_ids=["alpha"]
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_empty_node_ids_is_rejected(self, tools):
        tools_map, manager = tools
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=[])

        assert result["success"] is False
        assert "node_ids" in result["error"]


class TestAuthorization:
    def test_read_only_mode_denies_the_write(self, tools, monkeypatch):
        """The tool goes through the same gate as the other session mutations."""
        tools_map, manager = tools
        sid = _session(manager)

        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")
        result = tools_map["add_nodes_to_session"](session_id=sid, node_ids=["alpha"])

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert manager.get_session(sid).state["node_refs"] == []

    def test_a_node_outside_the_callers_graph_scope_is_not_added(self, tmp_path):
        """Graph-scope narrowing decides what may enter the session."""
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.add_nodes(
            [
                Node(
                    id="mine",
                    type="Initiative",
                    name="Mine",
                    metadata={"origin_graph_id": "graph-alpha"},
                ),
                Node(
                    id="theirs",
                    type="Actor",
                    name="Theirs",
                    metadata={"origin_graph_id": "graph-beta"},
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
        tools_map, manager = _wire(storage, service)
        sid = _session(manager)

        result = tools_map["add_nodes_to_session"](
            session_id=sid, node_ids=["mine", "theirs"]
        )

        assert result["added"] == ["mine"]
        assert result["skipped"] == ["theirs"]
        assert manager.get_session(sid).state["node_refs"] == ["mine"]
