"""
Event emission helpers for GraphStorage.

These functions contain the event-building and dispatch logic extracted from
GraphStorage._emit_event and the federated-cache wrappers. All functions
receive the required state as explicit arguments so they have no dependency
on the storage object itself.
"""

from typing import TYPE_CHECKING, Dict, Any, List, Optional, Callable

from .events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
)
from .models import Node, Edge

if TYPE_CHECKING:
    from .events.dispatcher import EventDispatcher
    from .history_store import GraphHistoryStore


def emit_event(
    history_store: Optional["GraphHistoryStore"],
    system_listeners: List[Callable[[Event], None]],
    events_enabled: bool,
    event_dispatcher: Optional["EventDispatcher"],
    event_type: EventType,
    entity_kind: EntityKind,
    entity_id: str,
    entity_type: str,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    context: Optional[EventContext] = None,
) -> None:
    """
    Build and emit a graph mutation event.

    Writes to durable history first (independent of webhook delivery), then
    notifies system listeners, then optionally dispatches via the webhook
    delivery pipeline.
    """
    patch_data = None
    if before and after and event_type == EventType.NODE_UPDATE:
        patch_data = {
            key: after[key]
            for key in after
            if key not in before or before.get(key) != after.get(key)
        }

    event = Event(
        event_type=event_type,
        origin=context or EventContext(),
        entity=EntityData(
            kind=entity_kind,
            id=entity_id,
            type=entity_type,
            before=before,
            after=after,
            patch=patch_data,
        ),
    )

    # Persist to durable history first (independent of webhook delivery).
    # History is an audit trail so it must be written even when the event
    # system is disabled. Never let a history failure break the mutation.
    if history_store is not None:
        try:
            history_store.append_event(event)
        except Exception as e:
            print(f"Warning: Failed to persist mutation history: {e}")

    # Notify system listeners (always, even if events disabled for webhooks)
    for listener in system_listeners:
        try:
            listener(event)
        except Exception as e:
            print(f"Error in system listener: {e}")

    if not events_enabled or not event_dispatcher:
        print(
            f"EVENT: Skipped (events_enabled={events_enabled}, dispatcher={event_dispatcher is not None})"
        )
        return

    print(
        f"EVENT: Emitting {event_type.value} for {entity_kind.value} {entity_id} ({entity_type})"
    )

    try:
        event_dispatcher.dispatch(event)
    except Exception as e:
        print(f"Warning: Failed to dispatch event: {e}")


def emit_federated_node_event(
    emit_fn: Callable[..., None],
    operation: str,
    node_before: Optional[Node] = None,
    node_after: Optional[Node] = None,
    event_origin: str = "federation-sync",
) -> None:
    """Emit an event for a federated cache node change."""
    operation_map = {
        "create": EventType.NODE_CREATE,
        "update": EventType.NODE_UPDATE,
        "delete": EventType.NODE_DELETE,
    }
    event_type = operation_map.get(operation)
    if event_type is None:
        return

    entity_node = node_after or node_before
    if entity_node is None:
        return

    context = EventContext(event_origin=event_origin)
    emit_fn(
        event_type=event_type,
        entity_kind=EntityKind.NODE,
        entity_id=entity_node.id,
        entity_type=entity_node.type_str,
        before=node_before.to_dict() if node_before else None,
        after=node_after.to_dict() if node_after else None,
        context=context,
    )


def emit_federated_edge_event(
    emit_fn: Callable[..., None],
    operation: str,
    edge_before: Optional[Edge] = None,
    edge_after: Optional[Edge] = None,
    event_origin: str = "federation-sync",
) -> None:
    """Emit an event for a federated cache edge change."""
    operation_map = {
        "create": EventType.EDGE_CREATE,
        "update": EventType.EDGE_UPDATE
        if hasattr(EventType, "EDGE_UPDATE")
        else EventType.EDGE_CREATE,
        "delete": EventType.EDGE_DELETE,
    }
    event_type = operation_map.get(operation)
    if event_type is None:
        return

    entity_edge = edge_after or edge_before
    if entity_edge is None:
        return

    context = EventContext(event_origin=event_origin)
    emit_fn(
        event_type=event_type,
        entity_kind=EntityKind.EDGE,
        entity_id=entity_edge.id,
        entity_type=entity_edge.type_str,
        before=edge_before.to_dict() if edge_before else None,
        after=edge_after.to_dict() if edge_after else None,
        context=context,
    )
