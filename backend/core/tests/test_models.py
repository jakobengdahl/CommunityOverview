"""
Unit tests for graph_core models
"""

import pytest
from datetime import datetime
import uuid

from backend.core.models import (
    Node, Edge, NodeType, RelationshipType,
    SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult,
    NODE_COLORS
)


class TestNodeType:
    """Tests for NodeType enum"""

    def test_node_types_exist(self):
        """Verify all expected node types exist"""
        expected_types = [
            "Actor", "Initiative", "Capability",
            "Resource", "Legislation", "Theme", "Goal", "Event", "SavedView"
        ]
        for type_name in expected_types:
            assert NodeType(type_name) is not None

    def test_node_type_values(self):
        """Verify node type string values"""
        assert NodeType.ACTOR.value == "Actor"
        assert NodeType.INITIATIVE.value == "Initiative"

    def test_all_node_types_have_colors(self):
        """Verify all node types have assigned colors"""
        for node_type in NodeType:
            assert node_type in NODE_COLORS
            assert NODE_COLORS[node_type].startswith("#")


class TestRelationshipType:
    """Tests for RelationshipType enum"""

    def test_relationship_types_exist(self):
        """Verify all expected relationship types exist"""
        expected_types = [
            "BELONGS_TO", "IMPLEMENTS", "PRODUCES",
            "GOVERNED_BY", "RELATES_TO", "PART_OF", "AIMS_FOR"
        ]
        for type_name in expected_types:
            assert RelationshipType(type_name) is not None


class TestNode:
    """Tests for Node model"""

    def test_create_node_minimal(self):
        """Test creating a node with minimal required fields"""
        node = Node(type=NodeType.ACTOR, name="Test Actor")

        assert node.name == "Test Actor"
        assert node.type == NodeType.ACTOR
        assert node.id is not None
        assert len(node.id) == 36  # UUID format
        assert node.description == ""
        assert node.summary == ""
        assert node.tags == []
        assert node.subtypes == []
        assert node.metadata == {}
        assert isinstance(node.created_at, datetime)
        assert isinstance(node.updated_at, datetime)

    def test_create_node_full(self):
        """Test creating a node with all fields"""
        node = Node(
            id="test-id-123",
            type=NodeType.INITIATIVE,
            name="Test Initiative",
            description="A test initiative description",
            summary="Test summary",
            tags=["tag1", "tag2"],
            metadata={"key": "value"}
        )

        assert node.id == "test-id-123"
        assert node.name == "Test Initiative"
        assert node.description == "A test initiative description"
        assert node.summary == "Test summary"
        assert node.tags == ["tag1", "tag2"]
        assert node.metadata == {"key": "value"}

    def test_node_to_dict(self):
        """Test converting node to dictionary"""
        node = Node(
            type=NodeType.ACTOR,
            name="Test Actor",
            tags=["governance"]
        )

        data = node.to_dict()

        assert data['name'] == "Test Actor"
        assert data['type'] == "Actor"
        assert data['tags'] == ["governance"]
        assert isinstance(data['created_at'], str)  # ISO format string

    def test_node_from_dict(self):
        """Test creating node from dictionary"""
        data = {
            'id': 'test-id',
            'type': 'Actor',
            'name': 'Test Actor',
            'description': 'Test description',
            'summary': 'Summary',
            'tags': ['tag1'],
            'metadata': {},
            'created_at': '2024-01-01T00:00:00',
            'updated_at': '2024-01-01T00:00:00'
        }

        node = Node.from_dict(data)

        assert node.id == 'test-id'
        assert node.name == 'Test Actor'
        assert node.type == NodeType.ACTOR
        assert isinstance(node.created_at, datetime)

    def test_node_get_color(self):
        """Test getting node color"""
        node = Node(type=NodeType.ACTOR, name="Test")
        assert node.get_color() == "#3B82F6"  # blue

        node2 = Node(type=NodeType.INITIATIVE, name="Test")
        assert node2.get_color() == "#10B981"  # green

    def test_node_name_validation(self):
        """Test that node name must be non-empty"""
        with pytest.raises(ValueError):
            Node(type=NodeType.ACTOR, name="")

    def test_create_node_with_subtypes(self):
        """Test creating a node with subtypes"""
        node = Node(
            type=NodeType.ACTOR,
            name="Test Agency",
            subtypes=["Government agency", "Regulatory body"]
        )

        assert node.subtypes == ["Government agency", "Regulatory body"]

    def test_node_subtypes_default_empty(self):
        """Test that subtypes defaults to empty list"""
        node = Node(type=NodeType.ACTOR, name="Test")
        assert node.subtypes == []

    def test_node_subtypes_in_to_dict(self):
        """Test that subtypes are included in to_dict"""
        node = Node(
            type=NodeType.ACTOR,
            name="Test",
            subtypes=["Municipality"]
        )
        data = node.to_dict()
        assert data['subtypes'] == ["Municipality"]

    def test_node_subtypes_from_dict(self):
        """Test that subtypes are loaded from dict"""
        data = {
            'id': 'test-id',
            'type': 'Actor',
            'name': 'Test Actor',
            'subtypes': ["Government agency"],
            'created_at': '2024-01-01T00:00:00',
            'updated_at': '2024-01-01T00:00:00'
        }
        node = Node.from_dict(data)
        assert node.subtypes == ["Government agency"]

    def test_node_subtypes_missing_from_dict(self):
        """Test that missing subtypes in dict defaults to empty list"""
        data = {
            'id': 'test-id',
            'type': 'Actor',
            'name': 'Test Actor',
            'created_at': '2024-01-01T00:00:00',
            'updated_at': '2024-01-01T00:00:00'
        }
        node = Node.from_dict(data)
        assert node.subtypes == []

    def test_node_auto_generates_uuid(self):
        """Test that nodes get unique UUIDs"""
        node1 = Node(type=NodeType.ACTOR, name="Node 1")
        node2 = Node(type=NodeType.ACTOR, name="Node 2")

        assert node1.id != node2.id
        # Verify it's a valid UUID
        uuid.UUID(node1.id)
        uuid.UUID(node2.id)


