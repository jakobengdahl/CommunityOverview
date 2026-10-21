from __future__ import annotations

import pytest

from backend.core import Edge, Node
from backend.core.layer_store import LayerRaced
from backend.runtime.session_context import bind_session
from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _items, _stage


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


@pytest.fixture
def pgw(request, tmp_path):
    request.param = "postgres"
    yield from make_world(request, tmp_path)


@pytest.fixture
def other(pgw):
    from backend.core import GraphStorage
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    from backend.core.tests.test_persistence_contract_postgres import DSN

    backend = PostgresGraphPersistenceBackend(
        DSN, schema=pgw.storage._persistence_backend.schema, staged_sessions=True
    )
    o = GraphStorage(persistence_backend=backend)
    yield o
    o.shutdown_events()
    backend.close()


def _in_window(world, fn):
    """Run `fn` against main after the plan, before the store's unit."""
    store = world.storage.layer_store
    original = store.merge

    def merge(*args, **kwargs):
        fn()
        return original(*args, **kwargs)

    return store, merge


def _race(world, monkeypatch, fn):
    store, merge = _in_window(world, fn)
    monkeypatch.setattr(store, "merge", merge)


def _edge(i, s, t):
    return {"id": i, "source": s, "target": t, "type": "RELATES_TO"}


# SR04 / CP03
def test_a_narrowed_caller_does_not_see_an_edge_create_to_a_hidden_endpoint(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("add_nodes", ([], [_edge("ar2", "a", "r")]), {}))
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []
    assert svc.merge_staged_changes(sid, "m")["merged"] == []
    assert "ar2" not in world.storage.edges


# SR22
def test_as_new_moves_an_edge_that_leaves_the_new_node(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
        ("add_nodes", ([], [_edge("ta", "t", "a")]), {}),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    res = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}]
    )
    new_id = {i["entity_id"]: i for i in res["merged"]}["t"]["new_entity_id"]
    assert world.storage.edges["ta"].source == new_id


# SR27 node delete expectation
def test_a_node_changed_inside_the_window_fails_its_delete(pgw, other, monkeypatch):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    _race(world, monkeypatch, lambda: other.update_node("c", {"summary": "z"}))
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_raced"
    assert "c" in world.storage.nodes and ("node", "c") in world.layer(sid)


# SR28 edge delete expectation
def test_an_edge_changed_inside_the_window_fails_its_delete(pgw, other, monkeypatch):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_edges", (["ab"],), {"confirmed": True}))
    _race(
        world,
        monkeypatch,
        lambda: other.update_edge("ab", {"metadata": {"k": 1}}),
    )
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_raced"
    assert "ab" in world.storage.edges


# SR32 / ST31
def test_a_node_created_inside_the_window_fails_a_create(pgw, other, monkeypatch):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("add_nodes", ([{"id": "n", "type": "Actor", "name": "M"}], []), {})
    )
    _race(
        world,
        monkeypatch,
        lambda: other.add_nodes([Node(id="n", type="Actor", name="T")], []),
    )
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_raced"
    assert ("node", "n") in world.layer(sid)


# SR33
def test_a_node_changed_inside_the_window_fails_a_replacing_create(
    pgw, other, monkeypatch
):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("add_nodes", ([{"id": "t", "type": "Actor", "name": "M"}], []), {})
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    other.load(bootstrap_if_missing=False)
    _race(world, monkeypatch, lambda: other.update_node("t", {"summary": "z"}))
    take = [{"kind": "node", "entity_id": "t", "choice": "take_session"}]
    assert (
        svc.merge_staged_changes(sid, "m", resolutions=take)["error"] == "merge_raced"
    )


# SR26
def test_an_endpoint_deleted_inside_the_window_fails_an_edge_create(
    pgw, other, monkeypatch
):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("add_nodes", ([], [_edge("ac", "a", "c")]), {}))
    _race(world, monkeypatch, lambda: other.delete_nodes(["c"], confirmed=True))
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_raced"
    assert "ac" not in world.storage.edges


# SR25
def test_an_edge_added_inside_the_window_fails_a_node_delete(pgw, other, monkeypatch):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["b"],), {"confirmed": True}))
    _race(
        world,
        monkeypatch,
        lambda: other.add_edge(
            Edge(id="cb", source="c", target="b", type="RELATES_TO")
        ),
    )
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_raced"
    assert "b" in world.storage.nodes


# SR29
def test_an_edge_added_then_changed_fails_a_taken_conflicting_delete(
    pgw, other, monkeypatch
):
    world = pgw
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["b"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="cb", source="c", target="b", type="RELATES_TO"))
    other.load(bootstrap_if_missing=False)
    _race(world, monkeypatch, lambda: other.update_edge("cb", {"metadata": {"k": 1}}))
    take = [{"kind": "node", "entity_id": "b", "choice": "take_session"}]
    assert (
        svc.merge_staged_changes(sid, "m", resolutions=take)["error"] == "merge_raced"
    )
    assert "b" in world.storage.nodes


