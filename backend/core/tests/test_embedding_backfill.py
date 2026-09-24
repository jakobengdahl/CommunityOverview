"""
Regression tests for the embedding backfill path.

`add_nodes` (and its siblings) try to embed a node inline and, by design,
swallow the failure when the optional ML stack is unavailable at that
moment - see the try/except around `vector_store.update_nodes_embeddings`
in `GraphStorage.add_nodes`. Nothing used to revisit that node afterwards:
neither a later write nor a restart (`VectorStore.rebuild_index` only reads
vectors nodes already carry). These tests drive the real write path with a
deterministic stand-in for the sentence-transformers encoder, the same
pattern `test_embedding_persistence.py` uses, so they exercise production
code without the optional ML stack CI does not install.
"""

import os
import tempfile
import threading
import time
import zlib
from typing import Dict

import numpy as np
import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.core import storage as storage_module

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


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def _make_storage(tmpdir, **kwargs) -> GraphStorage:
    storage = GraphStorage(json_path=os.path.join(tmpdir, "graph.json"), **kwargs)
    storage.vector_store.model = _FakeEncoder()
    return storage


def _add_node_without_embedding(storage: GraphStorage, node: Node) -> None:
    """Land a node the way a real ML-unavailable moment would: `add_nodes`
    tries to embed it, the attempt raises, and the exception is swallowed."""

    def _raise(nodes):
        raise ImportError("No module named 'sentence_transformers'")

    original = storage.vector_store.update_nodes_embeddings
    storage.vector_store.update_nodes_embeddings = _raise
    try:
        result = storage.add_nodes([node], [])
        assert result.success
    finally:
        storage.vector_store.update_nodes_embeddings = original


class TestEmbeddingCoverage:
    def test_full_coverage_when_every_node_embedded(self, tmpdir_path):
        storage = _make_storage(tmpdir_path)
        storage.add_nodes(
            [Node(id="a", type=NodeType.ACTOR, name="Alpha")],
            [],
        )
        assert storage.embedding_coverage() == (1, 1)
        storage.flush()

    def test_reports_the_gap_left_by_a_failed_inline_embed(self, tmpdir_path):
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        _add_node_without_embedding(
            storage, Node(id="b", type=NodeType.ACTOR, name="Beacon")
        )

        assert storage.embedding_coverage() == (1, 2)
        assert storage.vector_store.has_embedding("a")
        assert not storage.vector_store.has_embedding("b")
        storage.flush()


class TestBackfillMissingEmbeddings:
    def test_backfills_exactly_the_node_that_was_missing(self, tmpdir_path):
        """The scenario the task exists for: a node added without an
        embedding gets backfilled, and the node that already had one is
        left untouched."""
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        existing_vector = storage.vector_store.get_vector_list("a")

        _add_node_without_embedding(
            storage, Node(id="b", type=NodeType.ACTOR, name="Beacon")
        )
        assert not storage.vector_store.has_embedding("b")

        backfilled = storage.backfill_missing_embeddings()

        assert backfilled == 1
        assert storage.embedding_coverage() == (2, 2)
        assert storage.vector_store.has_embedding("b")
        # The node that already had a vector was not re-embedded.
        assert storage.vector_store.get_vector_list("a") == existing_vector
        storage.flush()

    def test_is_a_noop_once_the_index_is_complete(self, tmpdir_path):
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])

        assert storage.backfill_missing_embeddings() == 0
        storage.flush()

    def test_backfilled_vector_survives_a_restart(self, tmpdir_path):
        """A restart is exactly the case the task calls out as unrepaired
        today (`VectorStore.rebuild_index` only reads vectors a node already
        carries). Backfilling must persist through the sidecar, not just
        update the in-memory index."""
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        _add_node_without_embedding(
            storage, Node(id="b", type=NodeType.ACTOR, name="Beacon")
        )

        assert storage.backfill_missing_embeddings() == 1
        storage.save().result()

        reloaded = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))
        assert reloaded.embedding_coverage() == (2, 2)
        assert reloaded.vector_store.has_embedding("b")
        reloaded.flush()

    def test_a_batch_where_generation_fails_leaves_coverage_unchanged(
        self, tmpdir_path
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )

        def _raise(nodes):
            raise RuntimeError("model unavailable")

        storage.vector_store.update_nodes_embeddings = _raise

        assert storage.backfill_missing_embeddings() == 0
        assert storage.embedding_coverage() == (0, 1)
        storage.flush()


