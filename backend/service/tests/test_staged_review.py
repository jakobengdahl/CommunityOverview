"""Diff, merge and discard of a staged session's changes
(docs/SESSION_OVERLAY_CONTRACT.md §10-§12).

Run over the file backend and, when a server is configured, PostgreSQL. The
seeded graph is test_composed_reads': nodes a, b, c and remote r (outside a
narrowed reader's view), edges ab, bc and ar.
"""

from __future__ import annotations

import pytest

from backend.core import Edge, Node
from backend.core.events.models import EventContext
from backend.runtime.session_context import bind_session
from backend.service.tests.test_composed_reads import REMOTE, make_world


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


def _items(diff):
    return {(i["kind"], i["entity_id"]): i for i in diff["items"]}


def _stage(world, sid, *calls):
    with bind_session(sid):
        for name, args, kwargs in calls:
            result = getattr(world.service, name)(*args, **kwargs)
            assert result.get("success", True), result


def _graph_events(world):
    return [e for e in world.events if e.event_type.value.startswith(("node", "edge"))]


# -- §10 the diff ---------------------------------------------------------------------


def test_the_diff_shows_base_staged_and_main_per_field_and_flags_only_real_conflicts(
    world,
):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "mine", "summary": "s"}}),
        (
            "update_node",
            ("b",),
            {"updates": {"metadata": {"k": 1}}, "metadata_merge": True},
        ),
    )
    # Main moves on: a's description to something else, a's summary to the
    # staged value, and b's name, which the session did not stage.
    world.storage.update_node("a", {"description": "theirs", "summary": "s"})
    world.storage.update_node("b", {"name": "Beta 2"})
    items = _items(svc.get_staged_changes(sid))
    a = items[("node", "a")]
    assert a["fields"]["description"] == {
        "base": "",
        "staged": "mine",
        "main": "theirs",
        "conflict": True,
    }
    assert a["fields"]["summary"]["conflict"] is False
    assert a["status"] == "conflict"
    assert a["main_revision"] == world.storage.get_node("a").revision
    b = items[("node", "b")]
    assert b["fields"] == {
        "metadata.k": {"base": None, "staged": 1, "main": None, "conflict": False}
    }
    # b's revision moved, but no field it stages: clean.
    assert b["status"] == "clean"
    assert "base" not in a and a["staged_by"] == "anonymous"


def test_each_status_is_reported_where_the_contract_puts_it(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "n", "type": "Actor", "name": "N"}], []), {}),
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "T"}], []), {}),
        ("update_node", ("c",), {"updates": {"description": "x"}}),
        (
            "add_nodes",
            ([], [{"id": "bn", "source": "b", "target": "n", "type": "RELATES_TO"}]),
            {},
        ),
        ("delete_nodes", (["b"],), {"confirmed": True}),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Taken")], [])
    world.storage.delete_nodes(["c"], confirmed=True)
    world.storage.add_nodes([Node(id="d", type="Actor", name="D")], [])
    world.storage.add_edge(Edge(id="bd", source="b", target="d", type="RELATES_TO"))
    items = _items(svc.get_staged_changes(sid))
    assert items[("node", "n")]["status"] == "clean"
    assert items[("node", "t")]["status"] == "id_taken"
    assert items[("node", "c")]["status"] == "orphaned"
    # b's delete meets an edge main added since.
    assert items[("node", "b")]["status"] == "conflict"
    assert items[("node", "b")]["unstaged_edges"] == ["bd"]
    # The tombstone of bc, whose node c main deleted, has nothing left to do.
    assert items[("edge", "bc")]["status"] == "clean"
    assert items[("edge", "bc")]["fields"] == {}
    # bn was removed with b's staged delete (create then delete).
    assert ("edge", "bn") not in items
    assert items[("node", "b")]["depends_on"] == [
        {"kind": "edge", "entity_id": "ab"},
        {"kind": "edge", "entity_id": "bc"},
    ]


