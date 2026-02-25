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

import json
import os
import sys
import tempfile
import threading
from typing import List, Dict, Optional, Any, TYPE_CHECKING, Callable
from datetime import datetime
import networkx as nx
from pathlib import Path
from rapidfuzz.distance import Levenshtein

from .models import (
    Node, Edge, NodeType, RelationshipType,
    SimilarNode, GraphStats, AddNodesResult, DeleteNodesResult
)
from .vector_store import VectorStore

# Event system imports
from .events.models import (
    Event, EventType, EntityKind, EventContext, EntityData
)

if TYPE_CHECKING:
    from .events.dispatcher import EventDispatcher
    from .events.delivery import DeliveryWorker


# Cross-platform file locking
if sys.platform == 'win32':
    import msvcrt

    def _lock_file(f, exclusive=True):
        """Acquire file lock on Windows."""
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if exclusive else msvcrt.LK_LOCK, 1)

    def _unlock_file(f):
        """Release file lock on Windows."""
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f, exclusive=True):
        """Acquire file lock on Unix."""
        fcntl.flock(f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _unlock_file(f):
        """Release file lock on Unix."""
        fcntl.flock(f, fcntl.LOCK_UN)


class GraphStorage:
    """
    Manages graph storage with NetworkX + JSON persistence.

    Thread-safety:
    - All public methods that modify state are protected by _lock (threading.RLock)
    - File operations use OS-level file locking for multi-process safety
    - Writes are atomic (temp file + rename) to prevent corruption
    """

    def __init__(self, json_path: str = "graph.json", embeddings_path: str = None):
        """
        Initialize GraphStorage.

        Args:
            json_path: Path to the JSON file for graph persistence
            embeddings_path: Path to the embeddings pickle file (Legacy/Deprecated).
                           New implementation stores embeddings in graph.json directly.
        """
        self.json_path = Path(json_path)

        # Thread lock for in-memory data structure protection
        # RLock allows same thread to acquire lock multiple times (reentrant)
        self._lock = threading.RLock()

        # We initialize VectorStore without a storage path as it now holds state in memory
        # and relies on GraphStorage for persistence via graph.json
        self.vector_store = VectorStore()
        self.vector_store.preload_model()  # Start loading embedding model in background

        self.graph = nx.MultiDiGraph()  # MultiDiGraph allows multiple edges between same nodes
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.edges: Dict[str, Edge] = {}  # edge_id -> Edge
        self.graph_metadata: Dict[str, Any] = {
            "version": "1.0",
            "graph_name": self.json_path.stem,
        }

        # Event system (initialized lazily via setup_events())
        self._event_dispatcher: Optional["EventDispatcher"] = None
        self._delivery_worker: Optional["DeliveryWorker"] = None
        self._events_enabled = False
        self._system_listeners: List[Callable[[Event], None]] = []

        self.load()

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
        """Shutdown the event system gracefully."""
        if self._delivery_worker:
            self._delivery_worker.stop(wait=True)
            self._delivery_worker = None

        self._event_dispatcher = None
        self._events_enabled = False

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
        """
        Emit a graph mutation event.

        Args:
            event_type: Type of event (create, update, delete)
            entity_kind: Node or edge
            entity_id: ID of the entity
            entity_type: Type of the entity (node type or relationship type)
            before: Entity state before mutation (for updates/deletes)
            after: Entity state after mutation (for creates/updates)
            context: Event context for tracking and loop prevention
        """
        # Create event object
        # Build patch for updates
        patch_data = None
        if before and after and event_type == EventType.NODE_UPDATE:
            patch_data = {}
            for key in after:
                if key not in before or before.get(key) != after.get(key):
                    patch_data[key] = after[key]

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

        # Notify system listeners (always, even if events disabled for webhooks)
        for listener in self._system_listeners:
            try:
                listener(event)
            except Exception as e:
                print(f"Error in system listener: {e}")

        if not self._events_enabled or not self._event_dispatcher:
            print(f"EVENT: Skipped (events_enabled={self._events_enabled}, dispatcher={self._event_dispatcher is not None})")
            return

        print(f"EVENT: Emitting {event_type.value} for {entity_kind.value} {entity_id} ({entity_type})")

        # Dispatch asynchronously (non-blocking)
        try:
            self._event_dispatcher.dispatch(event)
        except Exception as e:
            print(f"Warning: Failed to dispatch event: {e}")


    def emit_federated_node_event(
        self,
        operation: str,
        node_before: Optional[Node] = None,
        node_after: Optional[Node] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache changes so subscriptions can react."""
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
        self._emit_event(
            event_type=event_type,
            entity_kind=EntityKind.NODE,
            entity_id=entity_node.id,
            entity_type=entity_node.type_str,
            before=node_before.to_dict() if node_before else None,
            after=node_after.to_dict() if node_after else None,
            context=context,
        )


    def emit_federated_edge_event(
        self,
        operation: str,
        edge_before: Optional[Edge] = None,
        edge_after: Optional[Edge] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache edge changes."""
        operation_map = {
            "create": EventType.EDGE_CREATE,
            "update": EventType.EDGE_UPDATE if hasattr(EventType, "EDGE_UPDATE") else EventType.EDGE_CREATE,
            "delete": EventType.EDGE_DELETE,
        }
        event_type = operation_map.get(operation)
        if event_type is None:
            return

        entity_edge = edge_after or edge_before
        if entity_edge is None:
            return

        context = EventContext(event_origin=event_origin)
        self._emit_event(
            event_type=event_type,
            entity_kind=EntityKind.EDGE,
            entity_id=entity_edge.id,
            entity_type=entity_edge.type_str,
            before=edge_before.to_dict() if edge_before else None,
            after=edge_after.to_dict() if edge_after else None,
            context=context,
        )

    def load(self) -> None:
        """
        Load graph from JSON file.

        Thread-safe: Uses lock for in-memory updates and file lock for reading.
        """
        with self._lock:
            if not self.json_path.exists():
                print(f"No graph file found at {self.json_path}, creating new empty graph")
                self.save()
                return

            try:
                # Use file locking to prevent reading while another process writes
                with open(self.json_path, 'r', encoding='utf-8') as f:
                    _lock_file(f, exclusive=False)  # Shared lock for reading
                    try:
                        data = json.load(f)
                    finally:
                        _unlock_file(f)

                metadata = data.get('metadata') if isinstance(data, dict) else None
                if isinstance(metadata, dict):
                    self.graph_metadata = {
                        "version": metadata.get("version", "1.0"),
                        "graph_name": metadata.get("graph_name") or self.json_path.stem,
                        **{k: v for k, v in metadata.items() if k not in {"version", "graph_name"}},
                    }
                else:
                    self.graph_metadata = {
                        "version": "1.0",
                        "graph_name": self.json_path.stem,
                    }

                # Clear existing data
                self.nodes.clear()
                self.edges.clear()
                self.graph.clear()

                # Load nodes
                for node_data in data.get('nodes', []):
                    node = Node.from_dict(node_data)
                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)

                # Load edges
                for edge_data in data.get('edges', []):
                    edge = Edge.from_dict(edge_data)
                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source,
                        edge.target,
                        key=edge.id,
                        data=edge
                    )

                # Rebuild vector store index from loaded nodes
                self.vector_store.rebuild_index(list(self.nodes.values()))

                print(f"Loaded {len(self.nodes)} nodes and {len(self.edges)} edges from {self.json_path}")

            except Exception as e:
                print(f"Error loading graph: {e}")
                raise

    def save(self) -> None:
        """
        Save graph to JSON file.

        Thread-safe: Uses lock for reading in-memory data.
        Atomic: Writes to temp file first, then renames to prevent corruption.
        File-locked: Uses OS-level locking to prevent concurrent writes.
        """
        with self._lock:
            data = {
                'nodes': [node.to_dict() for node in self.nodes.values()],
                'edges': [edge.to_dict() for edge in self.edges.values()],
                'metadata': {
                    **(self.graph_metadata or {}),
                    'version': (self.graph_metadata or {}).get('version', '1.0'),
                    'graph_name': (self.graph_metadata or {}).get('graph_name', self.json_path.stem),
                    'last_updated': datetime.utcnow().isoformat()
                }
            }

            # Create directory if it doesn't exist
            self.json_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: write to temp file, then rename
            # This prevents corruption if the process is killed mid-write
            temp_fd, temp_path = tempfile.mkstemp(
                suffix='.json',
                prefix='graph_',
                dir=self.json_path.parent
            )

            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    _lock_file(f, exclusive=True)  # Exclusive lock for writing
                    try:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())  # Ensure data is written to disk
                    finally:
                        _unlock_file(f)

                # Atomic rename (on most filesystems)
                # On Windows, we need to remove the target first
                if sys.platform == 'win32' and self.json_path.exists():
                    os.replace(temp_path, self.json_path)
                else:
                    os.rename(temp_path, self.json_path)

                print(f"Saved {len(self.nodes)} nodes and {len(self.edges)} edges to {self.json_path}")

            except Exception as e:
                # Clean up temp file on error
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise e


    def get_graph_name(self) -> str:
        """Return configured graph name from graph metadata."""
        name = (self.graph_metadata or {}).get("graph_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return self.json_path.stem

    def reload(self) -> None:
        """
        Reload graph from disk, discarding any in-memory changes.

        Useful for refreshing state after external modifications.
        """
        self.load()

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        limit: int = 50
    ) -> List[Node]:
        """
        Search nodes based on text query
        Matches against name, description, summary, and tags.
        Empty query or '*' returns all nodes (subject to filtering and limit).
        """
        query_lower = query.lower().strip()
        results = []

        # Handle wildcard or empty query
        match_all = query_lower == "" or query_lower == "*"

        for node in self.nodes.values():
            # Filter by node type
            if node_types and node.type not in node_types:
                continue

            # Text matching including tags (if not matching all)
            if not match_all:
                tags_text = " ".join(node.tags) if hasattr(node, 'tags') and node.tags else ""
                subtypes_text = " ".join(node.subtypes) if hasattr(node, 'subtypes') and node.subtypes else ""
                searchable_text = f"{node.name} {node.description} {node.summary} {tags_text} {subtypes_text}".lower()
                if query_lower not in searchable_text:
                    continue

            results.append(node)

            if len(results) >= limit:
                break

        return results

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a specific node"""
        return self.nodes.get(node_id)

    def get_all_nodes(self) -> List[Node]:
        """Get all nodes in the graph"""
        return list(self.nodes.values())

    def get_all_edges(self) -> List[Edge]:
        """Get all edges in the graph"""
        return list(self.edges.values())

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        depth: int = 1
    ) -> Dict[str, Any]:
        """
        Get nodes connected to the given node
        Returns both nodes and edges
        """
        if node_id not in self.nodes:
            return {'nodes': [], 'edges': []}

        visited_nodes = set([node_id])
        visited_edges = set()
        current_layer = {node_id}

        for _ in range(depth):
            next_layer = set()

            for curr_id in current_layer:
                # Outgoing edges
                for _, target, edge_id, edge_data in self.graph.out_edges(curr_id, keys=True, data=True):
                    edge = edge_data['data']
                    if relationship_types and edge.type not in relationship_types:
                        continue
                    visited_edges.add(edge_id)
                    if target not in visited_nodes:
                        visited_nodes.add(target)
                        next_layer.add(target)

                # Incoming edges
                for source, _, edge_id, edge_data in self.graph.in_edges(curr_id, keys=True, data=True):
                    edge = edge_data['data']
                    if relationship_types and edge.type not in relationship_types:
                        continue
                    visited_edges.add(edge_id)
                    if source not in visited_nodes:
                        visited_nodes.add(source)
                        next_layer.add(source)

            current_layer = next_layer

        return {
            'nodes': [self.nodes[nid] for nid in visited_nodes if nid in self.nodes],
            'edges': [self.edges[eid] for eid in visited_edges if eid in self.edges]
        }

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> List[SimilarNode]:
        """
        Find similar nodes based on Levenshtein distance AND vector embeddings
        Used for duplicate detection
        """
        results = []
        seen_node_ids = set()

        # 1. Levenshtein Search (Exact string matching)
        name_lower = name.lower()

        for node in self.nodes.values():
            # Filter by type if specified
            if node_type and node.type != node_type:
                continue

            # Calculate similarity with Levenshtein
            node_name_lower = node.name.lower()
            distance = Levenshtein.distance(name_lower, node_name_lower)
            max_len = max(len(name_lower), len(node_name_lower))

            if max_len == 0:
                similarity = 1.0
            else:
                similarity = 1.0 - (distance / max_len)

            if similarity >= threshold:
                results.append(SimilarNode(
                    node=node,
                    similarity_score=round(similarity, 2),
                    match_reason=f"Name similarity: {int(similarity * 100)}%"
                ))
                seen_node_ids.add(node.id)

        # 2. Vector Search (Semantic similarity)
        # Lower threshold for vector search to catch semantic matches that might have different names
        vector_threshold = max(0.4, threshold - 0.2)
        vector_results = self.vector_store.search(
            query_text=name,
            limit=limit,
            threshold=vector_threshold
        )

        for node_id, score in vector_results:
            if node_id in seen_node_ids:
                continue

            node = self.nodes.get(node_id)
            if not node:
                continue

            if node_type and node.type != node_type:
                continue

            results.append(SimilarNode(
                node=node,
                similarity_score=round(score, 2),
                match_reason=f"Semantic similarity: {int(score * 100)}%"
            ))
            seen_node_ids.add(node_id)

        # Sort by similarity score
        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results[:limit]

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5
    ) -> Dict[str, List[SimilarNode]]:
        """
        Find similar nodes for multiple names at once (batch processing)
        Returns a dictionary mapping each name to its similar nodes

        This is much more efficient than calling find_similar_nodes multiple times
        as it processes all names in one go.
        """
        results = {}

        for name in names:
            results[name] = self.find_similar_nodes(
                name=name,
                node_type=node_type,
                threshold=threshold,
                limit=limit
            )

        return results

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
                            message=f"Node with ID {node.id} already exists"
                        )

                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)
                    added_node_ids.append(node.id)
                    nodes_to_embed.append(node)

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
                            raise ValueError(f"Source node '{edge.source}' does not exist (not found by ID or name)")

                    # If target is not a valid ID, try to resolve it as a name
                    if target_id not in self.nodes:
                        if target_id in name_to_id:
                            target_id = name_to_id[target_id]
                        else:
                            raise ValueError(f"Target node '{edge.target}' does not exist (not found by ID or name)")

                    # Update edge with resolved IDs
                    edge.source = source_id
                    edge.target = target_id

                    if edge.id in self.edges:
                        return AddNodesResult(
                            added_node_ids=[],
                            added_edge_ids=[],
                            success=False,
                            message=f"Edge with ID {edge.id} already exists"
                        )

                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source,
                        edge.target,
                        key=edge.id,
                        data=edge
                    )
                    added_edge_ids.append(edge.id)

                # Save to JSON
                self.save()

                # Emit events for added nodes
                for node_id in added_node_ids:
                    node = self.nodes.get(node_id)
                    if node:
                        node_type = node.type.value if hasattr(node.type, 'value') else str(node.type)
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
                        edge_type = edge.type.value if hasattr(edge.type, 'value') else str(edge.type)
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
                    message=f"Added {len(added_node_ids)} nodes and {len(added_edge_ids)} edges"
                )

            except Exception as e:
                return AddNodesResult(
                    added_node_ids=[],
                    added_edge_ids=[],
                    success=False,
                    message=f"Error during add: {str(e)}"
                )

    def update_node(
        self,
        node_id: str,
        updates: Dict,
        event_context: Optional[EventContext] = None,
    ) -> Optional[Node]:
        """
        Update an existing node.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update
            event_context: Optional context for event tracking and loop prevention
        """
        with self._lock:
            if node_id not in self.nodes:
                return None

            node = self.nodes[node_id]

            # Capture before state for events
            before_state = node.to_dict()

            # Update allowed fields
            allowed_fields = {'name', 'description', 'summary', 'tags', 'subtypes', 'metadata'}
            for key, value in updates.items():
                if key in allowed_fields:
                    setattr(node, key, value)

            node.updated_at = datetime.utcnow()

            # Update in graph
            self.graph.nodes[node_id]['data'] = node

            # Update embedding if text fields or tags changed (non-blocking)
            if any(k in updates for k in ['name', 'description', 'summary', 'tags']):
                try:
                    self.vector_store.update_node_embedding(node)
                except Exception as embed_error:
                    print(f"Warning: Could not update embedding: {embed_error}")

            # Save
            self.save()

            # Emit update event
            node_type = node.type.value if hasattr(node.type, 'value') else str(node.type)
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
                    message="Max 10 nodes can be deleted at a time. Contact admin for bulk deletion."
                )

            if not confirmed:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message="Deletion requires confirmed=True parameter"
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
                    message=f"Deleted {len(deleted_node_ids)} nodes and {len(affected_edge_ids)} edges"
                )

            except Exception as e:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message=f"Error during deletion: {str(e)}"
                )

    def get_stats(self) -> GraphStats:
        """Get statistics for the graph"""
        # Count nodes per type
        nodes_by_type = {}
        for node in self.nodes.values():
            type_name = node.type.value if hasattr(node.type, 'value') else str(node.type)
            nodes_by_type[type_name] = nodes_by_type.get(type_name, 0) + 1

        return GraphStats(
            total_nodes=len(self.nodes),
            total_edges=len(self.edges),
            nodes_by_type=nodes_by_type,
            last_updated=datetime.utcnow()
        )

    def get_subtypes_by_node_type(self, node_type: Optional[str] = None) -> Dict[str, List[str]]:
        """Get all unique subtypes grouped by node type.

        Args:
            node_type: If provided, only return subtypes for this node type.

        Returns:
            Dict mapping node type names to sorted lists of unique subtypes.
        """
        result: Dict[str, set] = {}
        for node in self.nodes.values():
            type_name = node.type.value if hasattr(node.type, 'value') else str(node.type)
            if node_type and type_name != node_type:
                continue
            if hasattr(node, 'subtypes') and node.subtypes:
                if type_name not in result:
                    result[type_name] = set()
                result[type_name].update(node.subtypes)
        return {k: sorted(v) for k, v in result.items()}

    def get_edges_between_nodes(self, node_ids: List[str]) -> List[Edge]:
        """Get all edges where both source and target are in the given node IDs"""
        node_id_set = set(node_ids)
        return [
            edge for edge in self.edges.values()
            if edge.source in node_id_set and edge.target in node_id_set
        ]

    def get_edges_for_node(self, node_id: str) -> List[Edge]:
        """Get all edges connected to a specific node"""
        return [
            edge for edge in self.edges.values()
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
            allowed_fields = {'type', 'label', 'metadata'}
            for key, value in updates.items():
                if key in allowed_fields:
                    if key == 'type' and (value is None or value == ""):
                        value = "RELATES_TO"
                    setattr(edge, key, value)

            # Save
            self.save()

            # Emit update event
            edge_type = edge.type.value if hasattr(edge.type, 'value') else str(edge.type)
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
            edge_type = edge.type.value if hasattr(edge.type, 'value') else str(edge.type)

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

            self.edges[edge.id] = edge
            self.graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)

            # Save
            self.save()

            # Emit create event
            edge_type = edge.type.value if hasattr(edge.type, 'value') else str(edge.type)
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
