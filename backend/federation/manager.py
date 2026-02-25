"""Federation manager for read-only graph.json ingestion and cached search."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable
from urllib.request import urlopen

from backend.core.models import Edge, Node

from .config import FederationFileConfig, FederationGraphConfig


@dataclass
class FederatedGraphCache:
    graph_id: str
    display_name: str
    status: str = "offline"
    last_synced_at: Optional[str] = None
    last_error: Optional[str] = None
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)


class FederationManager:
    """Maintains cached read-models for configured federated graphs."""

    def __init__(self, config: FederationFileConfig, on_node_event: Optional[Callable[[str, Optional[Node], Optional[Node]], None]] = None, on_edge_event: Optional[Callable[[str, Optional[Edge], Optional[Edge]], None]] = None):
        self._config = config
        self._on_node_event = on_node_event
        self._on_edge_event = on_edge_event
        self._lock = threading.RLock()
        self._cache: Dict[str, FederatedGraphCache] = {}
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._next_sync_at: Dict[str, float] = {}

        for graph in self._config.federation.graphs:
            self._cache[graph.graph_id] = FederatedGraphCache(
                graph_id=graph.graph_id,
                display_name=graph.display_name,
                status="offline" if graph.enabled else "disabled",
            )

    @property
    def enabled(self) -> bool:
        return self._config.federation.enabled

    def start(self) -> None:
        """Start optional scheduler for configured periodic federation sync."""
        if not self.enabled:
            return

        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        scheduled_graphs = [
            graph for graph in self._config.federation.graphs
            if graph.enabled and graph.sync.mode == "scheduled"
        ]
        if not scheduled_graphs:
            return

        now = time.monotonic()
        with self._lock:
            for graph in scheduled_graphs:
                self._next_sync_at[graph.graph_id] = now + graph.sync.interval_seconds

        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._sync_loop,
            name="federation-sync-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop(self) -> None:
        """Stop scheduler thread cleanly."""
        self._stop_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        self._scheduler_thread = None

    def sync_on_startup(self) -> None:
        if not self.enabled:
            return

        for graph in self._config.federation.graphs:
            if graph.enabled and graph.sync.on_startup:
                self.sync_graph(graph.graph_id)

    def sync_graph(self, graph_id: str) -> Dict[str, Any]:
        graph = self._get_graph_config(graph_id)
        if graph is None:
            return {"success": False, "error": f"Unknown graph_id: {graph_id}"}

        if not graph.enabled:
            return {"success": False, "error": f"Graph {graph_id} is disabled"}

        graph_json_url = graph.endpoints.graph_json_url
        if not graph_json_url:
            self._set_degraded(graph_id, "graph_json_url is missing; only MCP/GUI configured")
            return {"success": False, "error": "No graph_json_url configured"}

        timeout_s = max(0.1, self._config.federation.default_timeout_ms / 1000.0)

        try:
            with urlopen(graph_json_url, timeout=timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))

            nodes = payload.get("nodes", [])
            edges = payload.get("edges", [])
            cache_nodes, cache_edges = self._build_cache(graph, nodes, edges)

            with self._lock:
                entry = self._cache[graph.graph_id]
                previous_nodes = dict(entry.nodes)
                previous_edges = dict(entry.edges)
                entry.nodes = cache_nodes
                entry.edges = cache_edges
                entry.status = "healthy"
                entry.last_error = None
                entry.last_synced_at = datetime.now(timezone.utc).isoformat()

            self._emit_node_events(previous_nodes, cache_nodes)
            self._emit_edge_events(previous_edges, cache_edges)

            return {
                "success": True,
                "graph_id": graph.graph_id,
                "nodes": len(cache_nodes),
                "edges": len(cache_edges),
            }
        except Exception as exc:
            self._set_degraded(graph.graph_id, str(exc))
            return {"success": False, "graph_id": graph.graph_id, "error": str(exc)}

    def sync_all(self) -> Dict[str, Any]:
        results = [self.sync_graph(graph.graph_id) for graph in self._config.federation.graphs if graph.enabled]
        return {
            "success": all(r.get("success", False) for r in results) if results else True,
            "results": results,
        }

    def _allowed_depth_for_graph(self, graph_id: str) -> int:
        global_depth = self._config.federation.max_traversal_depth
        # Backward-compatibility: if depth is unset/0, allow first-hop federated results.
        if global_depth <= 0:
            global_depth = 1
        graph_cfg = self._get_graph_config(graph_id)
        if graph_cfg and graph_cfg.max_depth_override is not None:
            return min(global_depth, graph_cfg.max_depth_override)
        return global_depth

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[str]],
        limit: int,
        max_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        query_lower = query.lower().strip()
        match_all = query_lower in {"", "*"}

        matched_nodes: List[Node] = []
        matched_node_ids: set[str] = set()
        matched_edges: List[Edge] = []

        with self._lock:
            caches = list(self._cache.values())

        for cache in caches:
            for node in cache.nodes.values():
                if node_types and node.type_str not in node_types:
                    continue

                graph_id = (node.metadata or {}).get("origin_graph_id")
                node_distance = (node.metadata or {}).get("federation_distance", 1)
                if graph_id:
                    try:
                        allowed_depth = self._allowed_depth_for_graph(graph_id)
                        if max_depth is not None:
                            allowed_depth = min(allowed_depth, max_depth)
                        if int(node_distance) > int(allowed_depth):
                            continue
                    except Exception:
                        continue

                if not match_all:
                    tags_text = " ".join(node.tags) if node.tags else ""
                    searchable_text = f"{node.name} {node.description} {node.summary} {tags_text}".lower()
                    if query_lower not in searchable_text:
                        continue

                matched_nodes.append(node)
                matched_node_ids.add(node.id)
                if len(matched_nodes) >= limit:
                    break

            if len(matched_nodes) >= limit:
                break

        if matched_node_ids:
            for cache in caches:
                for edge in cache.edges.values():
                    if edge.source in matched_node_ids or edge.target in matched_node_ids:
                        matched_edges.append(edge)

        return {
            "nodes": matched_nodes[:limit],
            "edges": matched_edges,
        }


    def get_cached_node(self, federated_node_id: str) -> Optional[Node]:
        """Fetch a federated cached node by its synthetic ID."""
        with self._lock:
            for cache in self._cache.values():
                node = cache.nodes.get(federated_node_id)
                if node is not None:
                    return node
        return None


    def get_graph_config_for_node(self, federated_node_id: str) -> Optional[FederationGraphConfig]:
        """Resolve source graph config for a cached federated node ID."""
        node = self.get_cached_node(federated_node_id)
        if node is None:
            return None
        origin_graph_id = (node.metadata or {}).get("origin_graph_id")
        if not origin_graph_id:
            return None
        return self._get_graph_config(origin_graph_id)


    def get_max_selectable_depth(self) -> int:
        """Return effective max depth users may select for federated search."""
        max_depth = int(self._config.federation.max_traversal_depth or 0)
        if max_depth <= 0:
            return 1

        per_graph_limits = [
            int(g.max_depth_override)
            for g in self._config.federation.graphs
            if g.enabled and g.max_depth_override is not None
        ]
        if per_graph_limits:
            max_depth = min(max_depth, max(per_graph_limits))

        return max(1, max_depth)


    def get_selectable_depth_levels(self) -> List[int]:
        """Return effective selectable depth levels for UI/runtime controls."""
        configured_levels = self._config.federation.depth_levels
        max_depth = self.get_max_selectable_depth()

        if configured_levels:
            levels = sorted({int(v) for v in configured_levels if int(v) >= 1})
            bounded = [v for v in levels if v <= max_depth]
            return bounded or [1]

        return list(range(1, max_depth + 1))

    def get_graph_display_names(self) -> Dict[str, str]:
        """Map configured remote graph IDs to display names."""
        return {
            graph.graph_id: graph.display_name
            for graph in self._config.federation.graphs
            if graph.enabled
        }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "scheduler_running": bool(self._scheduler_thread and self._scheduler_thread.is_alive()),
                "graphs": [
                    {
                        "graph_id": entry.graph_id,
                        "display_name": entry.display_name,
                        "status": entry.status,
                        "last_synced_at": entry.last_synced_at,
                        "last_error": entry.last_error,
                        "cached_nodes": len(entry.nodes),
                        "cached_edges": len(entry.edges),
                    }
                    for entry in self._cache.values()
                ],
            }

    def _sync_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            now = time.monotonic()
            for graph in self._config.federation.graphs:
                if not graph.enabled or graph.sync.mode != "scheduled":
                    continue

                next_at = self._next_sync_at.get(graph.graph_id)
                if next_at is None:
                    self._next_sync_at[graph.graph_id] = now + graph.sync.interval_seconds
                    continue

                if now >= next_at:
                    self.sync_graph(graph.graph_id)
                    self._next_sync_at[graph.graph_id] = now + graph.sync.interval_seconds


    def _emit_node_events(self, previous_nodes: Dict[str, Node], current_nodes: Dict[str, Node]) -> None:
        if not self._on_node_event:
            return

        previous_ids = set(previous_nodes.keys())
        current_ids = set(current_nodes.keys())

        for node_id in current_ids - previous_ids:
            self._on_node_event("create", None, current_nodes[node_id])

        for node_id in previous_ids - current_ids:
            self._on_node_event("delete", previous_nodes[node_id], None)

        for node_id in previous_ids & current_ids:
            old = previous_nodes[node_id]
            new = current_nodes[node_id]
            if old.to_dict() != new.to_dict():
                self._on_node_event("update", old, new)


    def _emit_edge_events(self, previous_edges: Dict[str, Edge], current_edges: Dict[str, Edge]) -> None:
        if not self._on_edge_event:
            return

        previous_ids = set(previous_edges.keys())
        current_ids = set(current_edges.keys())

        for edge_id in current_ids - previous_ids:
            self._on_edge_event("create", None, current_edges[edge_id])

        for edge_id in previous_ids - current_ids:
            self._on_edge_event("delete", previous_edges[edge_id], None)

        for edge_id in previous_ids & current_ids:
            old = previous_edges[edge_id]
            new = current_edges[edge_id]
            if old.to_dict() != new.to_dict():
                # EDGE_UPDATE event type is not part of current event contract; emit replace semantics
                self._on_edge_event("delete", old, None)
                self._on_edge_event("create", None, new)

    def _set_degraded(self, graph_id: str, error: str) -> None:
        with self._lock:
            entry = self._cache[graph_id]
            entry.status = "degraded"
            entry.last_error = error

    def _get_graph_config(self, graph_id: str) -> Optional[FederationGraphConfig]:
        for graph in self._config.federation.graphs:
            if graph.graph_id == graph_id:
                return graph
        return None

    def _build_cache(
        self,
        graph: FederationGraphConfig,
        source_nodes: List[Dict[str, Any]],
        source_edges: List[Dict[str, Any]],
    ) -> tuple[Dict[str, Node], Dict[str, Edge]]:
        nodes: Dict[str, Node] = {}
        edges: Dict[str, Edge] = {}

        id_mapping: Dict[str, str] = {}
        for source_node in source_nodes:
            origin_node_id = source_node.get("id")
            federated_node_id = f"federated::{graph.graph_id}::{origin_node_id}"
            id_mapping[str(origin_node_id)] = federated_node_id

            metadata = dict(source_node.get("metadata") or {})
            metadata.update({
                "origin_graph_id": graph.graph_id,
                "origin_graph_name": graph.display_name,
                "origin_node_id": origin_node_id,
                "federation_distance": 1,
                "federation_path": [graph.graph_id],
                "sync_state": "fresh",
                "is_federated": True,
            })

            node_payload = {
                "id": federated_node_id,
                "type": source_node.get("type", "Resource"),
                "name": source_node.get("name", "Unnamed"),
                "description": source_node.get("description", ""),
                "summary": source_node.get("summary", ""),
                "tags": source_node.get("tags", []),
                "metadata": metadata,
            }
            try:
                nodes[federated_node_id] = Node.from_dict(node_payload)
            except Exception:
                continue

        for source_edge in source_edges:
            source = id_mapping.get(str(source_edge.get("source")))
            target = id_mapping.get(str(source_edge.get("target")))
            if not source or not target:
                continue

            edge_id = f"federated::{graph.graph_id}::{source_edge.get('id', f'{source}->{target}') }"
            edge_payload = {
                "id": edge_id,
                "source": source,
                "target": target,
                "type": source_edge.get("type", "RELATES_TO"),
                "label": source_edge.get("label", ""),
                "metadata": {
                    **(source_edge.get("metadata") or {}),
                    "origin_graph_id": graph.graph_id,
                    "origin_graph_name": graph.display_name,
                    "origin_edge_id": source_edge.get("id"),
                    "federation_distance": 1,
                    "federation_path": [graph.graph_id],
                    "sync_state": "fresh",
                    "is_federated": True,
                },
            }
            try:
                edges[edge_id] = Edge.from_dict(edge_payload)
            except Exception:
                continue

        return nodes, edges
