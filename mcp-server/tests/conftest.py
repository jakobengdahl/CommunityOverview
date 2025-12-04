import pytest
import os
import sys
import tempfile
import json
from pathlib import Path

# Add the parent directory to sys.path so we can import from mcp-server
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from graph_storage import GraphStorage
from models import Node, Edge, NodeType, RelationshipType

@pytest.fixture
def test_graph_file():
    """Create a temporary graph JSON file"""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        # Initialize with empty graph data structure
        json.dump({'nodes': [], 'edges': [], 'metadata': {}}, f)
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.remove(temp_path)

@pytest.fixture
def graph_storage(test_graph_file):
    """Create a GraphStorage instance using the temporary file"""
    storage = GraphStorage(test_graph_file)
    return storage

@pytest.fixture
def sample_node():
    return Node(
        type=NodeType.INITIATIVE,
        name="Test Initiative",
        description="A test initiative",
        summary="Test summary",
        communities=["eSam"],
        metadata={"status": "active"}
    )
