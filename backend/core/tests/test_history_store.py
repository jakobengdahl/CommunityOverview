"""
Tests for durable append-only graph mutation history.

Covers:
- a durable history record is written for each mutation type
- node-specific and edge-specific filtering
- newest-first ordering and offset/limit pagination
- origin + attribution persistence
- AI-action detection derived from origin/attribution
"""

import copy
import json
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
from backend.core.events.models import EntityData, EntityKind, Event, EventType
from backend.core.history_store import GraphHistoryStore, event_to_history_record


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
    storage.add_edge(
        Edge(
            id="edge-1",
            source="actor-1",
            target="actor-2",
            type=RelationshipType.RELATES_TO,
        )
    )
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
    storage.add_edge(
        Edge(
            id="edge-1",
            source="actor-1",
            target="actor-2",
            type=RelationshipType.RELATES_TO,
        )
    )
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
    storage.add_nodes(
        [Node(id="actor-1", type=NodeType.ACTOR, name="A")], [], event_context=ctx
    )

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
    storage.add_nodes(
        [Node(id="actor-1", type=NodeType.ACTOR, name="A")], [], event_context=ctx
    )

    entry = storage.get_node_history("actor-1")[0]
    assert entry["is_ai_action"] is True


def test_derive_is_ai_action_rules():
    assert derive_is_ai_action("agent:x") is True
    assert derive_is_ai_action("mcp") is True
    assert derive_is_ai_action("web-ui") is False
    assert derive_is_ai_action("system") is False
    assert derive_is_ai_action(None) is False
    assert (
        derive_is_ai_action(
            "web-ui",
            EventAttribution(actor=EventActorAttribution(actor_type="agent")),
        )
        is True
    )
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
                "n-11",
                "n-10",
                "n-9",
                "n-8",
                "n-7",
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
            assert [e["entity_id"] for e in st.get_recent_history()] == [
                "n-4",
                "n-3",
                "n-2",
            ]

            # Records appended after the trim are not lost; the window slides.
            for i in range(5, 8):
                st.add_nodes([Node(id=f"n-{i}", type=NodeType.ACTOR, name=f"N{i}")], [])
            assert [e["entity_id"] for e in st.get_recent_history()] == [
                "n-7",
                "n-6",
                "n-5",
            ]
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


@pytest.mark.parametrize("cap", [0, -5])
def test_non_positive_max_events_disables_retention(cap):
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"),
            max_events=cap,
            compaction_interval=1,
        )
        for i in range(6):
            store.append_record(_record(f"n-{i}", f"2026-01-0{i + 1}T00:00:00Z"))
        assert len(store.get_recent(limit=100)) == 6


def test_unparseable_or_missing_timestamp_is_retained():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"),
            max_age_days=1,
        )
        old = (
            (datetime.now(timezone.utc) - timedelta(days=5))
            .isoformat()
            .replace("+00:00", "Z")
        )
        store.append_record(_record("bad-ts", "not-a-date"))
        store.append_record(
            {
                "event_id": "e",
                "entity_id": "no-ts",
                "entity_kind": "node",
                "event_type": "node.create",
            }
        )
        store.append_record(_record("old", old))

        store.compact()

        remaining = {r["entity_id"] for r in store.get_recent(limit=100)}
        # Only the genuinely-old record is dropped; unparseable/missing
        # timestamps are never silently discarded.
        assert remaining == {"bad-ts", "no-ts"}


def test_compaction_failure_does_not_break_append(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "graph.history.ndjson")
        store = GraphHistoryStore(path, max_events=2, compaction_interval=1)

        import backend.core.history_store as hs

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(hs.os, "rename", boom)
        monkeypatch.setattr(hs.os, "replace", boom)

        # Every append triggers a compaction that fails, but the mutation record
        # is already durably written, so appends must still succeed.
        for i in range(4):
            store.append_record(_record(f"n-{i}", f"2026-01-0{i + 1}T00:00:00Z"))

        assert len(store.get_recent(limit=100)) == 4


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


