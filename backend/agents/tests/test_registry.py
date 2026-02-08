"""
Tests for agent registry.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import threading
import time

from backend.agents.config import AgentConfig, AgentsSettings, MCPIntegration
from backend.agents.registry import AgentRegistry


class TestAgentRegistry:
    """Tests for AgentRegistry functionality."""

    @pytest.fixture
    def disabled_settings(self):
        """Settings with agents disabled."""
        return AgentsSettings(enabled=False)

    @pytest.fixture
    def enabled_settings(self):
        """Settings with agents enabled."""
        return AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="test-key",
            mcp_integrations=[
                MCPIntegration(
                    id="GRAPH",
                    type="http",
                    url="http://localhost:8000/mcp",
                )
            ],
        )

    def test_registry_disabled_by_default(self, mock_storage, mock_service, disabled_settings):
        """Test that registry is disabled when settings.enabled is False."""
        registry = AgentRegistry(
            settings=disabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        assert registry.is_enabled is False

    def test_registry_enabled_when_configured(self, mock_storage, mock_service, enabled_settings):
        """Test that registry is enabled when settings.enabled is True."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        assert registry.is_enabled is True

    def test_start_disabled_does_nothing(self, mock_storage, mock_service, disabled_settings):
        """Test that start() does nothing when disabled."""
        registry = AgentRegistry(
            settings=disabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        registry.start()  # Should not raise

        assert registry.list_workers() == []

    def test_subscription_agent_mapping(self, mock_storage, mock_service, enabled_settings):
        """Test subscription to agent ID mapping."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        # Manually add a mapping (normally done by _start_worker)
        registry._subscription_agent_map["sub-001"] = "agent-001"

        assert registry.is_agent_subscription("sub-001") is True
        assert registry.is_agent_subscription("sub-unknown") is False
        assert registry.get_agent_for_subscription("sub-001") == "agent-001"
        assert registry.get_agent_for_subscription("sub-unknown") is None

    def test_get_all_status(self, mock_storage, mock_service, enabled_settings):
        """Test getting registry status."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        status = registry.get_all_status()

        assert "enabled" in status
        assert "worker_count" in status
        assert "subscription_count" in status
        assert "mcp_integrations" in status
        assert status["enabled"] is True
        assert status["worker_count"] == 0

    def test_list_workers_empty(self, mock_storage, mock_service, enabled_settings):
        """Test listing workers when none running."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        workers = registry.list_workers()

        assert workers == []


class TestAgentLifecycle:
    """Tests for agent lifecycle management."""

    @pytest.fixture
    def registry_with_agent(self, mock_storage, mock_service, sample_agent_node):
        """Create a registry with an agent node in storage."""
        mock_storage.nodes[sample_agent_node.id] = sample_agent_node

        settings = AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="test-key",
            mcp_integrations=[],
        )

        return AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

    def test_load_agents_finds_agent_nodes(self, registry_with_agent, sample_agent_node):
        """Test that _load_agents finds Agent nodes in storage."""
        agents = registry_with_agent._load_agents()

        assert len(agents) == 1
        assert agents[0].agent_id == sample_agent_node.id
        assert agents[0].name == sample_agent_node.name

    def test_handle_agent_deleted_stops_worker(self, mock_storage, mock_service):
        """Test that deleting an agent stops its worker."""
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        # Mock a running worker
        mock_worker = MagicMock()
        registry._workers["agent-001"] = mock_worker
        registry._subscription_agent_map["sub-001"] = "agent-001"
        mock_worker.config = MagicMock(subscription_id="sub-001")

        registry.handle_agent_deleted("agent-001")

        # Worker should have been stopped
        mock_worker.stop.assert_called_once_with(wait=True)
        assert "agent-001" not in registry._workers
        assert "sub-001" not in registry._subscription_agent_map


class TestEventRouting:
    """Tests for event routing to agents."""

    @pytest.fixture
    def registry_with_worker(self, mock_storage, mock_service):
        """Create a registry with a mock worker."""
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        mock_worker = MagicMock()
        registry._workers["agent-001"] = mock_worker
        registry._subscription_agent_map["sub-001"] = "agent-001"

        return registry, mock_worker

    def test_enqueue_for_subscription_routes_to_agent(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that enqueue_for_subscription routes events to the correct agent."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue_for_subscription("sub-001", sample_event_payload)

        assert result is True
        mock_worker.enqueue.assert_called_once_with(sample_event_payload)

    def test_enqueue_for_subscription_unknown_subscription(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that unknown subscriptions return False."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue_for_subscription("sub-unknown", sample_event_payload)

        assert result is False
        mock_worker.enqueue.assert_not_called()

    def test_enqueue_direct_to_agent(self, registry_with_worker, sample_event_payload):
        """Test enqueuing directly to an agent by ID."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue("agent-001", sample_event_payload)

        assert result is True
        mock_worker.enqueue.assert_called_once_with(sample_event_payload)

    def test_enqueue_unknown_agent_returns_false(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that enqueue to unknown agent returns False."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue("agent-unknown", sample_event_payload)

        assert result is False


class TestGetAvailableIntegrations:
    """Tests for getting available MCP integrations."""

    def test_returns_configured_integrations(self, mock_storage, mock_service):
        """Test that configured integrations are returned."""
        settings = AgentsSettings(
            enabled=True,
            mcp_integrations=[
                MCPIntegration(
                    id="GRAPH",
                    type="http",
                    url="http://localhost:8000/mcp",
                    description="Graph tools",
                ),
                MCPIntegration(
                    id="WEB",
                    type="stdio",
                    command="node",
                    args=["mcp-web"],
                    description="Web search",
                ),
            ],
        )

        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        integrations = registry.get_available_mcp_integrations()

        assert len(integrations) == 2
        ids = [i["id"] for i in integrations]
        assert "GRAPH" in ids
        assert "WEB" in ids
