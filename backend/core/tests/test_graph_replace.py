"""
Tests for GraphStorage.replace_all_nodes_and_edges — the atomic whole-graph
swap that backs graph.json import (see backend/service/import_service.py and
docs/adr/0006-graph-import-replace-mode.md).
"""

import json

import pytest

from backend.core.models import Edge, Node, NodeType, RelationshipType
from backend.core.storage import GraphStorage


class _SnapshotBackend:
    """Minimal snapshot-only fake backend (no capability declaration), same
    shape as ``_SnapshotBackend`` in test_persistence_seam.py, plus a knob to
    make the next ``save_graph_data`` call raise so a mid-replace failure can
    be exercised deterministically."""

    def __init__(self):
        self.data = None
        self.saves = 0
        self.fail_next_save = False

    def exists(self):
        return self.data is not None

    def load_graph_data(self):
        return json.loads(json.dumps(self.data)) if self.data is not None else {}

    def save_graph_data(self, data):
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("simulated disk failure")
        self.saves += 1
        self.data = json.loads(json.dumps(data))

    def default_graph_name(self):
        return "test-graph"


@pytest.fixture
def backend() -> _SnapshotBackend:
    return _SnapshotBackend()


@pytest.fixture
def storage(backend: _SnapshotBackend) -> GraphStorage:
    return GraphStorage(persistence_backend=backend)


def _old_node() -> Node:
    return Node(id="old-1", type=NodeType.ACTOR, name="Old node")


def _new_node() -> Node:
    return Node(id="new-1", type=NodeType.ACTOR, name="New node")


class TestReplaceSwapsContent:
    def test_replace_discards_old_nodes_and_edges(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        assert storage.get_node("old-1") is not None

        storage.replace_all_nodes_and_edges([_new_node()], [])

        assert storage.get_node("old-1") is None
        assert storage.get_node("new-1") is not None
        assert [n.id for n in storage.get_all_nodes()] == ["new-1"]
        assert storage.get_all_edges() == []

    def test_replace_keeps_edges_between_new_nodes(self, storage: GraphStorage):
        a = Node(id="a", type=NodeType.ACTOR, name="A")
        b = Node(id="b", type=NodeType.INITIATIVE, name="B")
        edge = Edge(id="e1", source="a", target="b", type=RelationshipType.BELONGS_TO)

        storage.replace_all_nodes_and_edges([a, b], [edge])

        assert storage.get_node("a") is not None
        assert storage.get_node("b") is not None
        edges = storage.get_all_edges()
        assert len(edges) == 1
        assert edges[0].id == "e1"

    def test_replace_persists_the_new_content_to_the_backend(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.replace_all_nodes_and_edges([_new_node()], [])

        node_ids = {n["id"] for n in backend.data["nodes"]}
        assert node_ids == {"new-1"}

    def test_replace_is_searchable_afterwards(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        storage.replace_all_nodes_and_edges([_new_node()], [])

        results = storage.search_nodes("New node")
        assert any(n.id == "new-1" for n in results)
        assert all(n.id != "old-1" for n in storage.search_nodes("Old node"))


class TestReplaceDropsStaleVectors:
    def test_replace_clears_every_existing_vector(self, storage: GraphStorage):
        storage.add_nodes([_old_node()], [])
        storage.vector_store.load_vectors({"old-1": [0.1, 0.2, 0.3]})
        assert storage.vector_store.has_embedding("old-1")

        # Reusing the SAME id in the new document is the exact footgun this
        # guards against: without clearing, "new-1-reused" would silently
        # inherit "old-1"'s vector for what may be entirely different text.
        reused = Node(id="old-1", type=NodeType.ACTOR, name="Different content now")
        storage.replace_all_nodes_and_edges([reused], [])

        assert storage.vector_store.embeddings == {}
        assert not storage.vector_store.has_embedding("old-1")


class TestReplaceRollsBackOnSaveFailure:
    def test_failed_save_restores_previous_nodes_and_edges(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        # Drain background saves add_nodes already queued before arming the
        # failure — otherwise the flag can be consumed by one of THOSE saves
        # instead of the replace's, since they run on the same executor and
        # add_nodes does not wait for them.
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        # The live graph must be exactly what it was before the failed
        # replace — not the new content, and not some partial mix.
        assert storage.get_node("old-1") is not None
        assert storage.get_node("new-1") is None
        assert [n.id for n in storage.get_all_nodes()] == ["old-1"]

    def test_failed_save_restores_vectors(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.vector_store.load_vectors({"old-1": [0.1, 0.2, 0.3]})
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        assert storage.vector_store.has_embedding("old-1")

    def test_failed_save_leaves_the_graph_searchable_as_before(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.flush()
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        results = storage.search_nodes("Old node")
        assert any(n.id == "old-1" for n in results)

    def test_failed_save_never_reached_the_backend(
        self, storage: GraphStorage, backend: _SnapshotBackend
    ):
        storage.add_nodes([_old_node()], [])
        storage.flush()
        saves_before = backend.saves
        backend.fail_next_save = True

        with pytest.raises(OSError):
            storage.replace_all_nodes_and_edges([_new_node()], [])

        # The bootstrap save from __init__/add_nodes already landed; the
        # failed replace must not have landed a further one.
        assert backend.saves == saves_before
        assert {n["id"] for n in backend.data["nodes"]} == {"old-1"}