def test_an_edge_whose_endpoint_main_deleted_is_dangling(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            ([], [{"id": "ac", "source": "a", "target": "c", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.delete_nodes(["c"], confirmed=True)
    item = _items(svc.get_staged_changes(sid))[("edge", "ac")]
    assert item["status"] == "dangling"


def test_a_delete_of_an_entity_main_changed_conflicts_on_the_changed_fields(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_edges", (["ab"],), {"confirmed": True}))
    world.storage.update_edge("ab", {"label": "now labelled"})
    item = _items(svc.get_staged_changes(sid))[("edge", "ab")]
    assert item["status"] == "conflict"
    assert item["fields"] == {
        "label": {"base": "", "staged": None, "main": "now labelled", "conflict": True}
    }


def test_an_edge_create_depends_on_its_endpoint_creates(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            (
                [{"id": "n", "type": "Actor", "name": "N"}],
                [{"id": "an", "source": "a", "target": "n", "type": "RELATES_TO"}],
            ),
            {},
        ),
    )
    items = _items(svc.get_staged_changes(sid))
    assert items[("edge", "an")]["depends_on"] == [{"kind": "node", "entity_id": "n"}]
    assert items[("node", "n")]["depends_on"] == []


def test_a_direct_session_has_an_empty_diff_and_nothing_to_merge_or_discard(world):
    did = world.manager.create_session("direct", "direct").id
    assert world.service.get_staged_changes(did) == {
        "success": True,
        "session_id": did,
        "write_mode": "direct",
        "items": [],
        "count": 0,
    }
    merged = world.service.merge_staged_changes(did, "m")
    assert merged["error"] == "session_not_staged"
    assert world.service.discard_staged_changes(did)["error"] == "session_not_staged"


@pytest.mark.parametrize("op", ["diff", "merge", "discard"])
def test_unknown_and_retired_sessions_are_not_found(world, op):
    sid = world.staged_session()
    world.manager.delete_session_sync(sid, discard_staged=True)
    for name in (sid, "9999-9999-9999-9999", "not a session"):
        result = {
            "diff": lambda: world.service.get_staged_changes(name),
            "merge": lambda: world.service.merge_staged_changes(name, "m"),
            "discard": lambda: world.service.discard_staged_changes(name),
        }[op]()
        assert result["error"] == "session_not_found", (name, result)


@pytest.mark.parametrize(
    "op,action,target,operation",
    [
        ("diff", "read", "get_staged_changes", ""),
        ("merge", "mutate", "merge_staged_changes", "merge"),
        ("discard", "mutate", "discard_staged_changes", "discard"),
    ],
)
def test_the_hook_is_asked_first_and_a_refusal_tells_nothing_of_the_session(
    world, op, action, target, operation
):
    sid = world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    world.hook.deny = True
    calls = {
        "diff": lambda s: world.service.get_staged_changes(s),
        "merge": lambda s: world.service.merge_staged_changes(s, "m"),
        "discard": lambda s: world.service.discard_staged_changes(s),
    }
    answers = [calls[op](s) for s in (sid, "9999-9999-9999-9999")]
    for answer in answers:
        assert answer["error_code"] == "access_denied"
    assert answers[0] == answers[1]
    context = world.hook.contexts[-1]
    assert (context.action, context.target) == (action, target)
    assert context.graph_operation == operation
    assert context.session_id == "9999-9999-9999-9999"
    world.hook.deny = False
    assert ("node", "a") in world.layer(sid)


# -- §11 merge ----------------------------------------------------------------------


def test_a_clean_merge_applies_every_entry_removes_it_and_fires_direct_events(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            (
                [{"id": "n", "type": "Actor", "name": "New"}],
                [{"id": "an", "source": "a", "target": "n", "type": "RELATES_TO"}],
            ),
            {},
        ),
        ("update_node", ("a",), {"updates": {"description": "merged"}}),
        ("delete_nodes", (["c"],), {"confirmed": True}),
    )
    before = len(world.events)
    revision = world.storage.get_node("a").revision
    result = svc.merge_staged_changes(sid, "merge-1")
    assert result["success"] is True, result
    assert world.layer(sid) == {}
    assert world.storage.get_node("a").description == "merged"
    assert world.storage.get_node("a").revision > revision
    assert "n" in world.storage.nodes and "an" in world.storage.edges
    assert "c" not in world.storage.nodes and "bc" not in world.storage.edges
    fired = [
        (e.event_type.value, e.entity.id, e.origin.event_origin)
        for e in world.events[before:]
    ]
    assert fired == [
        ("node.update", "a", "session-merge"),
        ("node.create", "n", "session-merge"),
        ("edge.delete", "bc", "session-merge"),
        ("edge.create", "an", "session-merge"),
        ("node.delete", "c", "session-merge"),
    ]
    for event in world.events[before:]:
        assert event.origin.event_correlation_id == "merge-1"
        assert event.origin.staged_by == "anonymous"
        assert event.origin.to_dict()["staged_by"] == "anonymous"
    by_id = {(i["kind"], i["entity_id"]): i for i in result["merged"]}
    assert by_id[("node", "a")]["revision"] == world.storage.get_node("a").revision
    assert by_id[("node", "c")]["outcome"] == "applied"
    op = world.published[-1][1]["op"]
    assert op["op"] == "graph_changes_merged" and op["merge_id"] == "merge-1"
    assert sorted(map(tuple, op["removals"])) == sorted(by_id)
    assert set(op) == {"op", "merge_id", "removals", "seq"}


def test_events_of_other_writes_carry_no_staged_by_key():
    assert "staged_by" not in EventContext(event_origin="mcp").to_dict()


def test_an_unresolved_conflict_blocks_the_whole_merge_and_writes_nothing(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "mine"}}),
        ("update_node", ("b",), {"updates": {"description": "clean"}}),
    )
    world.storage.update_node("a", {"description": "theirs"})
    main, layer, events = world.main(), world.layer(sid), len(world.events)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "merge_blocked"
    assert [(i["kind"], i["entity_id"]) for i in result["items"]] == [("node", "a")]
    assert result["items"][0]["fields"]["description"]["main"] == "theirs"
    assert world.main() == main and world.layer(sid) == layer
    assert len(world.events) == events
    assert world.storage.layer_store.load_merge("m") is None


@pytest.mark.parametrize(
    "choice,values,expected",
    [
        ("take_session", None, "mine"),
        ("keep_main", None, "theirs"),
        ("manual", {"description": "agreed"}, "agreed"),
    ],
)
def test_a_conflicting_update_merges_as_resolved(world, choice, values, expected):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "mine", "summary": "s"}}),
    )
    world.storage.update_node("a", {"description": "theirs"})
    resolution = {"kind": "node", "entity_id": "a", "choice": choice}
    if values is not None:
        resolution["values"] = values
    result = svc.merge_staged_changes(sid, "m", resolutions=[resolution])
    assert result["success"] is True, result
    node = world.storage.get_node("a")
    assert node.description == expected
    assert node.summary == ("" if choice == "keep_main" else "s")
    assert world.layer(sid) == {}


def test_a_manual_resolution_must_cover_every_conflicting_field(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "mine", "summary": "s"}}),
    )
    world.storage.update_node("a", {"description": "theirs", "summary": "other"})
    partial = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "node",
                "entity_id": "a",
                "choice": "manual",
                "values": {"summary": "x"},
            }
        ],
    )
    assert partial["error"] == "merge_blocked"
    stray = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "node",
                "entity_id": "a",
                "choice": "manual",
                "values": {"summary": "x", "description": "y", "name": "z"},
            }
        ],
    )
    assert stray["error"] == "invalid_merge_request"
    assert world.storage.get_node("a").description == "theirs"


def test_as_new_leaves_the_main_node_alone_and_moves_staged_edges_along(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
        (
            "add_nodes",
            ([], [{"id": "at", "source": "a", "target": "t", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    only_node = svc.merge_staged_changes(
        sid,
        "m0",
        entries=[{"kind": "node", "entity_id": "t"}],
        resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}],
    )
    assert only_node["error"] == "merge_incomplete"
    result = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}]
    )
    assert result["success"] is True, result
    new_id = {i["entity_id"]: i for i in result["merged"]}["t"]["new_entity_id"]
    assert world.storage.get_node("t").name == "Theirs"
    assert world.storage.get_node(new_id).name == "Mine"
    assert world.storage.edges["at"].target == new_id


