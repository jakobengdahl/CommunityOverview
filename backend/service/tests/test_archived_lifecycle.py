"""
Tests for the generic `archived` lifecycle on nodes and edges.

Archiving hides a node/edge from search and traversal by default while keeping
it in the graph (unlike deletion, which is permanent). An explicit
``include_archived`` opt-in brings archived items back into the results. These
tests cover the default-exclude behaviour, the opt-in, and the archive/unarchive
mutations across the storage query paths, search_graph and get_related_nodes.
"""

import os
import tempfile

from backend.core import GraphStorage, Node, Edge, NodeType, RelationshipType
from backend.service import GraphService


def _service(nodes, edges=None) -> GraphService:
    tmp = tempfile.mkdtemp()
    storage = GraphStorage(
        json_path=os.path.join(tmp, "g.json"),
        embeddings_path=os.path.join(tmp, "e.pkl"),
    )
    storage.add_nodes(nodes, edges or [])
    return GraphService(storage)


def _names(result):
    return {n["name"] for n in result["nodes"]}


def _sample_service() -> GraphService:
    return _service(
        [
            Node(id="a1", type=NodeType.ACTOR, name="Alpha"),
            Node(id="a2", type=NodeType.ACTOR, name="Beta"),
            Node(id="a3", type=NodeType.ACTOR, name="Gamma"),
        ]
    )


class TestSearchExcludesArchived:
    def test_archived_node_excluded_by_default(self):
        service = _sample_service()
        service.archive_nodes(["a2"])
        assert _names(service.search_graph(query="")) == {"Alpha", "Gamma"}

    def test_include_archived_returns_all(self):
        service = _sample_service()
        service.archive_nodes(["a2"])
        result = service.search_graph(query="", include_archived=True)
        assert _names(result) == {"Alpha", "Beta", "Gamma"}

    def test_filters_echo_include_archived(self):
        service = _sample_service()
        assert service.search_graph(query="")["filters"]["include_archived"] is False
        assert (
            service.search_graph(query="", include_archived=True)["filters"][
                "include_archived"
            ]
            is True
        )

    def test_archived_node_does_not_consume_limit(self):
        # a1 archived; with limit=2 we should still get the two visible nodes,
        # not one visible + one silently dropped for the archived one.
        service = _sample_service()
        service.archive_nodes(["a1"])
        result = service.search_graph(query="", limit=2)
        assert _names(result) == {"Beta", "Gamma"}

    def test_text_search_excludes_archived(self):
        service = _service(
            [
                Node(id="n1", type=NodeType.ACTOR, name="Statistics Sweden"),
                Node(id="n2", type=NodeType.ACTOR, name="Statistics Norway"),
            ]
        )
        service.archive_nodes(["n2"])
        assert _names(service.search_graph(query="Statistics")) == {"Statistics Sweden"}


class TestArchiveNodeMutation:
    def test_archive_then_unarchive_restores_visibility(self):
        service = _sample_service()
        service.archive_nodes(["a2"])
        assert "Beta" not in _names(service.search_graph(query=""))
        service.unarchive_nodes(["a2"])
        assert "Beta" in _names(service.search_graph(query=""))

    def test_archive_result_shape(self):
        service = _sample_service()
        result = service.archive_nodes(["a1"])
        assert result["success"] is True
        assert result["archived"] is True
        assert result["node_ids"] == ["a1"]
        assert result["nodes"][0]["archived"] is True

    def test_archive_is_idempotent(self):
        service = _sample_service()
        service.archive_nodes(["a1"])
        # Archiving an already-archived node still reports it as archived.
        result = service.archive_nodes(["a1"])
        assert result["success"] is True
        assert result["node_ids"] == ["a1"]
        assert result["nodes"][0]["archived"] is True

    def test_archive_does_not_delete(self):
        service = _sample_service()
        service.archive_nodes(["a1"])
        # Still fetchable by id even though hidden from search.
        details = service.get_node_details("a1")
        assert details["success"] is True
        assert details["node"]["archived"] is True

    def test_archive_persists_across_reload(self):
        tmp = tempfile.mkdtemp()
        json_path = os.path.join(tmp, "g.json")
        emb_path = os.path.join(tmp, "e.pkl")
        storage = GraphStorage(json_path=json_path, embeddings_path=emb_path)
        storage.add_nodes([Node(id="a1", type=NodeType.ACTOR, name="Alpha")], [])
        GraphService(storage).archive_nodes(["a1"])
        # save() is debounced onto a background thread; drain it before reload.
        storage.flush()

        reloaded = GraphStorage(json_path=json_path, embeddings_path=emb_path)
        assert reloaded.get_node("a1").archived is True