# --- Record trimming -------------------------------------------------------
#
# The history views read exactly two things off a record's snapshots: the
# entity's display name, and the before-value of each field the patch names.
# The helpers below mirror `frontend/web/src/utils/history.js` so the tests can
# assert what a reader actually renders, rather than the field layout it
# happens to render it from.


def _rendered_entity_name(record):
    """Mirror of entityName() in frontend/web/src/utils/history.js."""
    state = record.get("after") or record.get("before") or {}
    return state.get("name") or state.get("label") or record.get("entity_id") or ""


def _rendered_diff(record):
    """Mirror of computeDiff() in frontend/web/src/utils/history.js."""
    before = record.get("before") or {}
    patch = record.get("patch")
    if isinstance(patch, dict) and patch:
        return sorted(
            (field, before.get(field), after) for field, after in patch.items()
        )

    after = record.get("after")
    if record.get("before") and after:
        fields = set(before) | set(after)
        return sorted(
            (f, before.get(f), after.get(f))
            for f in fields
            if before.get(f) != after.get(f)
        )
    return []


BULK = "x" * 250  # under the 300-char cap on summary


def _node_with_bulk(node_id="actor-1", **overrides):
    fields = dict(
        id=node_id,
        type=NodeType.ACTOR,
        name="Actor One",
        description=BULK,
        summary=BULK,
        tags=["alpha", "beta"],
        metadata={"note": BULK},
    )
    fields.update(overrides)
    return Node(**fields)


def test_update_record_keeps_only_patched_fields_and_the_display_name(storage):
    storage.add_nodes([_node_with_bulk()], [])
    storage.update_node("actor-1", {"description": "new desc"})

    update = storage.get_node_history("actor-1")[0]

    # updated_at moves on every update, so it is genuinely part of the patch.
    retained = {"description", "name", "updated_at"}
    assert set(update["before"]) == retained
    assert set(update["after"]) == retained
    # The bulk that did not change is gone from both snapshots.
    assert "summary" not in update["before"]
    assert "metadata" not in update["before"]


def test_trimming_does_not_change_what_a_reader_renders(storage):
    seen = []
    storage.add_system_listener(seen.append)
    storage.add_nodes([_node_with_bulk()], [])
    storage.update_node("actor-1", {"description": "new desc", "tags": ["gamma"]})

    trimmed = storage.get_node_history("actor-1")[0]

    # The same record built WITHOUT the trim, from the event storage emitted:
    # a patch-less copy keeps both snapshots whole, and the real patch is put
    # back so a reader takes the same branch. Built from the trimmed record
    # instead, a key the trim wrongly dropped would be missing from both sides
    # and the comparison would still pass.
    event = copy.deepcopy(seen[-1])
    event.entity.patch = None
    untrimmed = event_to_history_record(event)
    untrimmed["patch"] = trimmed["patch"]
    assert set(untrimmed["before"]) > set(trimmed["before"]), "nothing was trimmed"

    assert _rendered_entity_name(trimmed) == _rendered_entity_name(untrimmed)
    assert _rendered_diff(trimmed) == _rendered_diff(untrimmed)

    # And the diff is the real one, not an empty list agreeing with itself.
    rendered = {
        field: (before, after) for field, before, after in _rendered_diff(trimmed)
    }
    assert rendered["description"] == (BULK, "new desc")
    assert rendered["tags"] == (["alpha", "beta"], ["gamma"])


def test_renamed_node_still_renders_under_its_new_name(storage):
    storage.add_nodes([_node_with_bulk()], [])
    storage.update_node("actor-1", {"name": "Actor Renamed"})

    update = storage.get_node_history("actor-1")[0]

    assert _rendered_entity_name(update) == "Actor Renamed"