def test_dropping_a_taken_create_makes_its_edges_dangling_until_resolved(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
        (
            "add_nodes",
            ([], [{"id": "at", "source": "a", "target": "t", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    drop = [{"kind": "node", "entity_id": "t", "choice": "keep_main"}]
    blocked = svc.merge_staged_changes(sid, "m", resolutions=drop)
    assert blocked["error"] == "merge_blocked"
    assert blocked["items"][0]["status"] == "dangling"
    attach = drop + [{"kind": "edge", "entity_id": "at", "choice": "take_session"}]
    result = svc.merge_staged_changes(sid, "m", resolutions=attach)
    assert result["success"] is True, result
    assert world.storage.get_node("t").name == "Theirs"
    assert (world.storage.edges["at"].source, world.storage.edges["at"].target) == (
        "a",
        "t",
    )


def test_an_edge_to_an_endpoint_main_deleted_can_only_be_dropped(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            ([], [{"id": "ac", "source": "a", "target": "c", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.delete_nodes(["c"], confirmed=True)
    take = [{"kind": "edge", "entity_id": "ac", "choice": "take_session"}]
    assert (
        svc.merge_staged_changes(sid, "m", resolutions=take)["error"] == "merge_blocked"
    )
    keep = [{"kind": "edge", "entity_id": "ac", "choice": "keep_main"}]
    assert svc.merge_staged_changes(sid, "m", resolutions=keep)["success"] is True
    assert "ac" not in world.storage.edges and world.layer(sid) == {}


def test_an_orphaned_update_is_recreated_from_its_base_on_take_session(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("c",), {"updates": {"description": "kept"}}))
    world.storage.delete_nodes(["c"], confirmed=True)
    assert svc.merge_staged_changes(sid, "m")["error"] == "merge_blocked"
    take = [{"kind": "node", "entity_id": "c", "choice": "take_session"}]
    assert svc.merge_staged_changes(sid, "m", resolutions=take)["success"] is True
    node = world.storage.get_node("c")
    assert (node.name, node.description) == ("Gamma", "kept")


def test_take_session_on_a_conflicting_node_delete_deletes_the_new_edges_too(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["b"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="cb", source="c", target="b", type="RELATES_TO"))
    take = [{"kind": "node", "entity_id": "b", "choice": "take_session"}]
    result = svc.merge_staged_changes(sid, "m", resolutions=take)
    assert result["success"] is True, result
    assert "b" not in world.storage.nodes
    assert not {"ab", "bc", "cb"} & set(world.storage.edges)


def test_a_selection_must_be_closed_under_dependencies(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["b"],), {"confirmed": True}))
    result = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "b"}]
    )
    assert result["error"] == "merge_incomplete"
    assert result["missing"] == [
        {"kind": "edge", "entity_id": "ab"},
        {"kind": "edge", "entity_id": "bc"},
    ]
    keep_edge = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[{"kind": "edge", "entity_id": "ab", "choice": "keep_main"}],
    )
    assert keep_edge["error"] == "invalid_merge_request"
    assert "b" in world.storage.nodes


def test_a_partial_merge_leaves_the_other_entries_untouched(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "one"}}),
        ("update_node", ("c",), {"updates": {"description": "two"}}),
    )
    untouched = world.layer(sid)[("node", "c")]
    result = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "a"}]
    )
    assert result["success"] is True
    assert world.layer(sid) == {("node", "c"): untouched}
    assert world.storage.get_node("c").description == ""


def test_repeating_a_merge_id_returns_the_first_result_and_writes_nothing(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    first = svc.merge_staged_changes(sid, "m")
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "y"}}))
    events, main = len(world.events), world.main()
    again = svc.merge_staged_changes(sid, "m")
    assert again["replayed"] is True
    assert again["merged"] == first["merged"]
    assert len(world.events) == events and world.main() == main
    assert world.layer(sid)[("node", "a")]["staged"] == {"description": "y"}
    other = world.staged_session()
    taken = svc.merge_staged_changes(other, "m")
    assert taken["error"] == "merge_id_taken"
    assert "merged" not in taken and "record" not in taken


def test_an_entry_restaged_after_the_plan_fails_the_merge_whole(world, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "planned"}}),
        ("update_node", ("b",), {"updates": {"description": "b"}}),
    )
    store = world.storage.layer_store
    original = store.merge

    def restage_then_merge(*args, **kwargs):
        # Another participant's restage, through the store: on this instance
        # a staged write waits for the merge to finish.
        _restage_as_another_instance(world, sid, "a", {"description": "later"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "merge", restage_then_merge)
    main, events = world.main(), len(world.events)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "merge_raced"
    assert world.main() == main and len(world.events) == events
    layer = world.layer(sid)
    assert layer[("node", "a")]["staged"] == {"description": "later"}
    assert ("node", "b") in layer
    assert store.load_merge("m") is None


def test_a_merge_that_failed_after_landing_completes(world, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    store = world.storage.layer_store
    original = store.merge

    def land_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("acknowledgement lost")

    monkeypatch.setattr(store, "merge", land_then_fail)
    result = svc.merge_staged_changes(sid, "m")
    assert result["success"] is True, result
    assert world.storage.get_node("a").description == "x"
    assert [e.origin.event_origin for e in world.events[-1:]] == ["session-merge"]


def test_a_merge_that_failed_before_landing_changes_nothing(world, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    store = world.storage.layer_store

    def fail(*args, **kwargs):
        raise ConnectionError("store down")

    monkeypatch.setattr(store, "merge", fail)
    main, events = world.main(), len(world.events)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "write_failed"
    assert world.main() == main and len(world.events) == events
    assert ("node", "a") in world.layer(sid)


def test_a_merge_id_must_be_a_usable_id(world):
    sid = world.staged_session()
    for merge_id in ("", None, "x" * 513, "a\x00b"):
        result = world.service.merge_staged_changes(sid, merge_id)
        assert result["error"] == "invalid_merge_request"


# -- narrowing ------------------------------------------------------------------------


def test_a_narrowed_caller_reviews_and_merges_only_what_its_view_covers(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "local"}}),
        ("update_node", ("r",), {"updates": {"description": "remote"}}),
        ("delete_edges", (["ar"],), {"confirmed": True}),
    )
    world.hook.narrow = True
    diff = svc.get_staged_changes(sid)
    assert sorted(_items(diff)) == [("node", "a")]
    assert diff["count"] == 1
    hidden = svc.merge_staged_changes(
        sid, "m0", entries=[{"kind": "node", "entity_id": "r"}]
    )
    assert hidden["error"] == "merge_raced"
    assert hidden["entries"] == [{"kind": "node", "entity_id": "r"}]
    assert svc.merge_staged_changes(sid, "m")["success"] is True
    assert world.storage.get_node("a").description == "local"
    assert world.storage.get_node("r").description == ""
    assert sorted(world.layer(sid)) == [("edge", "ar"), ("node", "r")]
    discarded = svc.discard_staged_changes(sid)
    assert discarded["removed_entries"] == []
    world.hook.narrow = False
    assert sorted(_items(svc.get_staged_changes(sid))) == [
        ("edge", "ar"),
        ("node", "r"),
    ]


