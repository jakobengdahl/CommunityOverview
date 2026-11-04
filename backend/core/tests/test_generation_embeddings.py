"""Import embedding commits must not replace a shared store's graph."""

# covers: backend/core/storage.py

from dataclasses import replace
from unittest.mock import Mock

import pytest

from backend.core import (
    BackendCapabilities,
    Edge,
    EntityRevisionConflict,
    GraphStorage,
    GraphWriteOutcomeUnknown,
    Node,
)
from backend.core.storage_backends import (
    EntityOperation,
    ExternalChange,
    GraphGenerationMoved,
    GraphWriteFailed,
    WriteRefusedByStore,
)
from backend.core.tests.persistence_contract import InMemoryGraphPersistenceBackend


@pytest.fixture(autouse=True)
def no_embedding_model(monkeypatch):
    def unavailable(self):
        raise ImportError("ML extras unavailable in this test")

    monkeypatch.setattr(
        "backend.core.vector_store.VectorStore._load_model", unavailable
    )


class _RevisionBackend(InMemoryGraphPersistenceBackend):
    """Node-upsert test double with store-assigned, checked revisions.

    Notifications are withheld to keep the importing instance behind the store.
    """

    def capabilities(self):
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            revision_enforcement=True,
        )

    def apply_batch(self, operations, write_token=None):
        stamped = []
        revisions = []
        stored_generation = self._store["metadata"].get("graph_generation", 0)
        for op in operations:
            if op.expected_generation not in (None, stored_generation):
                raise GraphGenerationMoved(op.expected_generation, stored_generation)
            assert (op.kind, op.action) == ("node", "upsert")
            current = self._store["nodes"].get(op.entity_id)
            revision = None if current is None else current.get("revision", 0)
            if op.expected_revision is not None and revision != op.expected_revision:
                raise EntityRevisionConflict(
                    "node", op.entity_id, op.expected_revision, revision
                )
            self._store["counter"] = self._store.get("counter", 0) + 1
            revision = self._store["counter"]
            revisions.append(revision)
            stamped.append(replace(op, payload={**op.payload, "revision": revision}))
        super().apply_batch(stamped)
        return revisions

    def read_write_evidence(self, keys):
        raise AssertionError("these tests must not need write-failure resolution")


@pytest.fixture(params=["file", "snapshot", "incremental", "revisions"])
def storage(request, tmp_path):
    if request.param == "file":
        graph = GraphStorage(json_path=str(tmp_path / "graph.json"))
    else:
        backend = (
            _RevisionBackend({})
            if request.param == "revisions"
            else InMemoryGraphPersistenceBackend(
                {}, incremental=request.param == "incremental"
            )
        )
        graph = GraphStorage(persistence_backend=backend)
    graph.replace_all_nodes_and_edges(
        [Node(id="a", type="Actor", name="A"), Node(id="b", type="Actor", name="B")],
        [Edge(id="e", source="a", target="b")],
    )
    yield graph
    graph.flush()
    graph.shutdown_events()


@pytest.mark.parametrize("generation", [None, "current"])
def test_committed_embeddings_are_durable_without_moving_generation(
    storage, generation
):
    current = storage.generation
    assert storage.commit_generation_embeddings(
        current if generation == "current" else None, {"a": [1.0, 0.0, 0.0]}
    )

    storage.reload()
    assert storage.vector_store.get_vector_list("a") == [1.0, 0.0, 0.0]
    assert storage.generation == current
    assert set(storage.nodes) == {"a", "b"}
    assert set(storage.edges) == {"e"}


