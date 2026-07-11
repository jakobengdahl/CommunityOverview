"""
Tests for durable append-only graph mutation history.

Covers:
- a durable history record is written for each mutation type
- node-specific and edge-specific filtering
- newest-first ordering and offset/limit pagination
- origin + attribution persistence
- AI-action detection derived from origin/attribution
"""

import os
import tempfile
from pathlib import Path

import pytest

from backend.core import (
    GraphStorage,
    Node,
    Edge,
    NodeType,
    RelationshipType,
    derive_is_ai_action,
)
from backend.core.events import (
    EventContext,
    EventAttribution,
    EventActorAttribution,
)


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "graph.json")
        st = GraphStorage(json_path=json_path)
        yield st
        st.flush()


def _seed_two_nodes(storage, ctx=None):
    nodes = [
        Node(id="actor-1", type=NodeType.ACTOR, name="Actor One"),
        Node(id="actor-2", type=NodeType.ACTOR, name="Actor Two"),
    ]
    storage.add_nodes(nodes, [], event_context=ctx)


def test_history_sidecar_lives_next_to_graph_json(storage):
    _seed_two_nodes(storage)
    expected = storage.json_path.with_name(storage.json_path.stem + ".history.ndjson")
    assert expected.exists()
    # The current snapshot file must not carry the history array.
    assert "history" not in Path(storage.json_path).read_text()


def test_durable_write_for_each_mutation_type(storage):
    # node.create (x2) via add_nodes
    _seed_two_nodes(storage)
    # edge.create
    storage.add_edge(Edge(id="edge-1", source="actor-1", target="actor-2",
                          type=RelationshipType.RELATES_TO))
    # node.update
    storage.update_node("actor-1", {"description": "updated"})
    # edge.update
    storage.update_edge("edge-1", {"label": "linked"})
    # edge.delete
    storage.delete_edge("edge-1")
    # node.delete
    storage.delete_nodes(["actor-2"], confirmed=True)

    recorded = {e["event_type"] for e in storage.get_recent_history(limit=100)}
    assert recorded == {
        "node.create",
        "node.update",
        "node.delete",
        "edge.create",
        "edge.update",
        "edge.delete",
    }


def test_history_written_even_without_event_system(storage):
    # setup_events is never called -> webhook delivery disabled, but the audit
    # trail must still be durable.
    assert storage._events_enabled is False
    _seed_two_nodes(storage)
    assert len(storage.get_recent_history()) == 2


def test_node_history_filtering(storage):
    _seed_two_nodes(storage)
    storage.update_node("actor-1", {"description": "changed"})

    actor1 = storage.get_node_history("actor-1")
    assert {e["event_type"] for e in actor1} == {"node.create", "node.update"}
    assert all(e["entity_id"] == "actor-1" for e in actor1)

    actor2 = storage.get_node_history("actor-2")
    assert {e["event_type"] for e in actor2} == {"node.create"}


def test_edge_history_filtering(storage):
    _seed_two_nodes(storage)
    storage.add_edge(Edge(id="edge-1", source="actor-1", target="actor-2",
                          type=RelationshipType.RELATES_TO))
    storage.update_edge("edge-1", {"label": "linked"})

    edge_hist = storage.get_edge_history("edge-1")
    assert {e["event_type"] for e in edge_hist} == {"edge.create", "edge.update"}
    assert all(e["entity_kind"] == "edge" for e in edge_hist)

    # A node query must not pick up an edge with a colliding id (and vice versa).
    assert storage.get_node_history("edge-1") == []


def test_recent_history_is_newest_first(storage):
    for i in range(3):
        storage.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])

    recent = storage.get_recent_history(limit=10)
    ids_in_order = [e["entity_id"] for e in recent]
    assert ids_in_order == ["n-2", "n-1", "n-0"]


def test_pagination_offset_and_limit(storage):
    for i in range(5):
        storage.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])

    page = storage.get_recent_history(limit=2, offset=1)
    # Newest is n-4; skipping 1 -> [n-3, n-2]
    assert [e["entity_id"] for e in page] == ["n-3", "n-2"]


def test_origin_and_attribution_persisted(storage):
    ctx = EventContext(
        event_origin="web-ui",
        event_session_id="sess-123",
        event_correlation_id="corr-abc",
        attribution=EventAttribution(
            actor=EventActorAttribution(actor_id="user-1", actor_type="user"),
        ),
    )
    storage.add_nodes([Node(id="actor-1", type=NodeType.ACTOR, name="A")], [], event_context=ctx)

    entry = storage.get_node_history("actor-1")[0]
    assert entry["event_origin"] == "web-ui"
    assert entry["event_session_id"] == "sess-123"
    assert entry["event_correlation_id"] == "corr-abc"
    assert entry["attribution"]["actor"]["actor_id"] == "user-1"
    assert entry["is_ai_action"] is False


def test_before_after_patch_captured(storage):
    _seed_two_nodes(storage)
    storage.update_node("actor-1", {"description": "new desc"})

    create_entry = storage.get_node_history("actor-1")[-1]
    assert create_entry["before"] is None
    assert create_entry["after"]["name"] == "Actor One"

    update_entry = storage.get_node_history("actor-1")[0]
    assert update_entry["before"]["description"] != "new desc"
    assert update_entry["after"]["description"] == "new desc"
    assert update_entry["patch"]["description"] == "new desc"


def test_ai_action_detected_for_agent_origin(storage):
    ctx = EventContext(event_origin="agent:collector-7")
    storage.add_nodes([Node(id="actor-1", type=NodeType.ACTOR, name="A")], [], event_context=ctx)

    entry = storage.get_node_history("actor-1")[0]
    assert entry["is_ai_action"] is True


def test_derive_is_ai_action_rules():
    assert derive_is_ai_action("agent:x") is True
    assert derive_is_ai_action("mcp") is True
    assert derive_is_ai_action("web-ui") is False
    assert derive_is_ai_action("system") is False
    assert derive_is_ai_action(None) is False
    assert derive_is_ai_action(
        "web-ui",
        EventAttribution(actor=EventActorAttribution(actor_type="agent")),
    ) is True
    assert derive_is_ai_action("web-ui", {"actor": {"actor_type": "ai"}}) is True


def test_history_disabled_for_non_file_backend():
    class InMemoryBackend:
        def exists(self):
            return False

        def load_graph_data(self):
            return {}

        def save_graph_data(self, data):
            pass

        def default_graph_name(self):
            return "in-memory"

    st = GraphStorage(persistence_backend=InMemoryBackend())
    try:
        assert st._history_store is None
        st.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
        assert st.get_recent_history() == []
        assert st.get_node_history("a") == []
    finally:
        st.flush()
