"""Tests for adopting federated nodes into the local graph."""

import json

import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService
from backend.service.mutations import _FEDERATION_BOOKKEEPING_METADATA_KEYS


def _service_with_cached_federated_node(tmp_path, source_node=None, source_nodes=None):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "capabilities": {"allow_adopt": True},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        source_nodes
        or [
            source_node or {"id": "remote-1", "type": "Actor", "name": "External Node"}
        ],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes

    return GraphService(storage, federation_manager=manager)


def _build_cache_stamped_metadata_keys():
    """The metadata keys FederationManager._build_cache adds to a cached node."""
    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )
    cache_nodes, _ = FederationManager(config)._build_cache(
        config.federation.graphs[0],
        [{"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    return set(cache_nodes["federated::esam-main::remote-1"].metadata)


_TAGGED_REMOTE_NODES = [
    {"id": f"remote-{i}", "type": "Actor", "name": f"External {i}", "tags": ["t"]}
    for i in (1, 2, 3)
]


def test_adopt_federated_node_creates_local_clone(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Local clone"
    )

    assert result["success"] is True
    assert result["adopted_node"]["name"] == "Local clone"
    assert result["adopted_node"]["metadata"]["is_adopted"] is True
    assert (
        result["adopted_node"]["metadata"]["adopted_from"]["origin_graph_id"]
        == "esam-main"
    )
    assert result["lineage_edge"]["metadata"]["is_federated_lineage"] is True
    assert len(result["added_edge_ids"]) == 1


def test_adopted_node_keeps_the_remote_aliases_and_subtypes(tmp_path):
    """The adopted copy must keep the aliases and subtypes the federated node was
    found by, so it stays findable by them in local search."""
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node={
            "id": "remote-1",
            "type": "Actor",
            "name": "External Node",
            "aliases": ["eSam", "Second Alias", "eSam"],
            "subtypes": ["Board", "Agency", "Board"],
        },
    )

    result = service.adopt_federated_node("federated::esam-main::remote-1")

    assert result["success"] is True
    adopted = service.storage.get_node(result["adopted_node"]["id"])
    assert (adopted.aliases, adopted.subtypes) == (
        ["eSam", "Second Alias", "eSam"],
        ["Board", "Agency", "Board"],
    )
    found = service.search_graph(query="second alias", limit=10)
    assert result["adopted_node"]["id"] in [node["id"] for node in found["nodes"]]


def test_adopted_node_appears_once_in_search_graph_with_correct_federated_count(
    tmp_path,
):
    """Regression test for task-smallfix-search-graph-node-dedup.

    adopt_federated_node adds a local reference stub keyed by the same id the
    node has in the federation cache, so that id is visible from both local
    storage and the federation cache. search_graph must dedup it (G1) rather
    than returning it twice and inflating `total`. The freshly adopted local
    node is a full local copy the user now owns, so it must not count as
    federated (G2) -- but the reference stub kept for lineage still legitimately
    represents a node living in the origin graph, so it should (G3).
    """
    service = _service_with_cached_federated_node(tmp_path)

    adopt_result = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Local clone"
    )
    assert adopt_result["success"] is True
    local_node_id = adopt_result["adopted_node"]["id"]
    stub_node_id = adopt_result["source_node"]["id"]
    assert stub_node_id == "federated::esam-main::remote-1"

    search_result = service.search_graph(query="")
    node_ids = [node["id"] for node in search_result["nodes"]]

    # G1: the id shared between local storage and the federation cache appears once.
    assert node_ids.count(stub_node_id) == 1
    assert node_ids.count(local_node_id) == 1
    assert len(node_ids) == 2
    assert search_result["total"] == 2

    # G2: the adopted local node's own metadata no longer carries the
    # federation-cache bookkeeping that would make it match as federated.
    local_node_metadata = next(
        node["metadata"]
        for node in search_result["nodes"]
        if node["id"] == local_node_id
    )
    assert not local_node_metadata.get("origin_graph_id")
    assert not local_node_metadata.get("is_federated")
    # Lineage is preserved, just not at the top level.
    assert local_node_metadata["adopted_from"]["origin_graph_id"] == "esam-main"

    # G3: only the origin reference stub counts as federated.
    assert search_result["federation"]["federated_nodes"] == 1


def test_bookkeeping_keys_are_exactly_what_build_cache_stamps():
    # A key _build_cache starts stamping must be stripped on adoption too; a
    # key it stops stamping is dead weight. Either drift fails here.
    assert (
        set(_FEDERATION_BOOKKEEPING_METADATA_KEYS)
        == _build_cache_stamped_metadata_keys()
    )


def test_adopted_node_carries_none_of_the_build_cache_bookkeeping_keys(tmp_path):
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node={
            "id": "remote-1",
            "type": "Actor",
            "name": "External Node",
            "metadata": {"owner": "remote team"},
        },
    )

    result = service.adopt_federated_node("federated::esam-main::remote-1")
    assert result["success"] is True

    stamped_keys = _build_cache_stamped_metadata_keys()
    stored_metadata = service.storage.get_node(result["adopted_node"]["id"]).metadata
    for metadata in (result["adopted_node"]["metadata"], stored_metadata):
        assert stamped_keys.isdisjoint(metadata)
        # The origin graph's own metadata survives adoption.
        assert metadata["owner"] == "remote team"


