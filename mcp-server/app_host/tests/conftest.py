"""
Pytest fixtures for app_host tests.

Provides test fixtures for creating test applications with temporary
graph files and proper cleanup.
"""

import pytest
import tempfile
import json
import os
from pathlib import Path
from typing import Generator, List, Dict, Any
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app_host import create_app, AppConfig
from graph_core import GraphStorage


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


class MockLLMProvider:
    """Mock LLM provider for testing."""

    def __init__(self):
        self.mock_tool_calls: List[Dict[str, Any]] = []
        self.mock_text_response: str = "Mock response from LLM"
        self.call_count = 0

    def set_response(self, text: str, tool_use: Dict[str, Any] = None):
        """Configure the mock response for the next call.

        Args:
            text: Text response to return
            tool_use: Optional tool use dict with 'name' and 'input' keys
        """
        self.mock_text_response = text
        if tool_use:
            self.mock_tool_calls = [tool_use]
        else:
            self.mock_tool_calls = []
        self.call_count = 0

    def create_completion(self, messages, system_prompt, tools, max_tokens=4096):
        from llm_providers import LLMResponse
        self.call_count += 1

        if self.mock_tool_calls and self.call_count == 1:
            content = []
            for i, tool_call in enumerate(self.mock_tool_calls):
                content.append({
                    "type": "tool_use",
                    "id": f"mock_tool_{i}",
                    "name": tool_call["name"],
                    "input": tool_call.get("input", {})
                })
            return LLMResponse(content=content, stop_reason="tool_use")
        else:
            return LLMResponse(
                content=[{"type": "text", "text": self.mock_text_response}],
                stop_reason="end_turn"
            )

    def reset(self):
        self.mock_tool_calls = []
        self.mock_text_response = "Mock response from LLM"
        self.call_count = 0


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider."""
    return MockLLMProvider()


@pytest.fixture
def sample_graph_data() -> dict:
    """Sample graph data for testing."""
    return {
        "nodes": [
            {
                "id": "node-1",
                "type": "Actor",
                "name": "Test Organization",
                "description": "A test organization for testing",
                "communities": ["TestCommunity"],
                "tags": ["test", "org"],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z"
            },
            {
                "id": "node-2",
                "type": "Initiative",
                "name": "Test Project",
                "description": "A test project initiative",
                "communities": ["TestCommunity"],
                "tags": ["test", "project"],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z"
            },
            {
                "id": "node-3",
                "type": "Resource",
                "name": "Test Document",
                "description": "A test resource document",
                "communities": ["OtherCommunity"],
                "tags": ["test", "doc"],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z"
            }
        ],
        "edges": [
            {
                "id": "edge-1",
                "source": "node-1",
                "target": "node-2",
                "type": "IMPLEMENTS",
                "created_at": "2024-01-01T00:00:00Z"
            },
            {
                "id": "edge-2",
                "source": "node-2",
                "target": "node-3",
                "type": "PRODUCES",
                "created_at": "2024-01-01T00:00:00Z"
            }
        ]
    }


@pytest.fixture
def temp_graph_file(sample_graph_data) -> Generator[str, None, None]:
    """Create a temporary graph file with sample data."""
    with tempfile.NamedTemporaryFile(
        mode='w',
        suffix='.json',
        delete=False
    ) as f:
        json.dump(sample_graph_data, f)
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def temp_static_dirs() -> Generator[tuple, None, None]:
    """Create temporary static directories with placeholder files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create web directory
        web_dir = Path(temp_dir) / "web"
        web_dir.mkdir()
        (web_dir / "index.html").write_text(
            "<!DOCTYPE html><html><body>Web App</body></html>"
        )

        # Create widget directory
        widget_dir = Path(temp_dir) / "widget"
        widget_dir.mkdir()
        (widget_dir / "index.html").write_text(
            "<!DOCTYPE html><html><body>Widget</body></html>"
        )

        yield str(web_dir), str(widget_dir)


@pytest.fixture
def app_config(temp_graph_file, temp_static_dirs) -> AppConfig:
    """Create test AppConfig with temporary files."""
    web_path, widget_path = temp_static_dirs
    return AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        api_prefix="/api",
    )


@pytest.fixture
def test_app(app_config, mock_llm_provider) -> TestClient:
    """Create test application with TestClient.

    Returns TestClient for backwards compatibility with existing tests.
    For tests that need to configure the mock LLM, use test_app_with_mock.
    """
    # Patch LLM provider BEFORE creating app
    with patch('chat_logic.create_provider', return_value=mock_llm_provider):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            app = create_app(app_config)
            # Update the chat service to use our mock
            if hasattr(app.state, 'chat_service'):
                app.state.chat_service._processor.default_api_key = 'test-key'
            yield TestClient(app)


@pytest.fixture
def test_app_with_mock(app_config, mock_llm_provider):
    """Create test application with TestClient and mocked LLM.

    Returns a tuple of (TestClient, mock_llm_provider) for tests that need to configure the mock.
    """
    # Patch LLM provider BEFORE creating app
    with patch('chat_logic.create_provider', return_value=mock_llm_provider):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            app = create_app(app_config)
            # Update the chat service to use our mock
            if hasattr(app.state, 'chat_service'):
                app.state.chat_service._processor.default_api_key = 'test-key'
            yield TestClient(app), mock_llm_provider


@pytest.fixture
def empty_graph_file() -> Generator[str, None, None]:
    """Create an empty graph file."""
    with tempfile.NamedTemporaryFile(
        mode='w',
        suffix='.json',
        delete=False
    ) as f:
        json.dump({"nodes": [], "edges": []}, f)
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def test_app_empty_graph(empty_graph_file, temp_static_dirs) -> TestClient:
    """Create test application with empty graph."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=empty_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
    )
    app = create_app(config)
    return TestClient(app)