def test_a_node_delete_whose_edge_tombstone_is_hidden_is_hidden_too(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["a"],), {"confirmed": True}))
    world.hook.narrow = True
    keys = sorted(_items(svc.get_staged_changes(sid)))
    # a's delete needs the tombstone of ar, which leads to the remote node.
    assert keys == [("edge", "ab")]


# -- §12 discard ----------------------------------------------------------------------


def test_discard_removes_entries_only_and_fans_out_keys(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "x"}}),
        ("update_node", ("b",), {"updates": {"description": "y"}}),
    )
    main, events = world.main(), len(world.events)
    result = svc.discard_staged_changes(
        sid,
        entries=[
            {"kind": "node", "entity_id": "a"},
            {"kind": "node", "entity_id": "zz"},
        ],
    )
    assert result["success"] is True
    assert result["removed_entries"] == [["node", "a"]]
    assert sorted(world.layer(sid)) == [("node", "b")]
    assert world.main() == main and len(world.events) == events
    op = world.published[-1][1]["op"]
    assert op == {
        "op": "graph_changes_discarded",
        "removals": [["node", "a"]],
        "seq": result["session_seq"],
    }
    activity = world.manager.get_session(sid).activity_log[-1]
    assert activity["op"] == "graph_changes_discarded"
    assert activity["inverse_op"] is None
    everything = svc.discard_staged_changes(sid)
    assert everything["removed_entries"] == [["node", "b"]]
    assert world.layer(sid) == {}


# -- concurrent staging (§6.2 under races) ------------------------------------------------


def test_two_ops_racing_on_one_entry_both_land(world, monkeypatch):
    """The second op read the layer before the first landed: refused in the
    store and decided again, so the first op's change is not lost."""
    svc, sid = world.service, world.staged_session()
    store = world.storage.layer_store
    original = store.stage
    raced = []

    def interleave(session_id, entries, removals=(), **kwargs):
        if not raced:
            raced.append(True)
            with bind_session(sid):
                svc.update_node("a", {"summary": "first"})
        return original(session_id, entries, removals, **kwargs)

    monkeypatch.setattr(store, "stage", interleave)
    with bind_session(sid):
        result = svc.update_node("a", {"description": "second"})
    assert result["success"] is True, result
    staged = world.layer(sid)[("node", "a")]["staged"]
    assert staged == {"summary": "first", "description": "second"}


# -- a shared store: another instance writes between the plan and the unit ---------------


@pytest.fixture
def pg_world(request, tmp_path):
    request.param = "postgres"
    yield from make_world(request, tmp_path)


def test_an_edit_by_another_instance_inside_the_window_fails_the_merge(
    pg_world, monkeypatch
):
    from backend.core import GraphStorage
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    from backend.core.tests.test_persistence_contract_postgres import DSN

    world = pg_world
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "mine"}}))
    backend = PostgresGraphPersistenceBackend(
        DSN, schema=world.storage._persistence_backend.schema, staged_sessions=True
    )
    other = GraphStorage(persistence_backend=backend)
    try:
        store = world.storage.layer_store
        original = store.merge

        def other_instance_first(*args, **kwargs):
            # Same value for an unrelated field: the plan's expectation on
            # a's revision is what refuses the unit, not a value conflict.
            other.update_node("a", {"summary": "elsewhere"})
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "merge", other_instance_first)
        events = len(world.events)
        result = svc.merge_staged_changes(sid, "m")
        assert result["error"] == "merge_raced"
        assert len(world.events) == events
        assert ("node", "a") in world.layer(sid)
        assert other.layer_store.load_merge("m") is None
        assert other.get_node("a").description == ""
        assert world.storage.get_node("a").description == ""
    finally:
        other.shutdown_events()
        backend.close()


# -- review round 1 regressions -----------------------------------------------------------


def test_an_edge_main_added_between_two_deleted_nodes_is_deleted_once(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["a", "b"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="ab2", source="a", target="b", type="RELATES_TO"))
    take = [
        {"kind": "node", "entity_id": n, "choice": "take_session"} for n in ("a", "b")
    ]
    result = svc.merge_staged_changes(sid, "m", resolutions=take)
    assert result["success"] is True, result
    assert not {"a", "b"} & set(world.storage.nodes)
    assert "ab2" not in world.storage.edges


