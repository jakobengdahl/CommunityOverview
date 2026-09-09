"""The file backend against the persistence contract.

`FileGraphPersistenceBackend` is the default backend and the one every
deployment runs today, so it is held to every clause, including the crash
shapes and the previous-version store.
"""

import json
import shutil
from pathlib import Path

import pytest

import backend.core.storage_backends as storage_backends
from backend.core.storage_backends import EntityOperation, FileGraphPersistenceBackend
from backend.core.tests.persistence_contract import (
    PersistenceBackendContract,
    node_payload,
    snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_VERSION_GRAPH = REPO_ROOT / "data" / "examples" / "default.json"


class TestFileBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self, tmp_path):
        path = tmp_path / "graph.json"
        return lambda: FileGraphPersistenceBackend(path)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        real_dump = json.dump

        def crash(*args, **kwargs):
            monkeypatch.setattr(storage_backends.json, "dump", real_dump)
            raise OSError("disk full during snapshot")

        monkeypatch.setattr(storage_backends.json, "dump", crash)

    def interrupt_next_append(self, backend, monkeypatch):
        real_fsync = storage_backends.os.fsync

        def crash(fd):
            monkeypatch.setattr(storage_backends.os, "fsync", real_fsync)
            raise OSError("power lost during append")

        monkeypatch.setattr(storage_backends.os, "fsync", crash)

    def test_apply_batch_journals_the_whole_batch_as_one_record(self, factory):
        """The atomicity clause tested in the shared contract
        (`test_a_declared_atomic_batch_lands_entirely_or_not_at_all`) relies on
        `apply_batch` writing ONE journal record per call, not one per
        operation: `interrupt_next_append` above fails whichever fsync happens
        first, so a regression that journalled each operation separately would
        still look atomic there - its first op's fsync fails before anything
        lands, exactly like today's single-record write, for the wrong reason.
        Assert the journal shape directly so that regression cannot hide."""
        backend = factory()
        backend.save_graph_data(snapshot([node_payload("a")]))

        backend.apply_batch(
            [
                EntityOperation.delete_node("a"),
                EntityOperation.upsert_node(node_payload("b")),
                EntityOperation.upsert_node(node_payload("c")),
            ]
        )

        lines = backend.journal_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, "one apply_batch call must write one journal line"
        assert len(json.loads(lines[0])["ops"]) == 3

    def previous_version_store(self, tmp_path):
        """A graph.json as shipped before the journal existed: whole graph,
        inline `embedding` keys, no journal beside it."""
        path = tmp_path / "graph.json"
        shutil.copy(PREVIOUS_VERSION_GRAPH, path)
        assert not (tmp_path / "graph.journal.ndjson").exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        node_ids = [n["id"] for n in data["nodes"]]
        with_vectors = [n["id"] for n in data["nodes"] if n.get("embedding")]
        return (lambda: FileGraphPersistenceBackend(path)), node_ids, with_vectors


def test_the_previous_version_fixture_is_the_shipped_example():
    """The example dataset is the graph.json shape PREVIOUS releases wrote -
    vectors inline under an `embedding` key, which the current release no
    longer writes. If it moves or changes shape, the compatibility clause
    above is testing something else."""
    data = json.loads(PREVIOUS_VERSION_GRAPH.read_text(encoding="utf-8"))
    assert data["nodes"] and data["edges"]
    assert "embedding" in data["nodes"][0]
    assert any(n.get("embedding") for n in data["nodes"]), "no inline vector to migrate"


class TestTheDefaultBackendDidNotChange:
    """The shared-store work's third acceptance criterion: the file backend is
    unaffected by it.

    Today that is true by ABSENCE - the eight change-notification clauses skip
    for this backend, and a skip is silence, not a statement. If it ever
    started declaring the capability the clauses would begin passing and
    nothing would report that the default had quietly become a backend two
    instances may share. It may not: two writers would fight over the
    checkpoint that folds the journal back into `graph.json`, and the
    `journal_id` binding a journal to its graph assumes one writer lineage.

    So the absence is asserted rather than left to the skips.
    """

    def test_it_declares_incremental_writes_and_transactions_and_no_more(
        self, tmp_path
    ):
        backend = FileGraphPersistenceBackend(tmp_path / "graph.json")
        assert backend.capabilities() == storage_backends.BackendCapabilities(
            incremental_writes=True, transactions=True
        )

    def test_it_is_not_a_change_notifying_backend(self, tmp_path):
        backend = FileGraphPersistenceBackend(tmp_path / "graph.json")
        assert not isinstance(backend, storage_backends.ChangeNotifyingBackend), (
            "the default backend grew the notification protocol; one graph "
            "file is not a store two instances can share"
        )

    def test_a_storage_on_it_never_starts_notification(self, tmp_path):
        """The other half: `GraphStorage` consults the declaration, so an
        undeclared capability must also mean an unstarted listener."""
        from backend.core.storage import GraphStorage

        storage = GraphStorage(
            persistence_backend=FileGraphPersistenceBackend(tmp_path / "graph.json")
        )
        try:
            assert not storage._backend_capabilities.change_notification
        finally:
            storage.shutdown_events()
