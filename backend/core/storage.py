"""
Graph storage with NetworkX and JSON persistence
Handles all CRUD operations on the graph

This module is part of graph_core - the core graph storage layer.
It provides the main GraphStorage class for persisting and querying the graph.

Concurrency Safety (PoC level):
- Uses threading.RLock for in-memory data structure protection
- Uses file locking (fcntl on Unix, msvcrt on Windows) for file access
- Implements atomic writes via temp file + rename
- Suitable for multiple concurrent users in a single-process deployment

Event System:
- Mutations emit events that can be delivered to webhooks
- EventSubscription nodes in the graph define webhook targets
- Event context (origin, session_id) enables loop prevention
"""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Dict, Optional, Any, TYPE_CHECKING, Callable
from datetime import datetime, timezone
import networkx as nx
from pathlib import Path

from .models import (
    Node,
    Edge,
    NodeType,
    RelationshipType,
    SimilarNode,
    GraphStats,
    AddNodesResult,
    DeleteNodesResult,
    DeleteEdgesResult,
    _parse_datetime,
)
from .storage_backends import FileGraphPersistenceBackend, GraphPersistenceBackend
from .vector_store import VectorStore
from . import storage_search
from . import storage_history
from . import storage_events

# Event system imports
from .events.models import EventType, EntityKind, EventContext

if TYPE_CHECKING:
    from .events.dispatcher import EventDispatcher
    from .events.delivery import DeliveryWorker
    from .events.models import Event
    from .history_store import GraphHistoryStore


class StaleUpdateError(Exception):
    """Raised when an optimistic-concurrency guard on update_node fails.

    The caller passed ``expected_updated_at`` but the node's live
    ``updated_at`` no longer matches it, meaning the node changed since the
    caller read it. The write is rejected instead of silently clobbering the
    concurrent change. ``current_updated_at`` carries the live value so the
    caller can re-read and retry.
    """

    def __init__(self, node_id: str, expected: Any, current: datetime):
        self.node_id = node_id
        self.expected = expected
        self.current_updated_at = current
        super().__init__(
            f"Node '{node_id}' was modified since expected_updated_at="
            f"{expected!r}; current updated_at is {current.isoformat()!r}"
        )


