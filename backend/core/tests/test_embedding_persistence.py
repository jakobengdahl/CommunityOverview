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

from backend.core import Edge, GraphStorage, Node, NodeType, RelationshipType
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

        # One save does both: the sidecar write lands before the node payloads
        # are serialised, so the inline copy is dropped in the same save rather
        # than surviving until some later one.
        np.testing.assert_allclose(
            FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()["n1"],
            np.float32(legacy_vector),
        )
        assert "embedding" not in _graph_json(tmpdir_path)["nodes"][0]
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

        # The drop is persisted, not just applied in memory.
        storage.save().result()
        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
            "n1",
            "n2",
        }
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


def test_every_inline_vector_is_adopted_not_just_the_first(tmpdir_path):
    """The migration is a loop; a single-node fixture would never exercise it."""
    graph = {
        "nodes": [
            {
                "id": f"n{i}",
                "type": "Actor",
                "name": f"Legacy {i}",
                "embedding": [float(i)] * DIM,
            }
            for i in range(4)
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"n0", "n1", "n2", "n3"}
        for node in storage.nodes.values():
            assert node.embedding is None

        storage.save().result()
        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
            "n0",
            "n1",
            "n2",
            "n3",
        }
    finally:
        storage.flush()


def test_a_supplied_vector_does_not_evict_the_vectors_already_indexed(
    storage, tmpdir_path
):
    """Adoption merges into the existing index; it must never replace it."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    storage.vector_store.model = None
    node = Node(id="supplied", type=NodeType.ACTOR, name="Given a vector")
    node.embedding = [0.25] * DIM
    storage.add_nodes([node], [])
    storage.flush()

    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
        "n1",
        "n2",
        "n3",
        "supplied",
    }


def test_a_supplied_vector_of_another_dimension_is_refused_not_honoured(
    storage, tmpdir_path
):
    """The index in memory anchors the dimension. The odd batch is deliberately
    LARGER than the existing index (5 vs 3): with equal counts a majority vote
    and the anchor agree, so only an outnumbering batch tells them apart."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()
    before = FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()

    storage.vector_store.model = None
    odd = []
    for i in range(5):
        node = Node(id=f"odd{i}", type=NodeType.ACTOR, name=f"Odd {i}")
        node.embedding = [0.5] * (DIM + 4)
        odd.append(node)

    result = storage.add_nodes(odd, [])
    storage.flush()

    assert result.success
    assert set(storage.vector_store.export_vectors()) == set(before)
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == set(before)
    # The nodes themselves are still added; only their vectors are refused.
    assert len(storage.nodes) == 8


def test_a_stale_graph_file_cannot_outvote_the_sidecar_on_dimension(tmpdir_path):
    """The sidecar is authoritative. A graph file carrying MORE inline vectors
    of another dimension must not evict the sidecar's."""
    FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).save({"n0": [1.0] * DIM})
    graph = {
        "nodes": [{"id": "n0", "type": "Actor", "name": "Covered"}]
        + [
            {
                "id": f"stale{i}",
                "type": "Actor",
                "name": f"Stale {i}",
                "embedding": [0.5] * (DIM + 4),
            }
            for i in range(5)
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"n0"}
    finally:
        storage.flush()


def test_one_add_nodes_call_writes_the_sidecar_once(storage, tmpdir_path):
    """add_nodes saves twice — once for the nodes, once for the edges — and the
    edge save changes no vector, so it must not resnapshot the same matrix."""
    calls = []
    real_save = storage._embedding_sidecar.save

    def counting_save(vectors):
        calls.append(sorted(vectors))
        return real_save(vectors)

    storage._embedding_sidecar.save = counting_save
    storage.add_nodes(
        _sample_nodes(),
        [Edge(id="e1", source="n1", target="n2", type=RelationshipType.RELATES_TO)],
    )
    storage.flush()

    assert len(calls) == 1, f"sidecar rewritten {len(calls)} times: {calls}"
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
        "n1",
        "n2",
        "n3",
    }


