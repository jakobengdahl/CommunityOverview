from __future__ import annotations

import pytest

from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _stage


@pytest.fixture(params=["file"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


# ST03
def test_a_pending_heal_is_awaited_before_the_unit_runs(world, monkeypatch):
    sid = world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    order = []

    class Healing:
        def result(self):
            order.append("healed")

    monkeypatch.setattr(world.storage, "_heal_if_needed", lambda: Healing())
    store = world.storage.layer_store
    original = store.merge

    def merge(*a, **k):
        order.append("unit")
        return original(*a, **k)

    monkeypatch.setattr(store, "merge", merge)
    assert world.service.merge_staged_changes(sid, "m")["success"] is True
    assert order == ["healed", "unit"]
