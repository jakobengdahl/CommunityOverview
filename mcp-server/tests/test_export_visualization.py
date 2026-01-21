"""
Tests for graph export and visualization view functionality
Ensures that export endpoint works correctly and visualization views display content properly
"""

import pytest
import json
from datetime import datetime
from httpx import AsyncClient, ASGITransport
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Node, Edge, NodeType, RelationshipType
from graph_storage import GraphStorage
import server


@pytest.fixture
def test_graph_with_views(tmp_path):
    """Create a temporary graph storage with test data including visualization views"""
    graph_file = tmp_path / "test_graph.json"
    storage = GraphStorage(str(graph_file))

    # Add test nodes
    actor1 = Node(
        id="actor-1",
        type=NodeType.ACTOR,
        name="Test Agency",
        description="A test government agency",
        communities=["eSam"]
    )

    actor2 = Node(
        id="actor-2",
        type=NodeType.ACTOR,
        name="Another Agency",
        description="Another test agency",
        communities=["eSam"]
    )

    initiative = Node(
        id="init-1",
        type=NodeType.INITIATIVE,
        name="Test Initiative",
        description="A test initiative",
        communities=["eSam"]
    )

    # Add visualization view
    view_node = Node(
        id="view-1",
        type=NodeType.VISUALIZATION_VIEW,
        name="Test View",
        description="A test visualization view",
        communities=["eSam"],
        metadata={
            "view_data": {
                "nodes": [
                    {"id": "actor-1", "position": {"x": 100, "y": 100}},
                    {"id": "init-1", "position": {"x": 300, "y": 100}}
                ],
                "hidden_nodes": []
            }
        }
    )

    # Add edges
    edge1 = Edge(
        id="edge-1",
        source="actor-1",
        target="init-1",
        type=RelationshipType.BELONGS_TO
    )

    edge2 = Edge(
        id="edge-2",
        source="init-1",
        target="actor-2",
        type=RelationshipType.RELATES_TO
    )

    storage.add_nodes([actor1, actor2, initiative, view_node], [edge1, edge2])

    return storage


@pytest.mark.asyncio
async def test_export_graph_endpoint(test_graph_with_views, monkeypatch):
    """Test that /export_graph endpoint returns properly serialized data"""
    # Mock the graph storage
    monkeypatch.setattr(server, "graph", test_graph_with_views)

    # Get the ASGI app
    app = server.mcp.streamable_http_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/export_graph")

        # Check response status
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        # Parse response
        data = response.json()

        # Verify structure
        assert "version" in data
        assert "exportDate" in data
        assert "nodes" in data
        assert "edges" in data
        assert "total_nodes" in data
        assert "total_edges" in data

        # Verify counts (4 nodes: 2 actors, 1 initiative, 1 view)
        assert data["total_nodes"] == 4
        assert data["total_edges"] == 2

        # Verify datetime serialization
        assert isinstance(data["exportDate"], str)
        datetime.fromisoformat(data["exportDate"])

        # Verify all nodes have properly serialized datetime fields
        for node in data["nodes"]:
            assert "created_at" in node
            assert "updated_at" in node
            assert isinstance(node["created_at"], str)
            assert isinstance(node["updated_at"], str)
            # Verify ISO format
            datetime.fromisoformat(node["created_at"])
            datetime.fromisoformat(node["updated_at"])

        # Verify all edges have properly serialized datetime fields
        for edge in data["edges"]:
            assert "created_at" in edge
            assert isinstance(edge["created_at"], str)
            datetime.fromisoformat(edge["created_at"])


def test_get_visualization_returns_content_not_view_node(test_graph_with_views):
    """Test that get_visualization returns actual content nodes, not the VisualizationView node itself"""
    from server import get_visualization

    result = get_visualization(name="Test View")

    # Verify success
    assert result["success"] is True
    assert "nodes" in result
    assert "edges" in result
    assert "positions" in result
    assert "action" in result
    assert result["action"] == "load_visualization"

    # Verify that we got the content nodes, not the view node
    node_ids = [node["id"] for node in result["nodes"]]
    assert "actor-1" in node_ids
    assert "init-1" in node_ids
    assert "view-1" not in node_ids  # The view node itself should NOT be included

    # Verify we got 2 nodes (actor-1 and init-1)
    assert len(result["nodes"]) == 2

    # Verify position data is included
    assert "actor-1" in result["positions"]
    assert "init-1" in result["positions"]
    assert result["positions"]["actor-1"] == {"x": 100, "y": 100}
    assert result["positions"]["init-1"] == {"x": 300, "y": 100}

    # Verify edges between visible nodes are included
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert edge["source"] == "actor-1"
    assert edge["target"] == "init-1"


