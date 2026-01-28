"""
Unit tests for graph_core storage
"""

import pytest
import tempfile
import os
import json
from pathlib import Path

from graph_core import (
    GraphStorage, Node, Edge, NodeType, RelationshipType
)


@pytest.fixture
def temp_storage():
    """Create a temporary GraphStorage instance for testing"""
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "test_graph.json")
        embeddings_path = os.path.join(tmpdir, "test_embeddings.pkl")
        storage = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)
        yield storage


@pytest.fixture
def storage_with_data(temp_storage):
    """Create a storage instance with sample data"""
    # Add some test nodes
    nodes = [
        Node(id="actor-1", type=NodeType.ACTOR, name="Test Actor 1",
             description="First test actor", communities=["eSam"]),
        Node(id="actor-2", type=NodeType.ACTOR, name="Test Actor 2",
             description="Second test actor", communities=["eSam"]),
        Node(id="init-1", type=NodeType.INITIATIVE, name="Test Initiative",
             description="A test initiative", communities=["eSam"]),
        Node(id="community-1", type=NodeType.COMMUNITY, name="eSam",
             description="eSam community"),
    ]

    edges = [
        Edge(id="edge-1", source="actor-1", target="init-1",
             type=RelationshipType.BELONGS_TO),
        Edge(id="edge-2", source="actor-2", target="init-1",
             type=RelationshipType.RELATES_TO),
        Edge(id="edge-3", source="init-1", target="community-1",
             type=RelationshipType.PART_OF),
    ]

    temp_storage.add_nodes(nodes, edges)
    return temp_storage


class TestGraphStorageInit:
    """Tests for GraphStorage initialization"""

    def test_creates_empty_graph_on_init(self, temp_storage):
        """Test that a new storage starts with empty graph"""
        assert len(temp_storage.nodes) == 0
        assert len(temp_storage.edges) == 0

    def test_creates_json_file(self, temp_storage):
        """Test that JSON file is created"""
        assert temp_storage.json_path.exists()

    def test_loads_existing_graph(self, storage_with_data):
        """Test that storage can reload existing data"""
        json_path = storage_with_data.json_path
        embeddings_path = storage_with_data.vector_store.storage_path

        # Create new storage instance pointing to same file
        new_storage = GraphStorage(
            json_path=str(json_path),
            embeddings_path=str(embeddings_path)
        )

        assert len(new_storage.nodes) == 4
        assert len(new_storage.edges) == 3


