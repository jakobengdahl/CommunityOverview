"""The in-memory reference backend against the layer-store contract, and the
declaration rules `capabilities_of` enforces for `staged_sessions`."""

import pytest

from backend.core.storage_backends import BackendCapabilities, capabilities_of
from backend.core.tests.layer_store_contract import LayerStoreContract
from backend.core.tests.persistence_contract import InMemoryGraphPersistenceBackend


class TestInMemoryLayerStoreContract(LayerStoreContract):
    @pytest.fixture
    def factory(self):
        store = {}
        return lambda: InMemoryGraphPersistenceBackend(store, staged=True)


class _Declares:
    """A backend declaring `caps`, implementing every method by name."""

    def __init__(self, caps, *, omit=()):
        self._caps = caps
        for name in (
            "upsert_node delete_node upsert_edge delete_edge apply_batch "
            "checkpoint read_write_evidence start_change_notification "
            "stop_change_notification load_layer stage merge discard "
            "load_merge drop_layer record_staged staged_status retire "
            "layers_in_use"
        ).split():
            if name not in omit:
                setattr(self, name, lambda *a, **k: None)

    def capabilities(self):
        return self._caps


STAGED = dict(incremental_writes=True, transactions=True, staged_sessions=True)


def test_a_single_writer_store_may_declare_staged_sessions():
    assert capabilities_of(_Declares(BackendCapabilities(**STAGED))).staged_sessions


def test_a_shared_store_may_declare_it_only_with_revision_enforcement():
    shared = BackendCapabilities(**STAGED, change_notification=True)
    with pytest.raises(TypeError, match="revision_enforcement"):
        capabilities_of(_Declares(shared))
    enforcing = BackendCapabilities(
        **STAGED, change_notification=True, revision_enforcement=True
    )
    assert capabilities_of(_Declares(enforcing)).staged_sessions


@pytest.mark.parametrize("missing", ["incremental_writes", "transactions"])
def test_it_needs_atomic_entity_batches(missing):
    caps = dict(STAGED)
    caps[missing] = False
    if missing == "incremental_writes":
        caps["transactions"] = False
    with pytest.raises(TypeError, match="staged_sessions without"):
        capabilities_of(_Declares(BackendCapabilities(**caps)))


@pytest.mark.parametrize("method", ["merge", "retire", "layers_in_use"])
def test_a_declaration_without_the_layer_store_is_refused(method):
    with pytest.raises(TypeError, match=method):
        capabilities_of(_Declares(BackendCapabilities(**STAGED), omit=(method,)))


def test_the_reference_declares_nothing_new_unless_asked():
    assert not InMemoryGraphPersistenceBackend({}).capabilities().staged_sessions
