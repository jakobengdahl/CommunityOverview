"""
Pytest configuration for agent system tests.
"""

import pytest
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (LLM calls, network dependent)"
    )


@dataclass
class MockNode:
    """Mock graph node for testing."""

    id: str
    name: str
    type: str
    description: str = ""
    summary: str = ""
    metadata: Dict[str, Any] = None
    tags: List[str] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.tags is None:
            self.tags = []


class MockGraphStorage:
    """Mock GraphStorage for testing agent registry."""

    def __init__(self, nodes: List[MockNode] = None):
        self.nodes = {n.id: n for n in (nodes or [])}

    def get_node(self, node_id: str) -> Optional[MockNode]:
        return self.nodes.get(node_id)


class MockGraphService:
    """Mock GraphService for testing agent tool calls."""

    def __init__(self):
        self.search_calls = []
        self.update_calls = []

    def search_graph(self, query: str = "", **kwargs) -> Dict[str, Any]:
        """Mock search returning empty results."""
        self.search_calls.append({"query": query, **kwargs})
        return {"nodes": [], "edges": []}

    def update_node(self, node_id: str, **kwargs) -> Dict[str, Any]:
        """Mock update node."""
        self.update_calls.append({"node_id": node_id, **kwargs})
        return {"success": True, "node_id": node_id}


@pytest.fixture
def mock_storage():
    """Create a mock storage with no nodes."""
    return MockGraphStorage()


@pytest.fixture
def mock_service():
    """Create a mock graph service."""
    return MockGraphService()


@pytest.fixture
def sample_agent_node():
    """Create a sample Agent node configuration."""
    return MockNode(
        id="agent-001",
        name="Test Agent",
        type="Agent",
        description="Test agent for unit tests",
        metadata={
            "subscription_id": "sub-001",
            "enabled": True,
            "prompts": {
                "task_prompt": "Process events and log a summary.",
            },
            "mcp_integration_ids": ["GRAPH"],
        },
    )


@pytest.fixture
def sample_subscription_node():
    """Create a sample EventSubscription node."""
    return MockNode(
        id="sub-001",
        name="Test Agent - Subscription",
        type="EventSubscription",
        description="Subscription for test agent",
        metadata={
            "filters": {
                "target": {"entity_kind": "node", "node_types": ["Initiative"]},
                "operations": ["create", "update"],
                "keywords": {"any": []},
            },
            "delivery": {
                "webhook_url": "internal://agent/agent-001",
                "ignore_origins": ["agent:agent-001"],
                "ignore_session_ids": [],
            },
        },
    )


@pytest.fixture
def sample_event_payload():
    """Create a sample event payload for testing."""
    return {
        "event_id": "evt-12345",
        "event_type": "node.create",
        "occurred_at": "2024-01-15T10:30:00Z",
        "origin": {
            "event_origin": "web-ui",
            "event_session_id": "session-abc",
        },
        "entity": {
            "kind": "node",
            "id": "node-new-001",
            "type": "Initiative",
            "before": None,
            "after": {
                "id": "node-new-001",
                "name": "AI Strategy Project",
                "type": "Initiative",
                "description": "New AI project for the organization",
                "tags": ["AI", "strategy"],
            },
        },
        "subscription": {
            "id": "sub-001",
            "name": "Test Agent - Subscription",
        },
    }
