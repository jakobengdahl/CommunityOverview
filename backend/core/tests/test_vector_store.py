"""
Unit tests for graph_core vector_store

Note: Some tests require the sentence-transformers model to be loaded,
which may take time on first run. Tests are designed to be skippable
if the model is not available.
"""

import pytest
from unittest.mock import patch
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
        Node(
            id="node-1",
            type=NodeType.ACTOR,
            name="Swedish Government",
            description="The government of Sweden",
            tags=["government", "sweden"],
        ),
        Node(
            id="node-2",
            type=NodeType.ACTOR,
            name="Norwegian Government",
            description="The government of Norway",
            tags=["government", "norway"],
        ),
        Node(
            id="node-3",
            type=NodeType.INITIATIVE,
            name="Digital Transformation",
            description="A digital transformation initiative",
            tags=["digital", "technology"],
        ),
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
            tags=["tag1", "tag2"],
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
            query_text="government", threshold=0.5, limit=5
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

    def test_rebuild_index_reads_vectors_carried_on_the_nodes(self, sample_nodes):
        """rebuild_index is the pre-sidecar path: it ingests vectors that a
        graph written before the split still carries on its node objects."""
        for i, node in enumerate(sample_nodes):
            node.embedding = [float(i), 1.0 - i, 0.5]

        store = VectorStore()
        store.rebuild_index(sample_nodes)

        assert store.get_embedding_count() == 3
        for i, node in enumerate(sample_nodes):
            assert store.has_embedding(node.id)
            np.testing.assert_allclose(
                store.embeddings[node.id], np.float32([float(i), 1.0 - i, 0.5])
            )

    @pytest.mark.slow
    def test_generated_vectors_stay_off_the_node_object(self, sample_nodes):
        """The vector store owns the vectors; leaving a copy on the node is
        what made a node cost ~51 kB resident and bloated every snapshot."""
        store = VectorStore()
        store.update_nodes_embeddings(sample_nodes)

        assert store.get_embedding_count() == 3
        for node in sample_nodes:
            assert node.embedding is None

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


class TestVectorStorePersistenceSeam:
    """The store is the owner of the vectors; GraphStorage persists them via
    these methods and uses `revision` to skip writes that change nothing."""

    def _store(self):
        store = VectorStore()
        store.load_vectors({"n1": [1.0, 0.0], "n2": [0.0, 1.0]})
        return store

    def test_load_vectors_replaces_the_index(self):
        store = self._store()
        store.load_vectors({"n3": [1.0, 1.0]})

        assert set(store.embeddings) == {"n3"}

    def test_vectors_are_held_as_float32(self):
        store = self._store()

        assert store.embeddings["n1"].dtype == np.float32

    def test_export_vectors_round_trips_through_load_vectors(self):
        store = self._store()
        exported = store.export_vectors()

        other = VectorStore()
        other.load_vectors(exported)

        assert set(other.embeddings) == set(store.embeddings)
        np.testing.assert_allclose(other.embeddings["n1"], store.embeddings["n1"])

    def test_get_vector_list_returns_json_serialisable_values(self):
        store = self._store()

        assert store.get_vector_list("n1") == [1.0, 0.0]
        assert store.get_vector_list("missing") is None

    def test_revision_advances_on_every_change(self):
        store = self._store()
        start = store.revision

        store.load_vectors({"n1": [1.0, 0.0]})
        after_load = store.revision
        assert after_load > start

        store.remove_node_embedding("n1")
        assert store.revision > after_load

    def test_revision_is_unchanged_by_a_read(self):
        store = self._store()
        before = store.revision

        store.export_vectors()
        store.get_vector_list("n1")
        store.search(query_text=None, query_node=None)

        assert store.revision == before

    def test_removing_an_absent_id_does_not_advance_the_revision(self):
        """A no-op removal must not make GraphStorage rewrite the sidecar."""
        store = self._store()
        before = store.revision

        store.remove_nodes_embeddings(["not-here"])

        assert store.revision == before


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


class TestVectorStoreNumpySearch:
    """Semantic search runs on numpy alone — no ML extras (torch /
    sentence-transformers) required. Regression for STRUCTURE_REVIEW.md A2,
    which moved those extras out of the base requirements."""

    def _store_with_embeddings(self):
        # Deterministic embeddings set directly on the nodes, so no embedding
        # model is loaded at any point in these tests.
        nodes = [
            Node(id="n1", type=NodeType.ACTOR, name="A"),
            Node(id="n2", type=NodeType.ACTOR, name="B"),
            Node(id="n3", type=NodeType.ACTOR, name="C"),
        ]
        nodes[0].embedding = [1.0, 0.0, 0.0]
        nodes[1].embedding = [0.9, 0.1, 0.0]
        nodes[2].embedding = [0.0, 0.0, 1.0]
        store = VectorStore()
        store.rebuild_index(nodes)
        return store, nodes

    def test_cosine_similarity_matrix_matches_expected(self):
        from backend.core.vector_store import _cosine_similarity_matrix

        store, _ = self._store_with_embeddings()
        query = np.array([[1.0, 0.0, 0.0]])
        sims = _cosine_similarity_matrix(query, store.embedding_matrix)

        assert sims.shape == (3,)
        assert sims[0] == pytest.approx(1.0, abs=1e-6)  # identical vector
        assert sims[2] == pytest.approx(0.0, abs=1e-6)  # orthogonal vector
        assert sims[0] > sims[1] > sims[2]

    def test_search_by_node_uses_numpy_only(self):
        """Searching by an already-embedded node needs only numpy; the model
        must never be loaded."""
        store, nodes = self._store_with_embeddings()

        with patch.object(
            store, "_load_model", side_effect=AssertionError("model must not load")
        ):
            results = store.search(query_node=nodes[0], limit=5)

        result_ids = [node_id for node_id, _ in results]
        assert "n1" not in result_ids  # query node itself excluded
        assert result_ids[0] == "n2"  # nearest neighbour ranked first

    def test_search_by_text_degrades_when_model_missing(self):
        """Query-text search embeds the query with the ML model; when that
        model is unavailable, search returns no semantic hits instead of
        raising."""
        store, _ = self._store_with_embeddings()

        def missing_model():
            raise ImportError("No module named 'sentence_transformers'")

        with patch.object(store, "_load_model", side_effect=missing_model):
            results = store.search(query_text="anything", limit=5)

        assert results == []


# Skip slow tests by default, run with: pytest -m slow
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (require model loading)"
    )
