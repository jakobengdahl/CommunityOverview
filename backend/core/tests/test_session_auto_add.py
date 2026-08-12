"""Tests for session-scoped auto-add agents.

An auto-add agent watches for newly created nodes matching a pattern and adds
each match to *one* visualization session's live view — additively, and only to
its own session. These tests lock in:

- the match semantics (node types AND keywords, mirroring EventDispatcher),
- the boundary guards (a pattern must have at least one constraint; caps),
- session isolation: a matching node is delivered only to the session whose
  agent matches, never leaking into another session,
- the additive push: the command carries ``action="add_to_visualization"`` and
  never clears the view (building on PR #327),
- end-to-end behaviour over a real GraphStorage node.create event.
"""

import os

import pytest

from backend.core import GraphStorage
from backend.core.events.models import (
    EntityData,
    EntityKind,
    Event,
    EventContext,
    EventType,
)
from backend.core.session_auto_add import (
    AutoAddRuleError,
    SessionAutoAddRegistry,
    build_node_create_listener,
    node_matches,
)
from backend.service import GraphService
from backend.service.mcp_tools import _push_to_session

SESSION_A = "1000-2000"
SESSION_B = "3000-4000"


def _create_event(node_type: str, after: dict, node_id: str = "n1") -> Event:
    return Event(
        event_type=EventType.NODE_CREATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE, id=node_id, type=node_type, after=after
        ),
    )


class TestMatcher:
    def test_node_types_only(self):
        assert node_matches("Actor", {"name": "SCB"}, ["Actor"], [])
        assert not node_matches("Goal", {"name": "SCB"}, ["Actor"], [])

    def test_keywords_only_case_insensitive_over_fields(self):
        assert node_matches("Goal", {"name": "Digital plan"}, [], ["digital"])
        assert node_matches("Goal", {"description": "about AI"}, [], ["ai"])
        assert node_matches("Goal", {"summary": "Cyber"}, [], ["cyber"])
        assert node_matches("Goal", {"tags": ["security"]}, [], ["security"])
        assert not node_matches("Goal", {"name": "unrelated"}, [], ["digital"])

    def test_types_and_keywords_are_anded(self):
        # Both constraints must pass.
        assert node_matches("Actor", {"name": "digital agency"}, ["Actor"], ["digital"])
        assert not node_matches(
            "Actor", {"name": "plain agency"}, ["Actor"], ["digital"]
        )
        assert not node_matches(
            "Goal", {"name": "digital agency"}, ["Actor"], ["digital"]
        )


class TestRegistry:
    def test_add_and_list(self):
        reg = SessionAutoAddRegistry()
        rule = reg.add_rule(SESSION_A, node_types=["Actor"])
        assert rule.agent_id
        assert reg.list_rules(SESSION_A)[0].node_types == ["Actor"]
        assert reg.total_rules == 1

    def test_pattern_is_normalized(self):
        reg = SessionAutoAddRegistry()
        rule = reg.add_rule(
            SESSION_A,
            node_types=["Actor", " Actor ", "", "Goal"],
            keywords=["AI", "ai", "  ai  "],
        )
        # dedup (after strip), drop empties, preserve order.
        assert rule.node_types == ["Actor", "Goal"]
        assert rule.keywords == ["AI", "ai"]

    def test_empty_pattern_rejected(self):
        reg = SessionAutoAddRegistry()
        with pytest.raises(AutoAddRuleError):
            reg.add_rule(SESSION_A)
        with pytest.raises(AutoAddRuleError):
            reg.add_rule(SESSION_A, node_types=[" ", ""], keywords=[])

    def test_per_session_cap(self):
        reg = SessionAutoAddRegistry()
        from backend.core.session_auto_add import MAX_RULES_PER_SESSION

        for _ in range(MAX_RULES_PER_SESSION):
            reg.add_rule(SESSION_A, node_types=["Actor"])
        with pytest.raises(AutoAddRuleError):
            reg.add_rule(SESSION_A, node_types=["Actor"])

    def test_remove_and_clear(self):
        reg = SessionAutoAddRegistry()
        rule = reg.add_rule(SESSION_A, node_types=["Actor"])
        assert reg.remove_rule(SESSION_A, rule.agent_id) is True
        assert reg.remove_rule(SESSION_A, rule.agent_id) is False
        assert reg.session_count == 0
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_A, keywords=["ai"])
        assert reg.clear_session(SESSION_A) == 2
        assert reg.session_count == 0

    def test_matching_sessions_dedupes_multiple_rules(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_A, keywords=["scb"])
        # Both rules match the same node; the session is returned once.
        assert reg.matching_sessions("Actor", {"name": "SCB"}) == [SESSION_A]

    def test_prune_to_sessions(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_B, node_types=["Goal"])
        assert reg.prune_to_sessions({SESSION_A}) == 1
        assert reg.session_count == 1
        assert reg.list_rules(SESSION_B) == []