def test_an_orphaned_node_merged_as_new_takes_its_staged_edge_along(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("c",), {"updates": {"description": "kept"}}),
        (
            "add_nodes",
            ([], [{"id": "ac", "source": "a", "target": "c", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.delete_nodes(["c"], confirmed=True)
    result = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "c", "choice": "as_new"}]
    )
    assert result["success"] is True, result
    new_id = {i["entity_id"]: i for i in result["merged"]}["c"]["new_entity_id"]
    assert world.storage.get_node(new_id).description == "kept"
    assert world.storage.edges["ac"].target == new_id


def test_a_clean_entry_takes_no_as_new_or_manual(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    result = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "a", "choice": "as_new"}]
    )
    assert result["error"] == "invalid_merge_request"
    assert world.storage.get_node("a").description == ""


def test_an_empty_merge_fans_out_nothing(world):
    sid = world.staged_session()
    published = len(world.published)
    result = world.service.merge_staged_changes(sid, "m")
    assert result["success"] is True and result["merged"] == []
    assert len(world.published) == published


def test_a_discard_never_drops_an_entry_restaged_after_it_read_the_layer(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "v1"}}))
    store = world.storage.layer_store
    original = store.discard
    raced = []

    def restage_first(*args, **kwargs):
        if not raced:
            raced.append(True)
            world.hook.narrow = False
            _stage(
                world,
                sid,
                (
                    "update_node",
                    ("a",),
                    {
                        "updates": {
                            "description": "v2",
                            "metadata": {"origin_graph_id": "remote"},
                        },
                        "metadata_merge": True,
                    },
                ),
            )
            world.hook.narrow = True
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "discard", restage_first)
    world.hook.narrow = True
    result = svc.discard_staged_changes(sid)
    # Decided again: a has left the narrowed view, so nothing is discarded.
    assert result["removed_entries"] == []
    assert world.layer(sid)[("node", "a")]["staged"]["description"] == "v2"


def test_an_edge_staged_while_its_endpoint_create_is_withdrawn_is_decided_again(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("add_nodes", ([{"id": "n", "type": "Actor", "name": "N"}], []), {})
    )
    store = world.storage.layer_store
    original = store.stage
    raced = []

    def withdraw_first(session_id, entries, removals=(), **kwargs):
        if not raced:
            raced.append(True)
            _stage(world, sid, ("delete_nodes", (["n"],), {"confirmed": True}))
        return original(session_id, entries, removals, **kwargs)

    monkeypatch.setattr(store, "stage", withdraw_first)
    with bind_session(sid):
        result = svc.add_nodes(
            [], [{"id": "an", "source": "a", "target": "n", "type": "RELATES_TO"}]
        )
    assert result["success"] is False
    assert ("edge", "an") not in world.layer(sid)


def test_a_node_delete_meeting_an_edge_the_reader_cannot_see_is_out_of_its_view(
    world,
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("delete_nodes", (["c"],), {"confirmed": True}))
    world.storage.add_edge(Edge(id="cr", source="c", target="r", type="RELATES_TO"))
    world.hook.narrow = True
    assert ("node", "c") not in _items(svc.get_staged_changes(sid))
    world.hook.narrow = False
    item = _items(svc.get_staged_changes(sid))[("node", "c")]
    assert (item["status"], item["unstaged_edges"]) == ("conflict", ["cr"])


# -- review round 2 regressions -----------------------------------------------------------


def _recreated_edge_conflicting_on_target(world, sid):
    """ab staged as deleted and re-created (an update staging its endpoints),
    while main moved ab to a -> c: its target conflicts."""
    _stage(
        world,
        sid,
        ("delete_edges", (["ab"],), {"confirmed": True}),
        (
            "add_nodes",
            ([], [{"id": "ab", "source": "a", "target": "b", "type": "RELATES_TO"}]),
            {},
        ),
    )
    world.storage.delete_edge("ab")
    world.storage.add_edge(Edge(id="ab", source="a", target="c", type="RELATES_TO"))


@pytest.mark.parametrize("target", ["r", "no-such-node"])
def test_a_manual_value_cannot_point_an_edge_where_the_caller_cannot(world, target):
    svc, sid = world.service, world.staged_session()
    _recreated_edge_conflicting_on_target(world, sid)
    world.hook.narrow = True
    manual = [
        {
            "kind": "edge",
            "entity_id": "ab",
            "choice": "manual",
            "values": {"target": target},
        }
    ]
    result = svc.merge_staged_changes(sid, "m", resolutions=manual)
    # A hidden node and a missing one get the same answer.
    assert result["error"] == "invalid_merge_request"
    assert result["message"] == "edge 'ab': an endpoint does not exist"
    assert world.storage.edges["ab"].target == "c"


def test_a_manual_value_is_validated_like_a_direct_write(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "mine"}}))
    world.storage.update_node("a", {"description": "theirs"})
    deep = current = {}
    for _ in range(900):
        current["k"] = {}
        current = current["k"]
    _stage(
        world,
        sid,
        (
            "update_node",
            ("b",),
            {"updates": {"metadata": {"k": 1}}, "metadata_merge": True},
        ),
    )
    world.storage.update_node("b", {"metadata": {"k": 2}}, metadata_merge=True)
    result = svc.merge_staged_changes(
        sid,
        "m",
        entries=[{"kind": "node", "entity_id": "b"}],
        resolutions=[
            {
                "kind": "node",
                "entity_id": "b",
                "choice": "manual",
                "values": {"metadata.k": deep},
            }
        ],
    )
    assert result["error"] == "invalid_merge_request"
    assert world.storage.get_node("b").metadata == {"k": 2}


def _restage_as_another_instance(world, sid, node_id, staged):
    """What an instance with its own session document stages: the same
    session_seq as the entry it replaces, a token of its own."""
    entry = world.layer(sid)[("node", node_id)]
    world.storage.layer_store.stage(
        sid, [{**entry, "staged": staged, "stage_token": "other-instance"}]
    )