def test_update_record_renders_a_name_not_a_raw_id(storage):
    storage.add_nodes([_node_with_bulk()], [])
    storage.update_node("actor-1", {"description": "new desc"})

    update = storage.get_node_history("actor-1")[0]

    assert _rendered_entity_name(update) == "Actor One"
    assert _rendered_entity_name(update) != update["entity_id"]


@pytest.mark.parametrize(
    "payload_key,expected_present",
    # Of the three records (create, update, delete): a create has no `before`,
    # a delete no `after`, and only the update carries a `patch`.
    [("before", 2), ("after", 2), ("patch", 1)],
)
def test_no_history_record_carries_an_embedding(storage, payload_key, expected_present):
    storage.add_nodes([_node_with_bulk(embedding=[0.25] * 8)], [])
    storage.update_node("actor-1", {"description": "new desc"})
    storage.delete_nodes(["actor-1"], confirmed=True)

    records = storage.get_node_history("actor-1")
    assert len(records) == 3

    # A create has no `before` and a delete no `after`, so those combinations
    # have nothing to inspect. Count the ones that do, and require that the
    # parametrisation actually looked at something.
    inspected = 0
    for record in records:
        payload = record.get(payload_key)
        if payload is None:
            continue
        assert isinstance(payload, dict)
        assert "embedding" not in payload
        inspected += 1
    assert inspected == expected_present


def _update_event(before, after, patch):
    return Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(
            kind=EntityKind.NODE,
            id="actor-1",
            type="Actor",
            before=before,
            after=after,
            patch=patch,
        ),
    )


def test_a_vector_in_the_patch_reaches_neither_the_patch_nor_the_snapshots():
    """The retained-key union is patch keys plus display keys.

    A union that did not exclude the embedding would pull a patched vector
    straight back into both snapshots. Storage strips inline vectors off the
    node before it builds the payloads, so this state cannot be reached through
    add_nodes/update_node today — the guarantee is asserted here against the
    record builder itself, which is where it is made.
    """
    vector = [0.5] * 8
    record = event_to_history_record(
        _update_event(
            before={"name": "A", "summary": "s", "embedding": [0.25] * 8},
            after={"name": "A", "summary": "s", "embedding": vector},
            patch={"embedding": vector},
        )
    )

    assert "embedding" not in record["before"]
    assert "embedding" not in record["after"]
    assert "embedding" not in record["patch"]
    assert vector not in record["before"].values()
    assert vector not in record["after"].values()


def test_a_create_payload_loses_its_vector_but_keeps_everything_else():
    record = event_to_history_record(
        Event(
            event_type=EventType.NODE_CREATE,
            origin=EventContext(),
            entity=EntityData(
                kind=EntityKind.NODE,
                id="actor-1",
                type="Actor",
                before=None,
                after={"name": "A", "summary": "s", "embedding": [0.25] * 8},
            ),
        )
    )

    assert record["before"] is None
    assert record["after"] == {"name": "A", "summary": "s"}


def test_create_and_delete_keep_their_whole_snapshot(storage):
    storage.add_nodes([_node_with_bulk()], [])
    storage.delete_nodes(["actor-1"], confirmed=True)

    records = storage.get_node_history("actor-1")
    delete_entry, create_entry = records[0], records[-1]

    assert create_entry["after"]["summary"] == BULK
    assert create_entry["after"]["metadata"] == {"note": BULK}
    assert delete_entry["before"]["summary"] == BULK
    assert delete_entry["before"]["metadata"] == {"note": BULK}


def test_edge_update_keeps_full_snapshots_so_its_diff_still_works(storage):
    _seed_two_nodes(storage)
    edge = Edge(
        id="e1",
        source="actor-1",
        target="actor-2",
        type=RelationshipType.RELATES_TO,
        label="first",
        # Bulk the trim would drop if edges were ever projected. Without it the
        # fixture has nothing outside the display key and would pass either way.
        metadata={"note": BULK},
    )
    storage.add_nodes([], [edge])
    storage.update_edge("e1", {"label": "second"})

    update = storage.get_edge_history("e1")[0]

    # Edge updates carry no patch, so a reader diffs the snapshots instead --
    # which needs both sides whole, bulk included.
    assert not update["patch"]
    assert update["before"]["metadata"] == {"note": BULK}
    assert update["after"]["metadata"] == {"note": BULK}
    assert _rendered_diff(update) == [("label", "first", "second")]