class TestEdge:
    """Tests for Edge model"""

    def test_create_edge_minimal(self):
        """Test creating an edge with minimal fields"""
        edge = Edge(
            source="node-1",
            target="node-2",
            type=RelationshipType.BELONGS_TO
        )

        assert edge.source == "node-1"
        assert edge.target == "node-2"
        assert edge.type == RelationshipType.BELONGS_TO
        assert edge.id is not None
        assert edge.metadata == {}

    def test_edge_to_dict(self):
        """Test converting edge to dictionary"""
        edge = Edge(
            source="node-1",
            target="node-2",
            type=RelationshipType.IMPLEMENTS,
            metadata={"weight": 1.0}
        )

        data = edge.to_dict()

        assert data['source'] == "node-1"
        assert data['target'] == "node-2"
        assert data['type'] == "IMPLEMENTS"
        assert data['metadata'] == {"weight": 1.0}

    def test_edge_from_dict(self):
        """Test creating edge from dictionary"""
        data = {
            'id': 'edge-1',
            'source': 'node-1',
            'target': 'node-2',
            'type': 'RELATES_TO',
            'metadata': {},
            'created_at': '2024-01-01T00:00:00'
        }

        edge = Edge.from_dict(data)

        assert edge.id == 'edge-1'
        assert edge.type == RelationshipType.RELATES_TO


class TestSimilarNode:
    """Tests for SimilarNode model"""

    def test_create_similar_node(self):
        """Test creating a SimilarNode result"""
        node = Node(type=NodeType.ACTOR, name="Test")
        similar = SimilarNode(
            node=node,
            similarity_score=0.85,
            match_reason="Name similarity: 85%"
        )

        assert similar.node == node
        assert similar.similarity_score == 0.85
        assert similar.match_reason == "Name similarity: 85%"

    def test_similarity_score_validation(self):
        """Test that similarity score must be between 0 and 1"""
        node = Node(type=NodeType.ACTOR, name="Test")

        with pytest.raises(ValueError):
            SimilarNode(node=node, similarity_score=1.5)

        with pytest.raises(ValueError):
            SimilarNode(node=node, similarity_score=-0.1)


class TestGraphStats:
    """Tests for GraphStats model"""

    def test_create_graph_stats(self):
        """Test creating GraphStats"""
        stats = GraphStats(
            total_nodes=100,
            total_edges=150,
            nodes_by_type={"Actor": 50, "Initiative": 30},
            last_updated=datetime.utcnow()
        )

        assert stats.total_nodes == 100
        assert stats.total_edges == 150
        assert stats.nodes_by_type["Actor"] == 50


class TestAddNodesResult:
    """Tests for AddNodesResult model"""

    def test_create_success_result(self):
        """Test creating a successful AddNodesResult"""
        result = AddNodesResult(
            added_node_ids=["node-1", "node-2"],
            added_edge_ids=["edge-1"],
            success=True,
            message="Added 2 nodes and 1 edge"
        )

        assert result.success is True
        assert len(result.added_node_ids) == 2
        assert len(result.added_edge_ids) == 1

    def test_create_failure_result(self):
        """Test creating a failed AddNodesResult"""
        result = AddNodesResult(
            added_node_ids=[],
            added_edge_ids=[],
            success=False,
            message="Duplicate node ID"
        )

        assert result.success is False
        assert "Duplicate" in result.message


class TestDeleteNodesResult:
    """Tests for DeleteNodesResult model"""

    def test_create_delete_result(self):
        """Test creating a DeleteNodesResult"""
        result = DeleteNodesResult(
            deleted_node_ids=["node-1"],
            affected_edge_ids=["edge-1", "edge-2"],
            success=True,
            message="Deleted 1 node and 2 edges"
        )

        assert result.success is True
        assert len(result.deleted_node_ids) == 1
        assert len(result.affected_edge_ids) == 2
