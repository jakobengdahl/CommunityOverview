"""
Test that search_graph returns edges along with nodes
"""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Node, Edge, NodeType, RelationshipType
from graph_storage import GraphStorage
import server


@pytest.fixture
def test_graph_storage(tmp_path, monkeypatch):
    """Create a temporary graph storage for testing"""
    graph_file = tmp_path / "test_graph.json"
    storage = GraphStorage(str(graph_file))

    # Add test nodes
    nis2_node = Node(
        id="nis2-1",
        type=NodeType.LEGISLATION,
        name="NIS2 Directive",
        description="Network and Information Security Directive 2",
        communities=["eSam"]
    )

    initiative_node = Node(
        id="init-1",
        type=NodeType.INITIATIVE,
        name="NIS2 Implementation Project",
        description="Project to implement NIS2 requirements",
        communities=["eSam"]
    )

    actor_node = Node(
        id="actor-1",
        type=NodeType.ACTOR,
        name="MSB",
        description="Myndigheten för samhällsskydd och beredskap",
        communities=["eSam"]
    )

    # Add edges
    edge1 = Edge(
        id="edge-1",
        source="init-1",
        target="nis2-1",
        type=RelationshipType.IMPLEMENTS
    )

    edge2 = Edge(
        id="edge-2",
        source="init-1",
        target="actor-1",
        type=RelationshipType.BELONGS_TO
    )

    storage.add_nodes([nis2_node, initiative_node, actor_node], [edge1, edge2])

    # Mock the graph in server
    monkeypatch.setattr(server, "graph", storage)

    return storage


def test_search_graph_returns_edges(test_graph_storage):
    """Test that search_graph returns edges along with matching nodes"""
    result = server.search_graph(query="NIS2", node_types=["Initiative"], limit=10)

    # Verify result structure
    assert "nodes" in result
    assert "edges" in result
    assert "total" in result

    # Should find the initiative
    assert result["total"] == 1
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["name"] == "NIS2 Implementation Project"

    # Should return edges connected to the found node
    assert len(result["edges"]) == 2
    edge_ids = [edge["id"] for edge in result["edges"]]
    assert "edge-1" in edge_ids  # IMPLEMENTS edge
    assert "edge-2" in edge_ids  # BELONGS_TO edge


def test_search_graph_returns_edges_for_multiple_nodes(test_graph_storage):
    """Test that edges are returned for searches returning multiple nodes"""
    result = server.search_graph(query="NIS2", limit=10)

    # Should find both legislation and initiative
    assert result["total"] >= 2

    # Should return edges connecting these nodes
    assert len(result["edges"]) >= 1


def test_search_graph_with_no_edges(tmp_path, monkeypatch):
    """Test that search works even for isolated nodes (no edges)"""
    graph_file = tmp_path / "test_graph.json"
    storage = GraphStorage(str(graph_file))

    # Add an isolated node with no edges
    isolated_node = Node(
        id="iso-1",
        type=NodeType.THEME,
        name="Isolated Theme",
        description="A theme with no connections",
        communities=["eSam"]
    )

    storage.add_nodes([isolated_node], [])

    # Mock the graph in server
    monkeypatch.setattr(server, "graph", storage)

    result = server.search_graph(query="Isolated", limit=10)

    # Should find the node
    assert result["total"] == 1
    assert len(result["nodes"]) == 1

    # Should return empty edges list (not None or missing)
    assert "edges" in result
    assert result["edges"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
