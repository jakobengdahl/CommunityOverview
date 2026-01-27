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
from typing import Generator

from fastapi.testclient import TestClient

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app_host import create_app, AppConfig
from graph_core import GraphStorage


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
def test_app(app_config) -> TestClient:
    """Create test application with TestClient."""
    app = create_app(app_config)
    return TestClient(app)


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
