"""
Tests for event routing integration between dispatcher and agent registry.
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
)
from backend.core.events.dispatcher import EventDispatcher
from backend.agents.config import AgentsSettings, MCPIntegration
from backend.agents.registry import AgentRegistry


class MockNode:
    """Mock node for testing."""

    def __init__(self, id: str, name: str, node_type: str, metadata: dict = None):
        self.id = id
        self.name = name
        self.type = node_type
        self.metadata = metadata or {}


class MockStorage:
    """Mock storage for testing."""

    def __init__(self, nodes: list = None):
        self.nodes = {n.id: n for n in (nodes or [])}

    def get_node(self, node_id: str):
        return self.nodes.get(node_id)


def create_subscription_node(
    id: str,
    name: str,
    node_types: list = None,
    operations: list = None,
    webhook_url: str = "https://example.com/hook",
    ignore_origins: list = None,
) -> MockNode:
    """Helper to create a subscription node for testing."""
    metadata = {
        "filters": {
            "target": {
                "entity_kind": "node",
                "node_types": node_types or [],
            },
            "operations": operations or ["create", "update", "delete"],
            "keywords": {"any": []},
        },
        "delivery": {
            "webhook_url": webhook_url,
            "ignore_origins": ignore_origins or [],
            "ignore_session_ids": [],
        },
    }
    return MockNode(id, name, "EventSubscription", metadata)


def create_agent_node(
    id: str,
    name: str,
    subscription_id: str,
    enabled: bool = True,
    task_prompt: str = "Process events",
) -> MockNode:
    """Helper to create an agent node for testing."""
    metadata = {
        "subscription_id": subscription_id,
        "agent": {
            "enabled": enabled,
            "task_prompt": task_prompt,
            "mcp_integrations": ["GRAPH"],
        },
    }
    return MockNode(id, name, "Agent", metadata)


def create_event(
    event_type: EventType = EventType.NODE_CREATE,
    entity_id: str = "node-1",
    entity_type: str = "Initiative",
    event_origin: str = None,
) -> Event:
    """Helper to create an event for testing."""
    return Event(
        event_type=event_type,
        origin=EventContext(event_origin=event_origin),
        entity=EntityData(
            kind=EntityKind.NODE,
            id=entity_id,
            type=entity_type,
            after={"name": "Test Node", "type": entity_type},
        ),
    )


class TestAgentDeliveryCallback:
    """Tests for agent delivery callback integration."""

    def test_dispatcher_calls_agent_callback_for_matching_subscription(self):
        """Test that dispatcher calls agent callback for agent-linked subscriptions."""
        sub = create_subscription_node("sub-001", "Agent Subscription")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        # Track agent delivery calls
        agent_deliveries = []

        def agent_callback(event, subscription_id):
            agent_deliveries.append((event, subscription_id))
            return True  # Handled by agent

        dispatcher.set_agent_delivery_callback(agent_callback)

        # Also set webhook callback (should not be called if agent handles)
        webhook_deliveries = []
        dispatcher.set_delivery_callback(lambda e, u: webhook_deliveries.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        # Agent callback should have been called
        assert len(agent_deliveries) == 1
        assert agent_deliveries[0][1] == "sub-001"

        # Webhook should NOT have been called (agent handled it)
        assert len(webhook_deliveries) == 0

    def test_dispatcher_falls_back_to_webhook_when_agent_returns_false(self):
        """Test that dispatcher falls back to webhook when agent doesn't handle."""
        sub = create_subscription_node("sub-001", "Agent Subscription")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        agent_deliveries = []

        def agent_callback(event, subscription_id):
            agent_deliveries.append((event, subscription_id))
            return False  # Not handled by agent

        dispatcher.set_agent_delivery_callback(agent_callback)

        webhook_deliveries = []
        dispatcher.set_delivery_callback(lambda e, u: webhook_deliveries.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        # Agent callback was called but returned False
        assert len(agent_deliveries) == 1

        # Webhook should have been called as fallback
        assert len(webhook_deliveries) == 1

    def test_dispatcher_uses_webhook_when_no_agent_callback(self):
        """Test that dispatcher uses webhook when no agent callback is set."""
        sub = create_subscription_node("sub-001", "Webhook Subscription")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        # Only set webhook callback, no agent callback
        webhook_deliveries = []
        dispatcher.set_delivery_callback(lambda e, u: webhook_deliveries.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        # Webhook should have been called
        assert len(webhook_deliveries) == 1


class TestRegistryAsAgentCallback:
    """Tests for using agent registry as the agent delivery callback."""

    def test_registry_callback_routes_to_worker(self):
        """Test that registry enqueue_for_subscription works as callback."""
        # Create subscription and agent nodes
        sub = create_subscription_node("sub-001", "Agent Subscription")
        agent = create_agent_node("agent-001", "Test Agent", "sub-001")
        storage = MockStorage([sub, agent])

        # Create registry with mock worker
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        mock_service = MagicMock()
        registry = AgentRegistry(settings, storage, mock_service)

        # Add a mock worker
        mock_worker = MagicMock()
        registry._workers["agent-001"] = mock_worker
        registry._subscription_agent_map["sub-001"] = "agent-001"

        # Use registry method as callback
        def agent_callback(event, subscription_id):
            if not registry.is_agent_subscription(subscription_id):
                return False
            return registry.enqueue_for_subscription(
                subscription_id,
                event.to_webhook_payload()
            )

        # Create dispatcher and wire up
        dispatcher = EventDispatcher(storage)
        dispatcher.set_agent_delivery_callback(agent_callback)
        dispatcher.set_delivery_callback(lambda e, u: None)  # Webhook fallback

        # Dispatch event
        event = create_event()
        count = dispatcher.dispatch(event)

        # Should have been routed to agent
        assert count == 1
        mock_worker.enqueue.assert_called_once()

    def test_non_agent_subscription_falls_back_to_webhook(self):
        """Test that non-agent subscriptions fall back to webhook."""
        # Only a subscription, no agent linked
        sub = create_subscription_node("sub-001", "Webhook Only")
        storage = MockStorage([sub])

        # Create registry (won't have this subscription mapped)
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        mock_service = MagicMock()
        registry = AgentRegistry(settings, storage, mock_service)

        def agent_callback(event, subscription_id):
            if not registry.is_agent_subscription(subscription_id):
                return False
            return registry.enqueue_for_subscription(
                subscription_id,
                event.to_webhook_payload()
            )

        # Track webhook calls
        webhook_calls = []

        dispatcher = EventDispatcher(storage)
        dispatcher.set_agent_delivery_callback(agent_callback)
        dispatcher.set_delivery_callback(lambda e, u: webhook_calls.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        # Should have fallen back to webhook
        assert len(webhook_calls) == 1