def test_update_record_size_does_not_scale_with_unchanged_bulk(storage):
    """The point of the trim: what a node carries but does not change is free.

    Two nodes differing only in the size of fields the update leaves alone,
    patched identically, must produce update records of the same size.
    """
    bulky = _node_with_bulk("actor-1")
    lean = _node_with_bulk("actor-2", summary="s", metadata={}, description="d")
    storage.add_nodes([bulky, lean], [])

    storage.update_node("actor-1", {"name": "Renamed"})
    storage.update_node("actor-2", {"name": "Renamed"})

    bulky_update = json.dumps(storage.get_node_history("actor-1")[0])
    lean_update = json.dumps(storage.get_node_history("actor-2")[0])

    # The two creates differ by the bulk; the two updates must not.
    bulky_create = json.dumps(storage.get_node_history("actor-1")[-1])
    lean_create = json.dumps(storage.get_node_history("actor-2")[-1])
    assert len(bulky_create) - len(lean_create) > 400

    assert abs(len(bulky_update) - len(lean_update)) < 20


def test_a_patched_entity_keeps_the_label_a_reader_falls_back_to():
    """Display name is `name` for a node and `label` for an edge.

    Edge updates carry no patch today, so this projection is only ever applied
    to nodes — but the retained set describes what a reader uses as a display
    name, and a reader falls back to `label`. Asserted at the record builder,
    which is where the set is applied.
    """
    record = event_to_history_record(
        Event(
            event_type=EventType.EDGE_UPDATE,
            origin=EventContext(),
            entity=EntityData(
                kind=EntityKind.EDGE,
                id="e1",
                type="RELATES_TO",
                before={"label": "the edge", "weight": 1, "bulk": BULK},
                after={"label": "the edge", "weight": 2, "bulk": BULK},
                patch={"weight": 2},
            ),
        )
    )

    # The label is not what changed, so it survives only as a display key.
    assert record["before"] == {"label": "the edge", "weight": 1}
    assert record["after"] == {"label": "the edge", "weight": 2}
    assert _rendered_entity_name(record) == "the edge"
    assert _rendered_entity_name(record) != record["entity_id"]
    assert _rendered_diff(record) == [("weight", 1, 2)]


def _node_shaped_payload(**overrides):
    """A node payload of the shape storage emits, with a real vector put back.

    Storage moves inline vectors to the sidecar before it builds a payload,
    but Node.to_dict() still emits the `embedding` key - 13 keys, the vector
    slot holding None. A real vector is put back here on purpose so the strip
    has something to strip rather than a key to drop.
    """
    payload = _node_with_bulk().to_dict()
    payload["embedding"] = [0.25] * 8
    payload.update(overrides)
    return payload