def test_a_zero_length_inline_vector_does_not_empty_the_whole_sidecar(tmpdir_path):
    """dim 0 would make the sidecar read back as no vectors at all.

    The empty one is listed FIRST on purpose: that is the ordering in which an
    unfiltered zero-length vector becomes the dimension every other vector is
    then measured against, taking all of them down with it.
    """
    graph = {
        "nodes": [
            {"id": "empty", "type": "Actor", "name": "Empty", "embedding": []},
            {"id": "good", "type": "Actor", "name": "Good", "embedding": [1.0] * DIM},
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"good"}
        assert storage.nodes["empty"].embedding is None
        storage.save().result()
        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {"good"}
    finally:
        storage.flush()


def test_a_non_file_backend_with_mixed_inline_dimensions_still_loads(tmpdir_path):
    backend = _MemoryBackend()
    backend.data = {
        # Odd one FIRST: first-seen and majority then disagree, so this
        # distinguishes "most common" from "whichever turned up first".
        "nodes": [
            {"id": "c", "type": "Actor", "name": "C", "embedding": [1.0] * (DIM + 4)},
            {"id": "a", "type": "Actor", "name": "A", "embedding": [1.0] * DIM},
            {"id": "b", "type": "Actor", "name": "B", "embedding": [1.0] * DIM},
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "memory"},
    }

    storage = GraphStorage(persistence_backend=backend)
    try:
        assert set(storage.vector_store.export_vectors()) == {"a", "b"}
        assert len(storage.nodes) == 3
    finally:
        storage.flush()


def test_a_tie_on_dimension_keeps_the_one_seen_first(tmpdir_path):
    """An exact split must not be settled by width: one stray wide vector
    would then outrank an equally common correct one."""
    graph = {
        "nodes": [
            {"id": "a", "type": "Actor", "name": "A", "embedding": [1.0] * DIM},
            {"id": "b", "type": "Actor", "name": "B", "embedding": [1.0] * DIM},
            {"id": "c", "type": "Actor", "name": "C", "embedding": [1.0] * (DIM + 4)},
            {"id": "d", "type": "Actor", "name": "D", "embedding": [1.0] * (DIM + 4)},
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"a", "b"}
    finally:
        storage.flush()


def test_refusing_every_supplied_vector_writes_no_sidecar_at_all(storage, tmpdir_path):
    """A refused batch changes nothing, so it must not bump the revision and
    trigger a rewrite of the whole matrix."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    calls = []
    real_save = storage._embedding_sidecar.save
    storage._embedding_sidecar.save = lambda v: (calls.append(sorted(v)), real_save(v))[
        1
    ]

    storage.vector_store.model = None
    odd = []
    for i in range(5):
        node = Node(id=f"odd{i}", type=NodeType.ACTOR, name=f"Odd {i}")
        node.embedding = [0.5] * (DIM + 4)
        odd.append(node)
    storage.add_nodes(odd, [])
    storage.flush()

    assert calls == [], f"sidecar rewritten despite nothing changing: {calls}"


def test_a_vector_added_while_a_write_is_in_flight_is_still_persisted(
    storage, tmpdir_path
):
    """The revision recorded as persisted must be the one actually written, not
    whatever the live index has reached — otherwise a change landing during a
    write is skipped by every later save and lost for good."""
    import threading

    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    release = threading.Event()
    entered = threading.Event()
    real_save = storage._embedding_sidecar.save

    def blocking_save(vectors):
        entered.set()
        release.wait(timeout=5)
        return real_save(vectors)

    storage._embedding_sidecar.save = blocking_save
    storage.update_node("n1", {"name": "Changed once"})
    assert entered.wait(timeout=5)

    # The index moves while that write is still in flight and BEFORE any save
    # has been queued for it — the window between generating a vector and the
    # save() call that follows it. Queueing a save here instead would hide the
    # bug, because the queued write carries the new vector regardless.
    vectors = storage.vector_store.export_vectors()
    vectors["late"] = np.full(DIM, 0.75, dtype=np.float32)
    storage.vector_store.load_vectors(vectors)

    release.set()
    storage.flush()

    storage._embedding_sidecar.save = real_save
    storage.save().result()

    assert "late" in FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()


def test_a_model_change_leaves_the_index_coherent_and_writable(storage, tmpdir_path):
    """A change in the model's output width makes every stored vector
    incomparable — with the new ones and with any query the new model embeds.
    They are already dead, so they are discarded rather than kept alongside.
    Keeping them mixed used to break the matrix, the sidecar write, search and
    delete, each of which then had to guess a recovery."""

    class _WiderEncoder:
        def encode(self, text):
            if isinstance(text, str):
                return np.ones(DIM + 4, dtype=np.float32)
            return np.vstack([np.ones(DIM + 4, dtype=np.float32) for _ in text])

    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    storage.vector_store.model = _WiderEncoder()
    result = storage.add_nodes(
        [Node(id="after-model-change", type=NodeType.ACTOR, name="Wider")], []
    )
    storage.flush()

    assert result.success

    # One width only, and the matrix agrees with the dict.
    vectors = storage.vector_store.export_vectors()
    assert {len(v) for v in vectors.values()} == {DIM + 4}
    assert set(storage.vector_store.node_ids) == set(vectors)
    assert storage.vector_store.embedding_matrix.shape == (len(vectors), DIM + 4)

    # The sidecar reflects it rather than freezing at the previous state.
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == set(vectors)

    # And the paths that a mixed index used to break all still work.
    storage.vector_store.search(query_text="anything", limit=3)
    assert storage.delete_nodes(["n2"], confirmed=True).success

    storage.update_node("n3", {"name": "Still writable"})
    storage.flush()
    persisted = FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()
    np.testing.assert_allclose(
        persisted["n3"], storage.vector_store.export_vectors()["n3"]
    )


def test_a_wider_vector_never_reaches_the_sidecar_as_a_mixed_matrix(
    storage, tmpdir_path
):
    """The sidecar refuses a mixed matrix and a refused write retries forever,
    so a mixed index would freeze it. The invariant is what prevents that, so
    pin it directly: whatever the index holds, the export is one width."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    storage.vector_store.load_vectors(
        {
            "n1": np.ones(DIM, dtype=np.float32),
            "n2": np.ones(DIM, dtype=np.float32),
            "wide": np.ones(DIM + 4, dtype=np.float32),
        }
    )
    storage.save().result()

    exported = storage.vector_store.export_vectors()
    assert {len(v) for v in exported.values()} == {DIM}
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == set(exported)


def test_a_sidecar_with_non_string_ids_degrades_instead_of_failing_the_load(
    storage, tmpdir_path
):
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    header = json.dumps(
        {"dtype": "float32", "rows": 1, "dim": 2, "ids": [["not", "a", "string"]]}
    ).encode()
    payload = np.float32([[1.0, 2.0]]).tobytes()
    _sidecar_path(tmpdir_path).write_bytes(
        MAGIC + struct.pack("<I", len(header)) + header + payload
    )

    reloaded = _make_storage(tmpdir_path)
    try:
        assert reloaded.vector_store.get_embedding_count() == 0
        assert len(reloaded.nodes) == 3
    finally:
        reloaded.flush()


def test_a_failed_sidecar_write_does_not_strip_a_pre_split_graph(tmpdir_path):
    """graph.json is the only durable copy of a pre-split vector. The sidecar
    write is allowed to fail without failing the graph write, so stripping the
    vector on that save would destroy it with nothing written in its place —
    and the in-memory retry only survives while the process does."""
    legacy = [0.5] * DIM
    graph = {
        "nodes": [{"id": "n1", "type": "Actor", "name": "Legacy", "embedding": legacy}],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:

        def failing_save(vectors):
            raise OSError("no space left on device")

        storage._embedding_sidecar.save = failing_save
        storage.save().result()

        assert not _sidecar_path(tmpdir_path).exists()
        np.testing.assert_allclose(
            np.float32(_graph_json(tmpdir_path)["nodes"][0]["embedding"]),
            np.float32(legacy),
        )
    finally:
        storage.flush()

    # And the vector is still there for a fresh process to pick up.
    reloaded = _make_storage(tmpdir_path)
    try:
        np.testing.assert_allclose(
            reloaded.vector_store.export_vectors()["n1"], np.float32(legacy)
        )
    finally:
        reloaded.flush()


def test_reload_onto_a_missing_graph_file_keeps_writing_the_sidecar(
    storage, tmpdir_path
):
    """load() is also reload()'s body. On a populated instance the bootstrap
    branch must not claim the live vectors are on disk: that stops every later
    save from writing them, and vectors_persisted — which both maintenance
    scripts trust — would report success for a write that never happened."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    os.unlink(os.path.join(tmpdir_path, "graph.json"))
    _sidecar_path(tmpdir_path).unlink()

    storage.reload()
    storage.flush()

    # The bootstrap save must actually write the vectors out. Marking them
    # persisted without writing leaves needs_write false for good, so the
    # sidecar is never recreated and the vectors die with the process.
    assert _sidecar_path(tmpdir_path).exists(), (
        "the populated index was marked persisted without being written, so no "
        "later save will write it either"
    )
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
        "n1",
        "n2",
        "n3",
    }


def test_reopening_a_migrated_graph_does_not_put_the_vectors_back_inline(
    storage, tmpdir_path
):
    """Inline retention is a bridge for vectors whose only durable copy is
    graph.json, and it has to end when the sidecar covers them. Widening it to
    every id the sidecar hands back would be invisible on the first process —
    it only shows on the second, where no vector changes, so no sidecar write
    lands to clear the retention, and every later save writes the whole matrix
    back into graph.json: the size regression this split exists to remove."""
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    reopened = _make_storage(tmpdir_path)
    try:
        assert reopened._pending_migration_ids == set(), (
            "nothing was migrated out of graph.json, so nothing is owed a copy there"
        )
        # metadata-only, so no vector moves and no sidecar write follows to
        # clear a wrongly-populated retention set.
        reopened.update_node("n1", {"metadata": {"reviewed": True}})
        reopened.flush()
    finally:
        reopened.flush()

    for payload in _graph_json(tmpdir_path)["nodes"]:
        assert "embedding" not in payload, (
            f"{payload['id']} was written back into graph.json although the "
            f"sidecar already holds its vector"
        )
    assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
        "n1",
        "n2",
        "n3",
    }


def test_a_sidecar_of_pure_orphans_does_not_veto_the_live_inline_vectors(tmpdir_path):
    """The sidecar decides the dimension because it is authoritative — but only
    over vectors it actually contributes. One whose ids have all gone from the
    graph contributes nothing, and letting it still set the width drops every
    live inline vector for disagreeing with a file made entirely of orphans:
    graph.json is stripped and an empty sidecar lands on top of the old one, so
    both durable copies go in a single save. This is what restoring a graph.json
    from another dataset onto an existing sidecar looks like."""
    legacy = [0.5] * DIM
    graph = {
        "nodes": [
            {"id": f"n{i}", "type": "Actor", "name": f"N{i}", "embedding": legacy}
            for i in (1, 2, 3)
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)
    # Wider, and belonging to no node the graph still has.
    FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).save(
        {"departed": np.ones(DIM * 2, dtype=np.float32)}
    )

    storage = _make_storage(tmpdir_path)
    try:
        assert set(storage.vector_store.export_vectors()) == {"n1", "n2", "n3"}, (
            "an all-orphan sidecar outvoted the live vectors on dimension"
        )
        storage.save().result()

        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
            "n1",
            "n2",
            "n3",
        }, "the surviving vectors were not written; the sidecar was emptied"
    finally:
        storage.flush()


def test_one_save_both_writes_the_sidecar_and_clears_graph_json(tmpdir_path):
    """Both maintenance scripts save exactly once and exit. If migration needed
    a second save to drop the inline copies, they would report success while
    leaving every vector in graph.json and the file the size it always was —
    the whole point of the split, silently not delivered."""
    legacy = [0.5] * DIM
    graph = {
        "nodes": [
            {"id": f"n{i}", "type": "Actor", "name": f"N{i}", "embedding": legacy}
            for i in (1, 2, 3)
        ],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:
        storage.save().result()

        assert storage.vectors_persisted
        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
            "n1",
            "n2",
            "n3",
        }
        for payload in _graph_json(tmpdir_path)["nodes"]:
            assert "embedding" not in payload, (
                f"{payload['id']} still carries its vector after the one save a "
                f"maintenance script performs"
            )
    finally:
        storage.flush()


def test_a_second_save_under_a_failing_sidecar_still_keeps_the_inline_copy(
    tmpdir_path,
):
    """The retention set is narrowed only by a write that landed. Narrowing it
    on the attempt instead is invisible on the first save and fatal on the
    second: graph.json would be rewritten without the vector while the sidecar
    still does not exist, destroying the only durable copy."""
    legacy = [0.5] * DIM
    graph = {
        "nodes": [{"id": "n1", "type": "Actor", "name": "Legacy", "embedding": legacy}],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "graph"},
    }
    with open(os.path.join(tmpdir_path, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f)

    storage = _make_storage(tmpdir_path)
    try:

        def failing_save(vectors):
            raise OSError("no space left on device")

        storage._embedding_sidecar.save = failing_save
        storage.save().result()
        storage.save().result()

        assert not _sidecar_path(tmpdir_path).exists()
        np.testing.assert_allclose(
            np.float32(_graph_json(tmpdir_path)["nodes"][0]["embedding"]),
            np.float32(legacy),
        )
    finally:
        storage.flush()


def test_a_failed_graph_write_does_not_cost_the_vectors_their_only_copy(tmpdir_path):
    """The sidecar is written before the graph, never after. Going second, one
    transient graph-write failure would skip it — and save() has already
    stamped the snapshot marker, so every later save treats that revision as
    already in flight and the vectors this process generated never reach disk
    at all. Nothing recovers them: they are absent from graph.json by design
    and were never in the migration retention set."""
    storage = _make_storage(tmpdir_path)
    try:
        original = storage._persistence_backend.save_graph_data
        failures = {"left": 1}

        def flaky(data):
            if failures["left"]:
                failures["left"] -= 1
                raise OSError("transient graph write failure")
            return original(data)

        storage._persistence_backend.save_graph_data = flaky
        storage.add_nodes(_sample_nodes(), [])
        storage.flush()

        assert _sidecar_path(tmpdir_path).exists(), (
            "the failed graph write took the vectors with it, and no later "
            "save will write them either"
        )
        assert set(FileEmbeddingSidecar(_sidecar_path(tmpdir_path)).load()) == {
            "n1",
            "n2",
            "n3",
        }
    finally:
        storage.flush()


def test_the_matrix_handed_to_the_writer_is_a_snapshot_not_the_live_index(
    storage, tmpdir_path
):
    """save() hands the vectors to a background thread. Passing the live dict
    lets that thread iterate it while the main thread mutates it — a
    RuntimeError swallowed as a sidecar failure, or a half-written matrix.
    The race is not reproducible on demand, so pin the property that prevents
    it: what is handed over is not the object the store keeps mutating."""
    # add_nodes saves twice — once for the nodes, once for the edges — and only
    # the first carries a matrix, so keep every call rather than the last.
    captured = []
    original = storage._do_save_to_disk

    def capture(data, node_count, edge_count, vectors=None, vector_revision=None):
        captured.append(vectors)
        return original(data, node_count, edge_count, vectors, vector_revision)

    storage._do_save_to_disk = capture
    storage.add_nodes(_sample_nodes(), [])
    storage.flush()

    handed_over = [vectors for vectors in captured if vectors is not None]
    assert handed_over, "no matrix reached the writer at all"
    for vectors in handed_over:
        assert vectors is not storage.vector_store.embeddings
