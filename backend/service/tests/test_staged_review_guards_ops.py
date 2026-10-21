from __future__ import annotations

import asyncio

import pytest

from backend.core.session_store import OpError
from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _stage


@pytest.fixture(params=["file"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


# SS01
@pytest.mark.parametrize("op", ["graph_changes_discarded", "graph_changes_merged"])
def test_clients_cannot_post_the_layer_change_ops(world, op):
    sid = world.staged_session()
    with pytest.raises(OpError):
        asyncio.run(
            world.manager.apply_ops(
                sid, "browser", None, [{"op": op, "removals": [], "merge_id": "m"}]
            )
        )


# SS02 / SS03
@pytest.mark.parametrize(
    "op",
    [
        {"op": "graph_changes_discarded", "removals": "x"},
        {"op": "graph_changes_merged", "removals": []},
    ],
)
def test_a_layer_change_op_must_carry_its_keys_and_merge_id(world, op):
    sid = world.staged_session()
    session = world.manager.get_session(sid)
    with pytest.raises(OpError):
        world.manager.store.apply_state_op(
            session, {**op, "client_id": "graph-staging"}
        )


# SS04
def test_the_activity_entry_names_the_removed_keys(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    svc.discard_staged_changes(sid)
    entry = world.manager.get_session(sid).activity_log[-1]
    assert entry["affected"]["keys"] == [["node", "a"]]


# SG04
@pytest.mark.parametrize("op", ["diff", "merge", "discard"])
def test_a_malformed_session_id_is_not_found(world, op):
    svc = world.service
    answer = {
        "diff": lambda: svc.get_staged_changes("nope"),
        "merge": lambda: svc.merge_staged_changes("nope", "m"),
        "discard": lambda: svc.discard_staged_changes("nope"),
    }[op]()
    assert answer["success"] is False and answer["error"] == "session_not_found"


# SM02
@pytest.mark.parametrize("op", ["merge", "discard"])
def test_a_session_without_its_document_is_refused(world, op, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    monkeypatch.setattr(world.manager.store, "get", lambda _sid: None)
    main = world.main()
    answer = (
        svc.merge_staged_changes(sid, "m")
        if op == "merge"
        else svc.discard_staged_changes(sid)
    )
    assert answer["error"] == "session_not_found"
    assert world.main() == main and ("node", "a") in world.layer(sid)


# SV26
@pytest.mark.parametrize("op", ["diff", "discard"])
def test_a_session_retired_after_it_was_resolved_is_refused_not_read_as_empty(
    world, op, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    manager_modes = world.manager.write_modes
    monkeypatch.setattr(world.service._session_gate, "named_mode", lambda _s: "staged")
    monkeypatch.setattr(manager_modes, "staged_status", lambda _s: "retired")
    answer = (
        svc.get_staged_changes(sid) if op == "diff" else svc.discard_staged_changes(sid)
    )
    assert answer["success"] is False and answer["error"] == "session_not_found"