class TestGraphStorageCRUD:
    """Tests for CRUD operations"""

    def test_add_single_node(self, temp_storage):
        """Test adding a single node"""
        node = Node(type=NodeType.ACTOR, name="New Actor")
        result = temp_storage.add_nodes([node], [])

        assert result.success is True
        assert len(result.added_node_ids) == 1
        assert temp_storage.get_node(node.id) is not None

    def test_add_node_with_edges(self, temp_storage):
        """Test adding nodes with edges"""
        node1 = Node(id="n1", type=NodeType.ACTOR, name="Actor")
        node2 = Node(id="n2", type=NodeType.INITIATIVE, name="Initiative")
        edge = Edge(source="n1", target="n2", type=RelationshipType.BELONGS_TO)

        result = temp_storage.add_nodes([node1, node2], [edge])

        assert result.success is True
        assert len(result.added_node_ids) == 2
        assert len(result.added_edge_ids) == 1

    def test_add_edge_by_name(self, temp_storage):
        """Test that edges can reference nodes by name"""
        node1 = Node(type=NodeType.ACTOR, name="Actor One")
        node2 = Node(type=NodeType.INITIATIVE, name="Initiative One")
        # Edge references nodes by name, not ID
        edge = Edge(source="Actor One", target="Initiative One",
                   type=RelationshipType.BELONGS_TO)

        result = temp_storage.add_nodes([node1, node2], [edge])

        assert result.success is True
        # Verify edge source/target were resolved to IDs
        added_edge = temp_storage.edges[result.added_edge_ids[0]]
        assert added_edge.source == node1.id
        assert added_edge.target == node2.id

    def test_add_duplicate_node_fails(self, storage_with_data):
        """Test that adding a duplicate node ID fails"""
        duplicate = Node(id="actor-1", type=NodeType.ACTOR, name="Duplicate")
        result = storage_with_data.add_nodes([duplicate], [])

        assert result.success is False
        assert "already exists" in result.message

    def test_add_edge_invalid_source_fails(self, temp_storage):
        """Test that adding edge with invalid source fails"""
        node = Node(id="n1", type=NodeType.ACTOR, name="Actor")
        edge = Edge(source="nonexistent", target="n1", type=RelationshipType.RELATES_TO)

        result = temp_storage.add_nodes([node], [edge])

        assert result.success is False
        assert "Source node" in result.message

    def test_get_node(self, storage_with_data):
        """Test getting a node by ID"""
        node = storage_with_data.get_node("actor-1")

        assert node is not None
        assert node.name == "Test Actor 1"

    def test_get_node_not_found(self, storage_with_data):
        """Test getting a non-existent node returns None"""
        node = storage_with_data.get_node("nonexistent")
        assert node is None

    def test_get_all_nodes(self, storage_with_data):
        """Test getting all nodes"""
        nodes = storage_with_data.get_all_nodes()
        assert len(nodes) == 4

    def test_get_all_edges(self, storage_with_data):
        """Test getting all edges"""
        edges = storage_with_data.get_all_edges()
        assert len(edges) == 3

    def test_update_node(self, storage_with_data):
        """Test updating a node"""
        updated = storage_with_data.update_node("actor-1", {
            "description": "Updated description",
            "tags": ["new-tag"]
        })

        assert updated is not None
        assert updated.description == "Updated description"
        assert "new-tag" in updated.tags

    def test_update_node_not_found(self, storage_with_data):
        """Test updating non-existent node returns None"""
        result = storage_with_data.update_node("nonexistent", {"name": "New"})
        assert result is None

    def test_delete_node(self, storage_with_data):
        """Test deleting a node"""
        result = storage_with_data.delete_nodes(["actor-1"], confirmed=True)

        assert result.success is True
        assert "actor-1" in result.deleted_node_ids
        assert storage_with_data.get_node("actor-1") is None
        # Edge should also be deleted
        assert "edge-1" in result.affected_edge_ids

    def test_delete_node_requires_confirmation(self, storage_with_data):
        """Test that deletion requires confirmation"""
        result = storage_with_data.delete_nodes(["actor-1"], confirmed=False)

        assert result.success is False
        assert "confirmed" in result.message.lower()
        # Node should still exist
        assert storage_with_data.get_node("actor-1") is not None

    def test_delete_max_10_nodes(self, storage_with_data):
        """Test that max 10 nodes can be deleted at once"""
        node_ids = [f"node-{i}" for i in range(15)]
        result = storage_with_data.delete_nodes(node_ids, confirmed=True)

        assert result.success is False
        assert "Max 10" in result.message


class TestGraphStorageSearch:
    """Tests for search functionality"""

    def test_search_by_name(self, storage_with_data):
        """Test searching nodes by name"""
        results = storage_with_data.search_nodes("Test Actor 1")

        assert len(results) >= 1
        assert any(n.name == "Test Actor 1" for n in results)

    def test_search_by_description(self, storage_with_data):
        """Test searching nodes by description"""
        results = storage_with_data.search_nodes("First test actor")

        assert len(results) >= 1
        assert any(n.id == "actor-1" for n in results)

    def test_search_filter_by_type(self, storage_with_data):
        """Test filtering search by node type"""
        results = storage_with_data.search_nodes("Test", node_types=[NodeType.ACTOR])

        assert len(results) >= 1
        assert all(n.type == NodeType.ACTOR for n in results)

    def test_search_filter_by_community(self, storage_with_data):
        """Test filtering search by community"""
        results = storage_with_data.search_nodes("", communities=["eSam"])

        # Should return all nodes in eSam community
        assert all(any(c == "eSam" for c in n.communities) for n in results)

    def test_search_limit(self, storage_with_data):
        """Test search result limit"""
        results = storage_with_data.search_nodes("", limit=2)
        assert len(results) <= 2

    def test_search_case_insensitive(self, storage_with_data):
        """Test that search is case-insensitive"""
        results1 = storage_with_data.search_nodes("TEST ACTOR")
        results2 = storage_with_data.search_nodes("test actor")

        assert len(results1) == len(results2)


