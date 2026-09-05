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
        """Fail while the edges are being copied - after the nodes were: a
        snapshot that assigned as it went would leave new nodes with old
        edges, which the contract must see."""
        _fail_on_deepcopy(monkeypatch, call=2)

    def interrupt_next_append(self, backend, monkeypatch):
        """Fail while the second operation of a batch is being applied: the
        reference applies to a copy, so nothing may have reached the store."""
        _fail_on_deepcopy(monkeypatch, call=1)


class TestSnapshotOnlyInMemoryBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store, incremental=False)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        _fail_on_deepcopy(monkeypatch, call=2)


def _fail_on_deepcopy(monkeypatch, call: int) -> None:
    """Make the Nth deepcopy after this call raise, then restore."""
    real_deepcopy = contract.copy.deepcopy
    calls = {"n": 0}

    def flaky(obj, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == call:
            monkeypatch.setattr(contract.copy, "deepcopy", real_deepcopy)
            raise RuntimeError("store unavailable part-way through the write")
        return real_deepcopy(obj, *args, **kwargs)

    monkeypatch.setattr(contract.copy, "deepcopy", flaky)
