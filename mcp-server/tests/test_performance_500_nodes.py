"""
Performance tests with 500-node graph
Tests loading, layout, and rendering performance
"""

import pytest
import time
import json
from graph_storage import GraphStorage
from models import Node, Edge

@pytest.fixture
def large_graph_storage():
    """Load the 500-node test graph"""
    return GraphStorage("graph_test_500.json")

def test_load_500_node_graph(large_graph_storage):
    """Test loading performance with 500 nodes"""
    start_time = time.time()

    nodes = large_graph_storage.nodes
    edges = large_graph_storage.edges

    load_time = time.time() - start_time

    print(f"\n✅ Loaded {len(nodes)} nodes and {len(edges)} edges in {load_time:.3f}s")

    # Performance assertion: should load in less than 2 seconds
    assert load_time < 2.0, f"Loading {len(nodes)} nodes took {load_time:.3f}s, should be < 2s"

    assert len(nodes) >= 400, f"Expected at least 400 nodes, got {len(nodes)}"
    assert len(edges) >= 500, f"Expected at least 500 edges, got {len(edges)}"

def test_search_performance_on_large_graph(large_graph_storage):
    """Test search performance on 500-node graph"""
    queries = [
        ("NIS2", None),
        ("AI", ["Initiative"]),
        ("digitalisering", ["Initiative", "Theme"]),
        ("Digg", ["Actor"])
    ]

    for query, node_types in queries:
        start_time = time.time()

        results = large_graph_storage.search_nodes(
            query=query,
            node_types=[nt for nt in (node_types or [])],
            limit=50
        )

        search_time = time.time() - start_time

        print(f"✅ Search for '{query}' returned {len(results)} results in {search_time:.4f}s")

        # Performance assertion: searches should complete in < 100ms
        assert search_time < 0.1, f"Search took {search_time:.4f}s, should be < 0.1s"

def test_get_related_nodes_performance(large_graph_storage):
    """Test get_related_nodes performance"""
    # Get a few sample nodes
    sample_nodes = large_graph_storage.nodes[:10]

    total_time = 0
    for node in sample_nodes:
        start_time = time.time()

        result = large_graph_storage.get_related_nodes(
            node_id=node.id,
            depth=1
        )

        elapsed = time.time() - start_time
        total_time += elapsed

        print(f"✅ get_related_nodes for {node.id} returned {len(result['nodes'])} nodes in {elapsed:.4f}s")

    avg_time = total_time / len(sample_nodes)
    print(f"✅ Average get_related_nodes time: {avg_time:.4f}s")

    # Should average < 50ms
    assert avg_time < 0.05, f"Average get_related took {avg_time:.4f}s, should be < 0.05s"

def test_graph_stats_performance(large_graph_storage):
    """Test get_stats performance"""
    start_time = time.time()

    stats = large_graph_storage.get_stats()

    stats_time = time.time() - start_time

    print(f"\n📊 Graph Stats:")
    print(f"  Total nodes: {stats.total_nodes}")
    print(f"  Total edges: {stats.total_edges}")
    print(f"  Nodes by type: {stats.nodes_by_type}")
    print(f"  Computed in: {stats_time:.4f}s")

    # Stats should compute quickly
    assert stats_time < 0.1, f"Stats computation took {stats_time:.4f}s, should be < 0.1s"

def test_add_node_performance(large_graph_storage, tmp_path):
    """Test performance of adding nodes to large graph"""
    # Create a temporary copy for this test
    test_graph_file = tmp_path / "test_add.json"

    # Copy existing graph
    import shutil
    shutil.copy("graph_test_500.json", test_graph_file)

    test_storage = GraphStorage(str(test_graph_file))

    initial_count = len(test_storage.nodes)

    # Add 10 new nodes
    new_nodes = []
    for i in range(10):
        new_nodes.append(Node(
            id=f"test_node_{i}",
            name=f"Test Node {i}",
            type="Actor",
            description="Test node for performance testing",
            summary="Performance test",
            communities=["eSam"],
            metadata={}
        ))

    start_time = time.time()

    result = test_storage.add_nodes(new_nodes, [])

    add_time = time.time() - start_time

    print(f"✅ Added {len(new_nodes)} nodes to graph of {initial_count} nodes in {add_time:.4f}s")

    # Adding should be fast
    assert add_time < 0.5, f"Adding nodes took {add_time:.4f}s, should be < 0.5s"

    assert result.success
    assert len(result.added_node_ids) == len(new_nodes)

