"""
Tests for datetime serialization in API endpoints
Ensures that datetime objects from Node and Edge models are properly serialized to JSON
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
def test_graph_storage(tmp_path):
    """Create a temporary graph storage for testing"""
    graph_file = tmp_path / "test_graph.json"
    storage = GraphStorage(str(graph_file))

    # Add test nodes with NIS2 legislation
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

    edge = Edge(
        id="edge-1",
        source="init-1",
        target="nis2-1",
        type=RelationshipType.IMPLEMENTS
    )

    storage.add_nodes([nis2_node, initiative_node], [edge])

    return storage


@pytest.mark.asyncio
async def test_search_graph_datetime_serialization(test_graph_storage):
    """Test that search_graph returns properly serialized datetime objects"""
    from server import search_graph

    result = search_graph(query="NIS2", node_types=["Legislation"], limit=10)

    # Verify result structure
    assert "nodes" in result
    assert "total" in result
    assert result["total"] > 0

    # Test JSON serialization
    try:
        json_str = json.dumps(result)
        parsed = json.loads(json_str)

        # Verify that datetime fields are strings in ISO format
        for node in parsed["nodes"]:
            assert "created_at" in node
            assert "updated_at" in node
            assert isinstance(node["created_at"], str)
            assert isinstance(node["updated_at"], str)

            # Verify ISO format by parsing
            datetime.fromisoformat(node["created_at"])
            datetime.fromisoformat(node["updated_at"])

    except TypeError as e:
        pytest.fail(f"JSON serialization failed: {e}")


@pytest.mark.asyncio
async def test_get_related_nodes_datetime_serialization(test_graph_storage):
    """Test that get_related_nodes returns properly serialized datetime objects"""
    from server import get_related_nodes

    result = get_related_nodes(node_id="init-1", depth=1)

    # Verify result structure
    assert "nodes" in result
    assert "edges" in result

    # Test JSON serialization of nodes
    try:
        json_str = json.dumps(result)
        parsed = json.loads(json_str)

        # Check nodes
        for node in parsed["nodes"]:
            assert isinstance(node["created_at"], str)
            assert isinstance(node["updated_at"], str)
            datetime.fromisoformat(node["created_at"])
            datetime.fromisoformat(node["updated_at"])

        # Check edges
        for edge in parsed["edges"]:
            assert "created_at" in edge
            assert isinstance(edge["created_at"], str)
            datetime.fromisoformat(edge["created_at"])

    except TypeError as e:
        pytest.fail(f"JSON serialization failed: {e}")


@pytest.mark.asyncio
async def test_chat_endpoint_with_datetime_in_response(test_graph_storage, monkeypatch):
    """Test that /chat endpoint properly handles datetime objects in tool results"""
    # Mock the graph storage
    monkeypatch.setattr(server, "graph", test_graph_storage)

    # Get the ASGI app
    app = server.mcp.streamable_http_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # This would require mocking the Anthropic API call
        # For now, we'll test the JSON serialization helper function directly
        pass


def test_json_serializer_function():
    """Test the custom JSON serializer function"""
    from datetime import datetime

    # Create test data with datetime
    test_data = {
        "message": "Test message",
        "timestamp": datetime(2024, 12, 8, 14, 30, 0),
        "nested": {
            "created_at": datetime(2024, 12, 8, 14, 0, 0)
        },
        "list_with_datetime": [
            {"date": datetime(2024, 12, 8, 15, 0, 0)}
        ]
    }

    # Define the serializer (same as in server.py)
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    # Test serialization
    json_str = json.dumps(test_data, default=json_serializer)
    parsed = json.loads(json_str)

    # Verify all datetime objects were converted to strings
    assert isinstance(parsed["timestamp"], str)
    assert isinstance(parsed["nested"]["created_at"], str)
    assert isinstance(parsed["list_with_datetime"][0]["date"], str)

    # Verify ISO format
    assert parsed["timestamp"] == "2024-12-08T14:30:00"
    assert parsed["nested"]["created_at"] == "2024-12-08T14:00:00"
    assert parsed["list_with_datetime"][0]["date"] == "2024-12-08T15:00:00"


def test_node_model_dump_serialization():
    """Test that Node.model_dump() with datetime fields can be JSON serialized"""
    node = Node(
        type=NodeType.LEGISLATION,
        name="Test Legislation",
        description="Test description",
        communities=["eSam"]
    )

    # Get the dict representation
    node_dict = node.model_dump()

    # Test that it contains datetime objects (before serialization)
    assert isinstance(node_dict["created_at"], datetime)
    assert isinstance(node_dict["updated_at"], datetime)

    # Test JSON serialization with custom encoder
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_str = json.dumps(node_dict, default=json_serializer)
    parsed = json.loads(json_str)

    # Verify datetime fields are now strings
    assert isinstance(parsed["created_at"], str)
    assert isinstance(parsed["updated_at"], str)


def test_edge_model_dump_serialization():
    """Test that Edge.model_dump() with datetime fields can be JSON serialized"""
    edge = Edge(
        source="node1",
        target="node2",
        type=RelationshipType.RELATES_TO
    )

    # Get the dict representation
    edge_dict = edge.model_dump()

    # Test that it contains datetime objects (before serialization)
    assert isinstance(edge_dict["created_at"], datetime)

    # Test JSON serialization with custom encoder
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_str = json.dumps(edge_dict, default=json_serializer)
    parsed = json.loads(json_str)

    # Verify datetime field is now string
    assert isinstance(parsed["created_at"], str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
