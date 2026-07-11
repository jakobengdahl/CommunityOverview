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
from datetime import datetime, timedelta, timezone
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
from backend.core.history_store import GraphHistoryStore


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


# --- Retention / compaction -------------------------------------------------


def _record(entity_id: str, occurred_at: str) -> dict:
    return {
        "event_id": f"evt-{entity_id}",
        "event_type": "node.create",
        "occurred_at": occurred_at,
        "entity_kind": "node",
        "entity_id": entity_id,
        "entity_type": "Actor",
    }


def test_history_unbounded_by_default(storage):
    # The default fixture sets no retention, so every record is kept.
    for i in range(30):
        storage.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])

    assert len(storage.get_recent_history(limit=1000)) == 30
    lines = storage._history_store.history_path.read_text().splitlines()
    assert len(lines) == 30


def test_max_events_cap_keeps_newest_and_paginates():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "graph.json")
        st = GraphStorage(
            json_path=json_path,
            history_max_events=5,
            history_compaction_interval=1,
        )
        try:
            for i in range(12):
                st.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])

            # Only the newest 5 records survive on disk, in append order.
            on_disk = st._history_store.history_path.read_text().splitlines()
            assert len(on_disk) == 5

            recent = st.get_recent_history(limit=100)
            assert [e["entity_id"] for e in recent] == [
                "n-11", "n-10", "n-9", "n-8", "n-7",
            ]

            # Pagination still works over the trimmed, newest-first view.
            page = st.get_recent_history(limit=2, offset=1)
            assert [e["entity_id"] for e in page] == ["n-10", "n-9"]
        finally:
            st.flush()


def test_compaction_preserves_records_appended_after_trim():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "graph.json")
        st = GraphStorage(
            json_path=json_path,
            history_max_events=3,
            history_compaction_interval=1,
        )
        try:
            for i in range(5):
                st.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])
            # After the first burst only the newest 3 remain.
            assert [e["entity_id"] for e in st.get_recent_history()] == ["n-4", "n-3", "n-2"]

            # Records appended after the trim are not lost; the window slides.
            for i in range(5, 8):
                st.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])
            assert [e["entity_id"] for e in st.get_recent_history()] == ["n-7", "n-6", "n-5"]
        finally:
            st.flush()


def test_max_age_cap_drops_old_records():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"),
            max_age_days=1,
        )
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        fresh = now.isoformat().replace("+00:00", "Z")
        store.append_record(_record("old", old))
        store.append_record(_record("fresh", fresh))

        store.compact()

        remaining = [r["entity_id"] for r in store.get_recent(limit=100)]
        assert remaining == ["fresh"]


def test_atomic_rewrite_never_corrupts_on_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "graph.history.ndjson")
        store = GraphHistoryStore(path, max_events=2, compaction_interval=1000)
        for i in range(5):
            store.append_record(_record(f"n-{i}", f"2026-01-0{i + 1}T00:00:00Z"))

        before = Path(path).read_text()

        import backend.core.history_store as hs

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(hs.os, "rename", boom)
        monkeypatch.setattr(hs.os, "replace", boom)

        with pytest.raises(OSError):
            store.compact()

        # The original sidecar is untouched: every record still readable,
        # and no stray temp file is left behind.
        assert Path(path).read_text() == before
        assert len(store.get_recent(limit=100)) == 5
        leftovers = [p for p in os.listdir(tmpdir) if p.startswith("graph_history_")]
        assert leftovers == []


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
