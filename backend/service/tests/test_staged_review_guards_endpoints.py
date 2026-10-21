from __future__ import annotations

import pytest

from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _stage


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


def _edge(i, s, t):
    return {"id": i, "source": s, "target": t, "type": "RELATES_TO"}


# SV29
def test_a_narrowed_replay_leaves_out_a_kept_edge_with_a_hidden_endpoint(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_edges", (["ar"],), {"confirmed": True}))
    keep = [{"kind": "edge", "entity_id": "ar", "choice": "keep_main"}]
    assert svc.merge_staged_changes(sid, "m", resolutions=keep)["success"] is True
    assert "ar" in world.storage.edges
    world.hook.narrow = True
    assert svc.merge_staged_changes(sid, "m")["merged"] == []


# CP03
def test_an_edge_create_to_an_endpoint_main_deleted_is_hidden_from_a_narrowed_caller(
    world,
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("add_nodes", ([], [_edge("ac", "a", "c")]), {}))
    world.storage.delete_nodes(["c"], confirmed=True)
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []
