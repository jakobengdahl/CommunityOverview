"""Guards on staged-session diff, merge and discard: races, narrowed readers,
edge endpoints, heals, layer-change ops, outcomes and layer-store checks.

Run over the file backend and, when a server is configured, PostgreSQL;
tests marked with an indirect ``world`` parameter run over the file backend only.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import Edge, Node, layer_store as ls
from backend.core.layer_store import LayerRaced, MergeCheck
from backend.core.session_store import OpError
from backend.core.storage_backends import EntityOperation
from backend.core.tests.layer_store_contract import A, entry
from backend.core.tests.persistence_contract import (
    InMemoryGraphPersistenceBackend,
    node_payload,
    snapshot,
)
from backend.runtime.session_context import bind_session
from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import (
    _items,
    _restage_as_another_instance,
    _stage,
)


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


# ==== Races and cross-instance guards =========================================


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
    assert (res["success"], res["error"]) == (False, "busy")


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


# ==== Edge endpoints ==========================================================


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


def test_a_taken_create_can_merge_without_an_incident_edge_tombstone(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "n", "type": "Actor", "name": "Mine"}], []), {}),
        ("delete_edges", (["ab"],), {"confirmed": True}),
    )
    world.storage.add_nodes(
        [Node(id="n", type="Actor", name="Theirs")],
        [Edge(id="an", source="a", target="n", type="RELATES_TO")],
    )
    # The tombstone is incident to the taken id, but a create is not the
    # delete/recreate update that requires its old edge tombstones.
    _stage(world, sid, ("delete_edges", (["an"],), {"confirmed": True}))
    result = svc.merge_staged_changes(
        sid,
        "m",
        entries=[{"kind": "node", "entity_id": "n"}],
        resolutions=[{"kind": "node", "entity_id": "n", "choice": "take_session"}],
    )
    assert result["success"] is True, result
    assert world.storage.get_node("n").name == "Mine"
    assert ("edge", "an") in world.layer(sid)
    assert "an" in world.storage.edges


def test_a_retype_can_merge_with_an_edge_updated_to_fit_the_new_type(world):
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes(
        [
            Node(id="i", type="Initiative", name="I"),
            Node(id="l", type="Legislation", name="L"),
        ],
        [Edge(id="il", source="i", target="l", type="IMPLEMENTS")],
    )
    _stage(
        world,
        sid,
        ("delete_nodes", (["l"],), {"confirmed": True}),
        ("add_nodes", ([{"id": "l", "type": "Actor", "name": "L"}], []), {}),
        ("add_nodes", ([], [_edge("il", "i", "l")]), {}),
    )
    assert _items(svc.get_staged_changes(sid))[("edge", "il")]["action"] == "update"
    result = svc.merge_staged_changes(sid, "m")
    assert result["success"] is True, result
    assert world.storage.get_node("l").type_str == "Actor"
    assert world.storage.edges["il"].type_str == "RELATES_TO"


def test_a_same_type_update_keeps_legacy_misfit_edges(world):
    # Old/imported data can carry an edge outside today's applicability.
    # Changing a name must not retroactively validate that relationship.
    op = EntityOperation.upsert_edge(
        Edge(id="legacy", source="a", target="b", type="IMPLEMENTS").to_dict()
    )
    with world.storage.write_lock:
        world.storage._external_upsert_edge(op)
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("b",), {"updates": {"name": "Renamed"}}))
    result = svc.merge_staged_changes(sid, "m")
    assert result["success"] is True, result
    assert world.storage.get_node("b").name == "Renamed"
    assert world.storage.edges["legacy"].type_str == "IMPLEMENTS"


def _subtype_edge(world, monkeypatch, endpoint, peer="c"):
    from backend.config import config_loader

    monkeypatch.setitem(
        config_loader._get_loader().config.schema_.relationship_types,
        "SUBTYPE_LINK",
        config_loader.RelationshipTypeConfig(**{f"{endpoint}_types": ["Agency"]}),
    )
    world.storage.add_edge(
        Edge(
            id="subtype-edge",
            source="a" if endpoint == "source" else peer,
            target="a" if endpoint == "target" else peer,
            type="SUBTYPE_LINK",
        )
    )


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_a_subtype_merge_rejudges_kept_edges_and_requires_their_staged_removal(
    world, monkeypatch, endpoint
):
    """A subtype update keeps only edges applicable to the resulting node."""
    _subtype_edge(world, monkeypatch, endpoint)
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"subtypes": ["Agency", "Municipality"]}}),
    )
    assert svc.merge_staged_changes(sid, "valid")["success"] is True
    assert "subtype-edge" in world.storage.edges
    _stage(world, sid, ("update_node", ("a",), {"updates": {"subtypes": []}}))
    result = svc.merge_staged_changes(sid, "refused")
    assert result["error"] == "invalid_merge_request", result
    assert world.storage.get_node("a").subtypes == ["Agency", "Municipality"]
    assert ("node", "a") in world.layer(sid)
    _stage(world, sid, ("delete_edges", (["subtype-edge"],), {"confirmed": True}))
    result = svc.merge_staged_changes(
        sid, "incomplete", entries=[{"kind": "node", "entity_id": "a"}]
    )
    assert result["error"] == "merge_incomplete", result
    assert svc.merge_staged_changes(sid, "complete")["success"] is True
    assert world.storage.get_node("a").subtypes == []
    assert "subtype-edge" not in world.storage.edges


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_a_subtype_change_does_not_disclose_hidden_incident_edges(
    world, monkeypatch, endpoint
):
    """Direct refusals and the diff never count or name hidden edges."""
    _subtype_edge(world, monkeypatch, endpoint, peer="r")
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"subtypes": []}}))
    world.hook.narrow = True
    assert svc.update_node("a", {"subtypes": []}) == {
        "success": False,
        "error": "Node with ID a not found",
    }
    assert svc.get_staged_changes(sid)["items"] == []
    result = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "a"}]
    )
    assert result["error"] == "merge_raced", result
    assert "subtype-edge" not in str(result)
    assert "existing edge" not in str(result)
    assert world.storage.get_node("a").subtypes == ["Agency"]


@pytest.mark.parametrize("world", ["file"], indirect=True)
def test_a_subtype_merge_refuses_an_incident_edge_added_after_planning(
    world, monkeypatch
):
    """Subtype changes carry the same store-side incident checks as retypes."""
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"subtypes": ["Municipality"]}}),
    )
    world.storage.flush()
    backend = world.storage._persistence_backend
    _race(
        world,
        monkeypatch,
        lambda: backend.apply_batch(
            [
                EntityOperation.upsert_edge(
                    Edge(id="new", source="a", target="c", type="RELATES_TO").to_dict()
                )
            ]
        ),
    )
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "merge_raced", result
    assert world.storage.get_node("a").subtypes == ["Agency"]
    assert ("node", "a") in world.layer(sid)


def test_a_source_retype_is_refused_while_a_kept_edge_would_no_longer_fit(world):
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes(
        [
            Node(id="i", type="Initiative", name="I"),
            Node(id="l", type="Legislation", name="L"),
        ],
        [],
    )
    _stage(
        world,
        sid,
        ("delete_nodes", (["i"],), {"confirmed": True}),
        ("add_nodes", ([{"id": "i", "type": "Actor", "name": "I"}], []), {}),
    )
    world.storage.add_edge(Edge(id="il", source="i", target="l", type="IMPLEMENTS"))
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request", result
    assert world.storage.get_node("i").type_str == "Initiative"
    assert ("node", "i") in world.layer(sid)


def test_a_changed_second_kept_edge_refuses_the_whole_retype_merge(world, monkeypatch):
    from backend.service.tests.test_staged_review_guards import _race

    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("add_nodes", ([{"id": "l", "type": "Actor", "name": "L"}], []), {})
    )
    world.storage.add_nodes(
        [
            Node(id="i", type="Initiative", name="I"),
            Node(id="l", type="Legislation", name="L"),
        ],
        [
            Edge(id="first", source="i", target="l", type="RELATES_TO"),
            Edge(id="second", source="i", target="l", type="RELATES_TO"),
        ],
    )
    world.storage.flush()
    edge = world.storage.edges["second"].to_dict()
    operation = EntityOperation.upsert_edge(
        {**edge, "type": "IMPLEMENTS", "revision": edge["revision"] + 1}
    )
    _race(
        world,
        monkeypatch,
        lambda: world.storage._persistence_backend.apply_batch([operation]),
    )
    result = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[{"kind": "node", "entity_id": "l", "choice": "take_session"}],
    )
    assert result["error"] == "merge_raced", result
    assert world.storage.get_node("l").type_str == "Legislation"
    assert ("node", "l") in world.layer(sid)


def test_merged_creates_quarantine_attachments_left_by_the_previous_node(
    world, tmp_path
):
    from backend.core.attachment_store import FileAttachmentStore, hashed_key
    from backend.service.tests.test_node_attachments import NS, _seed, _settings

    store = FileAttachmentStore(tmp_path / "attachments")
    orphan = _seed(store, "n")
    world.service._attachment_settings = _settings(store)
    sid = world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "n", "type": "Actor", "name": "New"}], []), {}),
    )
    assert store.list(NS, "n") == [orphan]
    assert world.service.merge_staged_changes(sid, "m")["success"] is True
    assert store.list(NS, "n") == []
    quarantine = store.root / hashed_key(NS) / ".quarantine"
    (target,) = quarantine.iterdir()
    assert (target / "blobs" / orphan.id).read_bytes() == b"seeded"


# ==== Narrowed readers ========================================================


# SR02
def test_a_node_whose_main_is_hidden_stays_hidden_when_the_staged_version_is_not(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("r",), {"updates": {"metadata": {"origin_graph_id": ""}}}),
    )
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []


# SR03
def test_a_deleted_edge_whose_base_endpoint_is_hidden_is_hidden(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_edges", (["ar"],), {"confirmed": True}))
    world.storage.delete_edges(["ar"])
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []


# SR06 / CP01
def test_an_endpoint_written_after_the_base_may_have_entered_the_view(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_edges", (["ab"],), {"confirmed": True}))
    world.storage.update_node("b", {"summary": "later"})
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []


# SR48 / SR49
def test_a_narrowed_item_does_not_list_unstaged_edges_to_hidden_nodes(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["b"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="br", source="b", target="r", type="RELATES_TO"))
    wide = _items(svc.get_staged_changes(sid))[("node", "b")]
    assert wide["unstaged_edges"] == ["br"]
    world.hook.narrow = True
    # The merge would delete br with b, so b's delete is out of the view.
    assert ("node", "b") not in _items(svc.get_staged_changes(sid))


# SR50
def test_an_edge_create_to_a_hidden_node_with_a_staged_update_is_hidden(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("r",), {"updates": {"description": "y"}}),
        ("add_nodes", ([], [_edge("ar2", "a", "r")]), {}),
    )
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []


# SR19
def test_a_node_taken_as_new_is_blocked_when_a_follower_edge_is_out_of_view(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
        ("add_nodes", ([], [_edge("tr", "t", "r")]), {}),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    world.hook.narrow = True
    res = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}]
    )
    assert res["error"] == "merge_blocked"
    assert world.storage.get_node("t").name == "Theirs"


# SR41
def test_a_manual_value_for_a_field_that_does_not_conflict_is_ignored(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "mine", "summary": "s"}}),
    )
    world.storage.update_node("a", {"description": "theirs"})
    res = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "node",
                "entity_id": "a",
                "choice": "manual",
                "values": {"description": "final", "summary": "override"},
            }
        ],
    )
    assert res["success"] is True, res
    node = world.storage.get_node("a")
    assert (node.description, node.summary) == ("final", "s")


def test_a_node_with_a_hidden_base_stays_hidden_after_main_enters_the_view(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("r",), {"updates": {"description": "mine"}}))
    world.storage.update_node("r", {"metadata": {}})
    world.hook.narrow = True
    assert svc.get_staged_changes(sid)["items"] == []
    result = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "r"}]
    )
    assert result["error"] == "merge_raced"
    assert ("node", "r") in world.layer(sid)


@pytest.mark.parametrize("endpoint", ["r", "Remote Rho", "missing"])
def test_a_narrowed_batch_cannot_create_an_edge_to_an_unseen_endpoint(world, endpoint):
    world.hook.narrow = True
    result = world.service.add_nodes(
        [{"id": "n", "type": "Actor", "name": "New"}], [_edge("nr", "n", endpoint)]
    )
    assert result["success"] is False
    assert result["message"] == "Could not add edge (source or target not found)"
    assert world.storage.get_node("n") is None
    assert "nr" not in world.storage.edges


@pytest.mark.parametrize("metadata", [{}, {"origin_graph_id": "remote"}])
def test_a_narrowed_batch_can_use_new_nodes_and_visible_names(world, metadata):
    world.hook.narrow = True
    result = world.service.add_nodes(
        [{"id": "n", "type": "Actor", "name": "New", "metadata": metadata}],
        [_edge("an", "Alpha", "New")],
    )
    assert result["success"] is True, result
    assert (world.storage.edges["an"].source, world.storage.edges["an"].target) == (
        "a",
        "n",
    )


def test_a_narrowed_update_waits_for_visibility_changes_under_the_write_lock(
    world, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    world.hook.narrow = True
    entered = threading.Event()
    original = world.hook.evaluate

    def evaluate(context):
        decision = original(context)
        if context.target == "update_node":
            entered.set()
        return decision

    world.hook.evaluate = evaluate
    storage = world.storage
    get_node = storage.get_node

    def locked_get_node(node_id):
        assert storage.write_lock._is_owned()
        return get_node(node_id)

    monkeypatch.setattr(storage, "get_node", locked_get_node)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with storage.write_lock:
            pending = pool.submit(
                world.service.update_node, "c", {"description": "must not land"}
            )
            assert entered.wait(5)
            storage.update_node("c", {"metadata": {"origin_graph_id": "remote"}})
        result = pending.result(timeout=5)
    assert result == {"success": False, "error": "Node with ID c not found"}
    assert get_node("c").description == ""


def test_a_retype_with_both_visible_and_hidden_main_edges_cannot_be_selected(world):
    from backend.service.tests.test_staged_review import _recreate_legislation_as_actor

    svc, sid = world.service, _recreate_legislation_as_actor(world)
    world.storage.add_nodes(
        [],
        [
            Edge(id="visible", source="i", target="l", type="IMPLEMENTS"),
            Edge(id="hidden", source="h", target="l", type="IMPLEMENTS"),
        ],
    )
    world.hook.narrow = True
    assert ("node", "l") not in _items(svc.get_staged_changes(sid))
    refused = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "l"}]
    )
    assert refused["error"] == "merge_raced", refused
    assert "hidden" not in str(refused)
    assert world.storage.get_node("l").type_str == "Legislation"


# ==== Pending heals ===========================================================


# ST03
@pytest.mark.parametrize("world", ["file"], indirect=True)
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


# ==== Layer-change ops and session lookup =====================================


# SS01
@pytest.mark.parametrize("op", ["graph_changes_discarded", "graph_changes_merged"])
@pytest.mark.parametrize("world", ["file"], indirect=True)
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
@pytest.mark.parametrize("world", ["file"], indirect=True)
def test_a_layer_change_op_must_carry_its_keys_and_merge_id(world, op):
    sid = world.staged_session()
    session = world.manager.get_session(sid)
    with pytest.raises(OpError):
        world.manager.store.apply_state_op(
            session, {**op, "client_id": "graph-staging"}
        )


# SS04
@pytest.mark.parametrize("world", ["file"], indirect=True)
def test_the_activity_entry_names_the_removed_keys(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    svc.discard_staged_changes(sid)
    entry = world.manager.get_session(sid).activity_log[-1]
    assert entry["affected"]["keys"] == [["node", "a"]]


# SG04
@pytest.mark.parametrize("op", ["diff", "merge", "discard"])
@pytest.mark.parametrize("world", ["file"], indirect=True)
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
@pytest.mark.parametrize("world", ["file"], indirect=True)
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
@pytest.mark.parametrize("world", ["file"], indirect=True)
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


# ==== Outcome of a failed merge ===============================================


def _failing_merge(world, monkeypatch):
    def fail(*a, **k):
        raise ConnectionError("store down")

    monkeypatch.setattr(world.storage.layer_store, "merge", fail)


def _setup(pgw, monkeypatch):
    sid = pgw.staged_session()
    _stage(pgw, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    _failing_merge(pgw, monkeypatch)
    return sid, ("node", "a"), pgw.storage.get_node("a").revision


# ST23
def test_an_unreadable_store_leaves_the_merge_outcome_unknown(pgw, monkeypatch):
    sid, key, _ = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend

    def unreadable(keys):
        raise ConnectionError("no read")

    monkeypatch.setattr(backend, "read_write_evidence", unreadable)
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_outcome_unknown"
    assert key in pgw.layer(sid)


# ST19 / ST22 / ST20
def test_a_second_reading_that_differs_leaves_the_outcome_unknown_and_reloads(
    pgw, monkeypatch
):
    sid, key, revision = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend
    readings = iter([{key: (revision,)}, {key: (revision + 1,)}])
    monkeypatch.setattr(backend, "read_write_evidence", lambda keys: next(readings))
    loaded = []
    original = pgw.storage.load
    monkeypatch.setattr(
        pgw.storage, "load", lambda *a, **k: (loaded.append(1), original(*a, **k))[1]
    )
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_outcome_unknown"
    assert loaded


# ST18 sanity (already killed): both readings unchanged -> write_failed
def test_two_unchanged_readings_rule_the_merge_out(pgw, monkeypatch):
    sid, key, revision = _setup(pgw, monkeypatch)
    backend = pgw.storage._persistence_backend
    monkeypatch.setattr(backend, "read_write_evidence", lambda keys: {key: (revision,)})
    res = pgw.service.merge_staged_changes(sid, "m")
    assert res["error"] == "write_failed"


# ==== Layer-store and merge-plan checks =======================================

# Scratch closing tests for round-4 mutation survivors.

# ---- layer-store level -----------------------------------------------------


def test_M02_legacy_entries_without_a_token_are_told_apart_by_their_seq():
    assert ls.entry_version({"session_seq": 3}) == "seq:3"
    assert ls.entry_version({"session_seq": 3}) != ls.entry_version({"session_seq": 4})
    assert ls.entry_version({"session_seq": 3, "stage_token": ""}) == "seq:3"


@pytest.fixture
def seeded():
    store = {}
    backend = InMemoryGraphPersistenceBackend(store, staged=True)
    backend.save_graph_data(
        snapshot(
            [node_payload("a", revision=1), node_payload("b", revision=2)],
            [],
        )
    )
    backend.record_staged(A)
    return backend


def test_M16_an_entry_check_on_an_absent_entry_fails_the_merge(seeded):
    seeded.stage(A, [entry("node", "b")])
    with pytest.raises(ls.MergeRaced):
        seeded.merge(
            A,
            "m",
            [],
            [MergeCheck.entry_unchanged("node", "a", None)],
            [("node", "b")],
            {},
        )


def test_M17_a_holds_only_check_with_a_malformed_key_is_refused(seeded):
    bad = MergeCheck("layer_holds_only", "*", keys=(("graph", "a"),))
    seeded.stage(A, [entry("node", "b")])
    with pytest.raises(ls.InvalidLayerRequest):
        seeded.merge(A, "m", [], [bad], [("node", "b")], {})
    assert [e["entity_id"] for e in seeded.load_layer(A)] == ["b"]


# ---- PostgreSQL / file: exact with no named expectation ---------------------


def _backends(tmp_path):
    from backend.core.storage_backends import FileGraphPersistenceBackend

    yield FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)


@pytest.fixture(params=["file", "postgres"])
def real_backend(request, tmp_path):
    import uuid

    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.core.tests.test_persistence_contract_postgres import (
        DSN,
        _server_reachable,
        psycopg,
    )

    if request.param == "file":
        yield FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
        return
    if not _server_reachable():
        pytest.skip("no pg")
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend

    schema = f"co_layer_{uuid.uuid4().hex[:16]}"
    b = PostgresGraphPersistenceBackend(DSN, schema=schema, staged_sessions=True)
    yield b
    b.close()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_M22_an_op_that_read_an_empty_layer_is_refused_when_it_grew(real_backend):
    b = real_backend
    b.save_graph_data(snapshot([node_payload("a", revision=1)], []))
    b.record_staged(A)
    b.stage(A, [entry("node", "a", session_seq=1)])
    with pytest.raises(ls.LayerRaced):
        b.stage(A, [entry("node", "c")], expected={}, exact=True)


# ---- service level -----------------------------------------------------------


def _delete_as_another_instance(world, sid, node_id):
    """What another instance stages meanwhile: a first entry for the node, a
    delete (without the incident edges' tombstones, which this build also
    stages, so that only the node's own entry differs)."""
    template = next(iter(world.layer(sid).values()))
    main = world.storage.nodes[node_id]
    world.storage.layer_store.stage(
        sid,
        [
            {
                **template,
                "kind": "node",
                "entity_id": node_id,
                "action": "delete",
                "base": {"id": node_id, "type": main.type_str},
                "staged": None,
                "stage_token": "peer",
            }
        ],
    )


def test_M26_M31_an_entry_an_op_only_looked_at_is_part_of_what_it_rested_on(
    world, monkeypatch
):
    """update_edge composes the edge from its endpoints without changing
    their entries; a peer deleting the target meanwhile (an entry it had none
    of when read) must redo the decision."""
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes([Node(id="res", type="Resource", name="Res")], [])
    world.storage.add_edge(Edge(id="pe", source="a", target="res", type="PRODUCES"))
    _stage(world, sid, ("update_node", ("c",), {"updates": {"description": "x"}}))
    store = world.storage.layer_store
    original = store.stage
    raced = []

    def retype_first(session_id, entries, removals=(), **kwargs):
        if not raced:
            raced.append(True)
            _delete_as_another_instance(world, sid, "res")
        return original(session_id, entries, removals, **kwargs)

    monkeypatch.setattr(store, "stage", retype_first)
    with bind_session(sid):
        result = svc.update_edge("pe", {"label": "x"})
    assert result["success"] is False, result
    assert ("edge", "pe") not in world.layer(sid)


def test_M28_M32_a_stage_tells_another_instances_restage_apart_by_its_token(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"summary": "s"}}))
    first = world.layer(sid)[("node", "a")]
    assert first["stage_token"]
    store = world.storage.layer_store
    original = store.stage
    raced = []

    def other_instance_first(session_id, entries, removals=(), **kwargs):
        if not raced:
            raced.append(True)
            _restage_as_another_instance(world, sid, "a", {"summary": "theirs"})
        return original(session_id, entries, removals, **kwargs)

    monkeypatch.setattr(store, "stage", other_instance_first)
    with bind_session(sid):
        result = svc.update_node("a", {"description": "mine"})
    assert result["success"] is True, result
    staged = world.layer(sid)[("node", "a")]["staged"]
    assert staged == {"summary": "theirs", "description": "mine"}


