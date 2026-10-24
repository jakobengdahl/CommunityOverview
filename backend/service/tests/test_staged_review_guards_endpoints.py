from __future__ import annotations

import pytest

from backend.core import Edge, Node
from backend.core.storage_backends import EntityOperation
from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _items, _stage


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
