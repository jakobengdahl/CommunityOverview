"""
Pytest fixtures for graph_services tests.

Provides shared test fixtures for:
- Temporary storage instances
- Pre-populated graph data
- GraphService instances
- FastAPI test clients
"""

import pytest
import tempfile
import os
from typing import Generator
from unittest.mock import patch, MagicMock

from backend.core import GraphStorage, Node, Edge, NodeType, RelationshipType
from backend.service import GraphService


# ==================== Mock Embedding Model ====================
# This fixture mocks the embedding model to avoid network requests during tests.
# The mock returns random-ish but deterministic embeddings based on input text.

class MockSentenceTransformer:
    """Mock SentenceTransformer that generates deterministic embeddings without network."""

    def __init__(self, model_name=None):
        self.model_name = model_name
        import numpy as np
        self._np = np

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        """Generate mock embeddings based on text hash."""
        if isinstance(texts, str):
            texts = [texts]
        embeddings = []
        for text in texts:
            # Create deterministic embedding based on text content
            self._np.random.seed(abs(hash(text)) % (2**32))
            embedding = self._np.random.rand(384).astype(self._np.float32)  # MiniLM dimension
            embeddings.append(embedding)
        return self._np.array(embeddings)


@pytest.fixture(autouse=True)
def mock_embedding_model():
    """
    Automatically mock the embedding model for all tests.
    This prevents network requests to HuggingFace during testing.
    """
    import backend.core.vector_store as vs

    # Save original function
    original_ensure = vs._ensure_sentence_transformers

    # Replace with mock
    def mock_ensure():
        return MockSentenceTransformer

    vs._ensure_sentence_transformers = mock_ensure

    # Also reset the global to force reload
    vs._SentenceTransformer = None

    yield

    # Restore original
    vs._ensure_sentence_transformers = original_ensure
    vs._SentenceTransformer = None


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def empty_storage(temp_dir: str) -> GraphStorage:
    """Create an empty GraphStorage instance for testing."""
    json_path = os.path.join(temp_dir, "test_graph.json")
    embeddings_path = os.path.join(temp_dir, "test_embeddings.pkl")
    return GraphStorage(json_path=json_path, embeddings_path=embeddings_path)


@pytest.fixture
def empty_service(empty_storage: GraphStorage) -> GraphService:
    """Create a GraphService with empty storage."""
    return GraphService(empty_storage)


@pytest.fixture
def sample_nodes() -> list:
    """Sample nodes for testing."""
    return [
        Node(
            id="actor-1",
            type=NodeType.ACTOR,
            name="Skatteverket",
            description="Swedish Tax Agency",
            summary="Tax authority",
            tags=["government", "tax"]
        ),
        Node(
            id="actor-2",
            type=NodeType.ACTOR,
            name="Bolagsverket",
            description="Swedish Companies Registration Office",
            summary="Company registration",
            tags=["government", "registration"]
        ),
        Node(
            id="init-1",
            type=NodeType.INITIATIVE,
            name="Digital First",
            description="A digital transformation initiative",
            summary="Digital transformation",
            tags=["digital", "transformation"]
        ),
        Node(
            id="community-1",
            type=NodeType.COMMUNITY,
            name="eSam",
            description="eGovernment collaboration community",
            summary="eGov community"
        ),
        Node(
            id="legislation-1",
            type=NodeType.LEGISLATION,
            name="GDPR",
            description="General Data Protection Regulation",
            summary="Data protection law",
            tags=["privacy", "data"]
        ),
    ]


@pytest.fixture
def sample_edges() -> list:
    """Sample edges for testing."""
    return [
        Edge(
            id="edge-1",
            source="actor-1",
            target="init-1",
            type=RelationshipType.BELONGS_TO
        ),
        Edge(
            id="edge-2",
            source="actor-2",
            target="init-1",
            type=RelationshipType.BELONGS_TO
        ),
        Edge(
            id="edge-3",
            source="init-1",
            target="community-1",
            type=RelationshipType.PART_OF
        ),
        Edge(
            id="edge-4",
            source="init-1",
            target="legislation-1",
            type=RelationshipType.GOVERNED_BY
        ),
    ]


@pytest.fixture
def populated_storage(empty_storage: GraphStorage, sample_nodes: list, sample_edges: list) -> GraphStorage:
    """Create a GraphStorage populated with sample data."""
    empty_storage.add_nodes(sample_nodes, sample_edges)
    return empty_storage


@pytest.fixture
def populated_service(populated_storage: GraphStorage) -> GraphService:
    """Create a GraphService with pre-populated data."""
    return GraphService(populated_storage)


@pytest.fixture
def saved_view_node() -> Node:
    """Create a sample SavedView node."""
    return Node(
        id="view-1",
        type=NodeType.SAVED_VIEW,
        name="Test View",
        description="A test saved view",
        summary="Test view",
        metadata={
            "node_ids": ["actor-1", "actor-2", "init-1"],
            "positions": {
                "actor-1": {"x": 100, "y": 100},
                "actor-2": {"x": 200, "y": 100},
                "init-1": {"x": 150, "y": 200}
            }
        }
    )


@pytest.fixture
def service_with_view(populated_storage: GraphStorage, saved_view_node: Node) -> GraphService:
    """Create a GraphService with a saved view."""
    populated_storage.add_nodes([saved_view_node], [])
    return GraphService(populated_storage)
