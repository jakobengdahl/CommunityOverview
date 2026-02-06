"""
Event dispatcher for routing events to matching subscriptions.

Responsibilities:
- Load and cache subscriptions from the graph
- Filter events based on subscription configuration
- Implement loop prevention based on origin/session
- Enqueue events for delivery
"""

import logging
from typing import List, Dict, Any, Optional, Callable, TYPE_CHECKING
from datetime import datetime

from .models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    SubscriptionFilters,
    SubscriptionDelivery,
    SubscriptionInfo,
    TargetFilters,
    KeywordFilters,
)

if TYPE_CHECKING:
    from ..storage import GraphStorage

logger = logging.getLogger(__name__)


class EventDispatcher:
    """
    Dispatches graph mutation events to matching subscriptions.

    The dispatcher:
    1. Loads EventSubscription nodes from the graph
    2. Filters events based on subscription configuration
    3. Applies loop prevention rules
    4. Calls the delivery callback for matched events
    """

    def __init__(
        self,
        storage: "GraphStorage",
        on_deliver: Optional[Callable[[Event, str], None]] = None
    ):
        """
        Initialize the dispatcher.

        Args:
            storage: GraphStorage instance to load subscriptions from
            on_deliver: Callback when an event should be delivered.
                       Called with (event, webhook_url).
        """
        self._storage = storage
        self._on_deliver = on_deliver
        self._subscriptions_cache: Optional[List[Dict[str, Any]]] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl_seconds = 30  # Refresh cache every 30 seconds

    def set_delivery_callback(self, callback: Callable[[Event, str], None]) -> None:
        """Set the delivery callback."""
        self._on_deliver = callback

    def _load_subscriptions(self) -> List[Dict[str, Any]]:
        """
        Load all EventSubscription nodes from the graph.

        Returns list of subscription configs with id, name, filters, and delivery.
        """
        # Check cache
        now = datetime.utcnow()
        if (
            self._subscriptions_cache is not None
            and self._cache_time is not None
            and (now - self._cache_time).total_seconds() < self._cache_ttl_seconds
        ):
            return self._subscriptions_cache

        subscriptions = []

        # Search for EventSubscription nodes
        for node in self._storage.nodes.values():
            node_type = node.type.value if hasattr(node.type, 'value') else str(node.type)
            if node_type != "EventSubscription":
                continue

            metadata = node.metadata or {}

            # Parse filters
            filters_data = metadata.get("filters", {})
            filters = self._parse_filters(filters_data)

            # Parse delivery config
            delivery_data = metadata.get("delivery", {})
            if not delivery_data.get("webhook_url"):
                logger.warning(
                    f"EventSubscription {node.id} has no webhook_url, skipping"
                )
                continue

            delivery = self._parse_delivery(delivery_data)

            subscriptions.append({
                "id": node.id,
                "name": node.name,
                "filters": filters,
                "delivery": delivery,
            })

        # Update cache
        self._subscriptions_cache = subscriptions
        self._cache_time = now

        print(f"EVENT: Loaded {len(subscriptions)} EventSubscription(s)")
        logger.debug(f"Loaded {len(subscriptions)} EventSubscription(s)")
        return subscriptions

    def _parse_filters(self, data: Dict[str, Any]) -> SubscriptionFilters:
        """Parse filter configuration from metadata."""
        target_data = data.get("target", {})
        target = TargetFilters(
            entity_kind=EntityKind(target_data.get("entity_kind", "node")),
            node_types=target_data.get("node_types", []),
            relationship_types=target_data.get("relationship_types", []),
        )

        keywords_data = data.get("keywords", {})
        keywords = KeywordFilters(
            any=keywords_data.get("any", [])
        )

        return SubscriptionFilters(
            target=target,
            operations=data.get("operations", ["create", "update", "delete"]),
            keywords=keywords,
        )

    def _parse_delivery(self, data: Dict[str, Any]) -> SubscriptionDelivery:
        """Parse delivery configuration from metadata."""
        return SubscriptionDelivery(
            webhook_url=data["webhook_url"],
            ignore_origins=data.get("ignore_origins", []),
            ignore_session_ids=data.get("ignore_session_ids", []),
        )

    def invalidate_cache(self) -> None:
        """Invalidate the subscriptions cache."""
        self._subscriptions_cache = None
        self._cache_time = None

    def dispatch(self, event: Event) -> int:
        """
        Dispatch an event to all matching subscriptions.

        Args:
            event: The event to dispatch

        Returns:
            Number of subscriptions the event was dispatched to
        """
        if self._on_deliver is None:
            logger.warning("No delivery callback set, event will not be delivered")
            return 0

        subscriptions = self._load_subscriptions()
        dispatch_count = 0

        print(f"EVENT: Dispatching to {len(subscriptions)} subscription(s), event type: {event.event_type.value}")

        for sub in subscriptions:
            matches = self._matches(event, sub)
            print(f"EVENT: Subscription '{sub['name']}' matches={matches}")
            if matches:
                # Check loop prevention
                if self._should_block(event, sub["delivery"]):
                    logger.debug(
                        f"Event {event.event_id} blocked for subscription {sub['id']} "
                        f"(loop prevention)"
                    )
                    continue

                # Create event copy with subscription info
                event_copy = event.model_copy(deep=True)
                event_copy.subscription = SubscriptionInfo(
                    id=sub["id"],
                    name=sub["name"],
                )

                # Deliver
                try:
                    self._on_deliver(event_copy, sub["delivery"].webhook_url)
                    dispatch_count += 1
                    logger.info(
                        f"Dispatched event {event.event_id} ({event.event_type.value}) "
                        f"to subscription {sub['name']}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error dispatching event {event.event_id} to {sub['name']}: {e}"
                    )

        return dispatch_count

    def _matches(self, event: Event, subscription: Dict[str, Any]) -> bool:
        """
        Check if an event matches a subscription's filters.

        Args:
            event: The event to check
            subscription: Subscription config dict

        Returns:
            True if the event matches all filters
        """
        filters: SubscriptionFilters = subscription["filters"]

        # Match entity kind
        if event.entity.kind != filters.target.entity_kind:
            return False

        # Match operation
        operation = event.event_type.value.split(".")[-1]  # "node.create" -> "create"
        if operation not in filters.operations:
            return False

        # Match node types (if specified)
        if event.entity.kind == EntityKind.NODE:
            if filters.target.node_types:
                if event.entity.type not in filters.target.node_types:
                    return False

        # Match relationship types (if specified, for edge events)
        if event.entity.kind == EntityKind.EDGE:
            if filters.target.relationship_types:
                if event.entity.type not in filters.target.relationship_types:
                    return False

        # Match keywords (if specified)
        if filters.keywords.any:
            if not self._matches_keywords(event, filters.keywords.any):
                return False

        return True

    def _matches_keywords(self, event: Event, keywords: List[str]) -> bool:
        """
        Check if event entity matches any of the keywords.

        Matches against name, description, summary, and tags (case-insensitive).
        Uses the 'after' state for creates/updates, 'before' for deletes.
        """
        # Get the relevant entity data
        entity_data = event.entity.after or event.entity.before
        if not entity_data:
            return False

        # Build searchable text
        searchable_parts = []
        if entity_data.get("name"):
            searchable_parts.append(entity_data["name"])
        if entity_data.get("description"):
            searchable_parts.append(entity_data["description"])
        if entity_data.get("summary"):
            searchable_parts.append(entity_data["summary"])
        if entity_data.get("tags"):
            searchable_parts.extend(entity_data["tags"])

        searchable_text = " ".join(searchable_parts).lower()

        # Check if any keyword matches
        for keyword in keywords:
            if keyword.lower() in searchable_text:
                return True

        return False

    def _should_block(self, event: Event, delivery: SubscriptionDelivery) -> bool:
        """
        Check if event should be blocked based on loop prevention rules.

        Args:
            event: The event to check
            delivery: Delivery configuration with ignore rules

        Returns:
            True if the event should be blocked
        """
        # Check origin
        if event.origin.event_origin:
            if event.origin.event_origin in delivery.ignore_origins:
                return True

        # Check session ID
        if event.origin.event_session_id:
            if event.origin.event_session_id in delivery.ignore_session_ids:
                return True

        return False