# The federated window's order is the cache's order, which match-all does not
# rank. Varying which remotes are adopted puts a repeated stub id ahead of the
# never-adopted remote under any such order in at least one case, so a
# trim-before-dedup regression cannot pass by the order happening to put the
# duplicate last.
_ADOPTION_CHOICES = [
    ("remote-1", "remote-2"),
    ("remote-1", "remote-3"),
    ("remote-2", "remote-3"),
]


@pytest.mark.parametrize("adopted_remotes", _ADOPTION_CHOICES)
@pytest.mark.parametrize(
    "limit,tags_any",
    # The four local nodes (two copies, two stubs) fill limits 1-4 on their own,
    # so those cases pin only the local window; the federated window and the
    # dedup of its repeated stub ids are reached at limits 5 and 6.
    # Match-all without filters: the federated window must refill the slots its
    # repeated stub ids take, so the cached-only remote still arrives there.
    [(limit, None) for limit in range(1, 7)]
    # Tag filter widens the federated fetch to the whole cache.
    + [(limit, ["t"]) for limit in range(1, 7)],
)
def test_search_graph_dedups_multiple_adoptions_at_every_limit(
    tmp_path, limit, tags_any, adopted_remotes
):
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    local_ids = set()
    for remote_id in adopted_remotes:
        adopted = service.adopt_federated_node(f"federated::esam-main::{remote_id}")
        assert adopted["success"] is True
        local_ids.add(adopted["adopted_node"]["id"])
    # Two adopted copies, two reference stubs, and the remote that is only cached.
    eligible_ids = local_ids | {f"federated::esam-main::remote-{i}" for i in (1, 2, 3)}

    result = service.search_graph(query="", limit=limit, tags_any=tags_any)
    node_ids = [node["id"] for node in result["nodes"]]

    assert len(node_ids) == len(set(node_ids))
    assert set(node_ids) <= eligible_ids
    assert result["total"] == len(node_ids) == min(limit, len(eligible_ids))
    assert result["federation"]["federated_nodes"] == len(set(node_ids) - local_ids)