class TestGraphStorageRelated:
    """Tests for get_related_nodes"""

    def test_get_related_nodes_depth_1(self, storage_with_data):
        """Test getting directly related nodes"""
        result = storage_with_data.get_related_nodes("actor-1", depth=1)

        assert len(result['nodes']) >= 2  # actor-1 and init-1
        assert len(result['edges']) >= 1  # edge-1

    def test_get_related_nodes_depth_2(self, storage_with_data):
        """Test getting nodes at depth 2"""
        result = storage_with_data.get_related_nodes("actor-1", depth=2)

        # Should reach community-1 through init-1
        node_ids = [n.id for n in result['nodes']]
        assert "community-1" in node_ids

    def test_get_related_nodes_filter_by_relationship(self, storage_with_data):
        """Test filtering by relationship type"""
        result = storage_with_data.get_related_nodes(
            "actor-1",
            relationship_types=[RelationshipType.BELONGS_TO],
            depth=1
        )

        # Should only follow BELONGS_TO edges
        edge_types = [e.type for e in result['edges']]
        assert all(t == RelationshipType.BELONGS_TO for t in edge_types)

    def test_get_related_nodes_not_found(self, storage_with_data):
        """Test getting related nodes for non-existent node"""
        result = storage_with_data.get_related_nodes("nonexistent")

        assert len(result['nodes']) == 0
        assert len(result['edges']) == 0


class TestGraphStorageSimilarity:
    """Tests for similarity search"""

    def test_find_similar_by_name(self, storage_with_data):
        """Test finding similar nodes by name"""
        results = storage_with_data.find_similar_nodes("Test Actor", threshold=0.5)

        assert len(results) >= 1
        # Check that results are SimilarNode objects
        assert hasattr(results[0], 'similarity_score')
        assert hasattr(results[0], 'match_reason')

    def test_find_similar_filter_by_type(self, storage_with_data):
        """Test filtering similar nodes by type"""
        results = storage_with_data.find_similar_nodes(
            "Test",
            node_type=NodeType.INITIATIVE,
            threshold=0.3
        )

        # All results should be initiatives
        assert all(r.node.type == NodeType.INITIATIVE for r in results)

    def test_find_similar_batch(self, storage_with_data):
        """Test batch similarity search"""
        names = ["Test Actor", "Test Initiative", "Unknown"]
        results = storage_with_data.find_similar_nodes_batch(names, threshold=0.5)

        assert "Test Actor" in results
        assert "Test Initiative" in results
        assert "Unknown" in results

    def test_similarity_score_range(self, storage_with_data):
        """Test that similarity scores are in valid range"""
        results = storage_with_data.find_similar_nodes("Test Actor", threshold=0.0)

        for r in results:
            assert 0.0 <= r.similarity_score <= 1.0


class TestGraphStorageStats:
    """Tests for statistics"""

    def test_get_stats(self, storage_with_data):
        """Test getting graph statistics"""
        stats = storage_with_data.get_stats()

        assert stats.total_nodes == 4
        assert stats.total_edges == 3
        assert "Actor" in stats.nodes_by_type
        assert stats.nodes_by_type["Actor"] == 2

    def test_get_stats_filter_by_community(self, storage_with_data):
        """Test getting stats filtered by community"""
        stats = storage_with_data.get_stats(communities=["eSam"])

        # Should only count nodes in eSam
        assert stats.total_nodes == 3  # 2 actors + 1 initiative


class TestGraphStoragePersistence:
    """Tests for data persistence"""

    def test_save_and_reload(self, temp_storage):
        """Test that data persists across storage instances"""
        # Add data
        node = Node(id="persist-1", type=NodeType.ACTOR, name="Persistent Node")
        temp_storage.add_nodes([node], [])

        # Get paths before closing
        json_path = str(temp_storage.json_path)
        embeddings_path = str(temp_storage.vector_store.storage_path)

        # Create new instance
        new_storage = GraphStorage(
            json_path=json_path,
            embeddings_path=embeddings_path
        )

        # Verify data loaded
        loaded_node = new_storage.get_node("persist-1")
        assert loaded_node is not None
        assert loaded_node.name == "Persistent Node"

    def test_json_format(self, storage_with_data):
        """Test that JSON file has correct format"""
        with open(storage_with_data.json_path, 'r') as f:
            data = json.load(f)

        assert 'nodes' in data
        assert 'edges' in data
        assert 'metadata' in data
        assert 'version' in data['metadata']
        assert 'last_updated' in data['metadata']


class TestGraphStorageEdgeHelpers:
    """Tests for edge helper methods"""

    def test_get_edges_between_nodes(self, storage_with_data):
        """Test getting edges between specific nodes"""
        edges = storage_with_data.get_edges_between_nodes(["actor-1", "init-1"])

        assert len(edges) == 1
        assert edges[0].id == "edge-1"

    def test_get_edges_for_node(self, storage_with_data):
        """Test getting all edges for a node"""
        edges = storage_with_data.get_edges_for_node("init-1")

        # init-1 has 3 edges: edge-1, edge-2 (incoming) and edge-3 (outgoing)
        assert len(edges) == 3
