"""
Tests for event system models.
"""

import pytest
from datetime import datetime

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EventOrigin,
    EntityData,
    DeliveryResult,
    DeliveryStatus,
    SubscriptionFilters,
    SubscriptionDelivery,
    TargetFilters,
    KeywordFilters,
)


class TestEventContext:
    """Tests for EventContext model."""

    def test_create_empty_context(self):
        """Test creating context with no parameters."""
        ctx = EventContext()
        assert ctx.event_origin is None
        assert ctx.event_session_id is None
        assert ctx.event_correlation_id is None

    def test_create_full_context(self):
        """Test creating context with all parameters."""
        ctx = EventContext(
            event_origin="web-ui",
            event_session_id="session-123",
            event_correlation_id="corr-456",
        )
        assert ctx.event_origin == "web-ui"
        assert ctx.event_session_id == "session-123"
        assert ctx.event_correlation_id == "corr-456"

    def test_to_dict(self):
        """Test converting context to dict."""
        ctx = EventContext(
            event_origin="mcp",
            event_session_id="s1",
        )
        d = ctx.to_dict()
        assert d["event_origin"] == "mcp"
        assert d["event_session_id"] == "s1"
        assert d["event_correlation_id"] is None


class TestEventOrigin:
    """Tests for EventOrigin helper class."""

    def test_agent_origin(self):
        """Test creating agent origin string."""
        origin = EventOrigin.agent("my-agent")
        assert origin == "agent:my-agent"

    def test_is_agent_origin_true(self):
        """Test identifying agent origins."""
        assert EventOrigin.is_agent_origin("agent:test") is True
        assert EventOrigin.is_agent_origin("agent:123") is True

    def test_is_agent_origin_false(self):
        """Test non-agent origins."""
        assert EventOrigin.is_agent_origin("web-ui") is False
        assert EventOrigin.is_agent_origin("mcp") is False
        assert EventOrigin.is_agent_origin(None) is False

    def test_get_agent_id(self):
        """Test extracting agent ID from origin."""
        assert EventOrigin.get_agent_id("agent:my-agent") == "my-agent"
        assert EventOrigin.get_agent_id("agent:123") == "123"
        assert EventOrigin.get_agent_id("web-ui") is None


class TestEvent:
    """Tests for Event model."""

    def test_create_event(self):
        """Test creating an event."""
        event = Event(
            event_type=EventType.NODE_CREATE,
            origin=EventContext(event_origin="web-ui"),
            entity=EntityData(
                kind=EntityKind.NODE,
                id="node-1",
                type="Actor",
                after={"name": "Test Actor", "type": "Actor"},
            ),
        )

        assert event.event_id is not None
        assert event.event_type == EventType.NODE_CREATE
        assert event.origin.event_origin == "web-ui"
        assert event.entity.id == "node-1"
        assert event.occurred_at is not None

    def test_event_auto_generates_id(self):
        """Test that events automatically get unique IDs."""
        event1 = Event(
            event_type=EventType.NODE_UPDATE,
            origin=EventContext(),
            entity=EntityData(kind=EntityKind.NODE, id="n1", type="Actor"),
        )
        event2 = Event(
            event_type=EventType.NODE_UPDATE,
            origin=EventContext(),
            entity=EntityData(kind=EntityKind.NODE, id="n1", type="Actor"),
        )

        assert event1.event_id != event2.event_id

    def test_to_webhook_payload(self):
        """Test converting event to webhook payload format."""
        event = Event(
            event_type=EventType.NODE_UPDATE,
            origin=EventContext(event_origin="mcp", event_session_id="s1"),
            entity=EntityData(
                kind=EntityKind.NODE,
                id="node-1",
                type="Actor",
                before={"name": "Old Name"},
                after={"name": "New Name"},
                patch={"name": "New Name"},
            ),
        )

        payload = event.to_webhook_payload()

        assert payload["event_id"] == event.event_id
        assert payload["event_type"] == "node.update"
        assert "occurred_at" in payload
        assert payload["origin"]["event_origin"] == "mcp"
        assert payload["entity"]["kind"] == "node"
        assert payload["entity"]["id"] == "node-1"
        assert payload["entity"]["data"]["before"]["name"] == "Old Name"
        assert payload["entity"]["data"]["after"]["name"] == "New Name"


class TestDeliveryResult:
    """Tests for DeliveryResult model."""

    def test_create_success_result(self):
        """Test creating a success result."""
        result = DeliveryResult(
            event_id="ev-1",
            subscription_id="sub-1",
            webhook_url="https://example.com/hook",
            status=DeliveryStatus.SUCCESS,
            attempt=1,
            max_attempts=3,
            status_code=200,
            delivered_at=datetime.utcnow(),
        )

        assert result.status == DeliveryStatus.SUCCESS
        assert result.status_code == 200

    def test_create_failed_result(self):
        """Test creating a failed result."""
        result = DeliveryResult(
            event_id="ev-1",
            subscription_id="sub-1",
            webhook_url="https://example.com/hook",
            status=DeliveryStatus.FAILED,
            attempt=1,
            max_attempts=3,
            error_message="Connection refused",
        )

        assert result.status == DeliveryStatus.FAILED
        assert result.error_message == "Connection refused"


class TestSubscriptionFilters:
    """Tests for SubscriptionFilters model."""

    def test_default_filters(self):
        """Test default filter values."""
        filters = SubscriptionFilters()

        assert filters.target.entity_kind == EntityKind.NODE
        assert filters.target.node_types == []
        assert filters.operations == ["create", "update", "delete"]
        assert filters.keywords.any == []

    def test_custom_filters(self):
        """Test custom filter configuration."""
        filters = SubscriptionFilters(
            target=TargetFilters(
                entity_kind=EntityKind.NODE,
                node_types=["Actor", "Initiative"],
            ),
            operations=["create"],
            keywords=KeywordFilters(any=["AI", "digitalisering"]),
        )

        assert filters.target.node_types == ["Actor", "Initiative"]
        assert filters.operations == ["create"]
        assert "AI" in filters.keywords.any


class TestSubscriptionDelivery:
    """Tests for SubscriptionDelivery model."""

    def test_create_delivery_config(self):
        """Test creating delivery configuration."""
        delivery = SubscriptionDelivery(
            webhook_url="https://example.com/hook",
            ignore_origins=["agent:my-agent"],
            ignore_session_ids=["session-1"],
        )

        assert delivery.webhook_url == "https://example.com/hook"
        assert "agent:my-agent" in delivery.ignore_origins
        assert "session-1" in delivery.ignore_session_ids

    def test_delivery_requires_url(self):
        """Test that webhook_url is required."""
        with pytest.raises(Exception):
            SubscriptionDelivery()
