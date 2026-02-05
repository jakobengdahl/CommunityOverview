"""
Unit tests for graph_core vector_store

Note: Some tests require the sentence-transformers model to be loaded,
which may take time on first run. Tests are designed to be skippable
if the model is not available.
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np

from backend.core.vector_store import VectorStore
from backend.core.models import Node, NodeType


@pytest.fixture
def temp_vector_store():
    """Create a temporary VectorStore instance"""
    # VectorStore no longer uses storage_path - embeddings are stored in graph.json
    store = VectorStore()
    yield store


@pytest.fixture
def sample_nodes():
    """Create sample nodes for testing"""
    return [
        Node(id="node-1", type=NodeType.ACTOR, name="Swedish Government",
             description="The government of Sweden", tags=["government", "sweden"]),
        Node(id="node-2", type=NodeType.ACTOR, name="Norwegian Government",
             description="The government of Norway", tags=["government", "norway"]),
        Node(id="node-3", type=NodeType.INITIATIVE, name="Digital Transformation",
             description="A digital transformation initiative", tags=["digital", "technology"]),
    ]


class TestVectorStoreInit:
    """Tests for VectorStore initialization"""

    def test_creates_empty_store(self, temp_vector_store):
        """Test that a new store starts empty"""
        assert temp_vector_store.get_embedding_count() == 0

    def test_lazy_model_loading(self, temp_vector_store):
        """Test that model is not loaded until needed"""
        assert temp_vector_store.model is None

    def test_default_model_name(self, temp_vector_store):
        """Test that default model name is set correctly"""
        assert temp_vector_store.model_name == "all-MiniLM-L6-v2"


class TestVectorStoreTextRepresentation:
    """Tests for text representation generation"""

    def test_get_text_representation(self, temp_vector_store):
        """Test text representation includes all fields"""
        node = Node(
            type=NodeType.ACTOR,
            name="Test Actor",
            description="A test description",
            summary="Test summary",
            tags=["tag1", "tag2"]
        )

        text = temp_vector_store._get_text_representation(node)

        assert "Test Actor" in text
        assert "test description" in text
        assert "Test summary" in text
        assert "tag1" in text
        assert "tag2" in text

    def test_get_text_representation_minimal(self, temp_vector_store):
        """Test text representation with minimal fields"""
        node = Node(type=NodeType.ACTOR, name="Minimal Node")

        text = temp_vector_store._get_text_representation(node)

        assert "Minimal Node" in text


class TestVectorStoreEmbeddings:
    """Tests for embedding generation and storage"""

    @pytest.mark.slow
    def test_generate_embedding(self, temp_vector_store, sample_nodes):
        """Test generating embedding for a single node"""
        node = sample_nodes[0]
        embedding = temp_vector_store.generate_embedding(node)

        assert embedding is not None
        assert isinstance(embedding, np.ndarray)
        assert len(embedding) == 384  # all-MiniLM-L6-v2 produces 384-dim embeddings

    @pytest.mark.slow
    def test_update_node_embedding(self, temp_vector_store, sample_nodes):
        """Test updating embedding for a node"""
        node = sample_nodes[0]
        temp_vector_store.update_node_embedding(node)

        assert temp_vector_store.has_embedding(node.id)
        assert temp_vector_store.get_embedding_count() == 1

    @pytest.mark.slow
    def test_update_nodes_embeddings_batch(self, temp_vector_store, sample_nodes):
        """Test batch updating embeddings"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        assert temp_vector_store.get_embedding_count() == 3
        for node in sample_nodes:
            assert temp_vector_store.has_embedding(node.id)

    @pytest.mark.slow
    def test_remove_node_embedding(self, temp_vector_store, sample_nodes):
        """Test removing a single embedding"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_node_embedding("node-1")

        assert not temp_vector_store.has_embedding("node-1")
        assert temp_vector_store.get_embedding_count() == 2

    @pytest.mark.slow
    def test_remove_nodes_embeddings(self, temp_vector_store, sample_nodes):
        """Test removing multiple embeddings"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_nodes_embeddings(["node-1", "node-2"])

        assert not temp_vector_store.has_embedding("node-1")
        assert not temp_vector_store.has_embedding("node-2")
        assert temp_vector_store.has_embedding("node-3")

    def test_remove_nonexistent_embedding(self, temp_vector_store):
        """Test removing a non-existent embedding is safe"""
        # Should not raise an error
        temp_vector_store.remove_node_embedding("nonexistent")
        temp_vector_store.remove_nodes_embeddings(["nonexistent"])


