"""
Unit tests for GraphService.

Tests the business logic layer in isolation.
"""

import pytest
from backend.service import GraphService
from backend.core import NodeType, RelationshipType


class TestGraphServiceSearch:
    """Tests for search operations."""

    def test_search_graph_by_query(self, populated_service: GraphService):
        """Test searching nodes by text query."""
        result = populated_service.search_graph(query="Skatteverket")

        assert "nodes" in result
        assert "edges" in result
        assert result["total"] >= 1
        assert any(n["name"] == "Skatteverket" for n in result["nodes"])

    def test_search_graph_with_type_filter(self, populated_service: GraphService):
        """Test filtering search by node type."""
        result = populated_service.search_graph(
            query="",
            node_types=["Actor"]
        )

        assert result["total"] >= 1
        assert all(n["type"] == "Actor" for n in result["nodes"])

    def test_search_graph_with_community_filter(self, populated_service: GraphService):
        """Test filtering search by community."""
        result = populated_service.search_graph(
            query="",
            communities=["eSam"]
        )

        assert result["total"] >= 1
        # All returned nodes should be in eSam
        for node in result["nodes"]:
            assert "eSam" in node.get("communities", [])

    def test_search_graph_returns_edges(self, populated_service: GraphService):
        """Test that search returns connecting edges."""
        result = populated_service.search_graph(query="Digital First")

        # Should find Digital First and have edges connecting it
        assert len(result["edges"]) > 0

    def test_search_graph_with_limit(self, populated_service: GraphService):
        """Test search result limit."""
        result = populated_service.search_graph(query="", limit=2)

        assert result["total"] <= 2

    def test_get_node_details_success(self, populated_service: GraphService):
        """Test getting node details for existing node."""
        result = populated_service.get_node_details("actor-1")

        assert result["success"] is True
        assert result["node"]["id"] == "actor-1"
        assert result["node"]["name"] == "Skatteverket"

    def test_get_node_details_not_found(self, populated_service: GraphService):
        """Test getting node details for non-existent node."""
        result = populated_service.get_node_details("nonexistent")

        assert result["success"] is False
        assert "error" in result

    def test_get_related_nodes(self, populated_service: GraphService):
        """Test getting related nodes."""
        result = populated_service.get_related_nodes("actor-1", depth=1)

        assert "nodes" in result
        assert "edges" in result
        # actor-1 is connected to init-1
        node_ids = [n["id"] for n in result["nodes"]]
        assert "init-1" in node_ids

    def test_get_related_nodes_with_depth(self, populated_service: GraphService):
        """Test getting related nodes with depth > 1."""
        result = populated_service.get_related_nodes("actor-1", depth=2)

        # With depth 2, should reach community-1 through init-1
        node_ids = [n["id"] for n in result["nodes"]]
        assert "community-1" in node_ids

    def test_get_related_nodes_with_relationship_filter(self, populated_service: GraphService):
        """Test filtering related nodes by relationship type."""
        result = populated_service.get_related_nodes(
            "init-1",
            relationship_types=["GOVERNED_BY"],
            depth=1
        )

        # Should only find legislation-1 via GOVERNED_BY
        node_ids = [n["id"] for n in result["nodes"]]
        assert "legislation-1" in node_ids
        # Should not find actors (BELONGS_TO)
        edge_types = [e["type"] for e in result["edges"]]
        assert all(t == "GOVERNED_BY" for t in edge_types)


class TestGraphServiceSimilarity:
    """Tests for similarity operations."""

    def test_find_similar_nodes(self, populated_service: GraphService):
        """Test finding similar nodes by name."""
        result = populated_service.find_similar_nodes(
            name="Skatteverk",  # Similar to "Skatteverket"
            threshold=0.5
        )

        assert "similar_nodes" in result
        assert result["total"] >= 1

    def test_find_similar_nodes_with_type_filter(self, populated_service: GraphService):
        """Test filtering similar nodes by type."""
        result = populated_service.find_similar_nodes(
            name="Digital",
            node_type="Initiative",
            threshold=0.3
        )

        # All results should be initiatives
        for item in result["similar_nodes"]:
            assert item["node"]["type"] == "Initiative"

    def test_find_similar_nodes_batch(self, populated_service: GraphService):
        """Test batch similarity search."""
        result = populated_service.find_similar_nodes_batch(
            names=["Skatteverk", "Bolagsverket", "Unknown"],
            threshold=0.5
        )

        assert "results" in result
        assert "Skatteverk" in result["results"]
        assert "Bolagsverket" in result["results"]
        assert "Unknown" in result["results"]
        assert result["total_searched"] == 3


