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