def test_a_discard_tells_another_instances_restage_apart_by_its_token(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    store = world.storage.layer_store
    original = store.discard

    def other_instance_first(*args, **kwargs):
        monkeypatch.setattr(store, "discard", original)
        # Same session_seq; the new version moves a out of the narrowed view.
        _restage_as_another_instance(
            world, sid, "a", {"metadata": {"origin_graph_id": "remote"}}
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "discard", other_instance_first)
    world.hook.narrow = True
    result = svc.discard_staged_changes(sid)
    assert result["removed_entries"] == []
    world.hook.narrow = False
    assert world.layer(sid)[("node", "a")]["stage_token"] == "other-instance"


def test_a_merge_tells_another_instances_restage_apart_by_its_token(world, monkeypatch):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    store = world.storage.layer_store
    original = store.merge

    def other_instance_first(*args, **kwargs):
        _restage_as_another_instance(world, sid, "a", {"description": "newer"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "merge", other_instance_first)
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "merge_raced"
    assert world.storage.get_node("a").description == ""
    assert world.layer(sid)[("node", "a")]["staged"] == {"description": "newer"}


def test_an_update_of_a_saved_view_holding_older_annotations_merges(world):
    """A direct update checks only the metadata it brings; so does a merge."""
    svc, sid = world.service, world.staged_session()
    legacy = [
        {"id": "x", "type": "image", "image": {"url": "https://example.org/a.png"}}
    ]
    world.storage.add_nodes(
        [Node(id="v", type="SavedView", name="View", metadata={"annotations": legacy})],
        [],
    )
    _stage(world, sid, ("update_node", ("v",), {"updates": {"name": "Renamed"}}))
    result = svc.merge_staged_changes(sid, "m")
    assert result["success"] is True, result
    assert world.storage.get_node("v").name == "Renamed"


def test_a_manual_value_on_a_saved_view_is_held_to_the_annotation_rules(world):
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes(
        [Node(id="v", type="SavedView", name="View", metadata={"annotations": []})], []
    )
    _stage(
        world,
        sid,
        (
            "update_node",
            ("v",),
            {"updates": {"metadata": {"annotations": [{"id": "s"}]}}},
        ),
    )
    world.storage.update_node("v", {"metadata": {"annotations": [{"id": "m"}]}})
    remote = [
        {"id": "x", "type": "image", "image": {"url": "https://example.org/a.png"}}
    ]
    result = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "node",
                "entity_id": "v",
                "choice": "manual",
                "values": {"metadata.annotations": remote},
            }
        ],
    )
    assert result["error"] == "invalid_merge_request"
    assert world.storage.get_node("v").metadata == {"annotations": [{"id": "m"}]}


def test_an_edge_staged_to_a_node_while_it_is_merged_as_new_fails_the_merge(
    world, monkeypatch
):
    """The edges that follow a node merged as new were decided on the whole
    layer; one staged inside the window must not attach to the old id."""
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("add_nodes", ([{"id": "t", "type": "Actor", "name": "Mine"}], []), {}),
    )
    world.storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    store = world.storage.layer_store
    original = store.merge

    def follower_first(*args, **kwargs):
        # Staged by another instance, through the store: on this instance a
        # staged write waits for the merge to finish.
        edge = {"id": "bt", "source": "b", "target": "t", "type": "RELATES_TO"}
        store.stage(
            sid,
            [
                {
                    "session_id": sid,
                    "kind": "edge",
                    "entity_id": "bt",
                    "action": "create",
                    "base_revision": None,
                    "base": None,
                    "staged": {**edge, "label": "", "metadata": {}, "revision": 0},
                    "staged_by": "other",
                    "staged_at": "2026-10-21T00:00:00+00:00",
                    "session_seq": 99,
                    "stage_token": "other-instance",
                }
            ],
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "merge", follower_first)
    result = svc.merge_staged_changes(
        sid, "m", resolutions=[{"kind": "node", "entity_id": "t", "choice": "as_new"}]
    )
    assert result["error"] == "merge_raced"
    assert sorted(world.layer(sid)) == [("edge", "bt"), ("node", "t")]
    assert world.storage.get_node("t").name == "Theirs"


# -- review round 4 regressions -----------------------------------------------------------

_REMOTE_IMAGE = [
    {"id": "x", "type": "image", "image": {"url": "https://example.org/a.png"}}
]


def test_a_manual_value_never_written_cannot_mask_staged_annotations(world):
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes([Node(id="q", type="Actor", name="Q")], [])
    _stage(
        world,
        sid,
        (
            "update_node",
            ("q",),
            {"updates": {"name": "B", "metadata": {"annotations": _REMOTE_IMAGE}}},
        ),
    )
    world.storage.delete_nodes(["q"], confirmed=True)
    world.storage.add_nodes([Node(id="q", type="SavedView", name="C")], [])
    result = svc.merge_staged_changes(
        sid,
        "m",
        resolutions=[
            {
                "kind": "node",
                "entity_id": "q",
                "choice": "manual",
                "values": {"name": "B", "metadata.annotations": []},
            }
        ],
    )
    assert result["error"] == "invalid_merge_request"
    assert "annotations" not in world.storage.get_node("q").metadata


def test_a_node_recreated_as_a_saved_view_is_checked_on_all_its_metadata(world):
    svc, sid = world.service, world.staged_session()
    world.storage.add_nodes([Node(id="q", type="Actor", name="Q")], [])
    _stage(
        world,
        sid,
        ("delete_nodes", (["q"],), {"confirmed": True}),
        ("add_nodes", ([{"id": "q", "type": "SavedView", "name": "View"}], []), {}),
    )
    world.storage.update_node("q", {"metadata": {"annotations": _REMOTE_IMAGE}})
    result = svc.merge_staged_changes(sid, "m")
    assert result["error"] == "invalid_merge_request"
    assert world.storage.get_node("q").type_str == "Actor"


@pytest.mark.parametrize("staged", [False, True])
def test_annotations_passed_outside_metadata_are_checked_on_a_saved_view(world, staged):
    """A key outside `metadata` is folded into it: it is held to the same
    annotation rule, direct and staged alike."""
    svc = world.service
    world.storage.add_nodes([Node(id="v", type="SavedView", name="View")], [])
    if staged:
        with bind_session(world.staged_session()):
            result = svc.update_node("v", {"annotations": _REMOTE_IMAGE})
    else:
        result = svc.update_node("v", {"annotations": _REMOTE_IMAGE})
    assert result["success"] is False
    assert result["error_code"] == "validation_error"
    assert world.storage.get_node("v").metadata == {}


