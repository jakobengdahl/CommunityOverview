"""Import embedding commits must not replace a shared store's graph."""

from dataclasses import replace

import pytest

from backend.core import (
    BackendCapabilities,
    Edge,
    EntityRevisionConflict,
    GraphStorage,
    Node,
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
        for op in operations:
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