@pytest.mark.parametrize("adopted_remote", ["remote-1", "remote-2", "remote-3"])
def test_search_graph_dedups_before_the_limit_trim_on_the_widened_path(
    tmp_path, adopted_remote
):
    """A duplicate id must not take a slot that a distinct node is eligible for.

    Local results (adopted copy + reference stub) come first, then the widened
    federated fetch returns the whole cache, including the stub's id again.
    With limit equal to the number of distinct eligible nodes, every one of
    them must come back; trimming before deduping spends a slot on the
    duplicate and drops the last federated node. That only shows while the
    duplicate is not the window's last entry, and the window's order is the
    cache's, so each remote takes a turn as the adopted one.
    """
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    adopted = service.adopt_federated_node(f"federated::esam-main::{adopted_remote}")
    assert adopted["success"] is True
    eligible_ids = {adopted["adopted_node"]["id"]} | {
        f"federated::esam-main::remote-{i}" for i in (1, 2, 3)
    }

    result = service.search_graph(query="", limit=len(eligible_ids), tags_any=["t"])
    node_ids = [node["id"] for node in result["nodes"]]

    assert set(node_ids) == eligible_ids
    assert result["total"] == len(node_ids) == len(eligible_ids)


@pytest.mark.parametrize("adopted_remote", ["remote-1", "remote-2", "remote-3"])
# A text query must refill the slots as well as match-all does: the stub-overlap
# widening is not specific to either.
@pytest.mark.parametrize("query", ["", "external"])
def test_search_graph_federated_window_refills_slots_taken_by_local_stub_ids(
    tmp_path, query, adopted_remote
):
    """Without a filter the federated fetch is not widened to the whole cache.
    After an adoption the window can repeat the stub's id, which the dedup pass
    drops; the window must be sized so the remaining remotes still fill the
    free slots, while the local nodes keep their places ahead of every
    federated one. Each remote takes a turn as the adopted one, so the repeat
    falls inside an un-widened two-node window in at least one case."""
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    stub_id = f"federated::esam-main::{adopted_remote}"
    adopted = service.adopt_federated_node(stub_id)
    assert adopted["success"] is True
    local_ids = [adopted["adopted_node"]["id"], stub_id]

    result = service.search_graph(query=query, limit=4)
    node_ids = [node["id"] for node in result["nodes"]]

    assert sorted(node_ids[:2]) == sorted(local_ids)
    assert set(node_ids[2:]) == {
        f"federated::esam-main::remote-{i}" for i in (1, 2, 3)
    } - {stub_id}
    assert result["total"] == 4


# The two-node window is the cache's first two remotes, so only adopting
# remote-3 leaves the stub's id outside it and makes the window overflow.
@pytest.mark.parametrize(
    "adopted_remote,overflows",
    [("remote-1", False), ("remote-2", False), ("remote-3", True)],
)
def test_search_graph_trims_a_federated_window_that_overflows_the_free_slots(
    tmp_path, monkeypatch, adopted_remote, overflows
):
    """The stub-overlap widening asks for one extra federated node per local
    stub. When the stub's own id is not in the window that extra node is not
    absorbed by the dedup pass, so the result overflows by one and the final
    limit trim must cut it, keeping the local nodes."""
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    stub_id = f"federated::esam-main::{adopted_remote}"
    adopted = service.adopt_federated_node(stub_id)
    assert adopted["success"] is True
    local_ids = [adopted["adopted_node"]["id"], stub_id]

    manager = service._federation_manager
    windows = []
    real_search = manager.search_nodes

    def _record(*args, **kwargs):
        found = real_search(*args, **kwargs)
        windows.append([node.id for node in found["nodes"]])
        return found

    monkeypatch.setattr(manager, "search_nodes", _record)

    result = service.search_graph(query="", limit=3)
    free_slots = 3 - len(local_ids)
    (window,) = windows
    assert (len(set(window) - set(local_ids)) > free_slots) is overflows
    node_ids = [node["id"] for node in result["nodes"]]

    assert len(node_ids) == len(set(node_ids)) == 3
    assert result["total"] == 3
    assert sorted(node_ids[:2]) == sorted(local_ids)
    assert node_ids[2] != stub_id
    assert result["federation"]["federated_nodes"] == 2


def _record_federated_fetch_limits(service, monkeypatch):
    manager = service._federation_manager
    requested = []
    real_search = manager.search_nodes

    def _record(**kwargs):
        requested.append(kwargs["limit"])
        return real_search(**kwargs)

    monkeypatch.setattr(manager, "search_nodes", _record)
    return requested


@pytest.mark.parametrize(
    "query,semantic",
    [
        # Explicit semantic ranking of the local results.
        ("external", True),
        # Auto-fallback: nothing matches lexically, so semantic ranking is
        # retried and supplies the local results.
        ("no-lexical-hit", False),
    ],
)
def test_search_graph_widens_the_federated_window_for_stubs_found_semantically(
    tmp_path, monkeypatch, query, semantic
):
    """A stub that reached the local results through semantic ranking repeats
    its id in the federated window just as a lexically found one does, so the
    window is widened by it on both semantic paths."""
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    stub_id = "federated::esam-main::remote-1"
    adopted = service.adopt_federated_node(stub_id)
    assert adopted["success"] is True
    local_nodes = [
        service.storage.get_node(adopted["adopted_node"]["id"]),
        service.storage.get_node(stub_id),
    ]
    # The ML-free install has no embeddings; stand in a ranking that returns
    # both local nodes.
    monkeypatch.setattr(
        service.storage, "semantic_search_nodes", lambda **kwargs: local_nodes
    )
    requested = _record_federated_fetch_limits(service, monkeypatch)

    result = service.search_graph(query=query, limit=4, semantic=semantic)

    assert result["semantic"] is True
    # Two free slots plus one for the stub whose id the window repeats.
    assert requested == [3]
    if semantic:
        assert {node["id"] for node in result["nodes"]} == {
            node.id for node in local_nodes
        } | {"federated::esam-main::remote-2", "federated::esam-main::remote-3"}


def test_search_graph_federated_window_is_widened_to_the_cache_by_a_tag_filter(
    tmp_path, monkeypatch
):
    """A tag filter is applied after the fetch, so the federated window must
    cover the whole cache rather than only the free slots."""
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    service.storage.add_nodes(
        [Node(id="local-a", type=NodeType.ACTOR, name="Local A", tags=["t"])], []
    )
    requested = _record_federated_fetch_limits(service, monkeypatch)

    result = service.search_graph(query="", limit=2, tags_any=["t"])

    # One free slot, widened to the three cached remotes.
    assert requested == [3]
    assert [node["id"] for node in result["nodes"]][0] == "local-a"
    assert result["total"] == 2


def test_search_graph_federated_window_is_not_widened_without_local_stubs(
    tmp_path, monkeypatch
):
    """With no adopted node nothing local can repeat in the window, so the
    federated fetch asks for exactly the free slots."""
    service = _service_with_cached_federated_node(
        tmp_path, source_nodes=_TAGGED_REMOTE_NODES
    )
    service.storage.add_nodes(
        [Node(id="local-a", type=NodeType.ACTOR, name="Local A")], []
    )
    manager = service._federation_manager
    requested = []
    real_search = manager.search_nodes

    def _record(**kwargs):
        requested.append(kwargs["limit"])
        return real_search(**kwargs)

    monkeypatch.setattr(manager, "search_nodes", _record)

    result = service.search_graph(query="", limit=3)

    assert requested == [2]
    assert [node["id"] for node in result["nodes"]][0] == "local-a"
    assert result["total"] == 3


def test_search_graph_keeps_same_named_local_nodes_with_different_ids(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)
    service.storage.add_nodes(
        [
            Node(id="local-a", type=NodeType.ACTOR, name="Shared Name"),
            Node(id="local-b", type=NodeType.ACTOR, name="Shared Name"),
        ],
        [],
    )

    result = service.search_graph(query="Shared Name")
    node_ids = [node["id"] for node in result["nodes"]]

    assert sorted(node_ids) == ["local-a", "local-b"]
    assert result["total"] == 2