def test_find_similar_nodes_performance(large_graph_storage):
    """Test similarity search performance on large graph"""
    test_names = [
        "Digital Transformation",
        "Cybersecurity Initiative",
        "Data Strategy",
        "Innovation Project"
    ]

    for name in test_names:
        start_time = time.time()

        similar = large_graph_storage.find_similar_nodes(
            name=name,
            threshold=0.6,
            limit=5
        )

        search_time = time.time() - start_time

        print(f"✅ Similarity search for '{name}' found {len(similar)} matches in {search_time:.4f}s")

        # Similarity search should be reasonably fast
        assert search_time < 0.5, f"Similarity search took {search_time:.4f}s, should be < 0.5s"

def test_update_node_performance(large_graph_storage, tmp_path):
    """Test update performance on large graph"""
    # Create temporary copy
    test_graph_file = tmp_path / "test_update.json"
    import shutil
    shutil.copy("graph_test_500.json", test_graph_file)

    test_storage = GraphStorage(str(test_graph_file))

    # Update first 10 nodes
    nodes_to_update = test_storage.nodes[:10]

    start_time = time.time()

    for node in nodes_to_update:
        test_storage.update_node(node.id, {
            "description": "Updated description for performance test",
            "summary": "Updated summary"
        })

    update_time = time.time() - start_time

    print(f"✅ Updated {len(nodes_to_update)} nodes in {update_time:.4f}s")

    # Updates should be fast
    assert update_time < 1.0, f"Updating nodes took {update_time:.4f}s, should be < 1s"

def test_layout_algorithm_performance_simulation():
    """
    Simulate layout calculation performance for 500 nodes
    This would normally be done in frontend, but we can simulate the complexity
    """
    import random

    # Simulate 500 nodes and ~800 edges (similar to our test graph)
    num_nodes = 500
    num_edges = 800

    # Measure time to process graph structure
    start_time = time.time()

    # Simulate creating node and edge data structures
    nodes = [{"id": f"node_{i}", "label": f"Node {i}"} for i in range(num_nodes)]

    edges = []
    for _ in range(num_edges):
        source = random.randint(0, num_nodes - 1)
        target = random.randint(0, num_nodes - 1)
        if source != target:
            edges.append({"source": f"node_{source}", "target": f"node_{target}"})

    processing_time = time.time() - start_time

    print(f"✅ Processed {num_nodes} nodes and {len(edges)} edges in {processing_time:.4f}s")

    # Should process quickly
    assert processing_time < 0.5, f"Processing took {processing_time:.4f}s, should be < 0.5s"

def test_memory_usage_with_large_graph(large_graph_storage):
    """Test memory efficiency with large graph"""
    import sys

    # Get size of nodes and edges in memory
    nodes_size = sys.getsizeof(large_graph_storage.nodes)
    edges_size = sys.getsizeof(large_graph_storage.edges)

    print(f"\n💾 Memory Usage:")
    print(f"  Nodes list: {nodes_size / 1024:.2f} KB")
    print(f"  Edges list: {edges_size / 1024:.2f} KB")
    print(f"  Total: {(nodes_size + edges_size) / 1024:.2f} KB")

    # With 500 nodes, should stay under 10MB
    total_size = nodes_size + edges_size
    assert total_size < 10 * 1024 * 1024, f"Memory usage {total_size / (1024*1024):.2f}MB too high"

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
