"""The reference in-memory backend against the persistence contract.

Two shapes: the incremental reference implementation, and the same store
declaring nothing, which is what a snapshot-only third-party backend looks
like to GraphStorage.
"""

import pytest

import backend.core.tests.persistence_contract as contract
from backend.core.tests.persistence_contract import (
    InMemoryGraphPersistenceBackend,
    PersistenceBackendContract,
)


class TestInMemoryBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        def refuse(data):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(backend, "save_graph_data", refuse)

    def interrupt_next_append(self, backend, monkeypatch):
        """Fail while the second operation of a batch is being applied: the
        reference applies to a copy, so nothing may have reached the store."""
        real_deepcopy = contract.copy.deepcopy
        calls = {"n": 0}

        def flaky(obj, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("store unavailable mid-batch")
            return real_deepcopy(obj, *args, **kwargs)

        monkeypatch.setattr(contract.copy, "deepcopy", flaky)


class TestSnapshotOnlyInMemoryBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store, incremental=False)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        def refuse(data):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(backend, "save_graph_data", refuse)