def test_M32_every_staging_op_stamps_a_token_of_its_own(world):
    sid = world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"summary": "1"}}))
    t1 = world.layer(sid)[("node", "a")]["stage_token"]
    _stage(world, sid, ("update_node", ("a",), {"updates": {"summary": "2"}}))
    t2 = world.layer(sid)[("node", "a")]["stage_token"]
    assert t1 and t2 and t1 != t2


def test_M29_M30_an_edge_added_by_name_is_redecided_when_the_layer_grew(
    world, monkeypatch
):
    """The name was resolved over every entry of the layer; a peer's entry for
    a node the op never looked up (c, now also named Beta) changes the answer,
    and the op must be decided again on it."""
    svc, sid = world.service, world.staged_session()
    store = world.storage.layer_store
    original = store.stage
    raced = []

    def rename_first(session_id, entries, removals=(), **kwargs):
        if not raced:
            raced.append(True)
            _stage(world, sid, ("update_node", ("c",), {"updates": {"name": "Beta"}}))
        return original(session_id, entries, removals, **kwargs)

    monkeypatch.setattr(store, "stage", rename_first)
    with bind_session(sid):
        result = svc.add_nodes(
            [], [{"id": "x", "source": "a", "target": "Beta", "type": "RELATES_TO"}]
        )
    assert result["success"] is True, result
    assert world.layer(sid)[("edge", "x")]["staged"]["target"] == "c"


