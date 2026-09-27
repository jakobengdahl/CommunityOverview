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
from backend.runtime.authorization import (
    AUTHORIZATION_MODE_ENV,
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, register_mcp_tools
from backend.service.mcp_tools import _INVALID_SESSION_ID_ERROR
from backend.service.tests.test_authorization import DenyMutationsHook

SESSION = "1000-2000"


class DenyAllRecordingHook:
    """Denies every action and records the ``(action, target)`` it was asked."""

    def __init__(self):
        self.seen = []

    def evaluate(self, context):
        self.seen.append((context.action, context.target))
        return GraphAuthorizationDecision(
            allowed=False, reason="denied", mode="custom", source="test"
        )


def _tools_with_hook(tmp_path, hook, *, session_registry=True, auto_add=True):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage, authorization_hook=hook)
    registry = SessionRegistry() if session_registry else None
    auto_add_registry = SessionAutoAddRegistry() if auto_add else None
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(
        mock_mcp,
        service,
        session_registry=registry,
        auto_add_registry=auto_add_registry,
    )
    return tools_map, registry, auto_add_registry


# (tool, action it must be authorized as, extra kwargs a well-formed call needs)
AUTO_ADD_TOOLS = [
    ("create_session_auto_add_agent", GRAPH_ACTION_MUTATE, {"node_types": ["Actor"]}),
    ("list_session_auto_add_agents", GRAPH_ACTION_READ, {}),
    ("remove_session_auto_add_agent", GRAPH_ACTION_MUTATE, {"agent_id": "a-1"}),
]


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


class TestAuthorizationOrdering:
    """Pins where the gate sits relative to the tools' own validation.

    Input validation and the availability check run before the hook is asked,
    so a denied caller still gets the specific error for a malformed id or an
    unwired registry, and the hook is never consulted for a call that could
    not have proceeded anyway. The gate itself names the tool as its target.
    """

    @pytest.mark.parametrize("tool,action,kwargs", AUTO_ADD_TOOLS)
    def test_malformed_session_id_is_rejected_before_the_gate(
        self, tmp_path, tool, action, kwargs
    ):
        hook = DenyAllRecordingHook()
        tools_map, *_ = _tools_with_hook(tmp_path, hook)

        result = tools_map[tool]("bad", **kwargs)

        assert result == {"success": False, "error": _INVALID_SESSION_ID_ERROR}
        assert hook.seen == []

    @pytest.mark.parametrize("tool,action,kwargs", AUTO_ADD_TOOLS)
    @pytest.mark.parametrize(
        "wiring",
        [{"auto_add": False}, {"session_registry": False}],
        ids=["no-auto-add-registry", "no-session-registry"],
    )
    def test_unavailable_registry_is_reported_before_the_gate(
        self, tmp_path, tool, action, kwargs, wiring
    ):
        hook = DenyAllRecordingHook()
        tools_map, *_ = _tools_with_hook(tmp_path, hook, **wiring)

        result = tools_map[tool](SESSION, **kwargs)

        assert result == {
            "success": False,
            "error": "Auto-add agents are not available",
        }
        assert hook.seen == []

    @pytest.mark.parametrize("tool,action,kwargs", AUTO_ADD_TOOLS)
    def test_gate_is_asked_once_with_the_tool_as_target(
        self, tmp_path, tool, action, kwargs
    ):
        hook = DenyAllRecordingHook()
        tools_map, *_ = _tools_with_hook(tmp_path, hook)

        result = tools_map[tool](SESSION, **kwargs)

        assert result["success"] is False
        assert result["error_code"] == "access_denied"
        assert result["authorization"]["action"] == action
        assert result["authorization"]["target"] == tool
        assert hook.seen == [(action, tool)]

    def test_denied_create_is_refused_before_pattern_validation(self, tmp_path):
        # An empty pattern is an AutoAddRuleError for an allowed caller; a caller
        # denied mutations must get the access error, not the validation one.
        hook = DenyMutationsHook()
        tools_map, _, auto_add_registry = _tools_with_hook(tmp_path, hook)

        result = tools_map["create_session_auto_add_agent"](SESSION)

        assert result["error_code"] == "access_denied"
        assert "at least one" not in result["error"]
        assert [(c.action, c.target) for c in hook.seen_contexts] == [
            (GRAPH_ACTION_MUTATE, "create_session_auto_add_agent")
        ]
        assert auto_add_registry.list_rules(SESSION) == []


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
