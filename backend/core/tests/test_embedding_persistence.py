"""
GraphStorage persists node vectors in the sidecar, not in graph.json.

These tests drive the real add/update paths with a deterministic stand-in for
the sentence-transformers encoder, so they exercise the production write path
without the optional ML stack.
"""

import json
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.core.embedding_sidecar import MAGIC, FileEmbeddingSidecar

DIM = 8


class _FakeEncoder:
    """Deterministic encoder: same text always yields the same vector."""

    def encode(self, text):
        if isinstance(text, str):
            return self._vector(text)
        return np.vstack([self._vector(t) for t in text])

    @staticmethod
    def _vector(text: str):
        rng = np.random.default_rng(zlib.crc32(text.encode("utf-8")))
        return rng.random(DIM).astype(np.float32)


class _MemoryBackend:
    """Persistence backend with no sidecar of its own."""

    def __init__(self):
        self.data: Dict[str, Any] = {}

    def exists(self) -> bool:
        return bool(self.data)

    def load_graph_data(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.data))

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        self.data = json.loads(json.dumps(data))

    def default_graph_name(self) -> str:
        return "memory"


def _make_storage(tmpdir, **kwargs) -> GraphStorage:
    storage = GraphStorage(json_path=os.path.join(tmpdir, "graph.json"), **kwargs)
    storage.vector_store.model = _FakeEncoder()
    return storage


def _sample_nodes():
    return [
        Node(id="n1", type=NodeType.ACTOR, name="Statistics Sweden"),
        Node(id="n2", type=NodeType.ACTOR, name="Statistics Norway"),
        Node(id="n3", type=NodeType.INITIATIVE, name="Metadata programme"),
    ]


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def storage(tmpdir_path):
    store = _make_storage(tmpdir_path)
    yield store
    store.flush()


def _graph_json(tmpdir) -> dict:
    with open(os.path.join(tmpdir, "graph.json"), encoding="utf-8") as f:
        return json.load(f)


def _sidecar_path(tmpdir) -> Path:
    return Path(tmpdir) / "graph.embeddings.bin"