def test_adopt_federated_node_requires_existing_cached_node(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node("federated::esam-main::missing")

    assert result["success"] is False


def test_adopt_reuses_existing_reference_node_when_forcing_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second", create_new_copy=True
    )

    assert first["success"] is True
    assert second["success"] is True
    assert len(second["added_edge_ids"]) == 1


def test_adopt_returns_existing_when_already_adopted(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second"
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second["already_adopted"] is True
    assert second["adopted_node"]["name"] == "First"
    assert second["added_node_ids"] == []


def test_adopt_can_force_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second", create_new_copy=True
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second.get("already_adopted") is not True
    assert second["adopted_node"]["name"] == "Second"


def test_adopt_blocked_by_capability_policy(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "capabilities": {"allow_adopt": False},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        [{"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes

    service = GraphService(storage, federation_manager=manager)
    result = service.adopt_federated_node("federated::esam-main::remote-1")

    assert result["success"] is False
    assert "not allowed" in result["message"]


# Regression coverage for
# f3e27923-3a2f-40cc-9d0a-a6f4ac3e29c4 ("adopt_federated_node bypasses the
# SavedView annotation write-guard"): adopt_federated_node builds its local
# Node copy and calls storage.add_nodes directly, bypassing the
# mutations.add_nodes wrapper where saved_view_annotation_error is normally
# enforced. A SavedView/VisualizationView adopted from an adversarial or
# unpatched federated source must still be rejected when its metadata carries
# a non-embedded image annotation URL.
_REMOTE_IMAGE_ANNOTATION = {
    "id": "img-1",
    "type": "image",
    "kind": "image",
    "position": {"x": 0, "y": 0},
    "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
    "image": {
        "url": "https://attacker.example/tracker.png",
        "width": 10,
        "height": 10,
    },
    "alt": "",
}
_EMBEDDED_IMAGE_ANNOTATION = {
    **_REMOTE_IMAGE_ANNOTATION,
    "image": {
        "url": (
            "data:image/webp;base64,UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
        ),
        "width": 10,
        "height": 10,
    },
}


def _federated_saved_view_source(annotation):
    return {
        "id": "remote-view-1",
        "type": "SavedView",
        "name": "Remote View",
        "metadata": {
            "node_ids": ["actor-1"],
            "positions": {"actor-1": {"x": 0, "y": 0}},
            "annotation_schema_version": 1,
            "annotation_document": {
                "schema_version": 1,
                "annotations": [annotation],
            },
            "annotations": [annotation],
        },
    }


def test_adopt_federated_node_rejects_saved_view_with_remote_image_annotation(
    tmp_path,
):
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node=_federated_saved_view_source(_REMOTE_IMAGE_ANNOTATION),
    )

    result = service.adopt_federated_node("federated::esam-main::remote-view-1")

    assert result["success"] is False
    assert "embedded" in result["message"]
    assert result["added_node_ids"] == []
    # Nothing was persisted locally: no local SavedView clone exists.
    assert service.get_saved_view("Remote View")["success"] is False


def test_adopt_federated_node_accepts_saved_view_with_embedded_image_annotation(
    tmp_path,
):
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node=_federated_saved_view_source(_EMBEDDED_IMAGE_ANNOTATION),
    )

    result = service.adopt_federated_node("federated::esam-main::remote-view-1")

    assert result["success"] is True
    assert (
        result["adopted_node"]["metadata"]["annotation_document"]["annotations"][0][
            "image"
        ]["url"]
        == _EMBEDDED_IMAGE_ANNOTATION["image"]["url"]
    )


def test_adopt_federated_node_accepts_saved_view_with_no_annotation_content(
    tmp_path,
):
    source = {
        "id": "remote-view-2",
        "type": "SavedView",
        "name": "Empty Remote View",
        "metadata": {
            "node_ids": ["actor-1"],
            "positions": {"actor-1": {"x": 0, "y": 0}},
        },
    }
    service = _service_with_cached_federated_node(tmp_path, source_node=source)

    result = service.adopt_federated_node("federated::esam-main::remote-view-2")

    assert result["success"] is True