def _event_payload_cases():
    """Payload shapes that all reach the projection, for the G6 check.

    Each covers a different way the builder could touch the live event:
    - the node-shaped pair carries an `embedding`, so `_without_excluded` has
      to strip it - an in-place pop there would strip it from the event. It
      then hands `_project` a fresh (shallow) copy, so no top-level key
      `_project` deletes in place is visible on this pair - though a nested
      value it altered still would be, since those stay shared;
    - the edge-shaped pair carries no `embedding`, which is the case where
      `_without_excluded` hands back the caller's own dict, so a `_project`
      that deleted in place would delete from the live event;
    - the large-no-embedding pair is the same case at a size a threshold-
      conditioned in-place narrowing would fire on, which the small edge pair
      is not;
    - the real-shape-no-embedding-key pair is a node payload as storage emits
      it with the `embedding` key removed outright, so `_project` receives
      the caller's own dict while the payload still has storage's real shape
      (a nested dict, a list, the real field names) and a three-key patch
      like a real update's. A narrowing gated on any of those fires here.
    All carry a field the patch does not name, so the projection has something
    to drop, and no patch is emptied by the embedding strip.
    """
    large = {f"field_{i}": BULK for i in range(12)}
    real_before = _node_with_bulk().to_dict()
    real_before.pop("embedding")
    real_after = dict(real_before, description="new desc", summary="s2")
    real_after["updated_at"] = "2026-09-05T00:00:00+00:00"
    return [
        pytest.param(
            _node_shaped_payload(),
            _node_shaped_payload(description="new desc", embedding=[0.5] * 8),
            {"description": "new desc"},
            id="node-shaped-with-vector",
        ),
        pytest.param(
            {"label": "the edge", "weight": 1, "note": BULK},
            {"label": "the edge", "weight": 2, "note": BULK},
            {"weight": 2},
            id="edge-shaped-no-embedding",
        ),
        pytest.param(
            {"name": "A", "weight": 1, "kind": "x", **large},
            {"name": "A", "weight": 2, "kind": "y", **large},
            # Two keys, so a narrowing conditioned on a multi-key patch (what
            # every real update carries) fires on the one case where _project
            # receives the caller's own dict.
            {"weight": 2, "kind": "y"},
            id="large-no-embedding",
        ),
        pytest.param(
            real_before,
            real_after,
            {
                "description": "new desc",
                "summary": "s2",
                "updated_at": "2026-09-05T00:00:00+00:00",
            },
            id="real-shape-no-embedding-key",
        ),
    ]


@pytest.mark.parametrize("before,after,patch", _event_payload_cases())
def test_building_a_record_does_not_alter_the_event_itself(before, after, patch):
    """History trims its own copy; subscribers still get the whole event.

    `append_event` builds the trimmed record while `dispatch(event)` hands the
    same Event to webhook subscribers and system listeners, so anything the
    record builder does in place is visible to them. Two helpers can return the
    caller's own dict rather than a copy, which is what makes an in-place strip
    or an in-place narrowing easy to write and invisible from the record alone.

    The payloads are parametrised to actually reach the projection: a patch that
    the embedding strip empties would skip `_project` entirely and assert
    nothing about it.
    """
    event = _update_event(before=before, after=after, patch=patch)
    untouched = copy.deepcopy(event.entity)

    record = event_to_history_record(event)

    # The record is trimmed: the patch's keys, plus whichever display keys
    # this payload actually has.
    assert set(record["before"]) == frozenset(patch) | ({"name", "label"} & set(before))
    assert "embedding" not in record["after"]

    # ...and the event the dispatcher will send is not.
    assert event.entity.before == untouched.before
    assert event.entity.after == untouched.after
    assert event.entity.patch == untouched.patch

    sent = event.to_webhook_payload()["entity"]["data"]
    assert sent["before"] == untouched.before
    assert sent["after"] == untouched.after
    assert sent["patch"] == untouched.patch


