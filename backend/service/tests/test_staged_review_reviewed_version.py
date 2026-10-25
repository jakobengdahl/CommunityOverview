"""A merge or discard bound to the entry versions its caller reviewed
(docs/SESSION_OVERLAY_CONTRACT.md §11.4, §12).

An entry restaged between the review and the request - by this instance or
another one sharing the layer - is refused, not merged or discarded unseen.
A request without versions behaves as before. Run over the file backend and,
when a server is configured, PostgreSQL.
"""

from __future__ import annotations

import pytest

from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import (
    _items,
    _restage_as_another_instance,
    _stage,
)


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


def _reviewed(world, sid, node_id):
    item = _items(world.service.get_staged_changes(sid))[("node", node_id)]
    return {"kind": "node", "entity_id": node_id, "version": item["version"]}


def _restage_here(world, sid, node_id, description):
    _stage(
        world,
        sid,
        ("update_node", (node_id,), {"updates": {"description": description}}),
    )


@pytest.mark.parametrize("restage", [_restage_here, None], ids=["this", "another"])
def test_a_merge_of_an_entry_restaged_since_its_review_is_refused(world, restage):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    reviewed = _reviewed(world, sid, "a")
    if restage:
        restage(world, sid, "a", "later")
    else:
        # Same session_seq, a token of its own.
        _restage_as_another_instance(world, sid, "a", {"description": "later"})
    main, events = world.main(), len(world.events)
    result = svc.merge_staged_changes(sid, "m", entries=[reviewed])
    assert result["error"] == "merge_raced"
    assert world.main() == main and len(world.events) == events
    assert world.layer(sid)[("node", "a")]["staged"] == {"description": "later"}
    assert world.storage.layer_store.load_merge("m") is None


def test_a_restage_inside_the_merge_window_is_refused_against_the_reviewed_version(
    world, monkeypatch
):
    """The plan reads the restaged entry, so only the reviewed version can
    tell: the unit checks it."""
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    reviewed = _reviewed(world, sid, "a")
    original = svc._load_review

    def restage_before_the_plan_reads(*args, **kwargs):
        monkeypatch.setattr(svc, "_load_review", original)
        _restage_as_another_instance(world, sid, "a", {"description": "later"})
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "_load_review", restage_before_the_plan_reads)
    result = svc.merge_staged_changes(sid, "m", entries=[reviewed])
    assert result["error"] == "merge_raced"
    assert world.storage.get_node("a").description != "later"


