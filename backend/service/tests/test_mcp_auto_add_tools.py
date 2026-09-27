"""Tests for the session-scoped auto-add-agent MCP tools.

``create_session_auto_add_agent`` / ``list_session_auto_add_agents`` /
``remove_session_auto_add_agent`` are thin, session-validated wrappers over
``SessionAutoAddRegistry``. The registry's matching/isolation behaviour is
covered in ``backend/core/tests/test_session_auto_add.py``; here we lock in the
tool contract (validation, shape, and that a created agent actually reacts).
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_auto_add import (
    SessionAutoAddRegistry,
    build_node_create_listener,
)
from backend.core.session_registry import SessionRegistry
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools

SESSION = "1000-2000"


@pytest.fixture
def auto_add_tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    storage.setup_events(enabled=True)
    service = GraphService(storage)
    session_registry = SessionRegistry()
    auto_add_registry = SessionAutoAddRegistry()

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(
        mock_mcp,
        service,
        session_registry=session_registry,
        auto_add_registry=auto_add_registry,
    )
    return tools_map, service, storage, session_registry, auto_add_registry


class TestCreate:
    def test_create_returns_agent(self, auto_add_tools):
        tools_map, *_ = auto_add_tools
        result = tools_map["create_session_auto_add_agent"](
            SESSION, node_types=["Actor"]
        )
        assert result["success"] is True
        agent = result["agent"]
        assert agent["session_id"] == SESSION
        assert agent["node_types"] == ["Actor"]
        assert agent["agent_id"]

    def test_invalid_session_id_rejected(self, auto_add_tools):
        tools_map, *_ = auto_add_tools
        result = tools_map["create_session_auto_add_agent"]("bad", node_types=["Actor"])
        assert result["success"] is False
        assert "Invalid session ID" in result["error"]

    def test_empty_pattern_rejected(self, auto_add_tools):
        tools_map, *_ = auto_add_tools
        result = tools_map["create_session_auto_add_agent"](SESSION)
        assert result["success"] is False
        assert "at least one" in result["error"]

    def test_create_materializes_push_session(self, auto_add_tools):
        tools_map, _, _, session_registry, _ = auto_add_tools
        tools_map["create_session_auto_add_agent"](SESSION, node_types=["Actor"])
        # So the periodic prune keeps the agent while the session is live.
        assert session_registry.session_exists(SESSION)


class TestListAndRemove:
    def test_list_and_remove(self, auto_add_tools):
        tools_map, *_ = auto_add_tools
        created = tools_map["create_session_auto_add_agent"](SESSION, keywords=["ai"])
        agent_id = created["agent"]["agent_id"]

        listed = tools_map["list_session_auto_add_agents"](SESSION)
        assert listed["success"] is True
        assert [a["agent_id"] for a in listed["agents"]] == [agent_id]

        removed = tools_map["remove_session_auto_add_agent"](SESSION, agent_id)
        assert removed["success"] is True
        assert tools_map["list_session_auto_add_agents"](SESSION)["agents"] == []

    def test_remove_unknown_agent(self, auto_add_tools):
        tools_map, *_ = auto_add_tools
        result = tools_map["remove_session_auto_add_agent"](SESSION, "nope")
        assert result["success"] is False


class TestReacts:
    def test_created_agent_reacts_to_new_node(self, auto_add_tools):
        tools_map, service, storage, _, auto_add_registry = auto_add_tools
        tools_map["create_session_auto_add_agent"](SESSION, node_types=["Actor"])

        pushed = []
        storage.add_system_listener(
            build_node_create_listener(
                auto_add_registry, lambda sid, node: pushed.append((sid, node["name"]))
            )
        )
        service.add_nodes(nodes=[{"type": "Actor", "name": "SCB"}], edges=[])

        assert pushed == [(SESSION, "SCB")]


class TestAuthorization:
    """The auto-add tools gate through the same seam as the other session tools.

    Regression for the bypass where all three skipped ``_authorize_session``: an
    actor a narrowing hook denied every session read could still install an agent
    that keeps pushing nodes into a session, or remove one. Create/remove are
    MUTATE (as the REST routes are), list is READ.
    """

    def test_read_only_blocks_create_and_installs_nothing(
        self, auto_add_tools, monkeypatch
    ):
        tools_map, _, _, session_registry, auto_add_registry = auto_add_tools
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["create_session_auto_add_agent"](
            SESSION, node_types=["Actor"]
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert auto_add_registry.list_rules(SESSION) == []
        assert not session_registry.session_exists(SESSION)

    def test_read_only_blocks_remove_and_keeps_the_agent(
        self, auto_add_tools, monkeypatch
    ):
        tools_map, _, _, _, auto_add_registry = auto_add_tools
        agent_id = tools_map["create_session_auto_add_agent"](
            SESSION, node_types=["Actor"]
        )["agent"]["agent_id"]
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["remove_session_auto_add_agent"](SESSION, agent_id)

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert [r.agent_id for r in auto_add_registry.list_rules(SESSION)] == [agent_id]

    def test_read_only_still_allows_list(self, auto_add_tools, monkeypatch):
        tools_map, *_ = auto_add_tools
        agent_id = tools_map["create_session_auto_add_agent"](SESSION, keywords=["ai"])[
            "agent"
        ]["agent_id"]
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        listed = tools_map["list_session_auto_add_agents"](SESSION)

        assert listed["success"] is True
        assert [a["agent_id"] for a in listed["agents"]] == [agent_id]

    def test_deny_all_blocks_list(self, auto_add_tools, monkeypatch):
        tools_map, *_ = auto_add_tools
        tools_map["create_session_auto_add_agent"](SESSION, keywords=["ai"])
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["list_session_auto_add_agents"](SESSION)

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert "agents" not in result

    def test_permissive_default_allows_all_three(self, auto_add_tools):
        tools_map, *_ = auto_add_tools

        created = tools_map["create_session_auto_add_agent"](
            SESSION, node_types=["Actor"]
        )
        listed = tools_map["list_session_auto_add_agents"](SESSION)
        removed = tools_map["remove_session_auto_add_agent"](
            SESSION, created["agent"]["agent_id"]
        )

        assert created["success"] is True
        assert listed["success"] is True
        assert len(listed["agents"]) == 1
        assert removed["success"] is True


class TestUnavailable:
    def test_tools_report_unavailable_without_registry(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        # No auto_add_registry / session_registry wired.
        tools_map = register_mcp_tools(mock_mcp, service)
        result = tools_map["create_session_auto_add_agent"](
            SESSION, node_types=["Actor"]
        )
        assert result["success"] is False