@pytest.mark.parametrize("node_type", [NodeType.ACTOR, NodeType.INITIATIVE])
def test_subscribers_get_the_whole_event_on_a_real_update(storage, node_type):
    """The builder-level check above chooses its own payloads; this one takes
    whatever storage actually emits, which is where the two diverged before.
    A real node update always patches at least two keys (updated_at moves
    every time), the entity type is whatever the graph holds, and the payload
    carries nested structure - a dict-valued field and a list.

    What this can see: a reassignment onto event.entity, anything
    _without_excluded does in place, and any change to a NESTED value by
    either helper - the copy _without_excluded makes is shallow, so nested
    dicts and lists stay shared with the live event. What it cannot see: a
    top-level key _project deletes in place - the real payload carries the
    `embedding` key, so _without_excluded hands _project a fresh copy here.
    That case is covered by the builder-level cases that carry no
    `embedding` key, whose patches have more than one key for exactly that
    reason.

    The listener runs AFTER the history record is built, so it sees the event
    as the builder left it.
    """
    seen = []
    storage.add_system_listener(seen.append)
    nested = {"note": BULK, "embedding": [0.25] * 8}
    storage.add_nodes(
        [
            _node_with_bulk(
                type=node_type, metadata=nested, tags=[f"t{i}" for i in range(80)]
            )
        ],
        [],
    )
    storage.update_node("actor-1", {"description": "new desc"})

    event = seen[-1]
    assert event.event_type.value == "node.update"
    assert len(event.entity.patch) >= 2, "a real update patches updated_at too"

    for payload in (event.entity.before, event.entity.after):
        assert payload["summary"] == BULK, "the live event was narrowed"
        assert payload["metadata"] == nested, "a nested dict was altered in place"
        assert len(payload["tags"]) == 80, "a nested list was truncated in place"
        # The key is always present on a node payload; the builder drops it
        # from the record, and must not drop it from the event.
        assert "embedding" in payload
    assert event.entity.after["description"] == "new desc"
    assert event.entity.before["description"] == BULK

    # And the record that was written from the same event IS trimmed.
    record = storage.get_node_history("actor-1")[0]
    assert "summary" not in record["before"]
    assert "embedding" not in record["after"]


def test_an_empty_patch_is_no_patch_and_keeps_both_snapshots_whole():
    """`{}` means nothing changed, not "everything was dropped".

    Storage cannot currently produce it — a node update always bumps
    `updated_at` — so this is asserted against the record builder, where the
    distinction is made.
    """
    whole = {"name": "A", "weight": 1, "summary": BULK}

    record = event_to_history_record(_update_event(before=whole, after=whole, patch={}))

    assert record["before"] == whole
    assert record["after"] == whole


def test_an_absent_snapshot_stays_absent_when_a_patch_is_present():
    record = event_to_history_record(
        _update_event(before=None, after={"name": "A", "x": 1}, patch={"x": 1})
    )

    assert record["before"] is None
    assert record["after"] == {"name": "A", "x": 1}


# --- History streaming residue (PR #544) ------------------------------------


def test_a_record_exactly_on_the_age_cutoff_is_kept(monkeypatch):
    """Retention keeps `ts >= cutoff`. Every age fixture was whole days from a
    live now(), so equality never arose and `>` passed them all."""
    import backend.core.history_store as hs

    frozen = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(hs, "datetime", _Frozen)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"), max_age_days=2
        )
        on_cutoff = (frozen - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        just_past = (
            (frozen - timedelta(days=2, microseconds=1))
            .isoformat()
            .replace("+00:00", "Z")
        )
        store.append_record(_record("on-cutoff", on_cutoff))
        store.append_record(_record("just-past", just_past))

        store.compact()

        remaining = [r["entity_id"] for r in store.get_recent(limit=100)]
        assert remaining == ["on-cutoff"]


def _counting_loads(monkeypatch):
    import backend.core.history_store as hs

    calls = []
    real = hs.json.loads

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hs.json, "loads", counted)
    return calls


def _fill(store, n, entity="n"):
    for i in range(n):
        store.append_record(_record(f"{entity}-{i}", f"2026-01-01T00:00:{i % 60:02d}Z"))