def test_M42_a_node_delete_is_out_of_view_when_any_main_edge_is_hidden(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="ca", source="c", target="a", type="RELATES_TO"))
    world.storage.add_edge(Edge(id="cr", source="c", target="r", type="RELATES_TO"))
    world.hook.narrow = True
    assert ("node", "c") not in _items(svc.get_staged_changes(sid))


def test_M50_an_unrelated_staged_edge_retargeted_in_the_window_fails_an_as_new_merge(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
        (
            "add_nodes",
            ([], [{"id": "e2", "source": "a", "target": "c", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    store = world.storage.layer_store
    original = store.merge

    def retarget_first(*args, **kwargs):
        # Same key, new content: names t, which the merge is renaming.
        e = world.layer(sid)[("edge", "e2")]
        world.storage.layer_store.stage(
            sid,
            [
                {
                    **e,
                    "staged": {**e["staged"], "target": "t"},
                    "stage_token": "retargeted",
                }
            ],
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "merge", retarget_first)
    result = svc.merge_staged_changes(
        sid,
        "m",
        entries=[{"kind": "node", "entity_id": "t"}],
        resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}],
    )
    assert result["error"] == "merge_raced", result


# ---- injected entries (what staging would have refused) ----------------------


def _inject_create(world, sid, node_id, mutate):
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": node_id, "type": "Actor", "name": "X"}], []), {}),
    )
    e = world.layer(sid)[("node", node_id)]
    staged = mutate(dict(e["staged"]))
    world.storage.layer_store.stage(
        sid, [{**e, "staged": staged, "stage_token": "injected"}]
    )


@pytest.mark.parametrize("node_type", ["SavedView", "VisualizationView"])
def test_M55_M56_a_created_saved_view_is_checked_whole(world, node_type):
    svc, sid = world.service, world.staged_session()
    remote = [
        {"id": "x", "type": "image", "image": {"url": "https://example.org/a.png"}}
    ]

    def mutate(doc):
        doc["type"] = node_type
        doc["metadata"] = {"annotations": remote}
        return doc

    _inject_create(world, sid, "sv", mutate)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request", result
    assert "sv" not in world.storage.nodes


def test_M52_deep_metadata_in_a_staged_create_is_refused(world):
    svc, sid = world.service, world.staged_session()
    deep = current = {}
    for _ in range(850):
        current["k"] = {}
        current = current["k"]

    def mutate(doc):
        doc["metadata"] = {"k": deep}
        return doc

    _inject_create(world, sid, "dn", mutate)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request", result
    assert "dn" not in world.storage.nodes


def test_M63_a_staged_create_the_model_rejects_is_refused(world):
    svc, sid = world.service, world.staged_session()

    def mutate(doc):
        doc["name"] = "x" * 1000
        return doc

    _inject_create(world, sid, "bad", mutate)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request", result
    assert "bad" not in world.storage.nodes


def test_M60_a_staged_edge_the_schema_refuses_between_its_endpoints_is_refused(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            ([], [{"id": "e2", "source": "a", "target": "c", "type": "RELATES_TO"}]),
            {},
        ),
    )
    e = world.layer(sid)[("edge", "e2")]
    staged = {**e["staged"], "type": "IMPLEMENTS"}
    world.storage.layer_store.stage(
        sid, [{**e, "staged": staged, "stage_token": "injected"}]
    )
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request", result
    assert "e2" not in world.storage.edges


def test_M59_an_edge_cannot_point_at_a_node_the_merge_itself_moves_out_of_view(world):
    from backend.service.tests.test_staged_review import (
        _recreated_edge_conflicting_on_target,
    )

    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes([Node(id="d", type="Actor", name="Delta")], [])
    # A blank origin is local, so d stays in the narrowed view while its
    # origin field conflicts (staged " " against main "  ").
    _stage(
        world,
        sid,
        (
            "update_node",
            ("d",),
            {"updates": {"metadata": {"origin_graph_id": " "}}, "metadata_merge": True},
        ),
    )
    world.storage.update_node(
        "d", {"metadata": {"origin_graph_id": "  "}}, metadata_merge=True
    )
    _recreated_edge_conflicting_on_target(world, sid)
    world.hook.narrow = True
    result = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "edge",
                "entity_id": "ab",
                "choice": "manual",
                "values": {"target": "d"},
            },
            {
                "kind": "node",
                "entity_id": "d",
                "choice": "manual",
                "values": {"metadata.origin_graph_id": "remote"},
            },
        ],
    )
    assert result["error"] == "invalid_merge_request", result
    assert result["message"] == "edge 'ab': an endpoint does not exist"
    assert world.storage.edges["ab"].target == "c"
    assert world.storage.nodes["d"].metadata == {"origin_graph_id": "  "}


def test_M57_an_edge_cannot_point_at_a_node_the_merge_itself_deletes(world):
    from backend.service.tests.test_staged_review import (
        _recreated_edge_conflicting_on_target,
    )

    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes([Node(id="d", type="Actor", name="Delta")], [])
    _stage(world, sid, ("delete_nodes", (["d"],), {"confirmed": True}))
    _recreated_edge_conflicting_on_target(world, sid)
    result = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "edge",
                "entity_id": "ab",
                "choice": "manual",
                "values": {"target": "d"},
            }
        ],
    )
    assert result["error"] == "invalid_merge_request", result
    assert result["message"] == "edge 'ab': an endpoint does not exist"
    assert "d" in world.storage.nodes