# -- review round 5 regressions -----------------------------------------------------------


def test_a_direct_update_is_judged_on_the_node_it_replaces_not_a_stale_read(
    world, monkeypatch
):
    """A merge can turn a node into a saved view between a direct update's
    read and its write; the check runs under the storage lock, on the live
    node."""
    svc = world.service
    world.storage.add_nodes([Node(id="q", type="SavedView", name="View")], [])
    stale = Node(id="q", type="Actor", name="Q")
    real_get = world.storage.get_node
    monkeypatch.setattr(
        world.storage,
        "get_node",
        lambda node_id: stale if node_id == "q" else real_get(node_id),
    )
    result = svc.update_node("q", {"metadata": {"annotations": _REMOTE_IMAGE}})
    monkeypatch.setattr(world.storage, "get_node", real_get)
    assert result["success"] is False
    assert result["error_code"] == "validation_error"
    assert world.storage.get_node("q").metadata == {}


def test_a_direct_update_re_sending_unsafe_annotations_is_refused_as_before(world):
    svc = world.service
    world.storage.add_nodes(
        [
            Node(
                id="v",
                type="SavedView",
                name="View",
                metadata={"annotations": _REMOTE_IMAGE},
            )
        ],
        [],
    )
    result = svc.update_node(
        "v", {"name": "X", "metadata": {"annotations": _REMOTE_IMAGE}}
    )
    assert result["error_code"] == "validation_error"
    assert world.storage.get_node("v").name == "View"
    # An unrelated change to a view holding them still goes through.
    assert svc.update_node("v", {"name": "Y"})["success"] is True


def test_metadata_as_deep_as_a_direct_write_stores_stages_and_merges(world):
    """Nesting the direct path accepts (up to MAX_METADATA_DEPTH) does not
    exhaust the interpreter's recursion on the staging and merge paths."""
    svc, sid = world.service, world.staged_session()
    deep = current = {}
    for _ in range(790):
        current["k"] = {}
        current = current["k"]
    with bind_session(sid):
        staged = svc.add_nodes(
            [{"id": "deep", "type": "Actor", "name": "Deep", "metadata": {"k": deep}}],
            [],
        )
    assert staged["success"] is True, staged
    assert svc.get_staged_changes(sid)["count"] == 1
    assert svc.merge_staged_changes(sid, "m")["success"] is True
    assert world.storage.get_node("deep") is not None


# -- review round 6 regressions -----------------------------------------------------------


@pytest.mark.parametrize("metadata_merge", [False, True])
def test_a_key_sent_both_folded_and_in_metadata_is_judged_as_stored(
    world, metadata_merge
):
    """The folded key wins in the stored node, so it is the one judged."""
    svc = world.service
    world.storage.add_nodes([Node(id="v", type="SavedView", name="View")], [])
    result = svc.update_node(
        "v",
        {"metadata": {"annotations": []}, "annotations": _REMOTE_IMAGE},
        metadata_merge=metadata_merge,
    )
    assert result["error_code"] == "validation_error"
    assert world.storage.get_node("v").metadata == {}


# -- review round 7 regressions -----------------------------------------------------------


def _legacy_view(world):
    world.storage.add_nodes(
        [
            Node(
                id="v",
                type="SavedView",
                name="View",
                metadata={"annotations": _REMOTE_IMAGE},
            )
        ],
        [],
    )


@pytest.mark.parametrize("staged", [False, True])
def test_re_sending_a_views_unsafe_annotations_is_refused_direct_and_staged(
    world, staged
):
    """The browser re-posts a view's whole metadata; annotations stored
    before today's rule are refused when sent again, however they travel."""
    _legacy_view(world)
    update = {"metadata": {"annotations": _REMOTE_IMAGE, "title": "t2"}}
    if staged:
        with bind_session(world.staged_session()):
            result = world.service.update_node("v", update)
    else:
        result = world.service.update_node("v", update)
    assert result["error_code"] == "validation_error"
    assert "title" not in world.storage.get_node("v").metadata


@pytest.mark.parametrize("staged", [False, True])
def test_an_annotation_document_given_as_a_list_is_held_to_the_rule(world, staged):
    """The browser reads a bare list as the document's annotations."""
    svc = world.service
    node = {
        "id": "v1",
        "type": "SavedView",
        "name": "V",
        "metadata": {"annotation_document": _REMOTE_IMAGE},
    }
    if staged:
        sid = world.staged_session()
        with bind_session(sid):
            result = svc.add_nodes([node], [])
    else:
        result = svc.add_nodes([node], [])
    assert result["success"] is False
    assert world.storage.get_node("v1") is None


def test_a_list_annotation_document_stored_before_the_rule_is_sanitised_on_read():
    from backend.core.session_annotations import sanitize_saved_view_metadata

    cleaned = sanitize_saved_view_metadata({"annotation_document": _REMOTE_IMAGE})
    assert "url" not in (cleaned["annotation_document"][0].get("image") or {})


# -- review round 8 regressions -----------------------------------------------------------


def test_the_diff_sanitises_a_saved_views_annotations_on_every_side(world):
    """As every read of a saved view does; conflicts are still judged on
    what is stored."""
    _legacy_view(world)
    sid = world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("v",), {"updates": {"metadata": {"annotations": []}}}),
    )
    field = _items(world.service.get_staged_changes(sid))[("node", "v")]["fields"][
        "metadata.annotations"
    ]
    for side in ("base", "main"):
        assert "url" not in (field[side][0].get("image") or {}), side
    assert field["staged"] == []


