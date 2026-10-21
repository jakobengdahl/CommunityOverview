"""Scratch closing tests for round-4 mutation survivors."""

from __future__ import annotations

import pytest

from backend.core import Edge, Node
from backend.core import layer_store as ls
from backend.core.layer_store import MergeCheck
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