def test_a_merge_of_the_reviewed_versions_lands(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    result = svc.merge_staged_changes(sid, "m", entries=[_reviewed(world, sid, "a")])
    assert result["success"] is True, result
    assert world.storage.get_node("a").description == "reviewed"


def test_a_merge_without_versions_merges_what_the_layer_holds_as_before(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    _restage_as_another_instance(world, sid, "a", {"description": "later"})
    result = svc.merge_staged_changes(
        sid, "m", entries=[{"kind": "node", "entity_id": "a"}]
    )
    assert result["success"] is True, result
    assert world.storage.get_node("a").description == "later"


@pytest.mark.parametrize("version", ["", 7, "x" * 201])
def test_a_malformed_version_is_refused(world, version):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    entry = {"kind": "node", "entity_id": "a", "version": version}
    assert svc.merge_staged_changes(sid, "m", entries=[entry])["error"] == (
        "invalid_merge_request"
    )
    assert svc.discard_staged_changes(sid, entries=[entry])["error"] == (
        "invalid_merge_request"
    )
    assert ("node", "a") in world.layer(sid)


def test_a_malformed_entry_with_a_version_is_refused_not_a_server_error(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    entry = {"kind": "node", "entity_id": ["a"], "version": "v"}
    assert svc.discard_staged_changes(sid, entries=[entry])["error"] == (
        "invalid_merge_request"
    )
    assert svc.merge_staged_changes(sid, "m", entries=[entry])["error"] == (
        "invalid_merge_request"
    )


@pytest.mark.parametrize("restage", [_restage_here, None], ids=["this", "another"])
def test_a_discard_of_an_entry_restaged_since_its_review_is_refused(world, restage):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    reviewed = _reviewed(world, sid, "a")
    if restage:
        restage(world, sid, "a", "later")
    else:
        _restage_as_another_instance(world, sid, "a", {"description": "later"})
    result = svc.discard_staged_changes(sid, entries=[reviewed])
    assert result["error"] == "merge_raced"
    assert world.layer(sid)[("node", "a")]["staged"] == {"description": "later"}


def test_a_discard_checks_every_entry_it_names_not_only_the_first(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world,
        sid,
        ("update_node", ("a",), {"updates": {"description": "reviewed"}}),
        ("update_node", ("b",), {"updates": {"description": "reviewed"}}),
    )
    reviewed = [_reviewed(world, sid, "a"), _reviewed(world, sid, "b")]
    _restage_as_another_instance(world, sid, "b", {"description": "later"})
    result = svc.discard_staged_changes(sid, entries=reviewed)
    assert result["error"] == "merge_raced"
    layer = world.layer(sid)
    assert ("node", "a") in layer and ("node", "b") in layer


def test_an_entry_named_with_two_versions_is_refused(world):
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    reviewed = _reviewed(world, sid, "a")
    twice = [reviewed, {**reviewed, "version": "another"}]
    assert svc.merge_staged_changes(sid, "m", entries=twice)["error"] == (
        "invalid_merge_request"
    )
    assert svc.discard_staged_changes(sid, entries=twice)["error"] == (
        "invalid_merge_request"
    )


def test_a_restage_inside_the_discard_window_is_refused_against_the_reviewed_version(
    world, monkeypatch
):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    reviewed = _reviewed(world, sid, "a")
    store = world.storage.layer_store
    original = store.discard

    def another_instance_first(*args, **kwargs):
        monkeypatch.setattr(store, "discard", original)
        _restage_as_another_instance(world, sid, "a", {"description": "later"})
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "discard", another_instance_first)
    result = svc.discard_staged_changes(sid, entries=[reviewed])
    assert result["error"] == "merge_raced"
    assert world.layer(sid)[("node", "a")]["staged"] == {"description": "later"}


def test_a_discard_without_versions_discards_what_the_layer_holds_as_before(world):
    svc, sid = world.service, world.staged_session()
    _stage(
        world, sid, ("update_node", ("a",), {"updates": {"description": "reviewed"}})
    )
    _restage_as_another_instance(world, sid, "a", {"description": "later"})
    result = svc.discard_staged_changes(
        sid, entries=[{"kind": "node", "entity_id": "a"}]
    )
    assert result["removed_entries"] == [["node", "a"]]


def test_a_refusal_names_no_version_of_an_entry_outside_the_view(world):
    """A narrowed reader naming an entry it cannot see, with any version,
    learns nothing of that entry's version."""
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    version = _items(svc.get_staged_changes(sid))[("node", "a")]["version"]
    _restage_as_another_instance(
        world, sid, "a", {"metadata": {"origin_graph_id": "remote"}}
    )
    world.hook.narrow = True
    entry = {"kind": "node", "entity_id": "a", "version": version}
    merged = svc.merge_staged_changes(sid, "m", entries=[entry])
    discarded = svc.discard_staged_changes(sid, entries=[entry])
    world.hook.narrow = False
    assert merged["error"] == "merge_raced"
    assert discarded["removed_entries"] == []
    current = world.layer(sid)[("node", "a")]
    for answer in (merged, discarded):
        assert current["stage_token"] not in str(answer)


@pytest.mark.parametrize("version", ["", 7, "x" * 201])
def test_a_malformed_version_on_an_entry_outside_the_view_answers_as_any_other_version(
    world, version
):
    """Discard decides the view before the versions, as merge does: an entry
    the reader cannot see is never refused for the shape of its version."""
    svc, sid = world.service, world.staged_session()
    _stage(world, sid, ("update_node", ("a",), {"updates": {"description": "x"}}))
    _restage_as_another_instance(
        world, sid, "a", {"metadata": {"origin_graph_id": "remote"}}
    )
    world.hook.narrow = True
    entry = {"kind": "node", "entity_id": "a", "version": version}
    merged = svc.merge_staged_changes(sid, "m", entries=[entry])
    discarded = svc.discard_staged_changes(sid, entries=[entry])
    well_formed = svc.discard_staged_changes(sid, entries=[{**entry, "version": "v"}])
    world.hook.narrow = False
    assert merged["error"] == "merge_raced"
    assert discarded == well_formed
    assert discarded["removed_entries"] == []
    assert ("node", "a") in world.layer(sid)