def test_stale_or_empty_embedding_commits_do_not_write(storage, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("a stale or empty embedding commit must not write")

    monkeypatch.setattr(storage, "save", unexpected)
    monkeypatch.setattr(storage, "_commit", unexpected)
    assert not storage.commit_generation_embeddings(
        storage.generation - 1, {"a": [1.0, 0.0, 0.0]}
    )
    assert storage.commit_generation_embeddings(storage.generation, {})
    assert not storage.vector_store.has_embedding("a")


def test_incremental_commit_only_upserts_embedded_live_nodes(storage, monkeypatch):
    if not storage._backend_capabilities.incremental_writes:
        pytest.skip("snapshot backend")
    backend = storage._persistence_backend
    original = storage._do_apply
    calls = []

    def record(operations, *args, **kwargs):
        calls.append(operations)
        return original(operations, *args, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("an incremental embedding commit must not save the graph")

    before = storage.nodes["a"].revision
    untouched = storage.nodes["b"].revision
    monkeypatch.setattr(storage, "_do_apply", record)
    monkeypatch.setattr(backend, "save_graph_data", unexpected)

    assert storage.commit_generation_embeddings(
        storage.generation,
        {"a": [1.0, 0.0, 0.0], "missing": [0.0, 1.0, 0.0]},
    )
    assert len(calls) == 1
    (op,) = calls[0]
    assert (op.kind, op.action, op.entity_id) == ("node", "upsert", "a")
    assert op.expected_revision == before
    assert storage.nodes["a"].revision > before
    assert storage.nodes["b"].revision == untouched
    assert not storage.vector_store.has_embedding("missing")


def test_embedding_commit_preserves_unseen_nodes_edges_and_metadata():
    backend = _RevisionBackend({})
    storage = GraphStorage(persistence_backend=backend)
    try:
        storage.replace_all_nodes_and_edges([Node(id="a", type="Actor", name="A")], [])
        remote = Node(id="remote", type="Actor", name="Remote").to_dict()
        backend._store["nodes"]["remote"] = remote
        edge = Edge(id="remote-edge", source="a", target="remote").to_dict()
        backend._store["edges"]["remote-edge"] = edge
        backend._store["metadata"]["remote"] = "preserved"
        assert "remote" not in storage.nodes

        assert storage.commit_generation_embeddings(
            storage.generation, {"a": [1.0, 0.0]}
        )

        persisted = backend.load_graph_data()
        assert remote in persisted["nodes"]
        assert persisted["edges"] == [edge]
        assert persisted["metadata"]["remote"] == "preserved"
        assert backend._store["nodes"]["a"]["embedding"] == [1.0, 0.0]
    finally:
        storage.shutdown_events()


@pytest.mark.parametrize("deleted", [False, True])
def test_a_stale_embedded_node_refuses_the_batch_and_restores_vectors(storage, deleted):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    storage.vector_store.absorb_embeddings({"a": [0.0, 1.0], "b": [0.0, 1.0]})
    before = {node_id: node.revision for node_id, node in storage.nodes.items()}
    if deleted:
        del backend._store["nodes"]["b"]
    else:
        backend._store["nodes"]["b"].update(name="Remote edit", revision=100)
    stored_before = backend.load_graph_data()

    with pytest.raises(EntityRevisionConflict):
        storage.commit_generation_embeddings(
            storage.generation, {"a": [1.0, 0.0], "b": [1.0, 0.0]}
        )

    assert backend.load_graph_data() == stored_before
    assert not storage._resync_pending
    for node_id in ("a", "b"):
        assert storage.vector_store.get_vector_list(node_id) == [0.0, 1.0]
        assert storage.nodes[node_id].revision == before[node_id]


def _texts(storage):
    return {
        node_id: storage.vector_store.embedding_text(node)
        for node_id, node in storage.nodes.items()
    }


def test_a_merge_vector_lands_only_on_a_vectorless_node_with_the_text_it_was_read_at(
    storage,
):
    storage.add_nodes(
        [
            Node(id="moved", type="Actor", name="Moved"),
            Node(id="embedded", type="Actor", name="Embedded"),
            Node(id="metadata", type="Actor", name="Metadata"),
        ],
        [],
    )
    storage.vector_store.absorb_embeddings({"embedded": [0.0, 1.0]})
    texts = _texts(storage)
    storage.update_node("moved", {"name": "Moved again"})
    storage.update_node("metadata", {"metadata": {"k": "v"}})

    committed = storage.commit_merge_embeddings(
        storage.generation,
        {key: [1.0, 0.0] for key in ("moved", "gone", "embedded", "metadata")},
        texts,
    )

    assert committed == 1
    assert storage.vector_store.get_vector_list("metadata") == [1.0, 0.0]
    assert not storage.vector_store.has_embedding("moved")
    assert not storage.vector_store.has_embedding("gone")
    assert storage.vector_store.get_vector_list("embedded") == [0.0, 1.0]


def test_a_merge_vector_the_store_refuses_is_dropped_and_the_rest_land(storage):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    backend._store["nodes"]["b"].update(name="Remote edit", revision=100)
    remote = dict(backend._store["nodes"]["b"])

    committed = storage.commit_merge_embeddings(
        storage.generation, {"a": [1.0, 0.0], "b": [1.0, 0.0]}, _texts(storage)
    )

    assert committed == 1
    assert backend._store["nodes"]["a"]["embedding"] == [1.0, 0.0]
    assert backend._store["nodes"]["b"] == remote
    assert not storage.vector_store.has_embedding("b")
    assert not storage._resync_pending


def test_a_replace_only_the_store_has_seen_supersedes_a_merge_embedding_commit(storage):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    backend._store["metadata"]["graph_generation"] = storage.generation + 1

    committed = storage.commit_merge_embeddings(
        storage.generation, {"a": [1.0, 0.0]}, _texts(storage)
    )

    assert committed is None
    assert not storage.vector_store.has_embedding("a")
    assert not backend._store["nodes"]["a"].get("embedding")


def test_two_replaces_from_one_generation_store_different_generations(storage):
    if not storage._store_assigns_revisions:
        pytest.skip("requires a store that assigns revisions")
    reserved = iter([70, 80])
    storage._persistence_backend.reserve_generation = lambda: next(reserved)
    backend = storage._persistence_backend

    storage.replace_all_nodes_and_edges([], [])
    assert storage.generation == 70
    assert backend._store["metadata"]["graph_generation"] == 70
    storage._generation = 1
    storage.replace_all_nodes_and_edges([], [])
    assert storage.generation == 80
    assert backend._store["metadata"]["graph_generation"] == 80


@pytest.mark.parametrize("failure", ["refused", "conflict"])
def test_a_failed_merge_vector_commit_leaves_no_regenerated_vector(
    storage, monkeypatch, failure
):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    undo_vector = [9.0, 9.0]
    monkeypatch.setattr(
        storage.vector_store,
        "update_nodes_embeddings",
        lambda nodes: storage.vector_store.absorb_embeddings(
            {node.id: undo_vector for node in nodes}
        ),
    )
    texts = _texts(storage)
    vectors = {"a": [1.0, 0.0], "b": [1.0, 0.0]}

    if failure == "refused":
        monkeypatch.setattr(
            backend,
            "apply_batch",
            Mock(side_effect=WriteRefusedByStore("refused")),
        )
        with pytest.raises(GraphWriteFailed):
            storage.commit_merge_embeddings(storage.generation, vectors, texts)
        assert {node.id for node in storage.nodes_without_embedding()} == {"a", "b"}
        return

    backend._store["nodes"]["b"].update(name="Remote edit", revision=100)
    assert storage.commit_merge_embeddings(storage.generation, vectors, texts) == 1
    assert [node.id for node in storage.nodes_without_embedding()] == ["b"]
    assert storage.vector_store.get_vector_list("a") == [1.0, 0.0]
    assert backend._store["nodes"]["a"]["embedding"] == [1.0, 0.0]


def _listed(storage):
    return {node.id for node in storage.nodes_without_embedding()}


def test_a_vector_generated_for_an_external_update_stays_owed_to_the_store(
    storage, monkeypatch
):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    monkeypatch.setattr(
        storage.vector_store,
        "update_nodes_embeddings",
        lambda nodes: storage.vector_store.absorb_embeddings(
            {node.id: [9.0, 9.0] for node in nodes}
        ),
    )
    texts = _texts(storage)

    def reported(node_id, **fields):
        storage.apply_external_change(
            ExternalChange.entities(
                [
                    EntityOperation.upsert_node(
                        {**backend._store["nodes"][node_id], **fields}
                    )
                ]
            )
        )

    reported("a", metadata={"k": "v"})
    reported("b", metadata={"k": "v"})
    assert _listed(storage) == {"a", "b"}
    reported("b", metadata={"k": "w"}, embedding=[0.0, 1.0])
    assert _listed(storage) == {"a"}

    committed = storage.commit_merge_embeddings(
        storage.generation, {"a": [1.0, 0.0], "b": [1.0, 0.0]}, texts
    )

    assert committed == 1
    assert backend._store["nodes"]["a"]["embedding"] == [1.0, 0.0]
    assert storage.vector_store.get_vector_list("b") == [0.0, 1.0]
    assert _listed(storage) == set()


def test_merge_vectors_of_unknown_outcome_are_committed_again_after_a_failed_reread(
    storage, monkeypatch
):
    if not storage._store_assigns_revisions:
        pytest.skip("requires revision enforcement")
    backend = storage._persistence_backend
    apply_batch = backend.apply_batch
    texts = _texts(storage)
    vectors = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
    monkeypatch.setattr(backend, "apply_batch", Mock(side_effect=RuntimeError("lost")))
    monkeypatch.setattr(storage, "load", Mock(side_effect=RuntimeError("down")))

    with pytest.raises(GraphWriteOutcomeUnknown):
        storage.commit_merge_embeddings(storage.generation, vectors, texts)

    assert _listed(storage) == {"a", "b"}
    monkeypatch.setattr(backend, "apply_batch", apply_batch)
    assert storage.commit_merge_embeddings(storage.generation, vectors, texts) == 2
    assert backend._store["nodes"]["a"]["embedding"] == [1.0, 0.0]
    assert _listed(storage) == set()
