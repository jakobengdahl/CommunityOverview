"""
Tests for event dispatcher.
"""

import pytest
from unittest.mock import MagicMock, patch
from typing import List, Dict, Any

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
    SubscriptionFilters,
    SubscriptionDelivery,
    TargetFilters,
    KeywordFilters,
)
from backend.core.events.dispatcher import EventDispatcher


class MockNode:
    """Mock node for testing."""

    def __init__(self, id: str, name: str, node_type: str, metadata: Dict = None):
        self.id = id
        self.name = name
        self.type = node_type
        self.metadata = metadata or {}


class MockStorage:
    """Mock storage for testing."""

    def __init__(self, nodes: List[MockNode] = None):
        self.nodes = {n.id: n for n in (nodes or [])}


def create_subscription_node(
    id: str,
    name: str,
    node_types: List[str] = None,
    operations: List[str] = None,
    keywords: List[str] = None,
    webhook_url: str = "https://example.com/hook",
    ignore_origins: List[str] = None,
    ignore_session_ids: List[str] = None,
) -> MockNode:
    """Helper to create a subscription node for testing."""
    metadata = {
        "filters": {
            "target": {
                "entity_kind": "node",
                "node_types": node_types or [],
            },
            "operations": operations or ["create", "update", "delete"],
            "keywords": {
                "any": keywords or [],
            },
        },
        "delivery": {
            "webhook_url": webhook_url,
            "ignore_origins": ignore_origins or [],
            "ignore_session_ids": ignore_session_ids or [],
        },
    }
    return MockNode(id, name, "EventSubscription", metadata)


def create_event(
    event_type: EventType = EventType.NODE_CREATE,
    entity_id: str = "node-1",
    entity_type: str = "Actor",
    event_origin: str = None,
    event_session_id: str = None,
    before: Dict = None,
    after: Dict = None,
) -> Event:
    """Helper to create an event for testing."""
    return Event(
        event_type=event_type,
        origin=EventContext(
            event_origin=event_origin,
            event_session_id=event_session_id,
        ),
        entity=EntityData(
            kind=EntityKind.NODE,
            id=entity_id,
            type=entity_type,
            before=before,
            after=after or {"name": "Test", "type": entity_type},
        ),
    )


