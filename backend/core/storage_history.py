"""
History read helpers for GraphStorage.

All functions are stateless — they receive the history_store (or None) as
an explicit argument and delegate directly to it.
"""

from typing import TYPE_CHECKING, Dict, Any, List, Optional

from .events.models import EntityKind

if TYPE_CHECKING:
    from .history_store import GraphHistoryStore


def get_recent_history(
    history_store: Optional["GraphHistoryStore"],
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return recent graph mutation history, newest first."""
    if history_store is None:
        return []
    return history_store.get_recent(limit=limit, offset=offset)


def get_node_history(
    history_store: Optional["GraphHistoryStore"],
    node_id: str,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return mutation history for a single node id, newest first."""
    if history_store is None:
        return []
    return history_store.get_entity_history(
        node_id, kind=EntityKind.NODE.value, limit=limit, offset=offset
    )


def get_edge_history(
    history_store: Optional["GraphHistoryStore"],
    edge_id: str,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return mutation history for a single edge id, newest first."""
    if history_store is None:
        return []
    return history_store.get_entity_history(
        edge_id, kind=EntityKind.EDGE.value, limit=limit, offset=offset
    )
