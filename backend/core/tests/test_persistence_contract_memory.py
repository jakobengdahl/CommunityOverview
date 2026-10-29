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
        """Fail on the second deepcopy. save_graph_data copies nodes, then
        edges, then metadata; the fixture that exercises this hook saves one
        node and no edges, so the edges comprehension makes no deepcopy call
        of its own and call two lands on the metadata copy - still after the
        nodes were copied and before the store swap. A snapshot that assigned
        as it went would leave new nodes with old edges, which the contract
        must see."""
        _fail_on_deepcopy(monkeypatch, call=2)

    def interrupt_next_append(self, backend, monkeypatch):
        """Fail while the second operation of a batch is being applied: the
        reference applies to a copy, so nothing may have reached the store."""
        _fail_on_deepcopy(monkeypatch, call=1)

    def settle_notifications(self, backend):
        backend.settle_notifications()


class TestSnapshotOnlyInMemoryBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store, incremental=False)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        _fail_on_deepcopy(monkeypatch, call=2)


class TestInMemoryBackendContractWithDeferredReports(PersistenceBackendContract):
    """The same contract, with every entity write reporting itself via
    `ExternalChange.entities_read_on_demand` instead of embedding its payload
    eagerly - the shape PostgreSQL uses for a real cross-process transport.

    PostgreSQL's own contract run (`TestPostgresBackendContract`) already
    happens to exercise the deferred form, but only when a server is
    reachable, and only through its specific SQL transport. This gives the
    deferred path a DB-free run of its own, so a review or a future backend
    cannot mistake "the contract passes" for "the eager form was tested" -
    see `InMemoryGraphPersistenceBackend`'s docstring.
    """

    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store, deferred=True)

    def interrupt_next_snapshot(self, backend, monkeypatch):
        _fail_on_deepcopy(monkeypatch, call=2)

    def interrupt_next_append(self, backend, monkeypatch):
        _fail_on_deepcopy(monkeypatch, call=1)

    def settle_notifications(self, backend):
        backend.settle_notifications()


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
