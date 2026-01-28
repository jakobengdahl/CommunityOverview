"""
Integration tests for graph_services against graph_core.

These tests verify that GraphService correctly integrates with GraphStorage
and that data flows correctly through all layers.
"""

import pytest
import tempfile
import os

from graph_core import GraphStorage, Node, Edge, NodeType, RelationshipType
from graph_services import GraphService


class TestGraphServiceIntegration:
    """Integration tests verifying GraphService <-> GraphStorage interaction."""

    def test_service_uses_storage_correctly(self, temp_dir):
        """Test that GraphService correctly delegates to GraphStorage."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Add via service
        result = service.add_nodes(
            nodes=[{"type": "Actor", "name": "Test Actor"}],
            edges=[]
        )
        assert result["success"] is True

        # Verify in storage
        assert len(storage.nodes) == 1
        node = list(storage.nodes.values())[0]
        assert node.name == "Test Actor"

    def test_persistence_across_service_instances(self, temp_dir):
        """Test that data persists when creating new service instances."""
        json_path = os.path.join(temp_dir, "test.json")
        embeddings_path = os.path.join(temp_dir, "embeddings.pkl")

        # Create service 1 and add data
        storage1 = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)
        service1 = GraphService(storage1)
        service1.add_nodes(
            nodes=[{"id": "persist-1", "type": "Actor", "name": "Persistent Node"}],
            edges=[]
        )

        # Create service 2 with same storage path
        storage2 = GraphStorage(json_path=json_path, embeddings_path=embeddings_path)
        service2 = GraphService(storage2)

        # Verify data is loaded
        result = service2.get_node_details("persist-1")
        assert result["success"] is True
        assert result["node"]["name"] == "Persistent Node"

    def test_crud_operations_update_storage(self, temp_dir):
        """Test that all CRUD operations properly update storage."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Create
        add_result = service.add_nodes(
            nodes=[{"id": "crud-1", "type": "Actor", "name": "CRUD Test"}],
            edges=[]
        )
        assert add_result["success"] is True
        assert "crud-1" in storage.nodes

        # Update
        update_result = service.update_node("crud-1", {"description": "Updated"})
        assert update_result["success"] is True
        assert storage.nodes["crud-1"].description == "Updated"

        # Delete
        delete_result = service.delete_nodes(["crud-1"], confirmed=True)
        assert delete_result["success"] is True
        assert "crud-1" not in storage.nodes

    def test_search_indexes_maintained(self, temp_dir):
        """Test that search works correctly after modifications."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Add nodes
        service.add_nodes(
            nodes=[
                {"type": "Actor", "name": "Searchable Actor One"},
                {"type": "Actor", "name": "Searchable Actor Two"}
            ],
            edges=[]
        )

        # Search should find them
        result = service.search_graph("Searchable")
        assert result["total"] == 2

        # Update one
        node_id = result["nodes"][0]["id"]
        service.update_node(node_id, {"name": "Different Name"})

        # Search should now find only one "Searchable"
        result2 = service.search_graph("Searchable")
        assert result2["total"] == 1

    def test_edge_relationships_maintained(self, temp_dir):
        """Test that edge relationships are correctly maintained."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Add nodes with edges
        service.add_nodes(
            nodes=[
                {"id": "n1", "type": "Actor", "name": "Actor"},
                {"id": "n2", "type": "Initiative", "name": "Initiative"},
                {"id": "n3", "type": "Community", "name": "Community"}
            ],
            edges=[
                {"source": "n1", "target": "n2", "type": "BELONGS_TO"},
                {"source": "n2", "target": "n3", "type": "PART_OF"}
            ]
        )

        # Verify relationships via service
        related = service.get_related_nodes("n1", depth=2)
        node_ids = [n["id"] for n in related["nodes"]]
        assert "n2" in node_ids  # Direct connection
        assert "n3" in node_ids  # Transitive connection

        # Delete middle node
        service.delete_nodes(["n2"], confirmed=True)

        # Both edges should be gone
        assert len(storage.edges) == 0

    def test_statistics_reflect_actual_data(self, temp_dir):
        """Test that statistics accurately reflect the current state."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Initial stats
        stats = service.get_graph_stats()
        assert stats["total_nodes"] == 0
        assert stats["total_edges"] == 0

        # Add data
        service.add_nodes(
            nodes=[
                {"type": "Actor", "name": "Actor 1", "communities": ["C1"]},
                {"type": "Actor", "name": "Actor 2", "communities": ["C1"]},
                {"type": "Initiative", "name": "Initiative 1", "communities": ["C1"]}
            ],
            edges=[]
        )

        # Verify stats updated
        stats = service.get_graph_stats()
        assert stats["total_nodes"] == 3
        assert stats["nodes_by_type"]["Actor"] == 2
        assert stats["nodes_by_type"]["Initiative"] == 1
        assert stats["nodes_by_community"]["C1"] == 3


class TestGraphCoreCompatibility:
    """Tests to ensure graph_services works correctly with graph_core data types."""

    def test_node_type_enum_compatibility(self, empty_service):
        """Test that string node types are correctly converted to NodeType."""
        result = empty_service.add_nodes(
            nodes=[{"type": "Actor", "name": "Test"}],  # String type
            edges=[]
        )
        assert result["success"] is True

        # Verify internal representation uses enum
        node = list(empty_service.storage.nodes.values())[0]
        assert node.type == NodeType.ACTOR

    def test_relationship_type_enum_compatibility(self, empty_service):
        """Test that string relationship types are correctly converted."""
        empty_service.add_nodes(
            nodes=[
                {"id": "n1", "type": "Actor", "name": "A1"},
                {"id": "n2", "type": "Initiative", "name": "I1"}
            ],
            edges=[{"source": "n1", "target": "n2", "type": "BELONGS_TO"}]  # String type
        )

        # Verify internal representation uses enum
        edge = list(empty_service.storage.edges.values())[0]
        assert edge.type == RelationshipType.BELONGS_TO

    def test_pydantic_model_validation(self, empty_service):
        """Test that Pydantic validation is applied to input data."""
        # Invalid node type should fail
        result = empty_service.add_nodes(
            nodes=[{"type": "InvalidType", "name": "Test"}],
            edges=[]
        )
        assert result["success"] is False

        # Empty name should fail
        result = empty_service.add_nodes(
            nodes=[{"type": "Actor", "name": ""}],
            edges=[]
        )
        assert result["success"] is False

    def test_metadata_handling(self, empty_service):
        """Test that metadata is properly preserved through all operations."""
        metadata = {
            "custom_field": "custom_value",
            "nested": {"key": "value"},
            "list_field": [1, 2, 3]
        }

        empty_service.add_nodes(
            nodes=[{
                "type": "Actor",
                "name": "Node With Metadata",
                "metadata": metadata
            }],
            edges=[]
        )

        # Get via service
        node_id = list(empty_service.storage.nodes.keys())[0]
        result = empty_service.get_node_details(node_id)

        assert result["node"]["metadata"]["custom_field"] == "custom_value"
        assert result["node"]["metadata"]["nested"]["key"] == "value"
        assert result["node"]["metadata"]["list_field"] == [1, 2, 3]

    def test_tags_handling(self, empty_service):
        """Test that tags are properly handled."""
        empty_service.add_nodes(
            nodes=[{
                "type": "Actor",
                "name": "Tagged Node",
                "tags": ["tag1", "tag2", "tag3"]
            }],
            edges=[]
        )

        # Search by tag should work
        result = empty_service.search_graph("tag1")
        assert result["total"] == 1
        assert "tag1" in result["nodes"][0]["tags"]


class TestConcurrentModifications:
    """Tests for handling concurrent-like modifications."""

    def test_rapid_additions(self, temp_dir):
        """Test that rapid additions don't cause issues."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Add many nodes in quick succession
        for i in range(50):
            service.add_nodes(
                nodes=[{"type": "Actor", "name": f"Actor {i}"}],
                edges=[]
            )

        # Verify all were added
        stats = service.get_graph_stats()
        assert stats["total_nodes"] == 50

    def test_interleaved_read_write(self, temp_dir):
        """Test interleaving read and write operations."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Add initial data
        service.add_nodes(
            nodes=[{"id": "base", "type": "Actor", "name": "Base Node"}],
            edges=[]
        )

        # Interleave reads and writes
        for i in range(10):
            # Read
            service.search_graph("Base")
            service.get_node_details("base")

            # Write
            service.add_nodes(
                nodes=[{"type": "Actor", "name": f"Node {i}"}],
                edges=[]
            )

            # Read again
            service.get_graph_stats()

        # Final verification
        assert service.get_graph_stats()["total_nodes"] == 11
