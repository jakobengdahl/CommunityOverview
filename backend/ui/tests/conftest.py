"""
Pytest fixtures for ui_backend tests.

Provides:
- Mock LLM provider that returns predetermined tool calls
- In-memory GraphStorage and GraphService
- ChatService with mocked LLM
- Test client for FastAPI endpoints
"""

import pytest
import json
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Dict, Any, List
import tempfile
import os

from backend.core import GraphStorage, Node, Edge, NodeType
from backend.service import GraphService
from backend.ui import ChatService, DocumentService, create_ui_router
from backend.llm_providers import LLMResponse


class MockLLMProvider:
    """
    Mock LLM provider that returns predetermined responses.

    Configure tool calls by setting mock_tool_calls before calling create_completion.
    """

    def __init__(self):
        self.mock_tool_calls: List[Dict[str, Any]] = []
        self.mock_text_response: str = "Mock response from LLM"
        self.call_count = 0
        self.received_messages: List[List[Dict]] = []

    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096
    ) -> LLMResponse:
        """Return mock response with optional tool calls."""
        self.received_messages.append(messages)
        self.call_count += 1

        # First call returns tool calls, subsequent calls return text
        if self.mock_tool_calls and self.call_count == 1:
            content = []
            for i, tool_call in enumerate(self.mock_tool_calls):
                content.append({
                    "type": "tool_use",
                    "id": f"mock_tool_{i}",
                    "name": tool_call["name"],
                    "input": tool_call.get("input", {})
                })
            return LLMResponse(
                content=content,
                stop_reason="tool_use"
            )
        else:
            return LLMResponse(
                content=[{"type": "text", "text": self.mock_text_response}],
                stop_reason="end_turn"
            )

    def reset(self):
        """Reset mock state."""
        self.mock_tool_calls = []
        self.mock_text_response = "Mock response from LLM"
        self.call_count = 0
        self.received_messages = []


class MockSentenceTransformer:
    """Mock SentenceTransformer that generates deterministic embeddings."""

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
            self._np.random.seed(abs(hash(text)) % (2**32))
            embedding = self._np.random.rand(384).astype(self._np.float32)
            embeddings.append(embedding)
        return self._np.array(embeddings)


@pytest.fixture(autouse=True)
def mock_embedding_model():
    """Mock the embedding model to avoid network calls."""
    import graph_core.vector_store as vs
    original_ensure = vs._ensure_sentence_transformers

    def mock_ensure():
        return MockSentenceTransformer

    vs._ensure_sentence_transformers = mock_ensure
    vs._SentenceTransformer = None
    yield
    vs._ensure_sentence_transformers = original_ensure
    vs._SentenceTransformer = None


@pytest.fixture
def temp_graph_file():
    """Create a temporary file for graph storage."""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    # Initialize with empty graph
    with open(path, 'w') as f:
        json.dump({"nodes": [], "edges": []}, f)
    yield path
    # Cleanup
    try:
        os.unlink(path)
    except Exception:
        pass


@pytest.fixture
def graph_storage(temp_graph_file):
    """Create an in-memory GraphStorage for tests."""
    return GraphStorage(temp_graph_file)


@pytest.fixture
def graph_service(graph_storage):
    """Create a GraphService with test storage."""
    return GraphService(graph_storage)


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider."""
    return MockLLMProvider()


@pytest.fixture
def chat_service(graph_service, mock_llm_provider):
    """
    Create a ChatService with mocked LLM provider.

    The LLM provider is mocked to return predetermined responses,
    but the GraphService is real (in-memory).
    """
    # Patch BEFORE creating ChatService so ChatProcessor uses mock
    with patch('chat_logic.create_provider', return_value=mock_llm_provider):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)
            service._processor.provider_type = "mock"
            service._processor.default_api_key = "test-key"
            yield service, mock_llm_provider


@pytest.fixture
def document_service():
    """Create a DocumentService with temp directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield DocumentService(upload_dir=tmpdir)


@pytest.fixture
def sample_nodes(graph_service):
    """Add sample nodes to the graph for testing."""
    nodes = [
        {
            "id": "test-actor-1",
            "name": "Test Agency",
            "type": "Actor",
            "description": "A test government agency",
            "communities": ["test-community"]
        },
        {
            "id": "test-initiative-1",
            "name": "Test Project",
            "type": "Initiative",
            "description": "A test project",
            "communities": ["test-community"]
        }
    ]
    edges = [
        {
            "source": "test-actor-1",
            "target": "test-initiative-1",
            "type": "BELONGS_TO"
        }
    ]

    graph_service.add_nodes(nodes, edges)
    return nodes, edges


@pytest.fixture
def test_text_file():
    """Create a temporary text file for upload tests."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("This is a test document.\n")
        f.write("It contains some sample text for testing.\n")
        f.write("The document mentions AI and digitalization.")
        path = f.name
    yield path
    try:
        os.unlink(path)
    except Exception:
        pass


@pytest.fixture
def fastapi_test_client(graph_service, mock_llm_provider):
    """Create a FastAPI test client with mocked services."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Patch BEFORE creating ChatService
    with patch('chat_logic.create_provider', return_value=mock_llm_provider):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            app = FastAPI()

            # Create services with mocked LLM
            chat_svc = ChatService(graph_service)
            chat_svc._processor.provider_type = "mock"
            chat_svc._processor.default_api_key = "test-key"
            document_svc = DocumentService()

            # Create and mount router
            router = create_ui_router(chat_svc, document_svc)
            app.include_router(router, prefix="/ui")

            yield TestClient(app), mock_llm_provider, graph_service
