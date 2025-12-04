import pytest
from models import Node, Edge, NodeType, RelationshipType, DeleteNodesResult, AddNodesResult

def test_add_node(graph_storage, sample_node):
    """Test adding a single node"""
    result = graph_storage.add_nodes([sample_node], [])

    assert result.success is True
    assert len(result.added_node_ids) == 1
    assert result.added_node_ids[0] == sample_node.id

    # Verify node exists
    stored_node = graph_storage.get_node(sample_node.id)
    assert stored_node is not None
    assert stored_node.name == "Test Initiative"

def test_add_duplicate_node(graph_storage, sample_node):
    """Test adding a duplicate node fails gracefully"""
    graph_storage.add_nodes([sample_node], [])
    result = graph_storage.add_nodes([sample_node], [])

    assert result.success is False
    assert "already exists" in result.message

def test_search_nodes(graph_storage, sample_node):
    """Test searching for nodes"""
    graph_storage.add_nodes([sample_node], [])

    # Search by name
    results = graph_storage.search_nodes("Test Initiative")
    assert len(results) == 1
    assert results[0].id == sample_node.id

    # Search by partial name
    results = graph_storage.search_nodes("Initiative")
    assert len(results) == 1

    # Search non-existent
    results = graph_storage.search_nodes("NonExistent")
    assert len(results) == 0

def test_search_with_filters(graph_storage, sample_node):
    """Test searching with type and community filters"""
    graph_storage.add_nodes([sample_node], [])

    # Correct filters
    results = graph_storage.search_nodes(
        "Test",
        node_types=[NodeType.INITIATIVE],
        communities=["eSam"]
    )
    assert len(results) == 1

    # Wrong type
    results = graph_storage.search_nodes(
        "Test",
        node_types=[NodeType.ACTOR],
        communities=["eSam"]
    )
    assert len(results) == 0

    # Wrong community
    results = graph_storage.search_nodes(
        "Test",
        node_types=[NodeType.INITIATIVE],
        communities=["OtherCommunity"]
    )
    assert len(results) == 0

def test_add_edge_and_get_related(graph_storage, sample_node):
    """Test adding edges and retrieving related nodes"""
    node2 = Node(
        type=NodeType.ACTOR,
        name="Test Actor",
        description="A test actor",
        summary="Actor",
        communities=["eSam"]
    )

    edge = Edge(
        source=sample_node.id,
        target=node2.id,
        type=RelationshipType.BELONGS_TO
    )

    # Must add nodes first
    graph_storage.add_nodes([sample_node, node2], [edge])

    related = graph_storage.get_related_nodes(sample_node.id)
    assert len(related['nodes']) == 2  # Includes self and target
    assert len(related['edges']) == 1

    # Verify the related node is node2
    related_ids = [n.id for n in related['nodes']]
    assert node2.id in related_ids

def test_find_similar_nodes(graph_storage, sample_node):
    """Test finding similar nodes by name"""
    graph_storage.add_nodes([sample_node], [])

    # Exact match
    similar = graph_storage.find_similar_nodes("Test Initiative")
    assert len(similar) > 0
    assert similar[0].node.id == sample_node.id
    assert similar[0].similarity_score == 1.0

    # Close match
    similar = graph_storage.find_similar_nodes("Test Initiativ")
    assert len(similar) > 0
    assert similar[0].node.id == sample_node.id
    assert similar[0].similarity_score > 0.7  # Assuming threshold

def test_update_node(graph_storage, sample_node):
    """Test updating a node"""
    graph_storage.add_nodes([sample_node], [])

    updates = {"description": "Updated description"}
    updated_node = graph_storage.update_node(sample_node.id, updates)

    assert updated_node.description == "Updated description"
    assert updated_node.name == sample_node.name  # Should be unchanged

    # Verify in storage
    stored = graph_storage.get_node(sample_node.id)
    assert stored.description == "Updated description"

def test_delete_nodes(graph_storage, sample_node):
    """Test deleting nodes"""
    graph_storage.add_nodes([sample_node], [])

    # Try deleting without confirmation
    result = graph_storage.delete_nodes([sample_node.id], confirmed=False)
    assert result.success is False

    # Delete with confirmation
    result = graph_storage.delete_nodes([sample_node.id], confirmed=True)
    assert result.success is True
    assert len(result.deleted_node_ids) == 1

    # Verify gone
    assert graph_storage.get_node(sample_node.id) is None

def test_get_stats(graph_storage, sample_node):
    """Test graph statistics"""
    graph_storage.add_nodes([sample_node], [])

    stats = graph_storage.get_stats()
    assert stats.total_nodes == 1
    assert stats.nodes_by_type[NodeType.INITIATIVE.value] == 1
    assert stats.nodes_by_community["eSam"] == 1
