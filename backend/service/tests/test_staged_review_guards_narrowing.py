from __future__ import annotations

import pytest

from backend.core import Edge, Node
from backend.service.tests.test_composed_reads import make_world
from backend.service.tests.test_staged_review import _items, _stage


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


def _edge(i, s, t):
    return {"id": i, "source": s, "target": t, "type": "RELATES_TO"}


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