def _apply_metadata_patch(
    base: Dict[str, Any], patch: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge ``patch`` onto ``base`` at the top level (JSON-Merge-Patch style).

    Each key in ``patch`` replaces that key in ``base``; a key whose patch value
    is ``None`` is removed from the result (RFC 7386 null-means-delete). Keys not
    mentioned in ``patch`` are preserved. Nested objects are replaced wholesale,
    not merged recursively — the merge is intentionally top-level only.
    """
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


class GraphStorage:
    """
    Manages graph storage with NetworkX + JSON persistence.

    Thread-safety:
    - All public methods that modify state are protected by _lock (threading.RLock)
    - File operations use OS-level file locking for multi-process safety
    - Writes are atomic (temp file + rename) to prevent corruption
    """

    def __init__(
        self,
        json_path: str = "graph.json",
        embeddings_path: str = None,
        persistence_backend: Optional[GraphPersistenceBackend] = None,
        history_max_events: Optional[int] = None,
        history_max_age_days: Optional[float] = None,
        history_compaction_interval: Optional[int] = None,
    ):
        """
        Initialize GraphStorage.

        Args:
            json_path: Path to the JSON file for graph persistence when using the
                default file-backed persistence backend.
            embeddings_path: Path to the embeddings pickle file (Legacy/Deprecated).
                           New implementation stores embeddings in graph.json directly.
            persistence_backend: Optional persistence backend adapter. Defaults to
                the file-backed JSON backend to preserve standalone behavior.
            history_max_events: Optional cap on retained history records. When set
                (> 0), the history sidecar is compacted down to the newest N
                records. Default None keeps unbounded history (current behaviour).
            history_max_age_days: Optional cap on history record age in days. When
                set (> 0), records older than the cutoff are dropped on compaction.
                Default None applies no age-based retention.
            history_compaction_interval: Optional number of appends between
                compaction passes (throttle). Defaults to an amortised value when
                retention is enabled; ignored when retention is disabled.
        """
        self._persistence_backend = persistence_backend or FileGraphPersistenceBackend(
            json_path
        )
        self.json_path = getattr(
            self._persistence_backend, "json_path", Path(json_path)
        )

        # Durable append-only mutation history sidecar. Only enabled for the
        # file-backed standalone backend, which owns a concrete json_path next
        # to which the history NDJSON lives. Non-file backends own their own
        # persistence and history strategy, so we leave this None there.
        self._history_max_events = history_max_events
        self._history_max_age_days = history_max_age_days
        self._history_compaction_interval = history_compaction_interval
        self._history_store = self._init_history_store()

        # Thread lock for in-memory data structure protection
        # RLock allows same thread to acquire lock multiple times (reentrant)
        self._lock = threading.RLock()

        # Executor for background I/O operations (saving to disk)
        # Using max_workers=1 to ensure sequential writes
        self._io_executor = ThreadPoolExecutor(max_workers=1)

        # We initialize VectorStore without a storage path as it now holds state in memory
        # and relies on GraphStorage for persistence via graph.json
        self.vector_store = VectorStore()
        self.vector_store.preload_model()  # Start loading embedding model in background

        self.graph = (
            nx.MultiDiGraph()
        )  # MultiDiGraph allows multiple edges between same nodes
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.edges: Dict[str, Edge] = {}  # edge_id -> Edge

        # Cache for searchable text to speed up search_nodes
        self._searchable_text_cache: Dict[str, str] = {}

        # Cache: node_type_key -> "typeName label1 label2 ..." (lowercased)
        self._type_searchable_text: Dict[str, str] = {}
        self._build_type_searchable_text()

        self.graph_metadata: Dict[str, Any] = {
            "version": "1.0",
            "graph_name": self._default_graph_name(),
        }

        # Event system (initialized lazily via setup_events())
        self._event_dispatcher: Optional["EventDispatcher"] = None
        self._delivery_worker: Optional["DeliveryWorker"] = None
        self._events_enabled = False
        self._system_listeners: List[Callable[["Event"], None]] = []

        self.load()

    def _default_graph_name(self) -> str:
        """Return the backend-specific default graph name."""
        return self._persistence_backend.default_graph_name()

    def _init_history_store(self) -> Optional["GraphHistoryStore"]:
        """Create the append-only history sidecar for file-backed standalone mode."""
        if not isinstance(self._persistence_backend, FileGraphPersistenceBackend):
            return None
        from .history_store import GraphHistoryStore

        history_path = self.json_path.with_name(self.json_path.stem + ".history.ndjson")
        return GraphHistoryStore(
            history_path,
            max_events=self._history_max_events,
            max_age_days=self._history_max_age_days,
            compaction_interval=self._history_compaction_interval,
        )

    def _build_type_searchable_text(self) -> None:
        """Build a lookup of node type -> searchable text including localized labels."""
        try:
            from backend.config.config_loader import get_schema

            schema = get_schema()
            for type_name, type_config in schema.get("node_types", {}).items():
                labels = type_config.get("labels", {})
                label_values = " ".join(labels.values()) if labels else ""
                self._type_searchable_text[type_name] = (
                    f"{type_name} {label_values}".lower().strip()
                )
        except Exception:
            # Config not available; type matching will use type name only
            pass

    def _build_searchable_text(self, node: "Node") -> str:
        return storage_search.build_searchable_text(node, self._type_searchable_text)

    def add_system_listener(self, listener: Callable[["Event"], None]) -> None:
        """
        Add a system-level event listener.
        This listener receives all events directly, bypassing filters/subscriptions.
        Used for internal system components like the Agent Registry.
        """
        with self._lock:
            self._system_listeners.append(listener)

    def setup_events(
        self,
        enabled: bool = True,
        max_attempts: int = 3,
        backoff_times: Optional[List[float]] = None,
    ) -> None:
        """
        Initialize the event system for webhook delivery.

        This must be called after the graph is loaded to enable event delivery.
        Events are dispatched to EventSubscription nodes in the graph.

        Args:
            enabled: Whether to enable event delivery
            max_attempts: Maximum delivery attempts per event
            backoff_times: Wait times between retries (seconds)
        """
        if not enabled:
            self._events_enabled = False
            return

        # Import here to avoid circular imports
        from .events.dispatcher import EventDispatcher
        from .events.delivery import DeliveryWorker

        # Create delivery worker
        self._delivery_worker = DeliveryWorker(
            max_attempts=max_attempts,
            backoff_times=backoff_times,
        )
        self._delivery_worker.start()

        # Create dispatcher with delivery callback
        self._event_dispatcher = EventDispatcher(
            storage=self,
            on_deliver=self._delivery_worker.enqueue,
        )

        self._events_enabled = True
        print(f"Event system initialized with max_attempts={max_attempts}")

    def set_agent_delivery_callback(
        self,
        callback: Callable[["Event", str], bool],
    ) -> None:
        """
        Set the callback for agent event delivery.

        This allows the agent registry to receive events for agent-linked
        subscriptions directly, bypassing webhook delivery.

        Args:
            callback: Function that receives (event, subscription_id) and
                     returns True if handled by an agent, False otherwise.
        """
        if self._event_dispatcher:
            self._event_dispatcher.set_agent_delivery_callback(callback)

    def shutdown_events(self) -> None:
        """Shutdown the event system and I/O executor gracefully."""
        if self._delivery_worker:
            self._delivery_worker.stop(wait=True)
            self._delivery_worker = None

        self._event_dispatcher = None
        self._events_enabled = False

        # Shut down I/O executor and wait for pending saves
        self._io_executor.shutdown(wait=True)

    def _emit_event(
        self,
        event_type: EventType,
        entity_kind: EntityKind,
        entity_id: str,
        entity_type: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        context: Optional[EventContext] = None,
    ) -> None:
        """Emit a graph mutation event.  Delegates to storage_events."""
        storage_events.emit_event(
            self._history_store,
            self._system_listeners,
            self._events_enabled,
            self._event_dispatcher,
            event_type,
            entity_kind,
            entity_id,
            entity_type,
            before=before,
            after=after,
            context=context,
        )

    def emit_federated_node_event(
        self,
        operation: str,
        node_before: Optional[Node] = None,
        node_after: Optional[Node] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache changes so subscriptions can react."""
        storage_events.emit_federated_node_event(
            self._emit_event, operation, node_before, node_after, event_origin
        )

    def emit_federated_edge_event(
        self,
        operation: str,
        edge_before: Optional[Edge] = None,
        edge_after: Optional[Edge] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache edge changes."""
        storage_events.emit_federated_edge_event(
            self._emit_event, operation, edge_before, edge_after, event_origin
        )

    def get_recent_history(
        self, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return recent graph mutation history, newest first.  Delegates to storage_history."""
        return storage_history.get_recent_history(self._history_store, limit, offset)

    def get_node_history(
        self, node_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return mutation history for a single node id, newest first."""
        return storage_history.get_node_history(
            self._history_store, node_id, limit, offset
        )

    def get_edge_history(
        self, edge_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return mutation history for a single edge id, newest first."""
        return storage_history.get_edge_history(
            self._history_store, edge_id, limit, offset
        )

    def load(self) -> None:
        """
        Load graph from the configured persistence backend.

        Thread-safe: Uses lock for in-memory updates.
        """
        with self._lock:
            if not self._persistence_backend.exists():
                print(
                    f"No graph file found at {self.json_path}, creating new empty graph"
                )
                # Wait for the initial file write to complete so callers can
                # immediately open the file (e.g. creating a second storage instance).
                self.save().result()
                return

            try:
                data = self._persistence_backend.load_graph_data()

                metadata = data.get("metadata") if isinstance(data, dict) else None
                if isinstance(metadata, dict):
                    self.graph_metadata = {
                        "version": metadata.get("version", "1.0"),
                        "graph_name": metadata.get("graph_name")
                        or self._default_graph_name(),
                        **{
                            k: v
                            for k, v in metadata.items()
                            if k not in {"version", "graph_name"}
                        },
                    }
                else:
                    self.graph_metadata = {
                        "version": "1.0",
                        "graph_name": self._default_graph_name(),
                    }

                # Clear existing data
                self.nodes.clear()
                self.edges.clear()
                self.graph.clear()

                # Clear searchable text cache
                self._searchable_text_cache.clear()

                # Load nodes
                for node_data in data.get("nodes", []):
                    node = Node.from_dict(node_data)
                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)

                    # Precompute searchable text
                    self._searchable_text_cache[node.id] = self._build_searchable_text(
                        node
                    )

                # Load edges
                for edge_data in data.get("edges", []):
                    edge = Edge.from_dict(edge_data)
                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source, edge.target, key=edge.id, data=edge
                    )

                # Rebuild vector store index from loaded nodes
                self.vector_store.rebuild_index(list(self.nodes.values()))

                print(
                    f"Loaded {len(self.nodes)} nodes and {len(self.edges)} edges from {self.json_path}"
                )

            except Exception as e:
                print(f"Error loading graph: {e}")
                raise

    def save(self) -> "Future[None]":
        """
        Save graph through the configured persistence backend.

        Captures graph state while holding the lock, then offloads the actual
        file I/O to a background thread to prevent blocking the event loop.
        Returns the Future representing the background write — callers that
        must know when the write completes can call .result() on it.

        Thread-safe: Uses lock for reading in-memory data.
        """
        with self._lock:
            data = {
                "nodes": [node.to_dict() for node in self.nodes.values()],
                "edges": [edge.to_dict() for edge in self.edges.values()],
                "metadata": {
                    **(self.graph_metadata or {}),
                    "version": (self.graph_metadata or {}).get("version", "1.0"),
                    "graph_name": (self.graph_metadata or {}).get(
                        "graph_name", self._default_graph_name()
                    ),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                },
            }
            node_count = len(self.nodes)
            edge_count = len(self.edges)

        # Offload blocking I/O to background thread to avoid blocking event loop.
        # Returns the Future so callers that must wait (e.g. load()) can call .result().
        return self._io_executor.submit(
            self._do_save_to_disk, data, node_count, edge_count
        )

    def _do_save_to_disk(
        self, data: Dict[str, Any], node_count: int, edge_count: int
    ) -> None:
        """
        Internal method: delegate serialized graph data to the persistence backend.
        Runs in the background executor so file I/O doesn't block the event loop.

        Exceptions are intentionally re-raised so the ThreadPoolExecutor stores
        them in the returned Future. Callers that call .result() (e.g. load())
        will then receive the exception rather than a silent no-op.
        """
        try:
            self._persistence_backend.save_graph_data(data)
            print(
                f"Saved {node_count} nodes and {edge_count} edges to {self.json_path}"
            )
        except Exception as e:
            print(f"Error saving graph to disk: {e}")
            raise

    def flush(self) -> None:
        """
        Wait for any pending background save operations to complete.

        Useful in tests and in code that reloads from disk immediately
        after mutating the graph.

        Correctness relies on max_workers=1 (FIFO task ordering). The no-op
        submitted here will only run after all previously submitted saves.
        """
        # Submit a no-op sentinel and block until it runs — drains the queue.
        self._io_executor.submit(lambda: None).result()

    def get_graph_name(self) -> str:
        """Return configured graph name from graph metadata."""
        name = (self.graph_metadata or {}).get("graph_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return self._default_graph_name()

    def reload(self) -> None:
        """
        Reload graph from disk, discarding any in-memory changes.

        Useful for refreshing state after external modifications.
        """
        self.load()

    def _score_node_match(self, node: "Node", query_lower: str) -> int:
        return storage_search.score_node_match(
            node, query_lower, self._type_searchable_text
        )

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        limit: int = 50,
        include_archived: bool = False,
        match_mode: str = storage_search.MATCH_MODE_SUBSTRING,
    ) -> List[Node]:
        """Search nodes based on text query.  Delegates to storage_search."""
        return storage_search.search_nodes(
            self.nodes,
            self._searchable_text_cache,
            self._type_searchable_text,
            query,
            node_types,
            limit,
            include_archived=include_archived,
            match_mode=match_mode,
        )

    def semantic_search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        limit: int = 50,
        threshold: float = storage_search.DEFAULT_SEMANTIC_THRESHOLD,
        include_archived: bool = False,
    ) -> List[Node]:
        """Embedding-ranked search over nodes.  Delegates to storage_search.

        Reuses the vector-store path shared with find_similar_nodes; returns
        an empty list when embeddings are unavailable (ML-free install).
        """
        return storage_search.semantic_search_nodes(
            self.nodes,
            self.vector_store,
            query,
            node_types,
            limit,
            threshold,
            include_archived=include_archived,
        )

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a specific node"""
        return self.nodes.get(node_id)

    def get_all_nodes(self) -> List[Node]:
        """Get all nodes in the graph"""
        return list(self.nodes.values())

    def get_all_edges(self) -> List[Edge]:
        """Get all edges in the graph"""
        return list(self.edges.values())

    def _validate_edge_applicability(self, edge: Edge) -> None:
        source_node = self.nodes.get(edge.source)
        target_node = self.nodes.get(edge.target)
        if source_node is None or target_node is None:
            return

        from backend.config import config_loader

        decision = config_loader.relationship_type_allows_node_types(
            edge.type_str, source_node.type_str, target_node.type_str
        )
        if not decision.get("allowed"):
            raise ValueError(decision.get("message") or "Relationship type not allowed")

    def audit_relationship_applicability(self) -> List[Dict[str, Any]]:
        """Return existing edges that violate configured relationship applicability."""
        violations = []
        for edge in self.edges.values():
            source_node = self.nodes.get(edge.source)
            target_node = self.nodes.get(edge.target)
            if source_node is None or target_node is None:
                violations.append(
                    {
                        "edge_id": edge.id,
                        "type": edge.type_str,
                        "source": edge.source,
                        "target": edge.target,
                        "source_type": source_node.type_str if source_node else None,
                        "target_type": target_node.type_str if target_node else None,
                        "message": "Edge references a missing source or target node.",
                    }
                )
                continue
            try:
                self._validate_edge_applicability(edge)
            except ValueError as exc:
                violations.append(
                    {
                        "edge_id": edge.id,
                        "type": edge.type_str,
                        "source": edge.source,
                        "target": edge.target,
                        "source_type": source_node.type_str,
                        "target_type": target_node.type_str,
                        "message": str(exc),
                    }
                )
        return violations

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        depth: int = 1,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        """Get nodes connected to the given node.  Delegates to storage_search."""
        return storage_search.get_related_nodes(
            self.nodes,
            self.edges,
            self.graph,
            node_id,
            relationship_types,
            depth,
            include_archived=include_archived,
        )

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> List[SimilarNode]:
        """Find similar nodes by name (Levenshtein + vector).  Delegates to storage_search."""
        return storage_search.find_similar_nodes(
            self.nodes, self.vector_store, name, node_type, threshold, limit
        )

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, List[SimilarNode]]:
        """Batch variant of find_similar_nodes.  Delegates to storage_search."""
        return storage_search.find_similar_nodes_batch(
            self.nodes, self.vector_store, names, node_type, threshold, limit
        )

    def add_nodes(
        self,
        nodes: List[Node],
        edges: List[Edge],
        event_context: Optional[EventContext] = None,
    ) -> AddNodesResult:
        """
        Add nodes and edges.
        Validates and saves to JSON.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            nodes: List of nodes to add
            edges: List of edges to add
            event_context: Optional context for event tracking and loop prevention
        """
        with self._lock:
            added_node_ids = []
            added_edge_ids = []

            try:
                # Add nodes
                nodes_to_embed = []
                for node in nodes:
                    if node.id in self.nodes:
                        return AddNodesResult(
                            added_node_ids=[],
                            added_edge_ids=[],
                            success=False,
                            message=f"Node with ID {node.id} already exists",
                        )

                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)
                    added_node_ids.append(node.id)
                    nodes_to_embed.append(node)

                    # Precompute searchable text
                    self._searchable_text_cache[node.id] = self._build_searchable_text(
                        node
                    )

                # Generate embeddings for new nodes (non-blocking)
                if nodes_to_embed:
                    try:
                        self.vector_store.update_nodes_embeddings(nodes_to_embed)
                    except Exception as embed_error:
                        # Embedding generation is optional - log but don't fail
                        print(f"Warning: Could not generate embeddings: {embed_error}")

                # Save again to persist embeddings generated above
                self.save()

                # Create name-to-ID mapping for newly added nodes and existing nodes
                name_to_id = {}
                for node_id, node in self.nodes.items():
                    name_to_id[node.name] = node_id

                # Add edges
                for edge in edges:
                    # Resolve source and target - they might be names or IDs
                    source_id = edge.source
                    target_id = edge.target

                    # If source is not a valid ID, try to resolve it as a name
                    if source_id not in self.nodes:
                        if source_id in name_to_id:
                            source_id = name_to_id[source_id]
                        else:
                            raise ValueError(
                                f"Source node '{edge.source}' does not exist (not found by ID or name)"
                            )

                    # If target is not a valid ID, try to resolve it as a name
                    if target_id not in self.nodes:
                        if target_id in name_to_id:
                            target_id = name_to_id[target_id]
                        else:
                            raise ValueError(
                                f"Target node '{edge.target}' does not exist (not found by ID or name)"
                            )

                    # Update edge with resolved IDs
                    edge.source = source_id
                    edge.target = target_id

                    if edge.id in self.edges:
                        return AddNodesResult(
                            added_node_ids=[],
                            added_edge_ids=[],
                            success=False,
                            message=f"Edge with ID {edge.id} already exists",
                        )

                    self._validate_edge_applicability(edge)

                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source, edge.target, key=edge.id, data=edge
                    )
                    added_edge_ids.append(edge.id)

                # Save to JSON
                self.save()

                # Emit events for added nodes
                for node_id in added_node_ids:
                    node = self.nodes.get(node_id)
                    if node:
                        node_type = (
                            node.type.value
                            if hasattr(node.type, "value")
                            else str(node.type)
                        )
                        self._emit_event(
                            event_type=EventType.NODE_CREATE,
                            entity_kind=EntityKind.NODE,
                            entity_id=node_id,
                            entity_type=node_type,
                            before=None,
                            after=node.to_dict(),
                            context=event_context,
                        )

                # Emit events for added edges
                for edge_id in added_edge_ids:
                    edge = self.edges.get(edge_id)
                    if edge:
                        edge_type = (
                            edge.type.value
                            if hasattr(edge.type, "value")
                            else str(edge.type)
                        )
                        self._emit_event(
                            event_type=EventType.EDGE_CREATE,
                            entity_kind=EntityKind.EDGE,
                            entity_id=edge_id,
                            entity_type=edge_type,
                            before=None,
                            after=edge.to_dict(),
                            context=event_context,
                        )

                return AddNodesResult(
                    added_node_ids=added_node_ids,
                    added_edge_ids=added_edge_ids,
                    success=True,
                    message=f"Added {len(added_node_ids)} nodes and {len(added_edge_ids)} edges",
                )

            except Exception as e:
                return AddNodesResult(
                    added_node_ids=[],
                    added_edge_ids=[],
                    success=False,
                    message=f"Error during add: {str(e)}",
                )

    def update_node(
        self,
        node_id: str,
        updates: Dict,
        event_context: Optional[EventContext] = None,
        *,
        metadata_merge: bool = False,
        expected_updated_at: Optional[Any] = None,
    ) -> Optional[Node]:
        """
        Update an existing node.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update
            event_context: Optional context for event tracking and loop prevention
            metadata_merge: When True, ``metadata`` (and any schema-defined extra
                fields folded into metadata) is deep-merged at the top level onto
                the node's existing metadata instead of replacing it wholesale.
                A key whose value is ``None`` deletes that key (RFC 7386
                null-means-delete). Keys not mentioned are preserved. Default is
                False, i.e. the legacy replace-whole-metadata behaviour.
            expected_updated_at: Optional optimistic-concurrency guard. When set
                (an ISO 8601 string — as returned in a node's ``updated_at`` — or
                a timezone-aware datetime), the update only proceeds if the node's
                current ``updated_at`` still equals this value; a mismatch raises
                :class:`StaleUpdateError` instead of clobbering a concurrent write.
                Note ``updated_at`` is a UTC wall-clock timestamp used as the
                version token: two writes within the same microsecond tick would
                share a timestamp, so this guards against realistic interleaving,
                not adversarial same-tick races.

        Raises:
            StaleUpdateError: if ``expected_updated_at`` no longer matches the
                node's live ``updated_at``.
            ValueError: if the resulting node fails model validation.
        """
        with self._lock:
            if node_id not in self.nodes:
                return None

            node = self.nodes[node_id]

            # Optimistic-concurrency guard: reject the write if the node changed
            # since the caller read it. Checked inside the lock so the compare and
            # the subsequent mutation are atomic with respect to other writers.
            if expected_updated_at is not None:
                expected = (
                    _parse_datetime(expected_updated_at)
                    if isinstance(expected_updated_at, str)
                    else expected_updated_at
                )
                if node.updated_at != expected:
                    raise StaleUpdateError(
                        node_id, expected_updated_at, node.updated_at
                    )

            # Capture before state for events
            before_state = node.to_dict()

            # Update allowed fields
            allowed_fields = {
                "name",
                "description",
                "summary",
                "tags",
                "subtypes",
                "aliases",
                "metadata",
            }
            # "archived" is reserved so a generic update can neither set it directly
            # nor fold it into metadata — archiving goes through set_nodes_archived.
            reserved_fields = {
                "id",
                "type",
                "embedding",
                "created_at",
                "updated_at",
                "archived",
            }

            # Build the candidate state and validate it through the model BEFORE
            # mutating the live node. setattr on a pydantic model does not re-run
            # field validators, so without this an over-limit field (e.g. a
            # description longer than 2000 chars, or oversized tags/aliases) would
            # silently bypass the model's limits on write and only fail at the next
            # graph load — bricking startup. Validating a candidate first means an
            # invalid update is rejected here, atomically, with the live node untouched.
            candidate = node.to_dict()
            for key, value in updates.items():
                if key in allowed_fields and key != "metadata":
                    candidate[key] = value

            # Schema-defined extra fields (anything outside the base model) fold
            # into metadata, same as an explicit `metadata` update.
            extra = {
                k: v
                for k, v in updates.items()
                if k not in allowed_fields and k not in reserved_fields
            }

            if metadata_merge:
                # Opt-in field-level merge: patch existing metadata top-level.
                merged = dict(candidate.get("metadata") or {})
                if "metadata" in updates:
                    meta_update = updates["metadata"] or {}
                    if not isinstance(meta_update, dict):
                        raise ValueError(
                            "metadata must be an object when metadata_merge is set"
                        )
                    merged = _apply_metadata_patch(merged, meta_update)
                if extra:
                    merged = _apply_metadata_patch(merged, extra)
                candidate["metadata"] = merged
            else:
                # Legacy/default: an explicit `metadata` replaces it wholesale.
                # Assign the value as-is (no None->{} coercion) so a bad
                # metadata=None with no extra fields is rejected by model
                # validation below, exactly as before merge mode existed. When an
                # extra field is present the fold step normalizes None to {},
                # also matching the prior behaviour.
                if "metadata" in updates:
                    candidate["metadata"] = updates["metadata"]
                if extra:
                    meta = dict(candidate.get("metadata") or {})
                    meta.update(extra)
                    candidate["metadata"] = meta

            try:
                validated = Node.from_dict(candidate)
            except Exception as exc:
                raise ValueError(f"Invalid node update: {exc}") from exc

            # Apply the validated field values to the live node.
            for key in allowed_fields:
                setattr(node, key, getattr(validated, key))

            node.updated_at = datetime.now(timezone.utc)

            # Update in graph
            self.graph.nodes[node_id]["data"] = node

            # Update searchable text cache
            self._searchable_text_cache[node.id] = self._build_searchable_text(node)

            # Update embedding if text fields or tags changed (non-blocking)
            if any(
                k in updates
                for k in ["name", "description", "summary", "tags", "aliases"]
            ):
                try:
                    self.vector_store.update_node_embedding(node)
                except Exception as embed_error:
                    print(f"Warning: Could not update embedding: {embed_error}")

            # Save
            self.save()

            # Emit update event
            node_type = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            self._emit_event(
                event_type=EventType.NODE_UPDATE,
                entity_kind=EntityKind.NODE,
                entity_id=node_id,
                entity_type=node_type,
                before=before_state,
                after=node.to_dict(),
                context=event_context,
            )

            return node

    def set_nodes_archived(
        self,
        node_ids: List[str],
        archived: bool,
        event_context: Optional[EventContext] = None,
    ) -> List[Node]:
        """Set the ``archived`` flag on the given nodes.

        Archiving hides a node from search/traversal by default while keeping it
        (and its edges) in the graph, unlike deletion which is permanent. Returns
        every node that was found (whether or not its state changed) so callers
        get an idempotent view; only nodes whose flag actually changed emit an
        update event, and the graph is saved once if anything changed.

        Thread-safe: protected by _lock for the entire operation.
        """
        with self._lock:
            found: List[Node] = []
            changed = False
            for node_id in node_ids:
                node = self.nodes.get(node_id)
                if node is None:
                    continue
                found.append(node)
                if node.archived == archived:
                    continue

                before_state = node.to_dict()
                node.archived = archived
                node.updated_at = datetime.now(timezone.utc)
                self.graph.nodes[node_id]["data"] = node
                changed = True

                node_type = (
                    node.type.value if hasattr(node.type, "value") else str(node.type)
                )
                self._emit_event(
                    event_type=EventType.NODE_UPDATE,
                    entity_kind=EntityKind.NODE,
                    entity_id=node_id,
                    entity_type=node_type,
                    before=before_state,
                    after=node.to_dict(),
                    context=event_context,
                )

            if changed:
                self.save()

            return found

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False,
        event_context: Optional[EventContext] = None,
    ) -> DeleteNodesResult:
        """
        Delete nodes (max 10 at a time for safety).
        Requires confirmed=True.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion
            event_context: Optional context for event tracking and loop prevention
        """
        with self._lock:
            if len(node_ids) > 10:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message="Max 10 nodes can be deleted at a time. Contact admin for bulk deletion.",
                )

            if not confirmed:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message="Deletion requires confirmed=True parameter",
                )

            deleted_node_ids = []
            affected_edge_ids = []

            # Capture before states for events
            node_before_states: Dict[str, Dict[str, Any]] = {}
            edge_before_states: Dict[str, Dict[str, Any]] = {}

            try:
                for node_id in node_ids:
                    if node_id not in self.nodes:
                        continue

                    # Capture node before state
                    node = self.nodes[node_id]
                    node_before_states[node_id] = node.to_dict()

                    # Find all edges connected to this node
                    edges_to_remove = []
                    for edge_id, edge in self.edges.items():
                        if edge.source == node_id or edge.target == node_id:
                            edges_to_remove.append(edge_id)
                            affected_edge_ids.append(edge_id)
                            # Capture edge before state
                            if edge_id not in edge_before_states:
                                edge_before_states[edge_id] = edge.to_dict()

                    # Remove edges
                    for edge_id in edges_to_remove:
                        edge = self.edges[edge_id]
                        self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                        del self.edges[edge_id]

                    # Remove node
                    self.graph.remove_node(node_id)
                    del self.nodes[node_id]
                    self._searchable_text_cache.pop(node_id, None)
                    deleted_node_ids.append(node_id)

                # Remove embeddings
                self.vector_store.remove_nodes_embeddings(deleted_node_ids)

                # Save
                self.save()

                # Emit delete events for edges (before nodes, to maintain referential integrity info)
                for edge_id, before_state in edge_before_states.items():
                    edge_type = before_state.get("type", "RELATES_TO")
                    self._emit_event(
                        event_type=EventType.EDGE_DELETE,
                        entity_kind=EntityKind.EDGE,
                        entity_id=edge_id,
                        entity_type=edge_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                # Emit delete events for nodes
                for node_id, before_state in node_before_states.items():
                    node_type = before_state.get("type", "Unknown")
                    self._emit_event(
                        event_type=EventType.NODE_DELETE,
                        entity_kind=EntityKind.NODE,
                        entity_id=node_id,
                        entity_type=node_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                return DeleteNodesResult(
                    deleted_node_ids=deleted_node_ids,
                    affected_edge_ids=affected_edge_ids,
                    success=True,
                    message=f"Deleted {len(deleted_node_ids)} nodes and {len(affected_edge_ids)} edges",
                )

            except Exception as e:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message=f"Error during deletion: {str(e)}",
                )

    def get_node_count(self) -> int:
        """Total node count, for callers that only need a count and should not
        pay for building the full per-type breakdown get_stats() computes."""
        return len(self.nodes)

    def get_stats(self) -> GraphStats:
        """Get statistics for the graph"""
        # Count nodes per type
        nodes_by_type = {}
        for node in self.nodes.values():
            type_name = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            nodes_by_type[type_name] = nodes_by_type.get(type_name, 0) + 1

        # Count edges per type, for the metamodel explorer's relationship counts.
        edges_by_type = {}
        for edge in self.edges.values():
            type_name = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            edges_by_type[type_name] = edges_by_type.get(type_name, 0) + 1

        return GraphStats(
            total_nodes=len(self.nodes),
            total_edges=len(self.edges),
            nodes_by_type=nodes_by_type,
            edges_by_type=edges_by_type,
            last_updated=datetime.now(timezone.utc),
        )

    def get_subtypes_by_node_type(
        self, node_type: Optional[str] = None
    ) -> Dict[str, List[str]]:
        """Get all unique subtypes grouped by node type.

        Args:
            node_type: If provided, only return subtypes for this node type.

        Returns:
            Dict mapping node type names to sorted lists of unique subtypes.
        """
        result: Dict[str, set] = {}
        for node in self.nodes.values():
            type_name = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            if node_type and type_name != node_type:
                continue
            if hasattr(node, "subtypes") and node.subtypes:
                if type_name not in result:
                    result[type_name] = set()
                result[type_name].update(node.subtypes)
        return {k: sorted(v) for k, v in result.items()}

    def get_edges_between_nodes(self, node_ids: List[str]) -> List[Edge]:
        """Get all edges where both source and target are in the given node IDs"""
        node_id_set = set(node_ids)
        return [
            edge
            for edge in self.edges.values()
            if edge.source in node_id_set and edge.target in node_id_set
        ]

    def get_edges_for_node(self, node_id: str) -> List[Edge]:
        """Get all edges connected to a specific node"""
        return [
            edge
            for edge in self.edges.values()
            if edge.source == node_id or edge.target == node_id
        ]

    def update_edge(
        self,
        edge_id: str,
        updates: Dict,
        event_context: Optional[EventContext] = None,
    ) -> Optional[Edge]:
        """
        Update an existing edge.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge_id: ID of the edge to update
            updates: Dict with fields to update (type, label, metadata)
            event_context: Optional context for event tracking
        """
        with self._lock:
            if edge_id not in self.edges:
                return None

            edge = self.edges[edge_id]
            before_state = edge.to_dict()

            # Update allowed fields
            allowed_fields = {"type", "label", "metadata"}
            candidate = edge.to_dict()
            for key, value in updates.items():
                if key in allowed_fields:
                    if key == "type" and (value is None or value == ""):
                        value = "RELATES_TO"
                    candidate[key] = value

            validated_edge = Edge.from_dict(candidate)
            self._validate_edge_applicability(validated_edge)

            for key in allowed_fields:
                setattr(edge, key, getattr(validated_edge, key))

            # Save
            self.save()

            # Emit update event
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            self._emit_event(
                event_type=EventType.EDGE_UPDATE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge_id,
                entity_type=edge_type,
                before=before_state,
                after=edge.to_dict(),
                context=event_context,
            )

            return edge

    def set_edges_archived(
        self,
        edge_ids: List[str],
        archived: bool,
        event_context: Optional[EventContext] = None,
    ) -> List[Edge]:
        """Set the ``archived`` flag on the given edges.

        Mirrors :meth:`set_nodes_archived`: archived edges are hidden from
        search/traversal by default but remain in the graph. Returns every edge
        that was found; only edges whose flag changed emit an update event, and
        the graph is saved once if anything changed.

        Thread-safe: protected by _lock for the entire operation.
        """
        with self._lock:
            found: List[Edge] = []
            changed = False
            for edge_id in edge_ids:
                edge = self.edges.get(edge_id)
                if edge is None:
                    continue
                found.append(edge)
                if edge.archived == archived:
                    continue

                before_state = edge.to_dict()
                edge.archived = archived
                changed = True

                edge_type = (
                    edge.type.value if hasattr(edge.type, "value") else str(edge.type)
                )
                self._emit_event(
                    event_type=EventType.EDGE_UPDATE,
                    entity_kind=EntityKind.EDGE,
                    entity_id=edge_id,
                    entity_type=edge_type,
                    before=before_state,
                    after=edge.to_dict(),
                    context=event_context,
                )

            if changed:
                self.save()

            return found

    def delete_edge(
        self,
        edge_id: str,
        event_context: Optional[EventContext] = None,
    ) -> bool:
        """
        Delete a single edge.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge_id: ID of the edge to delete
            event_context: Optional context for event tracking

        Returns:
            True if edge was deleted, False if not found
        """
        with self._lock:
            if edge_id not in self.edges:
                return False

            edge = self.edges[edge_id]
            before_state = edge.to_dict()
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )

            # Remove from graph
            try:
                self.graph.remove_edge(edge.source, edge.target, key=edge_id)
            except Exception:
                pass  # Edge might not exist in graph

            # Remove from edges dict
            del self.edges[edge_id]

            # Save
            self.save()

            # Emit delete event
            self._emit_event(
                event_type=EventType.EDGE_DELETE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge_id,
                entity_type=edge_type,
                before=before_state,
                after=None,
                context=event_context,
            )

            return True

    def delete_edges(
        self,
        edge_ids: List[str],
        event_context: Optional[EventContext] = None,
    ) -> DeleteEdgesResult:
        """Delete edges by ID. Limit enforcement is the caller's responsibility."""
        with self._lock:
            deleted_edge_ids = []
            edge_before_states: Dict[str, Dict[str, Any]] = {}

            try:
                for edge_id in edge_ids:
                    edge = self.edges.get(edge_id)
                    if edge is None:
                        continue

                    edge_before_states[edge_id] = edge.to_dict()

                    try:
                        self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                    except Exception:
                        pass

                    del self.edges[edge_id]
                    deleted_edge_ids.append(edge_id)

                self.save()

                for edge_id in deleted_edge_ids:
                    before_state = edge_before_states[edge_id]
                    edge_type = before_state.get("type", "RELATES_TO")
                    self._emit_event(
                        event_type=EventType.EDGE_DELETE,
                        entity_kind=EntityKind.EDGE,
                        entity_id=edge_id,
                        entity_type=edge_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                return DeleteEdgesResult(
                    deleted_edge_ids=deleted_edge_ids,
                    success=True,
                    message=f"Deleted {len(deleted_edge_ids)} edges",
                )
            except Exception as e:
                return DeleteEdgesResult(
                    deleted_edge_ids=[],
                    success=False,
                    message=f"Error during edge deletion: {str(e)}",
                )

    def add_edge(
        self,
        edge: Edge,
        event_context: Optional[EventContext] = None,
    ) -> Optional[str]:
        """
        Add a single edge between existing nodes.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge: Edge object to add
            event_context: Optional context for event tracking

        Returns:
            Edge ID if successful, None if failed
        """
        with self._lock:
            # Validate source and target exist
            if edge.source not in self.nodes:
                # Try name resolution
                name_to_id = {n.name: nid for nid, n in self.nodes.items()}
                if edge.source in name_to_id:
                    edge.source = name_to_id[edge.source]
                else:
                    return None

            if edge.target not in self.nodes:
                name_to_id = {n.name: nid for nid, n in self.nodes.items()}
                if edge.target in name_to_id:
                    edge.target = name_to_id[edge.target]
                else:
                    return None

            if edge.id in self.edges:
                return None

            self._validate_edge_applicability(edge)

            self.edges[edge.id] = edge
            self.graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)

            # Save
            self.save()

            # Emit create event
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            self._emit_event(
                event_type=EventType.EDGE_CREATE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge.id,
                entity_type=edge_type,
                before=None,
                after=edge.to_dict(),
                context=event_context,
            )

            return edge.id

    def get_incident_edges(self, node_ids: List[str]) -> List[Edge]:
        """
        Get all edges connected to any of the given nodes (incoming or outgoing).
        More efficient than iterating through all edges.
        """
        collected_edges = {}

        for node_id in node_ids:
            if node_id not in self.nodes:
                continue

            if node_id in self.graph:
                # Outgoing edges
                for _, _, _, edge_data in self.graph.out_edges(
                    node_id, keys=True, data=True
                ):
                    edge = edge_data["data"]
                    collected_edges[edge.id] = edge

                # Incoming edges
                for _, _, _, edge_data in self.graph.in_edges(
                    node_id, keys=True, data=True
                ):
                    edge = edge_data["data"]
                    collected_edges[edge.id] = edge

        return list(collected_edges.values())