class TestRelatedNodesExcludesArchived:
    def _linked_service(self) -> GraphService:
        return _service(
            [
                Node(id="root", type=NodeType.ACTOR, name="Root"),
                Node(id="near", type=NodeType.ACTOR, name="Near"),
                Node(id="far", type=NodeType.INITIATIVE, name="Far"),
            ],
            [
                Edge(
                    id="e-root-near",
                    source="root",
                    target="near",
                    type=RelationshipType.RELATES_TO,
                ),
                Edge(
                    id="e-near-far",
                    source="near",
                    target="far",
                    type=RelationshipType.RELATES_TO,
                ),
            ],
        )

    def test_archived_neighbour_excluded(self):
        service = self._linked_service()
        service.archive_nodes(["near"])
        result = service.get_related_nodes(node_id="root", depth=1)
        assert "Near" not in _names(result)

    def test_include_archived_returns_neighbour(self):
        service = self._linked_service()
        service.archive_nodes(["near"])
        result = service.get_related_nodes(
            node_id="root", depth=1, include_archived=True
        )
        assert "Near" in _names(result)

    def test_archived_node_not_reached_through_at_depth(self):
        # near is archived: a depth-2 traversal from root must not reach far
        # *through* near.
        service = self._linked_service()
        service.archive_nodes(["near"])
        result = service.get_related_nodes(node_id="root", depth=2)
        assert "Far" not in _names(result)
        assert "Near" not in _names(result)

    def test_archived_edge_not_traversed(self):
        # Archive only the edge; both endpoints stay visible but the edge is
        # hidden, so the neighbour is not reached.
        service = self._linked_service()
        service.archive_edges(["e-root-near"])
        result = service.get_related_nodes(node_id="root", depth=1)
        assert "Near" not in _names(result)
        # Opt-in brings it back.
        opened = service.get_related_nodes(
            node_id="root", depth=1, include_archived=True
        )
        assert "Near" in _names(opened)

    def test_unarchive_edge_restores_traversal(self):
        service = self._linked_service()
        service.archive_edges(["e-root-near"])
        service.unarchive_edges(["e-root-near"])
        result = service.get_related_nodes(node_id="root", depth=1)
        assert "Near" in _names(result)


class TestArchivedEdgesInSearch:
    def test_archived_connecting_edge_hidden(self):
        service = _service(
            [
                Node(id="a1", type=NodeType.ACTOR, name="Alpha"),
                Node(id="a2", type=NodeType.ACTOR, name="Beta"),
            ],
            [Edge(id="e1", source="a1", target="a2")],
        )
        service.archive_edges(["e1"])
        result = service.search_graph(query="")
        edge_ids = {e["id"] for e in result["edges"]}
        assert "e1" not in edge_ids
        # Opt-in shows it.
        opened = service.search_graph(query="", include_archived=True)
        assert "e1" in {e["id"] for e in opened["edges"]}

    def test_edge_to_archived_node_hidden(self):
        # Archive the *node*, not the edge: the (still-active) edge must not leak
        # the hidden node back into results as a dangling reference.
        service = _service(
            [
                Node(id="a1", type=NodeType.ACTOR, name="Alpha"),
                Node(id="a2", type=NodeType.ACTOR, name="Beta"),
            ],
            [Edge(id="e1", source="a1", target="a2")],
        )
        service.archive_nodes(["a2"])
        result = service.search_graph(query="")
        assert "Beta" not in _names(result)
        assert "e1" not in {e["id"] for e in result["edges"]}
        # Opt-in restores both node and its edge.
        opened = service.search_graph(query="", include_archived=True)
        assert "Beta" in _names(opened)
        assert "e1" in {e["id"] for e in opened["edges"]}


class TestTypedListExcludesArchived:
    def _typed_service(self) -> GraphService:
        return _service(
            [
                Node(id="a1", type=NodeType.ACTOR, name="Alpha"),
                Node(id="a2", type=NodeType.ACTOR, name="Beta"),
            ],
            [Edge(id="e1", source="a1", target="a2", type=RelationshipType.RELATES_TO)],
        )

    def test_list_typed_nodes_excludes_archived(self):
        service = self._typed_service()
        service.archive_nodes(["a2"])
        result = service.list_typed_nodes(node_type="Actor")
        assert {n["name"] for n in result["nodes"]} == {"Alpha"}
        assert result["filters"]["include_archived"] is False

    def test_list_typed_nodes_include_archived(self):
        service = self._typed_service()
        service.archive_nodes(["a2"])
        result = service.list_typed_nodes(node_type="Actor", include_archived=True)
        assert {n["name"] for n in result["nodes"]} == {"Alpha", "Beta"}

    def test_list_typed_nodes_drops_edge_to_archived_node(self):
        service = self._typed_service()
        service.archive_nodes(["a2"])
        result = service.list_typed_nodes(node_type="Actor")
        # a2 is gone, so the connecting edge cannot be returned either.
        assert result["edges"] == []

    def test_list_typed_edges_excludes_archived_edge(self):
        service = self._typed_service()
        service.archive_edges(["e1"])
        result = service.list_typed_edges(edge_type="RELATES_TO")
        assert {e["id"] for e in result["edges"]} == set()
        included = service.list_typed_edges(
            edge_type="RELATES_TO", include_archived=True
        )
        assert {e["id"] for e in included["edges"]} == {"e1"}

    def test_list_typed_edges_excludes_edge_to_archived_node(self):
        service = self._typed_service()
        service.archive_nodes(["a2"])
        result = service.list_typed_edges(edge_type="RELATES_TO")
        assert result["edges"] == []


class TestArchivedAnchorTraversal:
    def test_archived_anchor_stays_connected_in_cycle(self):
        # anchor is archived; a depth-2 cycle back to it must keep the anchor and
        # the edge that reconnects to it (the anchor is always part of the result).
        service = _service(
            [
                Node(id="anchor", type=NodeType.ACTOR, name="Anchor"),
                Node(id="mid", type=NodeType.ACTOR, name="Mid"),
            ],
            [
                Edge(id="e-out", source="anchor", target="mid"),
                Edge(id="e-back", source="mid", target="anchor"),
            ],
        )
        service.archive_nodes(["anchor"])
        result = service.get_related_nodes(
            node_id="anchor", depth=2, include_archived=True
        )
        # With include_archived the whole cycle is visible.
        assert {n["name"] for n in result["nodes"]} == {"Anchor", "Mid"}
        # Default (exclude): the anchor is still the returned anchor, and the edge
        # reconnecting to it from the visible Mid node is not dropped.
        default = service.get_related_nodes(node_id="anchor", depth=2)
        assert "Anchor" in {n["name"] for n in default["nodes"]}
        assert "e-back" in {e["id"] for e in default["edges"]}
