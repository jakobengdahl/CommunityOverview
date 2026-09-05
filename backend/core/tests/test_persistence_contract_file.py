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
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.tests.persistence_contract import PersistenceBackendContract

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

    def previous_version_store(self, tmp_path):
        """A graph.json as shipped before the journal existed: whole graph,
        inline `embedding` keys, no journal beside it."""
        path = tmp_path / "graph.json"
        shutil.copy(PREVIOUS_VERSION_GRAPH, path)
        assert not (tmp_path / "graph.journal.ndjson").exists()
        return lambda: FileGraphPersistenceBackend(path)


def test_the_previous_version_fixture_is_the_shipped_example():
    """The example dataset is the one graph.json shape every release has
    written; if it moves or changes shape, the compatibility clause above is
    testing something else."""
    data = json.loads(PREVIOUS_VERSION_GRAPH.read_text(encoding="utf-8"))
    assert data["nodes"] and data["edges"]
    assert "embedding" in data["nodes"][0]