def test_a_staged_write_during_a_merge_reads_main_as_the_merge_left_it(
    world, monkeypatch
):
    """A merge holds the write lock until memory follows the store; a staged
    write waits for it rather than basing an entry on the node before."""
    import threading

    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "merged"}}))
    store = world.storage.layer_store
    original = store.merge
    stager = []

    def stage_meanwhile(*args, **kwargs):
        def run():
            with bind_session(sid):
                svc.update_node("a", {"summary": "after"})

        thread = threading.Thread(target=run)
        thread.start()
        stager.append(thread)
        thread.join(0.3)
        assert thread.is_alive()  # waiting for the merge
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "merge", stage_meanwhile)
    assert svc.merge_staged_changes(sid, "m")["success"] is True
    stager[0].join(10)
    entry = world.layer(sid)[("node", "a")]
    assert entry["base"]["description"] == "merged"
    assert entry["base_revision"] == world.storage.get_node("a").revision


# -- review round 9 regressions -----------------------------------------------------------


def test_the_diff_sanitises_a_side_that_is_a_saved_view_whatever_the_others_are(
    world,
):
    """A staged Actor over an id main holds as a saved view (id_taken): main's
    side is still a saved view's, and read as one."""
    sid = world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            (
                [
                    {
                        "id": "v",
                        "type": "Actor",
                        "name": "A",
                        "metadata": {"annotations": []},
                    }
                ],
                [],
            ),
            {},
        ),
    )
    _legacy_view(world)
    item = _items(world.service.get_staged_changes(sid))[("node", "v")]
    assert item["status"] == "id_taken"
    main = item["fields"]["metadata.annotations"]["main"]
    assert "url" not in (main[0].get("image") or {})


# -- review round 10 regressions ----------------------------------------------------------


def test_each_side_of_the_diff_is_sanitised_by_its_own_type(world):
    """A staged Actor's annotations are what the merge would write, shown as
    they are; main's saved view is read as a saved view."""
    sid = world.staged_session()
    _stage(
        world,
        sid,
        (
            "add_nodes",
            (
                [
                    {
                        "id": "v",
                        "type": "Actor",
                        "name": "A",
                        "metadata": {"annotations": _REMOTE_IMAGE},
                    }
                ],
                [],
            ),
            {},
        ),
    )
    _legacy_view(world)
    field = _items(world.service.get_staged_changes(sid))[("node", "v")]["fields"][
        "metadata.annotations"
    ]
    assert field["staged"] == _REMOTE_IMAGE
    assert "url" not in (field["main"][0].get("image") or {})


def test_a_node_recreated_as_another_type_takes_its_edges_or_is_refused(world):
    """A re-create changing a node's type is an update in place: the old
    node's edge tombstones go with it, and a main edge that would no longer
    fit the new type refuses the merge."""
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
    )
    item = _items(svc.get_staged_changes(sid))[("node", "l")]
    assert {"kind": "edge", "entity_id": "il"} in item["depends_on"]
    alone = svc.merge_staged_changes(
        sid, "m0", entries=[{"kind": "node", "entity_id": "l"}]
    )
    assert alone["error"] == "merge_incomplete"
    world.storage.add_edge(Edge(id="il2", source="i", target="l", type="IMPLEMENTS"))
    refused = svc.merge_staged_changes(sid, "m1")
    assert refused["error"] == "invalid_merge_request"
    assert world.storage.get_node("l").type_str == "Legislation"
    world.storage.delete_edge("il2")
    assert svc.merge_staged_changes(sid, "m2")["success"] is True
    assert world.storage.get_node("l").type_str == "Actor"
    assert "il" not in world.storage.edges


# -- post-merge review round 1 regressions ------------------------------------------------


def _recreate_legislation_as_actor(world):
    sid = world.staged_session()
    world.storage.add_nodes(
        [
            Node(id="i", type="Initiative", name="I"),
            Node(id="l", type="Legislation", name="L"),
            Node(id="h", type="Initiative", name="H", metadata=dict(REMOTE)),
        ],
        [],
    )
    _stage(
        world,
        sid,
        ("delete_nodes", (["l"],), {"confirmed": True}),
        ("add_nodes", ([{"id": "l", "type": "Actor", "name": "L"}], []), {}),
    )
    return sid


def test_a_retype_is_out_of_view_while_main_holds_an_edge_the_reader_cannot_see(
    world,
):
    """The edges a retype is judged against are the reader's to see, or the
    refusal would count hidden ones."""
    svc, sid = world.service, _recreate_legislation_as_actor(world)
    world.storage.add_edge(Edge(id="hl", source="h", target="l", type="IMPLEMENTS"))
    world.hook.narrow = True
    assert ("node", "l") not in _items(svc.get_staged_changes(sid))
    result = svc.merge_staged_changes(sid, "m")
    assert "existing edge" not in str(result)
    assert world.storage.get_node("l").type_str == "Legislation"
    world.hook.narrow = False
    assert ("node", "l") in _items(svc.get_staged_changes(sid))


def test_an_edge_another_instance_adds_to_a_retyped_node_fails_the_merge(
    pg_world, monkeypatch
):
    """The kept edges of a retyped node are re-checked in the store: an edge
    another instance added since the plan refuses the unit."""
    from backend.core import GraphStorage
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    from backend.core.tests.test_persistence_contract_postgres import DSN

    world = pg_world
    svc, sid = world.service, _recreate_legislation_as_actor(world)
    backend = PostgresGraphPersistenceBackend(
        DSN, schema=world.storage._persistence_backend.schema, staged_sessions=True
    )
    other = GraphStorage(persistence_backend=backend)
    try:
        store = world.storage.layer_store
        original = store.merge

        def other_instance_first(*args, **kwargs):
            # Valid when written: l is still Legislation in the store.
            other.add_edge(Edge(id="x", source="i", target="l", type="IMPLEMENTS"))
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "merge", other_instance_first)
        result = svc.merge_staged_changes(sid, "m")
        assert result["error"] == "merge_raced"
        assert other.get_node("l").type_str == "Legislation"
        assert ("node", "l") in world.layer(sid)
    finally:
        other.shutdown_events()
        backend.close()