class TestListener:
    def test_matching_node_is_pushed(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        pushed = []
        listener = build_node_create_listener(
            reg, lambda sid, node: pushed.append((sid, node))
        )

        listener(_create_event("Actor", {"id": "n1", "name": "SCB"}))

        assert pushed == [(SESSION_A, {"id": "n1", "name": "SCB"})]

    def test_non_matching_node_is_not_pushed(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        pushed = []
        listener = build_node_create_listener(
            reg, lambda sid, node: pushed.append((sid, node))
        )

        listener(_create_event("Goal", {"id": "n1", "name": "SCB"}))

        assert pushed == []

    def test_session_isolation(self):
        # A matches Actor, B matches Goal. An Actor must reach only A.
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_B, node_types=["Goal"])
        pushed = []
        listener = build_node_create_listener(reg, lambda sid, node: pushed.append(sid))

        listener(_create_event("Actor", {"id": "n1", "name": "SCB"}))

        assert pushed == [SESSION_A]

    def test_embedding_is_stripped_from_payload(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        pushed = []
        listener = build_node_create_listener(
            reg, lambda sid, node: pushed.append(node)
        )

        listener(
            _create_event("Actor", {"id": "n1", "name": "SCB", "embedding": [0.1, 0.2]})
        )

        assert "embedding" not in pushed[0]
        assert pushed[0]["name"] == "SCB"

    def test_ignores_update_and_edge_events(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        pushed = []
        listener = build_node_create_listener(reg, lambda sid, node: pushed.append(sid))

        update = Event(
            event_type=EventType.NODE_UPDATE,
            origin=EventContext(),
            entity=EntityData(
                kind=EntityKind.NODE, id="n1", type="Actor", after={"name": "X"}
            ),
        )
        edge = Event(
            event_type=EventType.EDGE_CREATE,
            origin=EventContext(),
            entity=EntityData(
                kind=EntityKind.EDGE, id="e1", type="RELATES_TO", after={}
            ),
        )
        listener(update)
        listener(edge)

        assert pushed == []

    def test_one_push_error_does_not_stop_other_sessions(self):
        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_B, node_types=["Actor"])
        reached = []

        def flaky_push(sid, node):
            if sid == SESSION_A:
                raise RuntimeError("boom")
            reached.append(sid)

        listener = build_node_create_listener(reg, flaky_push)
        listener(_create_event("Actor", {"id": "n1", "name": "SCB"}))

        assert SESSION_B in reached


class _CapturingRegistry:
    """A minimal stand-in for SessionRegistry that records pushed commands, so
    the additive-action assertion doesn't depend on event-loop delivery."""

    def __init__(self):
        self.commands = []

    def is_valid_session_id(self, session_id):
        return True

    def push_command_sync(self, session_id, command):
        self.commands.append((session_id, command))
        return True


class TestAdditivePush:
    """The auto-add push must be additive (action=add_to_visualization), so it
    never clears what the session already shows (PR #327 semantics)."""

    def test_push_command_is_additive(self):
        registry = _CapturingRegistry()

        def push(session_id, node_payload):
            _push_to_session(
                registry,
                session_id,
                "session_auto_add_agent",
                {"nodes": [node_payload], "edges": []},
            )

        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        listener = build_node_create_listener(reg, push)

        listener(_create_event("Actor", {"id": "n1", "name": "SCB"}))

        assert len(registry.commands) == 1
        session_id, command = registry.commands[0]
        assert session_id == SESSION_A
        assert command["type"] == "tool_result"
        result = command["result"]
        # Additive: never a clear/replace action, and the node is carried along.
        assert result["action"] == "add_to_visualization"
        assert result["nodes"][0]["name"] == "SCB"


class TestGraphIntegration:
    """End-to-end over a real GraphStorage node.create event."""

    def test_created_node_reaches_only_the_matching_session(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.setup_events(enabled=True)
        service = GraphService(storage)

        reg = SessionAutoAddRegistry()
        reg.add_rule(SESSION_A, node_types=["Actor"])
        reg.add_rule(SESSION_B, node_types=["Goal"])
        pushed = []
        storage.add_system_listener(
            build_node_create_listener(
                reg, lambda sid, node: pushed.append((sid, node["name"]))
            )
        )

        service.add_nodes(nodes=[{"type": "Actor", "name": "Skatteverket"}], edges=[])

        assert pushed == [(SESSION_A, "Skatteverket")]

    def test_no_agent_no_push(self, tmp_path):
        storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
        storage.setup_events(enabled=True)
        service = GraphService(storage)
        reg = SessionAutoAddRegistry()
        pushed = []
        storage.add_system_listener(
            build_node_create_listener(reg, lambda sid, node: pushed.append(sid))
        )

        service.add_nodes(nodes=[{"type": "Actor", "name": "Skatteverket"}], edges=[])

        assert pushed == []
