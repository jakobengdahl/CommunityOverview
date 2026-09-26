"""The level of each operational report the storage layer makes.

A report's level is how an operator filters it: an ERROR pages someone, a
WARNING is read when something looks off, an INFO is context. A report that
drifts to another level is lost to whoever filtered for the old one, so each
test here pins the exact level together with a stable part of the message.
"""

import logging
import threading

import pytest

from backend.core import storage as storage_module
from backend.core import vector_store as vector_store_module
from backend.core.models import Node, NodeType
from backend.core.storage import _BOOT_BUFFER_LIMIT, GraphStorage, _BootGate
from backend.core.storage_backends import (
    BackendCapabilities,
    EntityOperation,
    ExternalChange,
)
from backend.core.tests.test_persistence_seam import (
    _IncrementalBackend,
    _NotifyingBackend,
    _SnapshotBackend,
)
from backend.core.tests.test_storage import InMemoryPersistenceBackend
from backend.core.vector_store import VectorStore

_VECTOR_STORE_LOGGER = "backend.core.vector_store"


def _node(node_id="a", name="Alpha", **kwargs):
    return Node(id=node_id, type=NodeType.ACTOR, name=name, **kwargs)


def _raise(error):
    def raiser(*args, **kwargs):
        raise error

    return raiser


def _join_new_threads(before, name):
    for thread in set(threading.enumerate()) - before:
        if thread.name == name:
            thread.join(timeout=5)
            assert not thread.is_alive(), f"the {name} thread did not finish"


@pytest.fixture
def make_storage():
    """Storages built here are shut down at teardown, so no writer thread
    outlives the test that started it."""
    made = []

    def make(**kwargs):
        storage = GraphStorage(**kwargs)
        storage.flush()
        made.append(storage)
        return storage

    yield make
    for storage in made:
        storage.shutdown_events()


class _FailingSnapshotBackend(_SnapshotBackend):
    def __init__(self):
        super().__init__()
        self.fail = False

    def save_graph_data(self, data):
        if self.fail:
            raise OSError("disk full")
        super().save_graph_data(data)


class _TraversingBackend(_SnapshotBackend):
    def capabilities(self):
        return BackendCapabilities(store_traversal=True)

    def traverse(self, *args, **kwargs):
        raise ConnectionError("store unreachable")


class TestPersistenceFailures:
    def test_a_failed_load_is_an_error(self, make_storage, storage_log, monkeypatch):
        backend = InMemoryPersistenceBackend({"nodes": [], "edges": []})
        storage = make_storage(persistence_backend=backend)
        storage_log()

        monkeypatch.setattr(
            backend, "load_graph_data", _raise(ValueError("corrupt graph"))
        )
        with pytest.raises(ValueError):
            storage.load()

        errors = storage_log()[logging.ERROR]
        assert any(
            "Error loading graph" in m and "corrupt graph" in m for m in errors
        ), errors

    def test_a_failed_whole_graph_save_is_an_error(self, make_storage, storage_log):
        backend = _FailingSnapshotBackend()
        storage = make_storage(persistence_backend=backend)
        storage_log()

        backend.fail = True
        with pytest.raises(OSError):
            storage.save().result()
        # Let the owed resync land at teardown rather than fail again.
        backend.fail = False

        errors = storage_log()[logging.ERROR]
        assert any(
            "Error saving graph to disk" in m and "disk full" in m for m in errors
        ), errors

    def test_a_failed_entity_write_is_an_error(
        self, make_storage, storage_log, monkeypatch
    ):
        backend = _IncrementalBackend()
        storage = make_storage(persistence_backend=backend)
        monkeypatch.setattr(
            storage.vector_store, "update_nodes_embeddings", lambda nodes: None
        )
        monkeypatch.setattr(backend, "upsert_node", _raise(OSError("write refused")))
        storage_log()

        storage.add_nodes([_node()], [])
        # The heal after the failure is a whole-graph save, which still works.
        storage.flush()

        errors = storage_log()[logging.ERROR]
        assert any(
            "Error applying 1 entity operation(s)" in m and "write refused" in m
            for m in errors
        ), errors

    def test_a_non_integer_graph_generation_is_a_warning(
        self, make_storage, storage_log
    ):
        backend = InMemoryPersistenceBackend(
            {"nodes": [], "edges": [], "metadata": {"graph_generation": "x"}}
        )
        make_storage(persistence_backend=backend)

        warnings = storage_log()[logging.WARNING]
        assert any("ignoring non-integer graph_generation" in m for m in warnings), (
            warnings
        )

    def test_a_refresh_from_a_missing_store_is_a_warning(
        self, make_storage, storage_log
    ):
        backend = InMemoryPersistenceBackend({"nodes": [], "edges": []})
        storage = make_storage(persistence_backend=backend)
        storage_log()

        backend.data = None
        storage.load(bootstrap_if_missing=False)
        # Put the store back so the teardown's writes have somewhere to land.
        backend.data = {"nodes": [], "edges": []}

        warnings = storage_log()[logging.WARNING]
        assert any(
            "cannot refresh from" in m and "it is not there" in m for m in warnings
        ), warnings

    def test_a_failing_store_traversal_is_a_warning(self, make_storage, storage_log):
        storage = make_storage(persistence_backend=_TraversingBackend())
        storage.add_nodes([_node()], [])
        storage.flush()
        storage_log()

        storage.get_related_nodes("a")

        warnings = storage_log()[logging.WARNING]
        assert any(
            "store traversal failed, walking instead" in m and "store unreachable" in m
            for m in warnings
        ), warnings