# SR11
def test_a_noop_touch_of_a_node_does_not_conflict_its_delete(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    world.storage.update_node("c", {})
    diff = _items(svc.get_staged_changes(sid))
    assert diff[("node", "c")]["status"] == "clean"


# SR52
def test_a_delete_main_already_made_is_a_noop(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    world.storage.delete_nodes(["c"], confirmed=True)
    before = len(world.events)
    res = svc.merge_staged_changes(sid, "m")
    assert {i["entity_id"]: i["outcome"] for i in res["merged"]}["c"] == "noop"
    assert len(world.events) == before


# SR46
def test_a_resolution_for_an_unselected_entry_is_invalid(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    res = svc.merge_staged_changes(
        sid,
        "m",
        entries=[{"kind": "node", "entity_id": "a"}],
        resolutions=[{"kind": "node", "entity_id": "zz", "choice": "keep_main"}],
    )
    assert res["error"] == "invalid_merge_request"


# SR51
def test_a_clean_entry_takes_only_take_session(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    res = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "a", "choice": "as_new"}]
    )
    assert res["error"] == "invalid_merge_request"


# SR54
def test_a_conflicting_delete_cannot_be_taken_as_new(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    world.storage.update_node("c", {"description": "changed"})
    res = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "c", "choice": "as_new"}]
    )
    assert res["error"] == "merge_blocked"


# SV11 / SV12
def test_a_narrowed_discard_of_a_named_hidden_entry_keeps_it(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "x"}}),
        ("update_node", ("r",), {"updates": {"description": "y"}}),
    )
    world.hook.narrow = True
    res = svc.discard_staged_changes(
        sid,
        entries=[
            {"kind": "node", "entity_id": "a"},
            {"kind": "node", "entity_id": "r"},
        ],
    )
    assert res["removed_entries"] == [["node", "a"]]
    assert ("node", "r") in world.layer(sid)


def test_a_malformed_discard_selection_is_refused_not_partly_applied(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    res = svc.discard_staged_changes(
        sid,
        entries=[
            {"kind": "node", "entity_id": "a"},
            {"kind": "bogus", "entity_id": "a"},
        ],
    )
    assert res["error"] == "invalid_merge_request"
    assert ("node", "a") in world.layer(sid)


# SV16 / SV17 / SM01
@pytest.mark.parametrize("op", ["merge", "discard"])
def test_merge_and_discard_are_refused_while_the_session_is_busy(
    world, op, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    main = world.main()
    import types

    monkeypatch.setattr(
        world.manager, "_lock", lambda _sid: types.SimpleNamespace(locked=lambda: True)
    )
    res = (
        svc.merge_staged_changes(sid, "m")
        if op == "merge"
        else svc.discard_staged_changes(sid)
    )
    assert res["success"] is False
    assert ("node", "a") in world.layer(sid) and world.main() == main


# SV24
def test_staging_that_always_races_answers_busy(world, monkeypatch):
    sid = world.staged_session()

    def always(*a, **k):
        raise LayerRaced(sid, ("node", "a"))

    monkeypatch.setattr(world.storage.layer_store, "stage", always)
    with bind_session(sid):
        res = world.service.update_node("a", {"description": "x"})
    assert res["success"] is False and res["error"] != ""


# SV25 / SV29
def test_a_narrowed_replay_names_only_what_its_view_covers(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "x"}}),
        ("update_node", ("r",), {"updates": {"description": "y"}}),
        ("delete_edges", (["ar"],), {"confirmed": True}),
    )
    assert svc.merge_staged_changes(sid, "m")["success"] is True
    world.hook.narrow = True
    again = svc.merge_staged_changes(sid, "m")
    assert again["replayed"] is True
    assert [(i["kind"], i["entity_id"]) for i in again["merged"]] == [("node", "a")]


# ST01
def test_repeating_a_merge_of_named_entries_replays_even_when_they_are_gone(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    sel = [{"kind": "node", "entity_id": "a"}]
    first = svc.merge_staged_changes(sid, "m", entries=sel)
    again = svc.merge_staged_changes(sid, "m", entries=sel)
    assert again["replayed"] is True and again["merged"] == first["merged"]


# ST07
def test_a_merge_recorded_between_lookup_and_unit_replays_and_writes_nothing(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    assert svc.merge_staged_changes(sid, "m")["success"] is True
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "y"}}))
    store = world.storage.layer_store
    original = store.load_merge
    calls = []

    def blind_once(merge_id):
        calls.append(1)
        return None if len(calls) == 1 else original(merge_id)

    monkeypatch.setattr(store, "load_merge", blind_once)
    events, main = len(world.events), world.main()
    res = svc.merge_staged_changes(sid, "m")
    assert res["replayed"] is True
    assert len(world.events) == events and world.main() == main


# SM06
def test_a_failing_announcement_never_undoes_the_merge(world, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))

    def boom(*a, **k):
        raise RuntimeError("fanout")

    monkeypatch.setattr(world.manager.store, "apply_state_op", boom)
    res = svc.merge_staged_changes(sid, "m")
    assert res["success"] is True and res["session_seq"] is None
    assert world.storage.get_node("a").description == "x"