class TestStartupBackfillPass:
    """Covers `GraphStorage._maybe_backfill_missing_embeddings_async`, the
    startup-time pass `__init__` runs once loading finishes."""

    def test_skips_probing_when_coverage_is_already_complete(
        self, tmpdir_path, monkeypatch
    ):
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])

        def _must_not_be_called(name):
            raise AssertionError("find_spec must not run when nothing is missing")

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", _must_not_be_called
        )

        storage._maybe_backfill_missing_embeddings_async()
        storage.flush()

    def test_stays_silent_and_starts_no_thread_when_ml_stack_is_absent(
        self, tmpdir_path, monkeypatch, capsys
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        capsys.readouterr()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: None
        )
        before = {t.ident for t in threading.enumerate()}

        storage._maybe_backfill_missing_embeddings_async()

        after = {t.ident for t in threading.enumerate()}
        assert after == before
        assert "have no" in capsys.readouterr().out
        assert not storage.vector_store.has_embedding("a")
        storage.flush()

    def test_backfills_in_the_background_when_the_ml_stack_is_available(
        self, tmpdir_path, monkeypatch, capsys
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        capsys.readouterr()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        storage._maybe_backfill_missing_embeddings_async()

        # The backfill runs on a background thread; with the fake encoder it
        # finishes almost immediately, so by the time this returns the
        # thread may already be gone rather than still enumerable - poll
        # coverage instead of trying to join a thread that could have
        # already exited.
        deadline = time.monotonic() + 5
        while storage.embedding_coverage() != (1, 1) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert storage.embedding_coverage() == (1, 1)
        assert "Backfilled 1" in capsys.readouterr().out
        storage.flush()

    def test_a_broken_find_spec_probe_does_not_propagate(
        self, tmpdir_path, monkeypatch, capsys
    ):
        """The synchronous portion of the startup pass - the find_spec probe
        and the Thread(...).start() call - runs from inside GraphStorage
        construction. If either raises (a broken import hook, a
        thread-limited container refusing Thread.start()), the exception
        must not escape and abort startup."""
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        capsys.readouterr()

        def _raise(name):
            raise RuntimeError("broken import hook")

        monkeypatch.setattr(storage_module.importlib.util, "find_spec", _raise)

        # Must not raise.
        storage._maybe_backfill_missing_embeddings_async()

        assert "could not start embedding backfill" in capsys.readouterr().out
        storage.flush()

    def test_a_thread_start_failure_does_not_propagate(
        self, tmpdir_path, monkeypatch, capsys
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        capsys.readouterr()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        def _raise_on_start(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", _raise_on_start)

        # Must not raise.
        storage._maybe_backfill_missing_embeddings_async()

        assert "could not start embedding backfill" in capsys.readouterr().out
        storage.flush()

    def test_construction_survives_a_broken_backfill_probe(
        self, tmpdir_path, monkeypatch
    ):
        """The real trigger this guards: `GraphStorage.__init__` calls the
        startup pass from inside its own try block. A raise from the
        synchronous portion must not abort construction of the storage
        itself."""
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage.save().result()
        storage.flush()

        def _raise(name):
            raise RuntimeError("broken import hook")

        monkeypatch.setattr(storage_module.importlib.util, "find_spec", _raise)

        # Must not raise, even though __init__ calls the startup pass itself.
        reloaded = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))
        reloaded.vector_store.model = _FakeEncoder()
        reloaded.flush()


class TestLoadModelConcurrency:
    """The lock `VectorStore._load_model` gained so the startup preload and
    a concurrent backfill pass cannot construct the model twice."""

    def test_concurrent_callers_construct_the_model_once(self, monkeypatch):
        from backend.core import vector_store as vector_store_module

        constructions: Dict[str, int] = {"count": 0}

        class _SlowModel:
            def __init__(self, name):
                # A real model load is slow enough for two threads to race
                # on `self.model is None`; a short sleep reproduces that
                # window deterministically.
                import time

                time.sleep(0.05)
                constructions["count"] += 1

        monkeypatch.setattr(
            vector_store_module,
            "_ensure_sentence_transformers",
            lambda: _SlowModel,
        )

        store = vector_store_module.VectorStore()
        threads = [threading.Thread(target=store._load_model) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert constructions["count"] == 1