class TestDispatcherFiltering:
    """Tests for event filtering logic."""

    def test_matches_all_events_with_empty_filters(self):
        """Test that empty filters match all events."""
        sub = create_subscription_node("sub-1", "All Events")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(EventType.NODE_CREATE, entity_type="Actor")
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_matches_specific_node_type(self):
        """Test filtering by node type."""
        sub = create_subscription_node(
            "sub-1", "Actor Events", node_types=["Actor"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        # Should match
        event1 = create_event(EventType.NODE_CREATE, entity_type="Actor")
        dispatcher.dispatch(event1)
        assert len(delivered) == 1

        # Should not match
        event2 = create_event(EventType.NODE_CREATE, entity_type="Initiative")
        dispatcher.dispatch(event2)
        assert len(delivered) == 1  # Still 1

    def test_matches_multiple_node_types(self):
        """Test filtering by multiple node types."""
        sub = create_subscription_node(
            "sub-1", "Actor/Initiative Events",
            node_types=["Actor", "Initiative"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event1 = create_event(entity_type="Actor")
        event2 = create_event(entity_type="Initiative")
        event3 = create_event(entity_type="Resource")

        dispatcher.dispatch(event1)
        dispatcher.dispatch(event2)
        dispatcher.dispatch(event3)

        assert len(delivered) == 2  # Actor and Initiative only

    def test_matches_specific_operations(self):
        """Test filtering by operation type."""
        sub = create_subscription_node(
            "sub-1", "Create Only", operations=["create"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event_create = create_event(EventType.NODE_CREATE)
        event_update = create_event(EventType.NODE_UPDATE)
        event_delete = create_event(EventType.NODE_DELETE)

        dispatcher.dispatch(event_create)
        dispatcher.dispatch(event_update)
        dispatcher.dispatch(event_delete)

        assert len(delivered) == 1  # Only create


class TestKeywordMatching:
    """Tests for keyword matching in events."""

    def test_matches_keyword_in_name(self):
        """Test keyword matching in name field."""
        sub = create_subscription_node(
            "sub-1", "AI Events", keywords=["AI"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={"name": "AI Strategy", "type": "Initiative"})
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_matches_keyword_in_description(self):
        """Test keyword matching in description field."""
        sub = create_subscription_node(
            "sub-1", "AI Events", keywords=["artificiell"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={
            "name": "Tech Project",
            "description": "Projekt om artificiell intelligens",
            "type": "Initiative"
        })
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_keyword_matching_is_case_insensitive(self):
        """Test that keyword matching is case insensitive."""
        sub = create_subscription_node(
            "sub-1", "AI Events", keywords=["digitalisering"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={
            "name": "DIGITALISERING Initiativ",
            "type": "Initiative"
        })
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_no_match_without_keyword(self):
        """Test that events without keywords don't match."""
        sub = create_subscription_node(
            "sub-1", "AI Events", keywords=["blockchain"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={"name": "AI Project", "type": "Initiative"})
        dispatcher.dispatch(event)

        assert len(delivered) == 0


class TestLoopPrevention:
    """Tests for loop prevention logic."""

    def test_blocks_ignored_origin(self):
        """Test that events from ignored origins are blocked."""
        sub = create_subscription_node(
            "sub-1", "Subscription",
            ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_origin="agent:my-agent")
        dispatcher.dispatch(event)

        assert len(delivered) == 0

    def test_allows_non_ignored_origin(self):
        """Test that events from non-ignored origins pass."""
        sub = create_subscription_node(
            "sub-1", "Subscription",
            ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_origin="web-ui")
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_blocks_ignored_session_id(self):
        """Test that events with ignored session IDs are blocked."""
        sub = create_subscription_node(
            "sub-1", "Subscription",
            ignore_session_ids=["session-123"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_session_id="session-123")
        dispatcher.dispatch(event)

        assert len(delivered) == 0

    def test_allows_events_without_origin(self):
        """Test that events without origin info are allowed (PoC default)."""
        sub = create_subscription_node(
            "sub-1", "Subscription",
            ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()  # No origin
        dispatcher.dispatch(event)

        assert len(delivered) == 1


class TestMultipleSubscriptions:
    """Tests for dispatching to multiple subscriptions."""

    def test_dispatches_to_multiple_matching_subscriptions(self):
        """Test that events are dispatched to all matching subscriptions."""
        sub1 = create_subscription_node(
            "sub-1", "All Events", webhook_url="https://hook1.com"
        )
        sub2 = create_subscription_node(
            "sub-2", "All Events Too", webhook_url="https://hook2.com"
        )
        storage = MockStorage([sub1, sub2])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()
        count = dispatcher.dispatch(event)

        assert count == 2
        assert len(delivered) == 2
        urls = [u for _, u in delivered]
        assert "https://hook1.com" in urls
        assert "https://hook2.com" in urls

    def test_subscription_info_included_in_event(self):
        """Test that subscription info is added to dispatched events."""
        sub = create_subscription_node("sub-1", "My Subscription")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        delivered_event = delivered[0][0]
        assert delivered_event.subscription is not None
        assert delivered_event.subscription.id == "sub-1"
        assert delivered_event.subscription.name == "My Subscription"

    def test_internal_urls_skipped_for_webhook_delivery(self):
        """Test that internal:// URLs are not sent to webhook delivery callback."""
        sub = create_subscription_node(
            "sub-1", "Internal Agent", webhook_url="internal://agent/agent-1"
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        # Simulate agent delivery failing or not being handled
        dispatcher.set_agent_delivery_callback(lambda e, s: False)

        event = create_event()
        dispatcher.dispatch(event)

        # Should be skipped because URL starts with internal://
        assert len(delivered) == 0
