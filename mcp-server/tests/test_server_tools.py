import pytest
from unittest.mock import patch, MagicMock
from models import Node, NodeType, RelationshipType

# Import the server module functions we want to test
# Note: we need to handle the fact that importing server might trigger GraphStorage init
# We can't easily avoid that unless we mock GraphStorage before import, but sys.modules patching is tricky.
# However, since we patched graph in server.py, the initial load is ignored or we replace it.

from server import (
    search_graph,
    get_node_details,
    get_related_nodes,
    find_similar_nodes,
    add_nodes,
    update_node,
    delete_nodes,
    get_graph_stats,
    list_node_types,
    list_relationship_types
)

@pytest.fixture
def mock_graph(graph_storage):
    """Patch the graph object in server.py"""
    with patch('server.graph', graph_storage) as mock:
        yield mock

def test_tool_search_graph(mock_graph, sample_node):
    """Test search_graph tool wrapper"""
    mock_graph.add_nodes([sample_node], [])

    result = search_graph(query="Test", limit=10)

    assert "nodes" in result
    assert result["total"] == 1
    assert result["nodes"][0]["id"] == sample_node.id
    assert result["query"] == "Test"

def test_tool_get_node_details(mock_graph, sample_node):
    """Test get_node_details tool wrapper"""
    mock_graph.add_nodes([sample_node], [])

    result = get_node_details(node_id=sample_node.id)

    assert result["success"] is True
    assert result["node"]["id"] == sample_node.id

    # Test not found
    result = get_node_details(node_id="nonexistent")
    assert result["success"] is False
    assert "error" in result

def test_tool_list_node_types():
    """Test list_node_types tool wrapper"""
    result = list_node_types()
    assert "node_types" in result
    assert len(result["node_types"]) > 0
    assert result["node_types"][0]["type"] == "Actor"  # Assuming Actor is first or at least present

def test_tool_list_relationship_types():
    """Test list_relationship_types tool wrapper"""
    result = list_relationship_types()
    assert "relationship_types" in result
    assert len(result["relationship_types"]) > 0

def test_tool_add_nodes(mock_graph):
    """Test add_nodes tool wrapper"""
    nodes = [{
        "type": "Initiative",
        "name": "New Initiative",
        "description": "Desc",
        "summary": "Sum",
        "communities": ["eSam"]
    }]
    edges = []

    result = add_nodes(nodes=nodes, edges=edges)

    assert result["success"] is True
    assert len(result["added_node_ids"]) == 1

def test_tool_update_node(mock_graph, sample_node):
    """Test update_node tool wrapper"""
    mock_graph.add_nodes([sample_node], [])

    result = update_node(node_id=sample_node.id, updates={"name": "New Name"})

    assert result["success"] is True
    assert result["node"]["name"] == "New Name"

def test_tool_delete_nodes(mock_graph, sample_node):
    """Test delete_nodes tool wrapper"""
    mock_graph.add_nodes([sample_node], [])

    result = delete_nodes(node_ids=[sample_node.id], confirmed=True)

    assert result["success"] is True
    assert len(result["deleted_node_ids"]) == 1

def test_tool_get_graph_stats(mock_graph, sample_node):
    """Test get_graph_stats tool wrapper"""
    mock_graph.add_nodes([sample_node], [])

    result = get_graph_stats()

    assert result["total_nodes"] == 1
    assert "nodes_by_type" in result