def test_a_page_parses_a_page_not_the_history(monkeypatch):
    """The cost guarantee, measured as work rather than as allocation. A
    generator that parses every record and discards it keeps peak memory flat,
    so the allocation test passes without the early stop that makes a query
    proportional to the page."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(os.path.join(tmpdir, "graph.history.ndjson"))
        _fill(store, 4000)
        calls = _counting_loads(monkeypatch)

        page = store.get_recent(limit=50)

        assert len(page) == 50
        assert len(calls) < 200, f"parsed {len(calls)} records to answer a page of 50"


def test_entity_history_parses_no_more_than_it_has_to(monkeypatch):
    """The entity path had no cost bound at all. Its matches sit at the end of
    the file, so a reader that stops at the page parses about a page; one that
    keeps going parses everything before them too."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(os.path.join(tmpdir, "graph.history.ndjson"))
        _fill(store, 3000, entity="other")
        for i in range(60):
            store.append_record(_record("wanted", f"2026-02-01T00:00:{i % 60:02d}Z"))
        calls = _counting_loads(monkeypatch)

        page = store.get_entity_history("wanted", limit=10)

        assert len(page) == 10
        assert len(calls) < 100, f"parsed {len(calls)} records to find 10 matches"


def _recording_locks(monkeypatch):
    """Record every file lock the store takes, as (name-of-file, exclusive).

    The compaction temp file is opened with os.fdopen, so its `.name` is the
    bare descriptor rather than a path; it is recorded as "<temp>", which is
    also the only way to tell it apart from the sidecar it replaces.
    """
    import backend.core.history_store as hs

    taken = []
    real_lock, real_unlock = hs._lock_file, hs._unlock_file

    def lock(f, exclusive):
        name = "<temp>" if isinstance(f.name, int) else os.path.basename(f.name)
        taken.append((name, exclusive))
        return real_lock(f, exclusive=exclusive)

    monkeypatch.setattr(hs, "_lock_file", lock)
    monkeypatch.setattr(hs, "_unlock_file", real_unlock)
    return taken


def test_an_append_takes_an_exclusive_file_lock(monkeypatch):
    """The in-process lock serialises everything one process does, so a suite
    of one process cannot see the OS lock go missing. Two instances appending
    to the same sidecar would then interleave a record."""
    taken = _recording_locks(monkeypatch)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(os.path.join(tmpdir, "graph.history.ndjson"))

        store.append_record(_record("n1", "2026-01-01T00:00:00Z"))

    assert ("graph.history.ndjson", True) in taken


def test_reads_take_shared_locks_and_the_rewrite_an_exclusive_one(monkeypatch):
    """Readers must not exclude each other, and the compaction's temp file
    must be held exclusively while it is written."""
    taken = _recording_locks(monkeypatch)
    with tempfile.TemporaryDirectory() as tmpdir:
        # A long interval so the appends do not compact by themselves and the
        # explicit compact() below is the rewrite being observed.
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"),
            max_events=2,
            compaction_interval=1000,
        )
        _fill(store, 5)
        taken.clear()

        store.get_recent(limit=2)
        store.get_entity_history("n-1", limit=1)
        store.compact()

    reads = [ex for name, ex in taken if name == "graph.history.ndjson"]
    assert reads and not any(reads), "a read path took an exclusive lock"
    temp = [ex for name, ex in taken if name == "<temp>"]
    assert temp == [True], "the rewrite did not hold its temp file exclusively"


def test_the_rewrite_syncs_its_temp_file_before_renaming_it(monkeypatch):
    """A rename can land while the temp file's contents have not, so a crash
    between them leaves a short sidecar. The only portable proxy is the order
    of the two calls."""
    import backend.core.history_store as hs

    order = []
    real_fsync, real_rename = hs.os.fsync, hs.os.rename

    def fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def rename(src, dst):
        order.append("rename")
        return real_rename(src, dst)

    monkeypatch.setattr(hs.os, "fsync", fsync)
    monkeypatch.setattr(hs.os, "rename", rename)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = GraphHistoryStore(
            os.path.join(tmpdir, "graph.history.ndjson"),
            max_events=2,
            compaction_interval=1000,
        )
        _fill(store, 5)
        order.clear()

        store.compact()

    assert "rename" in order, "no rewrite happened"
    assert "fsync" in order, "the temp file was renamed without being synced"
    assert order.index("fsync") < order.index("rename")