def test_get_visualization_missing_view(test_graph_with_views):
    """Test that get_visualization handles missing views gracefully"""
    from server import get_visualization

    result = get_visualization(name="Nonexistent View")

    assert result["success"] is False
    assert "error" in result
    assert "not found" in result["error"].lower()


def test_get_visualization_with_deleted_nodes(test_graph_with_views):
    """Test that get_visualization handles deleted nodes gracefully"""
    from server import get_visualization

    # Create a view referencing a non-existent node
    view_with_missing = Node(
        id="view-2",
        type=NodeType.VISUALIZATION_VIEW,
        name="View with Missing Nodes",
        description="A view referencing deleted nodes",
        communities=["eSam"],
        metadata={
            "view_data": {
                "nodes": [
                    {"id": "deleted-node-1", "position": {"x": 100, "y": 100}},
                    {"id": "deleted-node-2", "position": {"x": 300, "y": 100}}
                ],
                "hidden_nodes": []
            }
        }
    )

    test_graph_with_views.add_nodes([view_with_missing], [])

    result = get_visualization(name="View with Missing Nodes")

    # Should fail because no nodes exist
    assert result["success"] is False
    assert "error" in result
    assert "deleted" in result["error"].lower() or "not" in result["error"].lower()


def test_get_visualization_empty_view(test_graph_with_views):
    """Test that get_visualization handles empty views"""
    from server import get_visualization

    # Create an empty view
    empty_view = Node(
        id="view-3",
        type=NodeType.VISUALIZATION_VIEW,
        name="Empty View",
        description="A view with no nodes",
        communities=["eSam"],
        metadata={
            "view_data": {
                "nodes": [],
                "hidden_nodes": []
            }
        }
    )

    test_graph_with_views.add_nodes([empty_view], [])

    result = get_visualization(name="Empty View")

    assert result["success"] is False
    assert "error" in result
    assert "no nodes" in result["error"].lower()


def test_get_visualization_datetime_serialization(test_graph_with_views):
    """Test that get_visualization returns properly serialized datetime objects"""
    from server import get_visualization

    result = get_visualization(name="Test View")

    # Test JSON serialization
    try:
        json_str = json.dumps(result)
        parsed = json.loads(json_str)

        # Verify that datetime fields in nodes are strings
        for node in parsed["nodes"]:
            assert "created_at" in node
            assert "updated_at" in node
            assert isinstance(node["created_at"], str)
            assert isinstance(node["updated_at"], str)
            datetime.fromisoformat(node["created_at"])
            datetime.fromisoformat(node["updated_at"])

        # Verify that datetime fields in edges are strings
        for edge in parsed["edges"]:
            assert "created_at" in edge
            assert isinstance(edge["created_at"], str)
            datetime.fromisoformat(edge["created_at"])

    except TypeError as e:
        pytest.fail(f"JSON serialization failed: {e}")


@pytest.mark.asyncio
async def test_export_empty_graph(monkeypatch, tmp_path):
    """Test that export works with an empty graph"""
    # Create empty graph
    graph_file = tmp_path / "empty_graph.json"
    empty_storage = GraphStorage(str(graph_file))

    monkeypatch.setattr(server, "graph", empty_storage)

    app = server.mcp.streamable_http_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/export_graph")

        assert response.status_code == 200
        data = response.json()

        assert data["total_nodes"] == 0
        assert data["total_edges"] == 0
        assert len(data["nodes"]) == 0
        assert len(data["edges"]) == 0


def test_get_visualization_with_hidden_nodes(test_graph_with_views):
    """Test that get_visualization properly includes hidden node IDs"""
    from server import get_visualization

    # Create a view with hidden nodes
    view_with_hidden = Node(
        id="view-4",
        type=NodeType.VISUALIZATION_VIEW,
        name="View with Hidden",
        description="A view with hidden nodes",
        communities=["eSam"],
        metadata={
            "view_data": {
                "nodes": [
                    {"id": "actor-1", "position": {"x": 100, "y": 100}},
                    {"id": "actor-2", "position": {"x": 300, "y": 100}},
                    {"id": "init-1", "position": {"x": 200, "y": 200}}
                ],
                "hidden_nodes": ["actor-2"]
            }
        }
    )

    test_graph_with_views.add_nodes([view_with_hidden], [])

    result = get_visualization(name="View with Hidden")

    assert result["success"] is True
    assert "hidden_node_ids" in result
    assert "actor-2" in result["hidden_node_ids"]
    assert len(result["hidden_node_ids"]) == 1

    # All nodes should still be loaded (hidden is just a display state)
    assert len(result["nodes"]) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