class TestVectorStoreSearch:
    """Tests for semantic search"""

    @pytest.mark.slow
    def test_search_by_text(self, temp_vector_store, sample_nodes):
        """Test searching by text query"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(query_text="government", limit=5)

        assert len(results) > 0
        # Results should be sorted by score descending
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.slow
    def test_search_by_node(self, temp_vector_store, sample_nodes):
        """Test searching by existing node"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        # Search for nodes similar to Swedish Government
        results = temp_vector_store.search(query_node=sample_nodes[0], limit=5)

        assert len(results) > 0
        # The query node itself should not be in results
        result_ids = [node_id for node_id, _ in results]
        assert "node-1" not in result_ids

    @pytest.mark.slow
    def test_search_with_threshold(self, temp_vector_store, sample_nodes):
        """Test search with similarity threshold"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(
            query_text="government",
            threshold=0.5,
            limit=5
        )

        # All results should be above threshold
        for _, score in results:
            assert score >= 0.5

    @pytest.mark.slow
    def test_search_limit(self, temp_vector_store, sample_nodes):
        """Test search result limit"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        results = temp_vector_store.search(query_text="government", limit=1)

        assert len(results) <= 1

    def test_search_empty_store(self, temp_vector_store):
        """Test searching an empty store returns empty list"""
        results = temp_vector_store.search(query_text="government")
        assert results == []

    def test_search_no_query(self, temp_vector_store):
        """Test search without query returns empty list"""
        results = temp_vector_store.search()
        assert results == []


class TestVectorStoreRebuild:
    """Tests for rebuilding index from nodes"""

    @pytest.mark.slow
    def test_rebuild_index(self, sample_nodes):
        """Test that index can be rebuilt from nodes with embeddings"""
        # First create a store and generate embeddings
        store1 = VectorStore()
        store1.update_nodes_embeddings(sample_nodes)

        # Get the embeddings stored on nodes
        for node in sample_nodes:
            assert node.embedding is not None

        # Create a new store and rebuild from nodes
        store2 = VectorStore()
        store2.rebuild_index(sample_nodes)

        # Verify embeddings were loaded
        assert store2.get_embedding_count() == 3
        for node in sample_nodes:
            assert store2.has_embedding(node.id)

    @pytest.mark.slow
    def test_rebuild_empty_nodes(self):
        """Test rebuilding with nodes that have no embeddings"""
        nodes = [
            Node(id="no-embed-1", type=NodeType.ACTOR, name="No Embedding 1"),
            Node(id="no-embed-2", type=NodeType.ACTOR, name="No Embedding 2"),
        ]

        store = VectorStore()
        store.rebuild_index(nodes)

        # Should have no embeddings since nodes had none
        assert store.get_embedding_count() == 0


class TestVectorStoreMatrix:
    """Tests for embedding matrix operations"""

    @pytest.mark.slow
    def test_matrix_updated_on_add(self, temp_vector_store, sample_nodes):
        """Test that embedding matrix is updated when adding nodes"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)

        assert temp_vector_store.embedding_matrix is not None
        assert temp_vector_store.embedding_matrix.shape[0] == 3

    @pytest.mark.slow
    def test_matrix_updated_on_remove(self, temp_vector_store, sample_nodes):
        """Test that embedding matrix is updated when removing nodes"""
        temp_vector_store.update_nodes_embeddings(sample_nodes)
        temp_vector_store.remove_node_embedding("node-1")

        assert temp_vector_store.embedding_matrix.shape[0] == 2

    def test_empty_matrix(self, temp_vector_store):
        """Test that empty store has no matrix"""
        assert temp_vector_store.embedding_matrix is None
        assert temp_vector_store.node_ids == []


# Skip slow tests by default, run with: pytest -m slow
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (require model loading)")