class TestChangeNotificationReports:
    def test_overflowing_the_boot_buffer_is_a_warning(self, storage_log):
        storage_log()
        gate = _BootGate(lambda change: None)

        for _ in range(_BOOT_BUFFER_LIMIT + 1):
            gate(ExternalChange.unknown())

        warnings = storage_log()[logging.WARNING]
        assert any("changes arrived while loading" in m for m in warnings), warnings

    def test_an_unreadable_external_change_is_a_warning(
        self, make_storage, storage_log
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        storage_log()

        storage.apply_external_change(
            ExternalChange.entities_read_on_demand(_raise(OSError("read timed out")))
        )

        warnings = storage_log()[logging.WARNING]
        assert any(
            "could not read the content of an external change" in m
            and "read timed out" in m
            for m in warnings
        ), warnings

    def test_an_inapplicable_external_change_is_a_warning(
        self, make_storage, storage_log
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        storage_log()

        storage.apply_external_change(
            ExternalChange.entities([EntityOperation("node", "rename", "a")])
        )

        warnings = storage_log()[logging.WARNING]
        assert any(
            "could not apply an external change" in m and "rename" in m
            for m in warnings
        ), warnings

    def test_a_failing_stop_of_change_notification_is_a_warning(
        self, make_storage, storage_log
    ):
        backend = _NotifyingBackend()
        storage = make_storage(persistence_backend=backend)
        backend.stop_change_notification = _raise(ConnectionError("channel gone"))
        storage_log()

        storage.shutdown_events()

        warnings = storage_log()[logging.WARNING]
        assert any(
            "stopping change notification failed" in m and "channel gone" in m
            for m in warnings
        ), warnings


class TestEmbeddingReports:
    def test_a_sidecar_naming_the_graph_file_is_a_warning(
        self, tmp_path, make_storage, storage_log
    ):
        graph = str(tmp_path / "graph.json")
        make_storage(json_path=graph, embeddings_path=graph)

        warnings = storage_log()[logging.WARNING]
        assert any(
            "EMBEDDINGS_FILE names the graph file itself" in m for m in warnings
        ), warnings

    def test_an_unreadable_sidecar_is_a_warning(
        self, tmp_path, make_storage, storage_log
    ):
        graph = str(tmp_path / "graph.json")
        first = make_storage(json_path=graph)
        first.embeddings_path.write_bytes(b"not a sidecar")
        storage_log()

        make_storage(json_path=graph)

        warnings = storage_log()[logging.WARNING]
        assert any("ignoring unreadable embedding sidecar" in m for m in warnings), (
            warnings
        )

    def test_the_embedding_count_at_load_is_info(
        self, tmp_path, make_storage, storage_log, monkeypatch
    ):
        graph = str(tmp_path / "graph.json")
        first = make_storage(json_path=graph)
        monkeypatch.setattr(
            first.vector_store, "update_nodes_embeddings", lambda nodes: None
        )
        first.add_nodes([_node(embedding=[0.1, 0.2, 0.3])], [])
        first.flush()
        storage_log()

        make_storage(json_path=graph)

        infos = storage_log()[logging.INFO]
        assert any("Loaded 1 embeddings for 1 nodes" in m for m in infos), infos

    def test_a_failed_batch_embedding_on_add_is_a_warning(
        self, make_storage, storage_log, monkeypatch
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        monkeypatch.setattr(
            storage.vector_store,
            "update_nodes_embeddings",
            _raise(RuntimeError("encoder crashed")),
        )
        storage_log()

        storage.add_nodes([_node()], [])

        warnings = storage_log()[logging.WARNING]
        assert any(
            "could not generate embeddings" in m and "encoder crashed" in m
            for m in warnings
        ), warnings

    def test_a_failed_embedding_on_update_is_a_warning(
        self, make_storage, storage_log, monkeypatch
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        monkeypatch.setattr(
            storage.vector_store, "update_nodes_embeddings", lambda nodes: None
        )
        storage.add_nodes([_node()], [])
        monkeypatch.setattr(
            storage.vector_store,
            "update_node_embedding",
            _raise(RuntimeError("encoder crashed")),
        )
        storage_log()

        storage.update_node("a", {"name": "Renamed"})

        warnings = storage_log()[logging.WARNING]
        assert any(
            "could not update embedding:" in m and "encoder crashed" in m
            for m in warnings
        ), warnings

    def test_a_failed_embedding_of_an_external_change_is_a_warning(
        self, make_storage, storage_log, monkeypatch
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        monkeypatch.setattr(
            storage.vector_store,
            "update_nodes_embeddings",
            _raise(RuntimeError("encoder crashed")),
        )
        storage_log()

        storage.apply_external_change(
            ExternalChange.entities([EntityOperation.upsert_node(_node().to_dict())])
        )

        warnings = storage_log()[logging.WARNING]
        assert any(
            "could not update embeddings" in m and "encoder crashed" in m
            for m in warnings
        ), warnings

    def test_a_failed_background_backfill_is_a_warning(
        self, make_storage, storage_log, monkeypatch
    ):
        storage = make_storage(persistence_backend=_SnapshotBackend())
        monkeypatch.setattr(
            storage.vector_store,
            "update_nodes_embeddings",
            _raise(ImportError("No module named 'sentence_transformers'")),
        )
        storage.add_nodes([_node()], [])
        monkeypatch.setattr(
            storage, "backfill_missing_embeddings", _raise(RuntimeError("oom"))
        )
        monkeypatch.setattr(
            storage_module.importlib.util, "find_spec", lambda name: object()
        )
        storage_log()

        before = set(threading.enumerate())
        storage._maybe_backfill_missing_embeddings_async()
        _join_new_threads(before, "embedding-backfill")

        warnings = storage_log()[logging.WARNING]
        assert any(
            "background embedding backfill failed" in m and "oom" in m for m in warnings
        ), warnings


def _vector_store_logged(caplog, capsys, level):
    """Messages the vector store logged at exactly `level`, after checking
    that none of them also went to stdout."""
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == _VECTOR_STORE_LOGGER and record.levelno == level
    ]
    out = capsys.readouterr().out
    leaked = [m for m in messages if m in out]
    assert not leaked, f"a report went to stdout: {leaked}"
    return messages


class TestVectorStoreReports:
    def test_semantic_search_without_the_model_is_a_warning(
        self, caplog, capsys, monkeypatch
    ):
        caplog.set_level(logging.DEBUG, logger=_VECTOR_STORE_LOGGER)
        # Patched rather than left to the environment, so an install that has
        # the ML extra still takes this path.
        monkeypatch.setattr(
            vector_store_module,
            "_ensure_sentence_transformers",
            _raise(ImportError("No module named 'sentence_transformers'")),
        )
        store = VectorStore()
        store.load_vectors({"a": [1.0, 0.0]})

        assert store.search(query_text="alpha") == []

        warnings = _vector_store_logged(caplog, capsys, logging.WARNING)
        assert any(
            "semantic search unavailable (embedding model not installed)" in m
            for m in warnings
        ), warnings

    def test_a_failed_background_preload_is_a_warning(
        self, caplog, capsys, monkeypatch
    ):
        caplog.set_level(logging.DEBUG, logger=_VECTOR_STORE_LOGGER)
        store = VectorStore()
        monkeypatch.setattr(store, "_load_model", _raise(RuntimeError("no model")))

        before = set(threading.enumerate())
        store.preload_model()
        _join_new_threads(before, "embedding-preload")

        warnings = _vector_store_logged(caplog, capsys, logging.WARNING)
        assert any(
            "background model preload failed" in m and "no model" in m for m in warnings
        ), warnings


class TestStorageLogFixture:
    def test_a_caplog_clear_does_not_hide_a_report(self, caplog, storage_log):
        logging.getLogger("backend.core.storage").warning("before the clear")
        caplog.clear()

        assert storage_log()[logging.WARNING] == ["before the clear"]

    def test_a_printed_report_fails_the_next_read(self, storage_log):
        logging.getLogger("backend.core.storage").warning("printed too")
        print("printed too")

        with pytest.raises(AssertionError, match="went to stdout"):
            storage_log()
