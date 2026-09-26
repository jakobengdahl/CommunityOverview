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

import logging
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
        update the in-memory index - so this waits for backfill's OWN
        fire-and-forget `save()` to land (via `flush()`) instead of calling
        `save()` again itself, which would mask whether
        `backfill_missing_embeddings()` persists on its own."""
        storage = _make_storage(tmpdir_path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        _add_node_without_embedding(
            storage, Node(id="b", type=NodeType.ACTOR, name="Beacon")
        )

        assert storage.backfill_missing_embeddings() == 1
        storage.flush()

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
        self, tmpdir_path, monkeypatch, storage_log
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage_log()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: None
        )
        before = {t.ident for t in threading.enumerate()}

        storage._maybe_backfill_missing_embeddings_async()

        after = {t.ident for t in threading.enumerate()}
        assert after == before
        assert any("have no" in m for m in storage_log()[logging.WARNING])
        assert not storage.vector_store.has_embedding("a")
        storage.flush()

    def test_backfills_in_the_background_when_the_ml_stack_is_available(
        self, tmpdir_path, monkeypatch, storage_log
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage_log()

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
        assert any("Backfilled 1" in m for m in storage_log()[logging.INFO])
        storage.flush()

    def test_a_broken_find_spec_probe_does_not_propagate(
        self, tmpdir_path, monkeypatch, storage_log
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
        storage_log()

        def _raise(name):
            raise RuntimeError("broken import hook")

        monkeypatch.setattr(storage_module.importlib.util, "find_spec", _raise)

        # Must not raise.
        storage._maybe_backfill_missing_embeddings_async()

        assert any(
            "could not start embedding backfill" in m
            for m in storage_log()[logging.WARNING]
        )
        storage.flush()

    def test_a_thread_start_failure_does_not_propagate(
        self, tmpdir_path, monkeypatch, storage_log
    ):
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage_log()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        def _raise_on_start(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", _raise_on_start)

        # Must not raise.
        storage._maybe_backfill_missing_embeddings_async()

        assert any(
            "could not start embedding backfill" in m
            for m in storage_log()[logging.WARNING]
        )
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

    def test_a_save_failure_during_the_startup_backfill_does_not_abort_construction(
        self, tmpdir_path, monkeypatch
    ):
        """`backfill_missing_embeddings()` calls `self.save()` without
        waiting on or checking its result - fire-and-forget, per this
        module's own docstring - and nothing in it guards that call with a
        try/except of its own. This simulates `save()` itself raising
        synchronously (the same shape as `ThreadPoolExecutor.submit` raising
        `RuntimeError` when the executor is already shut down) and checks
        two things: the raise must not escape the background startup pass
        `__init__` starts and so must not abort construction; and, because
        the vector was already computed and put in the index before `save()`
        was even called, it must not be lost - any later successful `save()`
        reissues the whole graph and so recovers it, the general
        resync-on-next-save behaviour `__init__` leans on here."""
        from backend.core import vector_store as vector_store_module

        class _FakeSentenceTransformer:
            def __init__(self, model_name):
                self._encoder = _FakeEncoder()

            def encode(self, text):
                return self._encoder.encode(text)

        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage.save().result()
        storage.flush()

        monkeypatch.setattr(
            vector_store_module,
            "_ensure_sentence_transformers",
            lambda: _FakeSentenceTransformer,
        )
        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        original_save = storage_module.GraphStorage.save
        calls = {"n": 0}

        def _fail_first_save(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated save failure mid-backfill")
            return original_save(self)

        monkeypatch.setattr(storage_module.GraphStorage, "save", _fail_first_save)

        # Must not raise - the same tolerance the other startup-pass failure
        # modes in this class already pin, extended to the async body itself.
        reloaded = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))

        deadline = time.monotonic() + 5
        while calls["n"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["n"] == 1, "backfill's save() was never even attempted"

        # The vector was computed and put in the index even though the
        # call that would have persisted it raised.
        assert reloaded.vector_store.has_embedding("a")

        # The resync-on-next-save mechanism: a later, successful save
        # recovers it on disk.
        reloaded.save().result()
        assert calls["n"] == 2

        restarted = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))
        assert restarted.embedding_coverage() == (1, 1)
        assert restarted.vector_store.has_embedding("a")
        restarted.flush()
        reloaded.flush()
        storage.flush()

    def test_construction_actually_triggers_the_backfill_end_to_end(
        self, tmpdir_path, monkeypatch
    ):
        """Every other test in this class calls
        `_maybe_backfill_missing_embeddings_async` directly on an
        already-built instance, so none of them proves the automatic wiring
        `GraphStorage.__init__` calls it from ever fires the way a real
        restart would trigger it. This goes through real `GraphStorage()`
        construction, with the optional ML stack faked as installed at the
        seam `TestLoadModelConcurrency` below patches (not by assigning
        `vector_store.model` by hand, which construction has no chance to do
        before its own background thread needs it), and checks the backfill
        both runs and persists as a side effect of construction alone."""
        from backend.core import vector_store as vector_store_module

        class _FakeSentenceTransformer:
            def __init__(self, model_name):
                self._encoder = _FakeEncoder()

            def encode(self, text):
                return self._encoder.encode(text)

        # Seed a graph on disk with a node missing its embedding, the same
        # way the other startup-pass tests do.
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )
        storage.save().result()
        storage.flush()

        monkeypatch.setattr(
            vector_store_module,
            "_ensure_sentence_transformers",
            lambda: _FakeSentenceTransformer,
        )
        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        # Real construction - no direct call to the private method, and no
        # fake model assigned by hand: the automatic __init__ wiring and the
        # real `_load_model()` path are what does the work.
        reloaded = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))

        deadline = time.monotonic() + 5
        while (
            reloaded.embedding_coverage() != (1, 1) or not reloaded.vectors_persisted
        ) and time.monotonic() < deadline:
            reloaded.flush()
            time.sleep(0.01)

        assert reloaded.embedding_coverage() == (1, 1)
        assert reloaded.vectors_persisted
        reloaded.flush()

        restarted = GraphStorage(json_path=os.path.join(tmpdir_path, "graph.json"))
        assert restarted.embedding_coverage() == (1, 1)
        assert restarted.vector_store.has_embedding("a")
        restarted.flush()
        storage.flush()

    def test_the_backfill_pass_runs_on_a_background_thread_not_synchronously(
        self, tmpdir_path, monkeypatch
    ):
        """`test_backfills_in_the_background_when_the_ml_stack_is_available`
        above only ever observes the RESULT (coverage becomes complete, a
        message is printed) - with the fake encoder that finishes so fast
        that a synchronous implementation would produce the exact same
        observations. This uses a deliberately slow encoder that blocks
        until released, so a synchronous call would still be blocked inside
        it when this test checks: the call returning while the encoder is
        still blocked is what only a background thread can produce."""
        storage = _make_storage(tmpdir_path)
        _add_node_without_embedding(
            storage, Node(id="a", type=NodeType.ACTOR, name="Alpha")
        )

        release = threading.Event()

        class _SlowEncoder:
            def encode(self, text):
                release.wait(timeout=5)
                return _FakeEncoder().encode(text)

        storage.vector_store.model = _SlowEncoder()

        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )

        before = {t.ident: t.name for t in threading.enumerate()}
        storage._maybe_backfill_missing_embeddings_async()
        after_call = {t.ident: t.name for t in threading.enumerate()}

        # A synchronous implementation would still be running `.encode()`
        # (blocked on `release`) on THIS thread at this point, so no new
        # thread would exist yet.
        new_threads = {
            ident: name for ident, name in after_call.items() if ident not in before
        }
        assert new_threads, (
            "no new thread appeared after the call returned - the startup "
            "pass may be running synchronously on the caller's thread"
        )
        assert "embedding-backfill" in new_threads.values()

        release.set()
        deadline = time.monotonic() + 5
        while storage.embedding_coverage() != (1, 1) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert storage.embedding_coverage() == (1, 1)
        storage.flush()


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
