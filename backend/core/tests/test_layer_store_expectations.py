import pytest

import backend.core.layer_store as ls
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.tests.layer_store_contract import A, LayerStoreContract, entry


class TestFile(LayerStoreContract):
    @pytest.fixture
    def factory(self, tmp_path):
        path = tmp_path / "graph.json"
        return lambda: FileGraphPersistenceBackend(path, staged_sessions=True)

    # LS05
    def test_every_expectation_is_verified_not_only_the_first(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", session_seq=1)])
        before = factory().load_layer(A)
        with pytest.raises(ls.LayerRaced) as raised:
            seeded.stage(
                A,
                [entry("node", "a", session_seq=9)],
                expected={("node", "a"): 1, ("node", "b"): 1},
            )
        assert raised.value.key == ("node", "b")
        assert factory().load_layer(A) == before
