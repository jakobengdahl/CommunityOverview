"""GraphStorage.apply_import_merge (docs/adr/0013-graph-import-merge-mode.md §4).

The commit's locked unit, held to the same clauses on the file backend (the
default, one journal record per batch) and on the in-memory reference
backend; a store that assigns revisions (PostgreSQL mode) is refused until
slice 3. The plan and digest that produce the writes are the service's
(backend/service/tests/test_import_merge_commit.py).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from backend.core import (
    BackendCapabilities,
    Edge,
    EntityRevisionConflict,
    GraphStorage,
    GraphWriteOutcomeUnknown,
    Node,
)
from backend.core.events.models import EventContext, EventType
from backend.core.storage_merge import (
    EdgeOverwrite,
    ImportMergeUnsupported,
    MergeWrites,
    NodeOverwrite,
)
from backend.core.tests.persistence_contract import InMemoryGraphPersistenceBackend


@pytest.fixture(params=["file", "memory"])
def make_storage(request, tmp_path):
    made: List[GraphStorage] = []
    store: Dict[str, Any] = {}

    def make() -> GraphStorage:
        if request.param == "file":
            storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
        else:
            storage = GraphStorage(
                persistence_backend=InMemoryGraphPersistenceBackend(store)
            )
        made.append(storage)
        return storage

    make.kind = request.param
    yield make
    for storage in made:
        storage.flush()
        storage.shutdown_events()


def _seed(storage: GraphStorage) -> None:
    storage.add_nodes(
        [
            Node(id="a1", type="Actor", name="Alice"),
            Node(id="a2", type="Actor", name="Bob"),
            Node(id="i1", type="Initiative", name="Project X"),
        ],
        [Edge(id="e1", source="a1", target="i1", type="BELONGS_TO")],
    )


def _events(storage: GraphStorage) -> List[Any]:
    seen: List[Any] = []
    storage.add_system_listener(seen.append)
    return seen


def _state(storage: GraphStorage) -> Dict[str, Any]:
    return {
        "nodes": {k: v.to_dict() for k, v in storage.nodes.items()},
        "edges": {k: v.to_dict() for k, v in storage.edges.items()},
        "graph_edges": sorted(storage.graph.edges(keys=True)),
        "vectors": {
            k: storage.vector_store.get_vector_list(k) for k in sorted(storage.nodes)
        },
        "generation": storage.generation,
    }


def _vector(storage: GraphStorage, node_id: str, value: float) -> None:
    storage.vector_store.absorb_embeddings({node_id: [value, 0.0, 1.0]})


def _writes(storage: GraphStorage, **overrides: Any) -> MergeWrites:
    writes = MergeWrites(
        node_creates=[Node(id="n1", type="Actor", name="New")],
        node_overwrites=[
            NodeOverwrite(
                node_id="a1",
                expected_revision=storage.nodes["a1"].revision,
                fields={"name": "Alice B", "description": "changed"},
                drop_vector=True,
            ),
            NodeOverwrite(
                node_id="a2",
                expected_revision=storage.nodes["a2"].revision,
                fields={"metadata": {"k": "v"}},
                drop_vector=False,
            ),
        ],
        edge_creates=[Edge(id="e2", source="n1", target="i1", type="BELONGS_TO")],
        edge_overwrites=[
            EdgeOverwrite(
                edge_id="e1",
                expected_revision=storage.edges["e1"].revision,
                fields={"label": "relabelled"},
            )
        ],
    )
    for name, value in overrides.items():
        setattr(writes, name, value)
    return writes


def test_a_merge_writes_its_entities_with_fresh_revisions_and_leaves_the_rest(
    make_storage,
):
    storage = make_storage()
    _seed(storage)
    storage.add_nodes([Node(id="u1", type="Actor", name="Untouched")], [])
    untouched = storage.nodes["u1"].revision
    held = max(n.revision for n in storage.nodes.values())
    generation = storage.generation

    applied = storage.apply_import_merge(lambda: _writes(storage))

    assert storage.nodes["a1"].name == "Alice B"
    assert storage.nodes["a2"].metadata == {"k": "v"}
    assert storage.edges["e1"].label == "relabelled"
    assert storage.graph.has_edge("n1", "i1", key="e2")
    written = [("node", "n1"), ("node", "a1"), ("node", "a2")]
    written += [("edge", "e2"), ("edge", "e1")]
    assert set(applied.revisions) == set(written)
    for (kind, entity_id), revision in applied.revisions.items():
        entity = (storage.nodes if kind == "node" else storage.edges)[entity_id]
        assert entity.revision == revision > held
    assert storage.nodes["u1"].revision == untouched
    assert storage.generation == generation


def test_a_merge_survives_a_restart(make_storage):
    storage = make_storage()
    _seed(storage)
    storage.apply_import_merge(lambda: _writes(storage))
    expected = _state(storage)
    storage.flush()

    reopened = make_storage()
    reopened.load()
    assert {k: v for k, v in _state(reopened).items() if k != "vectors"} == {
        k: v for k, v in expected.items() if k != "vectors"
    }


def test_the_file_backend_takes_a_merge_as_one_journal_record(make_storage):
    if make_storage.kind != "file":
        pytest.skip("journal records are the file backend's")
    storage = make_storage()
    _seed(storage)
    storage.flush()
    journal = storage.json_path.parent / "graph.journal.ndjson"
    before = journal.read_text().splitlines() if journal.exists() else []

    storage.apply_import_merge(lambda: _writes(storage))
    storage._last_write.result()

    after = journal.read_text().splitlines()
    assert len(after) == len(before) + 1
    ops = json.loads(after[-1])["ops"]
    assert [(op["kind"], op["entity_id"]) for op in ops] == [
        ("node", "n1"),
        ("node", "a1"),
        ("node", "a2"),
        ("edge", "e2"),
        ("edge", "e1"),
    ]


def test_one_ordinary_event_fires_per_written_entity_with_the_callers_context(
    make_storage,
):
    storage = make_storage()
    _seed(storage)
    seen = _events(storage)
    context = EventContext(event_origin="agent:x", event_correlation_id="corr-1")

    storage.apply_import_merge(lambda: _writes(storage), event_context=context)

    kinds = [(e.event_type, e.entity.id) for e in seen]
    assert kinds == [
        (EventType.NODE_CREATE, "n1"),
        (EventType.NODE_UPDATE, "a1"),
        (EventType.NODE_UPDATE, "a2"),
        (EventType.EDGE_CREATE, "e2"),
        (EventType.EDGE_UPDATE, "e1"),
    ]
    assert all(e.origin.event_origin == "agent:x" for e in seen)
    assert all(e.origin.event_correlation_id == "corr-1" for e in seen)
    update = seen[1]
    assert update.entity.before["name"] == "Alice"
    assert update.entity.after["name"] == "Alice B"


def test_an_overwrite_takes_only_the_fields_it_names(make_storage):
    storage = make_storage()
    _seed(storage)
    storage.set_nodes_archived(["a1"], True)
    before = storage.nodes["a1"].to_dict()

    storage.apply_import_merge(lambda: _writes(storage))

    after = storage.nodes["a1"].to_dict()
    assert after["archived"] is True
    assert after["type"] == before["type"]
    assert after["created_at"] == before["created_at"]
    assert after["tags"] == before["tags"]


def test_vectors_drop_for_created_and_rewritten_text_and_stay_for_the_rest(
    make_storage,
):
    storage = make_storage()
    _seed(storage)
    _vector(storage, "a1", 1.0)
    _vector(storage, "a2", 2.0)
    _vector(storage, "i1", 3.0)
    # A stray vector under the id a create takes must not describe it.
    _vector(storage, "n1", 4.0)

    storage.apply_import_merge(lambda: _writes(storage))

    assert not storage.vector_store.has_embedding("a1")
    assert not storage.vector_store.has_embedding("n1")
    assert storage.vector_store.has_embedding("a2")
    assert storage.vector_store.has_embedding("i1")


def test_the_vector_drop_is_durable_on_the_file_backend(make_storage):
    if make_storage.kind != "file":
        pytest.skip("the embedding sidecar is the file backend's")
    storage = make_storage()
    _seed(storage)
    _vector(storage, "a1", 1.0)
    _vector(storage, "a2", 2.0)
    storage.save().result()

    storage.apply_import_merge(lambda: _writes(storage))
    storage.flush()

    reopened = make_storage()
    reopened.load()
    assert not reopened.vector_store.has_embedding("a1")
    assert reopened.vector_store.has_embedding("a2")


@pytest.mark.parametrize(
    "spoil",
    [
        "create_taken",
        "node_revision_moved",
        "edge_revision_moved",
        "edge_create_taken",
    ],
)
def test_a_refused_expectation_changes_nothing_and_fires_nothing(make_storage, spoil):
    storage = make_storage()
    _seed(storage)
    storage.add_nodes([Node(id="taken", type="Actor", name="Taken")], [])
    _vector(storage, "a1", 1.0)
    writes = _writes(storage)
    if spoil == "create_taken":
        writes.node_creates.append(Node(id="taken", type="Actor", name="Dup"))
    elif spoil == "node_revision_moved":
        writes.node_overwrites[1] = NodeOverwrite("a2", 999_999, {"name": "x"}, False)
    elif spoil == "edge_revision_moved":
        writes.edge_overwrites[0] = EdgeOverwrite("e1", 999_999, {"label": "x"})
    else:
        writes.edge_creates.append(
            Edge(id="e1", source="a1", target="i1", type="BELONGS_TO")
        )
    before = _state(storage)
    seen = _events(storage)

    with pytest.raises(EntityRevisionConflict):
        storage.apply_import_merge(lambda: writes)

    assert _state(storage) == before
    assert seen == []


def test_a_failure_while_applying_puts_memory_and_vectors_back(make_storage):
    storage = make_storage()
    _seed(storage)
    _vector(storage, "a1", 1.0)
    writes = _writes(storage)
    # Its endpoint exists nowhere: refused after the creates and overwrites
    # above it are already in memory.
    writes.edge_creates.append(
        Edge(id="e9", source="ghost", target="i1", type="BELONGS_TO")
    )
    before = _state(storage)
    seen = _events(storage)

    with pytest.raises(ValueError, match="missing node"):
        storage.apply_import_merge(lambda: writes)

    assert _state(storage) == before
    assert not storage.graph.has_node("ghost")
    assert seen == []


def test_a_failed_revision_reservation_undoes_the_merge(make_storage, monkeypatch):
    storage = make_storage()
    _seed(storage)
    _vector(storage, "a1", 1.0)
    before = _state(storage)
    seen = _events(storage)

    def _no_reservation(count):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_take_revisions", _no_reservation)
    from backend.core import GraphWriteFailed

    with pytest.raises(GraphWriteFailed):
        storage.apply_import_merge(lambda: _writes(storage))

    assert _state(storage) == before
    assert seen == []


def test_prepare_runs_under_the_lock_and_a_refusal_there_changes_nothing(
    make_storage,
):
    storage = make_storage()
    _seed(storage)
    before = _state(storage)

    class Refused(Exception):
        pass

    def prepare():
        # Held by this thread: no other writer can move the graph between
        # the caller's recomputed plan and the writes it returns.
        assert storage._lock._is_owned()
        raise Refused()

    with pytest.raises(Refused):
        storage.apply_import_merge(prepare)
    assert _state(storage) == before


def test_a_batch_lost_after_hand_over_is_an_unknown_outcome_without_events(
    make_storage, monkeypatch
):
    if make_storage.kind != "file":
        pytest.skip("the single-writer re-save path is exercised on the file backend")
    storage = make_storage()
    _seed(storage)
    storage.flush()
    backend = storage._persistence_backend
    seen = _events(storage)

    def _fail(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(backend, "apply_batch", _fail)
    monkeypatch.setattr(backend, "save_graph_data", _fail)

    with pytest.raises(GraphWriteOutcomeUnknown):
        storage.apply_import_merge(lambda: _writes(storage))

    # Memory keeps the merge (ADR §4 step 5); nothing announced it.
    assert storage.nodes["a1"].name == "Alice B"
    assert "n1" in storage.nodes
    assert seen == []
    monkeypatch.undo()


class _EnforcingBackend(InMemoryGraphPersistenceBackend):
    """Declares what PostgreSQL mode declares: the store assigns revisions."""

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
            revision_enforcement=True,
        )

    def read_write_evidence(self, keys):
        return {}


def test_a_store_that_assigns_revisions_refuses_a_merge_before_planning():
    storage = GraphStorage(persistence_backend=_EnforcingBackend({}))
    assert storage.supports_import_merge is False
    called = []

    with pytest.raises(ImportMergeUnsupported):
        storage.apply_import_merge(lambda: called.append(True) or MergeWrites())
    assert called == []
    storage.shutdown_events()
