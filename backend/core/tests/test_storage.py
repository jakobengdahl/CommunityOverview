"""
Unit tests for graph_core storage
"""

import pytest
import tempfile
import os
import json
from pathlib import Path

from backend.core import (
    FileGraphPersistenceBackend,
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
        # Drain the background ThreadPoolExecutor before the TemporaryDirectory
        # context manager removes tmpdir — otherwise the executor's in-flight
        # write of test_graph.json races with directory deletion (OSError ENOTEMPTY).
        storage.flush()


@pytest.fixture
def storage_with_data(temp_storage):
    """Create a storage instance with sample data"""
    # Add some test nodes
    nodes = [
        Node(id="actor-1", type=NodeType.ACTOR, name="Test Actor 1",
             description="First test actor"),
        Node(id="actor-2", type=NodeType.ACTOR, name="Test Actor 2",
             description="Second test actor"),
        Node(id="init-1", type=NodeType.INITIATIVE, name="Test Initiative",
             description="A test initiative"),
        Node(id="theme-1", type=NodeType.THEME, name="eSam",
             description="eSam collaboration theme"),
    ]

    edges = [
        Edge(id="edge-1", source="actor-1", target="init-1",
             type=RelationshipType.BELONGS_TO),
        Edge(id="edge-2", source="actor-2", target="init-1",
             type=RelationshipType.RELATES_TO),
        Edge(id="edge-3", source="init-1", target="theme-1",
             type=RelationshipType.PART_OF),
    ]

    temp_storage.add_nodes(nodes, edges)
    temp_storage.flush()  # Wait for async save so tests that reload from disk see all data
    return temp_storage


class InMemoryPersistenceBackend:
    """Test backend used to verify GraphStorage persistence delegation."""

    def __init__(self, initial_data=None, default_name="in-memory-graph"):
        self.data = initial_data
        self.default_name_value = default_name
        self.load_calls = 0
        self.save_calls = 0

    def exists(self):
        return self.data is not None

    def load_graph_data(self):
        self.load_calls += 1
        return json.loads(json.dumps(self.data))

    def save_graph_data(self, data):
        self.save_calls += 1
        self.data = json.loads(json.dumps(data))

    def default_graph_name(self):
        return self.default_name_value


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

        # Create new storage instance pointing to same file
        new_storage = GraphStorage(json_path=str(json_path))

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

    def test_delete_edge(self, storage_with_data):
        """Test deleting a single edge."""
        deleted = storage_with_data.delete_edge("edge-1")

        assert deleted is True
        assert "edge-1" not in storage_with_data.edges

    def test_delete_edges_max_50(self, storage_with_data):
        """Test that bulk edge deletion is limited to 50 edges."""
        edge_ids = [f"edge-{i}" for i in range(60)]
        result = storage_with_data.delete_edges(edge_ids)

        assert result.success is False
        assert "Max 50" in result.message

    def test_delete_edges_bulk(self, storage_with_data):
        """Test deleting multiple edges in one call."""
        result = storage_with_data.delete_edges(["edge-1", "edge-2"])

        assert result.success is True
        assert set(result.deleted_edge_ids) == {"edge-1", "edge-2"}
        assert "edge-1" not in storage_with_data.edges
        assert "edge-2" not in storage_with_data.edges

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

    def test_search_wildcard_returns_all(self, storage_with_data):
        """Test that empty query returns all nodes"""
        results = storage_with_data.search_nodes("")

        assert len(results) == 4

    def test_search_limit(self, storage_with_data):
        """Test search result limit"""
        results = storage_with_data.search_nodes("", limit=2)
        assert len(results) <= 2

    def test_search_case_insensitive(self, storage_with_data):
        """Test that search is case-insensitive"""
        results1 = storage_with_data.search_nodes("TEST ACTOR")
        results2 = storage_with_data.search_nodes("test actor")

        assert len(results1) == len(results2)


class TestSearchRanking:
    """Tests for search result ranking/prioritization."""

    @pytest.fixture
    def ranking_storage(self, temp_storage):
        """Storage with nodes designed to test ranking order."""
        nodes = [
            # Exact name match – should rank #1 when searching "esam"
            Node(id="exact", type=NodeType.THEME, name="eSam",
                 description="Unrelated description"),
            # Name starts with query – should rank #2
            Node(id="prefix", type=NodeType.THEME, name="eSam collaboration",
                 description="Unrelated description"),
            # Name contains query – should rank #3
            Node(id="contains", type=NodeType.THEME, name="Nordic eSam initiative",
                 description="Unrelated description"),
            # Only description matches – should rank last
            Node(id="desc-only", type=NodeType.ACTOR, name="Unrelated actor",
                 description="This actor is part of the eSam network"),
        ]
        temp_storage.add_nodes(nodes, [])
        return temp_storage

    def test_exact_name_match_ranks_first(self, ranking_storage):
        results = ranking_storage.search_nodes("esam")
        assert results[0].id == "exact"

    def test_name_prefix_ranks_before_name_contains(self, ranking_storage):
        results = ranking_storage.search_nodes("esam")
        ids = [n.id for n in results]
        assert ids.index("prefix") < ids.index("contains")

    def test_name_match_ranks_before_description_only(self, ranking_storage):
        results = ranking_storage.search_nodes("esam")
        ids = [n.id for n in results]
        assert ids.index("exact") < ids.index("desc-only")

    def test_name_contains_ranks_before_description_only(self, ranking_storage):
        results = ranking_storage.search_nodes("esam")
        ids = [n.id for n in results]
        assert ids.index("contains") < ids.index("desc-only")

    def test_type_match_ranks_above_description(self, temp_storage):
        """A node whose type matches the query should rank above a description-only match."""
        nodes = [
            Node(id="type-match", type=NodeType.ACTOR, name="Unrelated name",
                 description="something else"),
            Node(id="desc-match", type=NodeType.THEME, name="Another name",
                 description="actor responsible for this initiative"),
        ]
        temp_storage.add_nodes(nodes, [])
        results = temp_storage.search_nodes("actor")
        ids = [n.id for n in results]
        assert ids.index("type-match") < ids.index("desc-match")

    def test_ranking_respects_limit(self, ranking_storage):
        """Ranked results still respect the limit parameter."""
        results = ranking_storage.search_nodes("esam", limit=2)
        assert len(results) == 2
        # First result should still be the best match
        assert results[0].id == "exact"

    def test_score_node_match_exact_name(self, ranking_storage):
        """_score_node_match returns the highest primary-tier score for an exact name match."""
        node = ranking_storage.nodes["exact"]
        score = ranking_storage._score_node_match(node, "esam")
        assert score >= 500_000

    def test_score_node_match_prefix_less_than_exact(self, ranking_storage):
        exact_node = ranking_storage.nodes["exact"]
        prefix_node = ranking_storage.nodes["prefix"]
        exact_score = ranking_storage._score_node_match(exact_node, "esam")
        prefix_score = ranking_storage._score_node_match(prefix_node, "esam")
        assert exact_score > prefix_score

    def test_exact_name_beats_prefix_plus_description(self, temp_storage):
        """Exact name match must rank above prefix+description even though additive scores
        would have exceeded 100 in the old single-band scheme (90+20=110 vs 100)."""
        nodes = [
            Node(id="exact", type=NodeType.THEME, name="esam",
                 description="unrelated"),
            # prefix + description hit — would score 110 with old scheme, should still lose
            Node(id="prefix-desc", type=NodeType.THEME, name="esam collaboration",
                 description="part of the esam network"),
        ]
        temp_storage.add_nodes(nodes, [])
        results = temp_storage.search_nodes("esam")
        assert results[0].id == "exact"

    def test_exact_name_beats_multi_secondary_match(self, temp_storage):
        """Exact name match must rank above a node with no name match but many
        secondary hits (type + tags + description)."""
        nodes = [
            Node(id="exact", type=NodeType.ACTOR, name="esam",
                 description="unrelated"),
            Node(id="multi", type=NodeType.THEME, name="Nordic collaboration",
                 description="part of the esam network", tags=["esam"],
                 subtypes=["esam working group"]),
        ]
        temp_storage.add_nodes(nodes, [])
        results = temp_storage.search_nodes("esam")
        assert results[0].id == "exact"

    def test_subtype_match_ranks_above_description_only(self, temp_storage):
        """A subtype hit (400 pts) should rank above a description-only hit (200 pts)."""
        nodes = [
            Node(id="subtype", type=NodeType.INITIATIVE, name="Unrelated name",
                 description="unrelated", subtypes=["esam working group"]),
            Node(id="desc", type=NodeType.ACTOR, name="Another unrelated name",
                 description="part of the esam network"),
        ]
        temp_storage.add_nodes(nodes, [])
        results = temp_storage.search_nodes("esam")
        ids = [n.id for n in results]
        assert ids.index("subtype") < ids.index("desc")

    def test_tag_exact_match_ranks_above_tag_substring(self, temp_storage):
        """An exact tag match (500 pts) should rank above a partial tag match (450 pts)."""
        nodes = [
            Node(id="exact-tag", type=NodeType.THEME, name="Unrelated name",
                 description="unrelated", tags=["esam"]),
            Node(id="partial-tag", type=NodeType.THEME, name="Another unrelated name",
                 description="unrelated", tags=["nordic-esam-initiative"]),
        ]
        temp_storage.add_nodes(nodes, [])
        results = temp_storage.search_nodes("esam")
        ids = [n.id for n in results]
        assert ids.index("exact-tag") < ids.index("partial-tag")


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

        # Should reach theme-1 through init-1
        node_ids = [n.id for n in result['nodes']]
        assert "theme-1" in node_ids

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

    def test_get_stats_counts_by_type(self, storage_with_data):
        """Test that stats correctly count nodes by type"""
        stats = storage_with_data.get_stats()

        assert "Theme" in stats.nodes_by_type
        assert stats.nodes_by_type["Theme"] == 1


class TestGraphStorageSubtypes:
    """Tests for subtypes functionality"""

    def test_add_node_with_subtypes(self, temp_storage):
        """Test adding a node with subtypes"""
        node = Node(
            id="actor-sub-1",
            type=NodeType.ACTOR,
            name="Test Agency",
            subtypes=["Government agency", "Regulatory body"]
        )
        temp_storage.add_nodes([node], [])
        stored = temp_storage.get_node("actor-sub-1")
        assert stored.subtypes == ["Government agency", "Regulatory body"]

    def test_update_node_subtypes(self, temp_storage):
        """Test updating subtypes on an existing node"""
        node = Node(id="actor-sub-2", type=NodeType.ACTOR, name="Test Org")
        temp_storage.add_nodes([node], [])

        temp_storage.update_node("actor-sub-2", {"subtypes": ["Municipality"]})
        updated = temp_storage.get_node("actor-sub-2")
        assert updated.subtypes == ["Municipality"]

    def test_get_subtypes_by_node_type(self, temp_storage):
        """Test getting subtypes grouped by node type"""
        nodes = [
            Node(id="s1", type=NodeType.ACTOR, name="A1", subtypes=["Government agency"]),
            Node(id="s2", type=NodeType.ACTOR, name="A2", subtypes=["Municipality", "Government agency"]),
            Node(id="s3", type=NodeType.INITIATIVE, name="I1", subtypes=["Research project"]),
            Node(id="s4", type=NodeType.ACTOR, name="A3"),  # No subtypes
        ]
        temp_storage.add_nodes(nodes, [])

        result = temp_storage.get_subtypes_by_node_type()
        assert "Actor" in result
        assert sorted(result["Actor"]) == ["Government agency", "Municipality"]
        assert "Initiative" in result
        assert result["Initiative"] == ["Research project"]

    def test_get_subtypes_filtered_by_type(self, temp_storage):
        """Test filtering subtypes by a specific node type"""
        nodes = [
            Node(id="f1", type=NodeType.ACTOR, name="A1", subtypes=["Government agency"]),
            Node(id="f2", type=NodeType.INITIATIVE, name="I1", subtypes=["Pilot program"]),
        ]
        temp_storage.add_nodes(nodes, [])

        result = temp_storage.get_subtypes_by_node_type("Actor")
        assert "Actor" in result
        assert "Initiative" not in result

    def test_search_includes_subtypes(self, temp_storage):
        """Test that search matches against subtypes"""
        node = Node(
            id="search-sub-1",
            type=NodeType.ACTOR,
            name="Test Entity",
            subtypes=["Steering group"]
        )
        temp_storage.add_nodes([node], [])

        results = temp_storage.search_nodes("steering group")
        assert len(results) == 1
        assert results[0].id == "search-sub-1"

    def test_get_stats_with_string_typed_nodes_does_not_crash(self, temp_storage):
        """get_stats must not crash when nodes have string types (e.g. EventSubscription).

        Config-defined node types such as EventSubscription and Agent are stored
        as plain strings rather than NodeType enum members.  Before the fix,
        calling node.type.value on a string raised AttributeError.
        """
        nodes = [
            Node(id="sub-1", type="EventSubscription", name="My Subscription"),
            Node(id="agent-1", type="Agent", name="My Agent"),
            Node(id="actor-1", type=NodeType.ACTOR, name="An Actor"),
        ]
        temp_storage.add_nodes(nodes, [])

        stats = temp_storage.get_stats()

        assert stats.total_nodes == 3
        assert stats.nodes_by_type.get("EventSubscription") == 1
        assert stats.nodes_by_type.get("Agent") == 1
        assert stats.nodes_by_type.get("Actor") == 1


class TestGraphStoragePersistence:
    """Tests for data persistence"""

    def test_uses_file_backend_by_default(self, temp_storage):
        """Standalone mode should still default to the file-backed adapter."""
        assert isinstance(temp_storage._persistence_backend, FileGraphPersistenceBackend)
        assert temp_storage._persistence_backend.json_path == temp_storage.json_path

    def test_persistence_backend_can_be_injected(self):
        """GraphStorage should delegate load/save through the persistence seam."""
        backend = InMemoryPersistenceBackend(initial_data={
            "nodes": [
                {
                    "id": "persist-backend-1",
                    "type": NodeType.ACTOR.value,
                    "name": "Injected Backend Node",
                    "description": "Loaded via custom backend",
                    "summary": "",
                    "tags": [],
                    "subtypes": [],
                    "metadata": {},
                }
            ],
            "edges": [],
            "metadata": {"version": "1.0"},
        })

        storage = GraphStorage(persistence_backend=backend)

        assert backend.load_calls == 1
        assert storage.get_node("persist-backend-1") is not None
        assert storage.get_graph_name() == "in-memory-graph"

        storage.add_nodes([Node(id="persist-backend-2", type=NodeType.ACTOR, name="Saved Node")], [])
        storage.flush()  # Wait for async save before reading backend state

        assert backend.save_calls >= 1
        persisted_ids = {node["id"] for node in backend.data["nodes"]}
        assert {"persist-backend-1", "persist-backend-2"}.issubset(persisted_ids)
        assert backend.data["metadata"]["graph_name"] == "in-memory-graph"

    def test_save_and_reload(self, temp_storage):
        """Test that data persists across storage instances"""
        # Add data
        node = Node(id="persist-1", type=NodeType.ACTOR, name="Persistent Node")
        temp_storage.add_nodes([node], [])

        # Flush pending async saves before reloading from disk
        temp_storage.flush()

        # Get path before closing
        json_path = str(temp_storage.json_path)

        # Create new instance
        new_storage = GraphStorage(json_path=json_path)

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


class TestGraphStorageConcurrency:
    """Tests for concurrent access safety.

    These tests verify that multiple threads can safely access the storage
    without data loss or corruption.
    """

    def test_concurrent_add_nodes_no_data_loss(self, temp_storage):
        """
        Test that concurrent add_nodes operations don't lose data.

        Multiple threads adding nodes simultaneously should result in
        all nodes being present in the final graph.
        """
        import threading
        import uuid

        num_threads = 10
        nodes_per_thread = 5
        errors = []
        added_ids = []
        lock = threading.Lock()

        def add_nodes_worker(thread_id):
            try:
                for i in range(nodes_per_thread):
                    node_id = f"concurrent-{thread_id}-{i}-{uuid.uuid4().hex[:8]}"
                    node = Node(
                        id=node_id,
                        type=NodeType.ACTOR,
                        name=f"Thread {thread_id} Node {i}",
                        description=f"Created by thread {thread_id}"
                    )
                    result = temp_storage.add_nodes([node], [])
                    if result.success:
                        with lock:
                            added_ids.extend(result.added_node_ids)
                    else:
                        with lock:
                            errors.append(f"Thread {thread_id}: {result.message}")
            except Exception as e:
                with lock:
                    errors.append(f"Thread {thread_id} exception: {e}")

        # Start all threads
        threads = []
        for t_id in range(num_threads):
            t = threading.Thread(target=add_nodes_worker, args=(t_id,))
            threads.append(t)
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join()

        # Verify results
        assert len(errors) == 0, f"Errors occurred: {errors}"

        expected_count = num_threads * nodes_per_thread
        assert len(added_ids) == expected_count, \
            f"Expected {expected_count} nodes added, got {len(added_ids)}"

        # Verify all nodes are actually in storage
        for node_id in added_ids:
            assert temp_storage.get_node(node_id) is not None, \
                f"Node {node_id} was added but not found in storage"

        # Flush all pending async saves before reloading from disk
        temp_storage.flush()

        # Verify persistence - reload and check
        json_path = str(temp_storage.json_path)
        reloaded = GraphStorage(json_path=json_path)
        assert len(reloaded.nodes) == expected_count, \
            f"After reload: expected {expected_count} nodes, got {len(reloaded.nodes)}"

    def test_concurrent_update_nodes_no_data_loss(self, temp_storage):
        """
        Test that concurrent update operations don't lose changes.

        Multiple threads updating the same node should all apply their changes
        (though order may vary due to race conditions).
        """
        import threading

        # First add a node to update
        node = Node(
            id="update-target",
            type=NodeType.ACTOR,
            name="Update Target",
            tags=[]
        )
        temp_storage.add_nodes([node], [])

        num_threads = 10
        errors = []
        lock = threading.Lock()

        def update_worker(thread_id):
            try:
                # Each thread adds its own tag
                result = temp_storage.update_node("update-target", {
                    "tags": [f"tag-{thread_id}"]
                })
                if result is None:
                    with lock:
                        errors.append(f"Thread {thread_id}: update returned None")
            except Exception as e:
                with lock:
                    errors.append(f"Thread {thread_id} exception: {e}")

        # Start all threads
        threads = []
        for t_id in range(num_threads):
            t = threading.Thread(target=update_worker, args=(t_id,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Verify no errors
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # The final state should have ONE tag (last write wins)
        # But critically, the node should still exist and be valid
        final_node = temp_storage.get_node("update-target")
        assert final_node is not None, "Node was lost during concurrent updates"
        assert final_node.name == "Update Target", "Node name was corrupted"

    def test_concurrent_mixed_operations(self, temp_storage):
        """
        Test mixed concurrent operations (add, update, delete, read).

        This simulates real-world usage where different users perform
        different operations simultaneously.
        """
        import threading
        import uuid
        import random
        import time

        # First add some base nodes
        base_nodes = []
        for i in range(5):
            node = Node(
                id=f"base-{i}",
                type=NodeType.ACTOR,
                name=f"Base Node {i}"
            )
            base_nodes.append(node)
        temp_storage.add_nodes(base_nodes, [])

        num_threads = 20
        operations_per_thread = 10
        errors = []
        lock = threading.Lock()

        def mixed_worker(thread_id):
            try:
                for _ in range(operations_per_thread):
                    op = random.choice(['add', 'read', 'update', 'search'])

                    if op == 'add':
                        node_id = f"mixed-{thread_id}-{uuid.uuid4().hex[:8]}"
                        node = Node(id=node_id, type=NodeType.ACTOR, name=f"Mixed {node_id}")
                        temp_storage.add_nodes([node], [])

                    elif op == 'read':
                        # Read a random base node
                        node_id = f"base-{random.randint(0, 4)}"
                        temp_storage.get_node(node_id)

                    elif op == 'update':
                        # Update a random base node
                        node_id = f"base-{random.randint(0, 4)}"
                        temp_storage.update_node(node_id, {
                            "description": f"Updated by thread {thread_id}"
                        })

                    elif op == 'search':
                        temp_storage.search_nodes("Node", limit=10)

                    # Small random delay to increase interleaving
                    time.sleep(random.uniform(0, 0.01))

            except Exception as e:
                with lock:
                    errors.append(f"Thread {thread_id} exception: {e}")

        # Start all threads
        threads = []
        for t_id in range(num_threads):
            t = threading.Thread(target=mixed_worker, args=(t_id,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Verify no errors
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify base nodes still exist and are valid
        for i in range(5):
            node = temp_storage.get_node(f"base-{i}")
            assert node is not None, f"Base node {i} was lost"
            assert node.name == f"Base Node {i}", f"Base node {i} name was corrupted"

        # Verify graph is still loadable
        json_path = str(temp_storage.json_path)
        reloaded = GraphStorage(json_path=json_path)
        assert len(reloaded.nodes) >= 5, "Graph is corrupted after concurrent operations"

    def test_atomic_save_prevents_corruption(self, temp_storage):
        """
        Test that the atomic save mechanism prevents file corruption.

        Even if multiple saves happen simultaneously, the JSON file
        should always be valid and parseable.
        """
        import threading
        import json as json_module

        num_threads = 20
        saves_per_thread = 10
        errors = []
        lock = threading.Lock()

        # Add initial data
        for i in range(10):
            node = Node(id=f"atomic-{i}", type=NodeType.ACTOR, name=f"Atomic Node {i}")
            temp_storage.add_nodes([node], [])

        def save_worker(thread_id):
            try:
                for i in range(saves_per_thread):
                    # Force a save and wait for the background write to complete
                    # before reading the file — save() is async.
                    temp_storage.save().result()

                    # Immediately try to read and parse the JSON file
                    try:
                        with open(temp_storage.json_path, 'r') as f:
                            data = json_module.load(f)
                            # Verify structure is valid
                            assert 'nodes' in data
                            assert 'edges' in data
                    except json_module.JSONDecodeError as e:
                        with lock:
                            errors.append(f"Thread {thread_id} save {i}: JSON decode error - {e}")
                    except Exception as e:
                        with lock:
                            errors.append(f"Thread {thread_id} save {i}: {e}")

            except Exception as e:
                with lock:
                    errors.append(f"Thread {thread_id} exception: {e}")

        # Start all threads
        threads = []
        for t_id in range(num_threads):
            t = threading.Thread(target=save_worker, args=(t_id,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Verify no errors
        assert len(errors) == 0, f"Corruption detected: {errors}"

        # Final verification
        with open(temp_storage.json_path, 'r') as f:
            final_data = json_module.load(f)
            assert len(final_data['nodes']) == 10

    def test_reload_during_concurrent_writes(self, temp_storage):
        """
        Test that reload() works correctly during concurrent writes.

        This tests the scenario where one thread calls reload() while
        other threads are writing.
        """
        import threading
        import uuid

        errors = []
        lock = threading.Lock()

        # Add initial data and flush so the file is on disk before reader threads start
        node = Node(id="reload-test", type=NodeType.ACTOR, name="Reload Test")
        temp_storage.add_nodes([node], [])
        temp_storage.flush()

        def writer_thread(thread_id):
            try:
                for i in range(20):
                    node_id = f"writer-{thread_id}-{i}-{uuid.uuid4().hex[:8]}"
                    node = Node(id=node_id, type=NodeType.ACTOR, name=f"Writer {thread_id}")
                    temp_storage.add_nodes([node], [])
            except Exception as e:
                with lock:
                    errors.append(f"Writer {thread_id}: {e}")

        def reader_thread():
            try:
                for _ in range(10):
                    temp_storage.reload()
                    # After reload, graph should be valid
                    assert temp_storage.get_node("reload-test") is not None
            except Exception as e:
                with lock:
                    errors.append(f"Reader: {e}")

        # Start threads
        threads = []
        for t_id in range(3):
            t = threading.Thread(target=writer_thread, args=(t_id,))
            threads.append(t)
            t.start()

        reader = threading.Thread(target=reader_thread)
        threads.append(reader)
        reader.start()

        # Wait for all
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"


class TestGraphStorageIncidentEdges:
    """Tests for get_incident_edges"""

    def test_get_incident_edges(self, storage_with_data):
        """Test getting edges for a list of nodes"""
        # actor-1 has edge-1 (outgoing)
        # init-1 has edge-1 (incoming), edge-2 (incoming), edge-3 (outgoing)
        edges = storage_with_data.get_incident_edges(["actor-1", "init-1"])

        edge_ids = sorted([e.id for e in edges])
        # Should include edge-1, edge-2, edge-3
        # edge-1 connects actor-1 and init-1 (internal to the set) - should appear once
        # edge-2 connects actor-2 -> init-1 (incoming to set)
        # edge-3 connects init-1 -> community-1 (outgoing from set)

        assert len(edges) == 3
        assert "edge-1" in edge_ids
        assert "edge-2" in edge_ids
        assert "edge-3" in edge_ids

    def test_get_incident_edges_no_edges(self, temp_storage):
        """Test with node having no edges"""
        node = Node(type=NodeType.ACTOR, name="Lonely Node")
        temp_storage.add_nodes([node], [])

        edges = temp_storage.get_incident_edges([node.id])
        assert len(edges) == 0

    def test_get_incident_edges_nonexistent_node(self, storage_with_data):
        """Test with non-existent node"""
        edges = storage_with_data.get_incident_edges(["nonexistent"])
        assert len(edges) == 0
