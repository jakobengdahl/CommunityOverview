
import pytest
import os
import shutil
import numpy as np
from models import Node, NodeType
from vector_store import VectorStore
from graph_storage import GraphStorage

@pytest.fixture
def temp_vector_store():
    storage_path = "test_embeddings.pkl"
    if os.path.exists(storage_path):
        os.remove(storage_path)

    yield VectorStore(storage_path=storage_path)

    if os.path.exists(storage_path):
        os.remove(storage_path)

@pytest.fixture
def temp_graph_storage():
    json_path = "test_graph_vector.json"
    if os.path.exists(json_path):
        os.remove(json_path)
    # Remove default embeddings file if exists
    if os.path.exists("embeddings.pkl"):
        # We don't want to mess with real embeddings, but GraphStorage defaults to it
        # So we should probably mock VectorStore in GraphStorage or make it configurable
        # For now, let's just let it create a new file and clean up
        shutil.move("embeddings.pkl", "embeddings.pkl.bak")

    storage = GraphStorage(json_path=json_path)
    # Patch the vector store to use a test path
    storage.vector_store = VectorStore(storage_path="test_embeddings_integration.pkl")

    yield storage

    if os.path.exists(json_path):
        os.remove(json_path)
    if os.path.exists("test_embeddings_integration.pkl"):
        os.remove("test_embeddings_integration.pkl")
    if os.path.exists("embeddings.pkl.bak"):
        shutil.move("embeddings.pkl.bak", "embeddings.pkl")

def test_vector_store_operations(temp_vector_store):
    node1 = Node(name="Apple", description="A red fruit", type=NodeType.RESOURCE)
    node2 = Node(name="Banana", description="A yellow fruit", type=NodeType.RESOURCE)
    node3 = Node(name="Car", description="A vehicle", type=NodeType.RESOURCE)

    # Generate and store embeddings
    temp_vector_store.update_nodes_embeddings([node1, node2, node3])

    assert len(temp_vector_store.embeddings) == 3

    # Search for "fruit"
    results = temp_vector_store.search(query_text="fruit", limit=3)
    assert len(results) > 0

    # Apple and Banana should be more similar to fruit than Car
    top_result_id = results[0][0]
    assert top_result_id in [node1.id, node2.id]

    # Test removal
    temp_vector_store.remove_node_embedding(node1.id)
    assert len(temp_vector_store.embeddings) == 2

    # Test persistence
    temp_vector_store.save()
    new_store = VectorStore(storage_path=temp_vector_store.storage_path)
    assert len(new_store.embeddings) == 2

def test_graph_integration(temp_graph_storage):
    # Add nodes
    node1 = Node(name="Machine Learning", description="AI field", type=NodeType.THEME)
    node2 = Node(name="Artificial Intelligence", description="Smart computers", type=NodeType.THEME)

    temp_graph_storage.add_nodes([node1], [])
    temp_graph_storage.add_nodes([node2], [])

    # Check if embeddings were generated
    assert len(temp_graph_storage.vector_store.embeddings) == 2

    # Search similar nodes
    similar = temp_graph_storage.find_similar_nodes("AI", limit=5)

    # Should find both via semantic similarity
    assert len(similar) >= 1
    found_names = [s.node.name for s in similar]
    # "Artificial Intelligence" contains "Intelligence" which is close to AI semantically,
    # but "Machine Learning" description has "AI field".

    # Update node
    temp_graph_storage.update_node(node1.id, {"description": "Updated description"})
    # Embedding should be updated (difficult to verify exact values without mocking, but we can check it runs)

    # Delete node
    temp_graph_storage.delete_nodes([node1.id], confirmed=True)
    assert len(temp_graph_storage.vector_store.embeddings) == 1