def test_graph_json_carries_no_vectors(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    payloads = _graph_json(tmpdir_path)["nodes"]
    assert len(payloads) == 3
    for payload in payloads:
        assert "embedding" not in payload

    vectors = FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()
    assert set(vectors) == {"n1", "n2", "n3"}


def test_vectors_survive_a_reload(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    before = storage.vector_store.export_vectors()

    reloaded = _make_storage(tmpdir_path)
    try:
        after = reloaded.vector_store.export_vectors()
        assert set(after) == set(before)
        for node_id in before:
            np.testing.assert_allclose(after[node_id], before[node_id])
    finally:
        reloaded.flush()


def test_semantic_ranking_is_unchanged_across_a_reload(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    before = storage.vector_store.search(query_text="statistics", limit=3)

    reloaded = _make_storage(tmpdir_path)
    try:
        after = reloaded.vector_store.search(query_text="statistics", limit=3)
    finally:
        reloaded.flush()

    assert [node_id for node_id, _ in after] == [node_id for node_id, _ in before]
    for (_, before_score), (_, after_score) in zip(before, after):
        assert after_score == pytest.approx(before_score, abs=1e-6)


def test_mutation_that_changes_no_vector_does_not_rewrite_the_sidecar(
    storage, tmpdir_path
):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    sidecar = _sidecar_path(tmpdir_path)
    before = sidecar.stat()

    # metadata-only: none of the fields the embedding text is built from.
    storage.update_node("n1", {"metadata": {"reviewed": True}})
    storage.flush()

    after = sidecar.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _graph_json(tmpdir_path)["nodes"][0]["metadata"] == {"reviewed": True}


def test_mutation_that_changes_a_vector_rewrites_the_sidecar(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    sidecar = _sidecar_path(tmpdir_path)
    before_stat = sidecar.stat()
    before_vector = storage.vector_store.export_vectors()["n1"].copy()

    storage.update_node("n1", {"name": "Statistics Sweden (SCB)"})
    storage.flush()

    after_stat = sidecar.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) != (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    persisted = FileEmbeddingSidecar(sidecar).load()["n1"]
    assert not np.allclose(persisted, before_vector)
    np.testing.assert_allclose(persisted, storage.vector_store.export_vectors()["n1"])


def test_deleting_a_node_drops_its_vector_from_the_sidecar(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    storage.delete_nodes(["n2"], confirmed=True)
    storage.flush()

    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {"n1", "n3"}


def test_legacy_inline_vectors_load_and_migrate_on_first_save(tmpdir_path):
    legacy_vector = [0.5] * DIM
    graph = {
        "nodes": [
            {
                "id": "n1",
                "type": "Actor",
                "name": "Legacy node",
                "embedding": legacy_vector,
            }
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        np.testing.assert_allclose(
            storage.vector_store.export_vectors()["n1"], np.float32(legacy_vector)
        )
        # The vector must not stay on the node object as well.
        assert storage.nodes["n1"].embedding is None

        storage.save().result()

        assert "embedding" not in _graph_json(tmpdir_path)["nodes"][0]
        np.testing.assert_allclose(
            FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()["n1"],
            np.float32(legacy_vector),
        )
    finally:
        storage.flush()


def test_sidecar_wins_over_a_stale_inline_vector(tmpdir_path):
    """A graph.json left over from before the split must not resurrect an old
    vector over the sidecar's current one."""
    FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).save({"n1": [1.0] * DIM})
    graph = {
        "nodes": [
            {
                "id": "n1",
                "type": "Actor",
                "name": "Legacy node",
                "embedding": [0.0] * DIM,
            }
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        np.testing.assert_allclose(
            storage.vector_store.export_vectors()["n1"], np.float32([1.0] * DIM)
        )
    finally:
        storage.flush()


def test_missing_sidecar_loads_without_vectors(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    _sidecar_path(tmpdir_path).unlink()

    reloaded = _make_storage(tmpdir_path)
    try:
        assert reloaded.vector_store.get_embedding_count() == 0
        assert len(reloaded.nodes) == 3
    finally:
        reloaded.flush()


def test_unreadable_sidecar_is_ignored_rather_than_failing_the_load(
    storage, tmpdir_path
):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    _sidecar_path(tmpdir_path).write_bytes(b"corrupted beyond recognition")

    reloaded = _make_storage(tmpdir_path)
    try:
        assert reloaded.vector_store.get_embedding_count() == 0
        assert len(reloaded.nodes) == 3
    finally:
        reloaded.flush()


def test_vectors_for_unknown_nodes_are_pruned_on_load(storage, tmpdir_path):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    sidecar = FileEmbeddingSidecar(_sidecar_path(tmpdir_path))
    vectors = sidecar.load()
    vectors["ghost"] = np.zeros(DIM, dtype=np.float32)
    sidecar.save(vectors)

    reloaded = _make_storage(tmpdir_path)
    try:
        assert set(reloaded.vector_store.export_vectors()) == {"n1", "n2", "n3"}
        # The pruning is persisted, not just applied in memory.
        reloaded.save().result()
        assert set(sidecar.load()) == {"n1", "n2", "n3"}
    finally:
        reloaded.flush()


def test_backend_without_a_sidecar_keeps_vectors_in_the_node_payload(tmpdir_path):
    """A custom persistence backend has no sidecar path, so it must keep
    persisting vectors inline rather than silently losing them."""
    backend = _MemoryBackend()
    storage = GraphStorage(persistence_backend=backend)
    storage.vector_store.model = _FakeEncoder()
    try:
        storage.add_nodes(_sample_nodes(), [])
        storage.flush()

        payload = {node["id"]: node for node in backend.data["nodes"]}
        assert len(payload["n1"]["embedding"]) == DIM
        np.testing.assert_allclose(
            np.float32(payload["n1"]["embedding"]),
            storage.vector_store.export_vectors()["n1"],
        )
    finally:
        storage.flush()

    reloaded = GraphStorage(persistence_backend=backend)
    try:
        np.testing.assert_allclose(
            reloaded.vector_store.export_vectors()["n1"],
            storage.vector_store.export_vectors()["n1"],
        )
        assert reloaded.nodes["n1"].embedding is None
    finally:
        reloaded.flush()


def test_explicit_embeddings_path_is_honoured(tmpdir_path):
    custom = os.path.join(tmpdir_path, "vectors", "custom.bin")
    storage = _make_storage(tmpdir_path, embeddings_path=custom)
    try:
        storage.add_nodes(_sample_nodes(), [])
        storage.flush()

        assert os.path.exists(custom)
        assert not _sidecar_path(tmpdir_path).exists()
        assert set(FileEmbeddingSidecar(custom).load()) == {"n1", "n2", "n3"}
    finally:
        storage.flush()


def test_a_missing_graph_file_does_not_destroy_an_existing_sidecar(
    storage, tmpdir_path
):
    """A graph file that is merely absent for now — wrong GRAPH_FILE, a restore
    in progress — must not cost the only copy of the vectors."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    before = FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()
    assert set(before) == {"n1", "n2", "n3"}

    os.unlink(os.path.join(tmpdir_path, "graph.json"))

    rebooted = _make_storage(tmpdir_path)
    try:
        after = FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()
        assert set(after) == set(before)
    finally:
        rebooted.flush()


def test_an_inline_vector_of_another_dimension_does_not_fail_the_load(tmpdir_path):
    """Sidecar and graph file can disagree on dimension after a model change.
    numpy cannot stack that, so the odd one out is dropped, not raised."""
    FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).save(
        {"n1": [1.0] * DIM, "n2": [2.0] * DIM}
    )
    graph = {
        "nodes": [
            {"id": "n1", "type": "Actor", "name": "Covered"},
            {"id": "n2", "type": "Actor", "name": "Also covered"},
            {
                "id": "n3",
                "type": "Actor",
                "name": "Odd one",
                "embedding": [0.5] * (DIM + 4),
            },
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"n1", "n2"}
        assert len(storage.nodes) == 3
    finally:
        storage.flush()


def test_a_sidecar_with_a_non_object_header_degrades_instead_of_failing_the_load(
    storage, tmpdir_path
):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    body = b"null"
    _sidecar_path(tmpdir_path).write_bytes(MAGIC + struct.pack("<I", len(body)) + body)

    reloaded = _make_storage(tmpdir_path)
    try:
        assert reloaded.vector_store.get_embedding_count() == 0
        assert len(reloaded.nodes) == 3
    finally:
        reloaded.flush()


def test_a_caller_supplied_vector_is_persisted_rather_than_dropped(
    storage, tmpdir_path
):
    """`embedding` is an accepted add_nodes field. The serialized payload no
    longer carries it, so the vector store has to adopt it or it lands nowhere.
    No encoder here — the ML-free install is where this bites."""
    storage.vector_store.model = None
    supplied = [0.25] * DIM
    node = Node(id="supplied", type=NodeType.ACTOR, name="Given a vector")
    node.embedding = list(supplied)

    storage.add_nodes([node], [])
    storage.flush()

    assert storage.nodes["supplied"].embedding is None
    np.testing.assert_allclose(
        FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()["supplied"],
        np.float32(supplied),
    )

    reloaded = _make_storage(tmpdir_path)
    try:
        np.testing.assert_allclose(
            reloaded.vector_store.export_vectors()["supplied"], np.float32(supplied)
        )
    finally:
        reloaded.flush()


def test_a_failed_sidecar_write_does_not_fail_the_graph_save_and_is_retried(
    storage, tmpdir_path
):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    _sidecar_path(tmpdir_path).unlink()

    real_save = storage._embedding_sidecar.save

    def failing_save(vectors):
        raise OSError("no space left on device")

    storage._embedding_sidecar.save = failing_save
    storage.update_node("n1", {"name": "Renamed while the disk is full"})
    storage.save().result()

    # The graph still landed, and the vectors are not marked persisted.
    assert len(_graph_json(tmpdir_path)["nodes"]) == 3
    assert not _sidecar_path(tmpdir_path).exists()
    assert storage._persisted_vector_revision != storage.vector_store.revision

    storage._embedding_sidecar.save = real_save
    storage.save().result()

    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
        "n1",
        "n2",
        "n3",
    }


def test_generated_vectors_are_not_left_on_the_node_objects(storage, tmpdir_path):
    """G7 on the fast path: both the batch (add_nodes) and singular
    (update_node) generation routes must leave the node object clean."""
    storage.add_nodes(_sample_nodes(), [])
    for node in storage.nodes.values():
        assert node.embedding is None

    storage.update_node("n1", {"name": "Renamed"})
    storage.flush()

    assert storage.nodes["n1"].embedding is None