class TestGraphServiceCRUD:
    """Tests for CRUD operations."""

    def test_add_nodes(self, empty_service: GraphService):
        """Test adding nodes."""
        nodes = [
            {
                "type": "Actor",
                "name": "New Actor",
                "description": "A new actor"
            }
        ]
        result = empty_service.add_nodes(nodes=nodes, edges=[])

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 1

    def test_add_nodes_with_edges(self, empty_service: GraphService):
        """Test adding nodes with edges."""
        nodes = [
            {"id": "n1", "type": "Actor", "name": "Actor 1"},
            {"id": "n2", "type": "Initiative", "name": "Init 1"}
        ]
        edges = [
            {"source": "n1", "target": "n2", "type": "BELONGS_TO"}
        ]
        result = empty_service.add_nodes(nodes=nodes, edges=edges)

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 2
        assert len(result["added_edge_ids"]) == 1

    def test_add_nodes_invalid_input(self, empty_service: GraphService):
        """Test adding nodes with invalid input."""
        nodes = [
            {"type": "InvalidType", "name": "Test"}  # Invalid type
        ]
        result = empty_service.add_nodes(nodes=nodes, edges=[])

        assert result["success"] is False
        assert "Error" in result["message"]

    def test_update_node(self, populated_service: GraphService):
        """Test updating a node."""
        result = populated_service.update_node(
            "actor-1",
            {"description": "Updated description", "tags": ["updated", "tax"]}
        )

        assert result["success"] is True
        assert result["node"]["description"] == "Updated description"
        assert "updated" in result["node"]["tags"]

    def test_update_node_not_found(self, populated_service: GraphService):
        """Test updating non-existent node."""
        result = populated_service.update_node("nonexistent", {"name": "New"})

        assert result["success"] is False
        assert "error" in result

    def test_delete_nodes(self, populated_service: GraphService):
        """Test deleting nodes."""
        result = populated_service.delete_nodes(["actor-1"], confirmed=True)

        assert result["success"] is True
        assert "actor-1" in result["deleted_node_ids"]
        # Related edges should be affected
        assert len(result["affected_edge_ids"]) > 0

    def test_delete_nodes_requires_confirmation(self, populated_service: GraphService):
        """Test that deletion requires confirmation."""
        result = populated_service.delete_nodes(["actor-1"], confirmed=False)

        assert result["success"] is False
        # Node should still exist
        node_result = populated_service.get_node_details("actor-1")
        assert node_result["success"] is True

    def test_delete_nodes_max_limit(self, populated_service: GraphService):
        """Test that deletion is limited to 10 nodes."""
        node_ids = [f"node-{i}" for i in range(15)]
        result = populated_service.delete_nodes(node_ids, confirmed=True)

        assert result["success"] is False
        assert "Max 10" in result["message"]


class TestGraphServiceStatistics:
    """Tests for statistics and metadata operations."""

    def test_get_graph_stats(self, populated_service: GraphService):
        """Test getting graph statistics."""
        result = populated_service.get_graph_stats()

        assert "total_nodes" in result
        assert "total_edges" in result
        assert "nodes_by_type" in result
        assert result["total_nodes"] == 5
        assert result["total_edges"] == 4

    def test_get_graph_stats_with_community_filter(self, populated_service: GraphService):
        """Test getting stats filtered by community."""
        result = populated_service.get_graph_stats(communities=["eSam"])

        # Should only count nodes in eSam
        assert result["total_nodes"] == 4  # All except community-1 which has no community

    def test_list_node_types(self, empty_service: GraphService):
        """Test listing node types."""
        result = empty_service.list_node_types()

        assert "node_types" in result
        types = [t["type"] for t in result["node_types"]]
        assert "Actor" in types
        assert "Initiative" in types
        assert "Community" in types
        # Each type should have color and description
        for nt in result["node_types"]:
            assert "color" in nt
            assert "description" in nt

    def test_list_relationship_types(self, empty_service: GraphService):
        """Test listing relationship types."""
        result = empty_service.list_relationship_types()

        assert "relationship_types" in result
        types = [t["type"] for t in result["relationship_types"]]
        assert "BELONGS_TO" in types
        assert "IMPLEMENTS" in types
        # Each type should have description
        for rt in result["relationship_types"]:
            assert "description" in rt


class TestGraphServiceSavedViews:
    """Tests for saved view operations."""

    def test_save_view_returns_signal(self, empty_service: GraphService):
        """Test that save_view returns a signal for the frontend."""
        result = empty_service.save_view("My View")

        assert result["action"] == "save_view"
        assert result["name"] == "My View"
        assert "message" in result

    def test_get_saved_view(self, service_with_view: GraphService):
        """Test loading a saved view."""
        result = service_with_view.get_saved_view("Test View")

        assert result["success"] is True
        assert "nodes" in result
        assert "edges" in result
        assert "positions" in result
        assert result["action"] == "load_visualization"

    def test_get_saved_view_not_found(self, populated_service: GraphService):
        """Test loading a non-existent view."""
        result = populated_service.get_saved_view("Nonexistent View")

        assert result["success"] is False
        assert "error" in result

    def test_list_saved_views(self, service_with_view: GraphService):
        """Test listing saved views."""
        result = service_with_view.list_saved_views()

        assert result["success"] is True
        assert "views" in result
        assert len(result["views"]) >= 1
        assert any(v["name"] == "Test View" for v in result["views"])


class TestGraphServiceExport:
    """Tests for export operations."""

    def test_export_graph(self, populated_service: GraphService):
        """Test exporting the entire graph."""
        result = populated_service.export_graph()

        assert "version" in result
        assert "exportDate" in result
        assert "nodes" in result
        assert "edges" in result
        assert result["total_nodes"] == 5
        assert result["total_edges"] == 4
        # Verify all nodes are included
        node_names = [n["name"] for n in result["nodes"]]
        assert "Skatteverket" in node_names
        assert "Digital First" in node_names


class TestGraphServiceSerialization:
    """Tests for response serialization."""

    def test_datetime_serialization(self, populated_service: GraphService):
        """Test that datetime fields are properly serialized."""
        result = populated_service.get_node_details("actor-1")

        # created_at and updated_at should be ISO format strings
        assert isinstance(result["node"]["created_at"], str)
        assert isinstance(result["node"]["updated_at"], str)

    def test_export_datetime_serialization(self, populated_service: GraphService):
        """Test datetime serialization in export."""
        result = populated_service.export_graph()

        assert isinstance(result["exportDate"], str)
        # All node timestamps should be strings
        for node in result["nodes"]:
            assert isinstance(node["created_at"], str)
