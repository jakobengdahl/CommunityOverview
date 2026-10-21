"""Composed reads (docs/SESSION_OVERLAY_CONTRACT.md §8).

A read made in a staged session answers from the graph with the session's
layer applied. ``ComposedStorage`` is that view for one read: it presents the
read half of ``GraphStorage`` - the node and edge maps, the adjacency the
traversal walks, the lexical index, the vector index - as overlays of the
layer's entries on the main graph, so the query functions run unchanged and
compose by construction (I4). Nothing of the main graph is copied (I3): the
overlays answer from the main maps for every id the layer has no entry for.

Every entry is composed per §8.1. A layer entry the reader's graph-access
narrowing does not wholly cover is absent from the view, not merely filtered:
a staged change answers with the composed entity, and that carries content of
the main entity it stands over and of its base, so all three must be in the
reader's view - the rule staged writes already apply (``StagedWrite._shown``).
The traversal always walks the composed adjacency; the store's own traversal
(``store_traversal``) never sees the layer.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from backend.core import Edge, Node, storage_search
from backend.core.storage import edge_applicability_error
from backend.core.storage_search import _copied_items
from backend.runtime.authorization import GraphAccessNarrowing

from .staging import compose

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from backend.core.layer_vectors import LayerVectors

STATE_CREATED = "created"
STATE_UPDATED = "updated"
STATE_ORPHANED = "orphaned"
STATE_NONE = "none"

_MISSING = object()


class _OverlayMap(Mapping):
    """``main`` with ``over`` laid on top: an id in ``over`` answers from it
    (None: absent), any other id from ``main`` when ``keep`` admits it."""

    def __init__(
        self,
        main: Dict[str, Any],
        over: Dict[str, Optional[Any]],
        keep: Optional[Callable[[Any], bool]] = None,
    ):
        self._main = main
        self._over = over
        self._keep = keep

    def get(self, key, default=None):
        if key in self._over:
            value = self._over[key]
            return default if value is None else value
        value = self._main.get(key)
        if value is None or (self._keep is not None and not self._keep(value)):
            return default
        return value

    def __getitem__(self, key):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def values(self) -> List[Any]:  # type: ignore[override]
        kept = [
            value
            for key, value in _copied_items(self._main)
            if key not in self._over and (self._keep is None or self._keep(value))
        ]
        return kept + [value for value in self._over.values() if value is not None]

    def items(self) -> List[Tuple[str, Any]]:  # type: ignore[override]
        return [(value.id, value) for value in self.values()]

    def keys(self) -> List[str]:  # type: ignore[override]
        return [value.id for value in self.values()]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        if self._keep is not None:
            return len(self.values())
        shadowed = sum(1 for key in self._over if key in self._main)
        present = sum(1 for value in self._over.values() if value is not None)
        return len(self._main) - shadowed + present


class _OverlayRecords:
    """The lexical index's records, with the layer's nodes as composed."""

    def __init__(self, main_records, over: Dict[str, Optional[Node]], type_text):
        self._main = main_records
        self._over = over
        self._type_text = type_text
        self._built: Dict[str, Any] = {}

    def get(self, node_id, default=None):
        if node_id in self._over:
            node = self._over[node_id]
            if node is None:
                return default
            if node_id not in self._built:
                self._built[node_id] = storage_search.build_match_fields(
                    node, self._type_text
                )
            return self._built[node_id]
        return self._main.get(node_id, default)


class _OverlayIndex:
    """The main lexical index, answering for the composed nodes.

    Candidates are the main index's minus every id the layer has an entry
    for, plus every node the layer composes: the search re-tests each one
    against its composed record, so a candidate never decides a match."""

    def __init__(self, main_index, main_nodes, nodes: _OverlayMap, over, type_text):
        self._main = main_index
        self._main_nodes = main_nodes
        self._nodes = nodes
        self._over = over
        self.records = _OverlayRecords(
            getattr(main_index, "records", main_index), over, type_text
        )

    def __len__(self) -> int:
        # The search may only use candidates while the main index covers the
        # main graph; otherwise it walks every composed node.
        if len(self._main) < len(self._main_nodes):
            return 0
        return len(self._nodes)

    def candidates(self, term: str) -> Optional[List[str]]:
        found = (
            self._main.candidates(term) if hasattr(self._main, "candidates") else None
        )
        if found is None:
            return None
        return [nid for nid in found if nid not in self._over] + [
            nid for nid, node in self._over.items() if node is not None
        ]


class _Adjacency:
    """One direction of the composed adjacency, shaped like networkx's
    ``_succ`` / ``_pred``: node -> neighbour -> edge id -> {"data": edge}."""

    def __init__(self, composed: "ComposedStorage", main_adjacency, outgoing: bool):
        self._composed = composed
        self._main = main_adjacency
        self._outgoing = outgoing

    def get(self, node_id, default=None):
        composed = self._composed
        if node_id not in composed.nodes:
            return default
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for neighbour, keydict in _copied_items(self._main.get(node_id) or {}):
            for key, _data in _copied_items(keydict):
                edge = composed.edges.get(key)
                # A main edge re-created over its tombstone may run elsewhere.
                if edge is None or (edge.source, edge.target) != (
                    (node_id, neighbour) if self._outgoing else (neighbour, node_id)
                ):
                    continue
                result.setdefault(neighbour, {})[key] = {"data": edge}
        for edge in composed._staged_edges_at(node_id, self._outgoing):
            other = edge.target if self._outgoing else edge.source
            result.setdefault(other, {})[edge.id] = {"data": edge}
        return result


class _Graph:
    def __init__(self, composed: "ComposedStorage", main_graph):
        self._succ = _Adjacency(composed, main_graph._succ, True)
        self._pred = _Adjacency(composed, main_graph._pred, False)


def _cosine(query, vectors: Dict[str, Any]) -> Dict[str, float]:
    import numpy as np

    q = np.asarray(query, dtype=np.float32).ravel()
    q_norm = float(np.linalg.norm(q)) + 1e-12
    scores: Dict[str, float] = {}
    for node_id, vector in vectors.items():
        v = np.asarray(vector, dtype=np.float32).ravel()
        if v.shape != q.shape:
            continue
        score = float(v @ q) / ((float(np.linalg.norm(v)) + 1e-12) * q_norm)
        if score == score:  # not NaN
            scores[node_id] = score
    return scores


class _ComposedVectorStore:
    """Semantic search over the composed view (§8.3): the main index's hits
    without any id the layer has an entry for - a staged update supersedes the
    main vector, a tombstone hides it - merged by score with the layer's
    composed nodes, scored against their own vectors."""

    def __init__(self, composed: "ComposedStorage", main_store):
        self._composed = composed
        self._main = main_store

    def search(
        self,
        query_text: Optional[str] = None,
        query_node: Optional[Node] = None,
        limit: int = 5,
        threshold: float = 0.0,
    ) -> List[Tuple[str, float]]:
        if limit <= 0:
            return []
        composed = self._composed
        superseded = composed.node_entry_ids
        hits = [
            (node_id, score)
            for node_id, score in self._main.search(
                query_text=query_text,
                query_node=query_node,
                limit=limit + len(superseded),
                threshold=threshold,
            )
            if node_id not in superseded
        ]
        if query_text and composed.layer_vectors is not None:
            # Called with no staged node too: it drops what discarded entries
            # left behind.
            vectors = composed.layer_vectors.vectors(
                composed.session_id, composed.staged_nodes(), superseded
            )
            query = self._main.embed_query(query_text) if vectors else None
            if query is not None:
                hits.extend(
                    (node_id, score)
                    for node_id, score in _cosine(query, vectors).items()
                    if score >= threshold
                )
        hits.sort(key=lambda hit: hit[1], reverse=True)
        return hits[:limit]


class ComposedStorage:
    """The composed view of one staged session, for one read (§8.1).

    ``graph_access`` is the reader's narrowing: a layer entry it does not
    wholly cover is absent from this view. None composes for the server
    itself, as background embedding does.
    """

    def __init__(
        self,
        storage: "GraphStorage",
        entries: List[Dict[str, Any]],
        *,
        session_id: str,
        graph_access: Optional[GraphAccessNarrowing] = None,
        layer_vectors: Optional["LayerVectors"] = None,
    ):
        self._main = storage
        self.session_id = session_id
        self.layer_vectors = layer_vectors
        narrowing = graph_access or GraphAccessNarrowing()
        self._states: Dict[Tuple[str, str], str] = {}

        node_entries = {e["entity_id"]: e for e in entries if e["kind"] == "node"}
        edge_entries = {e["entity_id"]: e for e in entries if e["kind"] == "edge"}
        self.node_entry_ids: Set[str] = set(node_entries)

        def visible(doc: Optional[Dict[str, Any]]) -> bool:
            # access.is_node_visible, on an entity as the layer holds it.
            if doc is None:
                return False
            graph_id = str((doc.get("metadata") or {}).get("origin_graph_id") or "")
            return narrowing.matches(graph_id=graph_id.strip())

        over_nodes: Dict[str, Optional[Node]] = {}
        for node_id, entry in node_entries.items():
            main_node = storage.get_node(node_id)
            main_doc = None if main_node is None else _doc(main_node)
            doc = compose(entry, main_doc)
            self._states[("node", node_id)] = _state(entry, main_doc)
            if doc is not None and narrowing.enabled:
                snapshots = (doc, main_doc, entry.get("base"))
                if not all(visible(s) for s in snapshots if s is not None):
                    doc = None
            over_nodes[node_id] = None if doc is None else Node.from_dict(doc)
        self._over_nodes = over_nodes
        self.nodes = _OverlayMap(storage.nodes, over_nodes)

        def main_node_visible(node_id: Any) -> bool:
            return visible(
                None
                if not isinstance(node_id, str) or storage.get_node(node_id) is None
                else _doc(storage.get_node(node_id))
            )

        over_edges: Dict[str, Optional[Edge]] = {}
        for edge_id, entry in edge_entries.items():
            main_edge = storage.edges.get(edge_id)
            main_doc = None if main_edge is None else _doc(main_edge)
            doc = compose(entry, main_doc)
            self._states[("edge", edge_id)] = _state(entry, main_doc)
            if doc is not None and (
                doc["source"] not in self.nodes or doc["target"] not in self.nodes
            ):
                doc = None
            if doc is not None and narrowing.enabled:
                # The main edge and the base keep no copy of a vanished
                # endpoint, so one main no longer holds counts as hidden.
                for snapshot in (main_doc, entry.get("base")):
                    if snapshot is not None and not all(
                        main_node_visible(snapshot.get(end))
                        for end in ("source", "target")
                    ):
                        doc = None
                        break
            over_edges[edge_id] = None if doc is None else Edge.from_dict(doc)
        self._over_edges = over_edges
        self.edges = _OverlayMap(
            storage.edges,
            over_edges,
            keep=lambda edge: edge.source in self.nodes and edge.target in self.nodes,
        )

        self._out: Dict[str, List[Edge]] = {}
        self._in: Dict[str, List[Edge]] = {}
        for edge in over_edges.values():
            if edge is not None:
                self._out.setdefault(edge.source, []).append(edge)
                self._in.setdefault(edge.target, []).append(edge)

        self.graph = _Graph(self, storage.graph)
        self._type_searchable_text = storage._type_searchable_text
        self._searchable_text_cache = _OverlayIndex(
            storage._searchable_text_cache,
            storage.nodes,
            self.nodes,
            over_nodes,
            self._type_searchable_text,
        )
        self.vector_store = _ComposedVectorStore(self, storage.vector_store)

    # -- what the layer holds ---------------------------------------------------

    def state(self, kind: str, entity_id: str) -> str:
        """``staged_state`` (§8.1) of an entity this view returns."""
        return self._states.get((kind, entity_id), STATE_NONE)

    def staged_nodes(self) -> List[Node]:
        """The nodes the layer composes and this view shows."""
        return [node for node in self._over_nodes.values() if node is not None]

    def _staged_edges_at(self, node_id: str, outgoing: bool) -> List[Edge]:
        return list((self._out if outgoing else self._in).get(node_id, ()))

    # -- the read half of GraphStorage ------------------------------------------

    def get_graph_name(self) -> str:
        return self._main.get_graph_name()

    def get_node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    def get_all_nodes(self) -> List[Node]:
        return list(self.nodes.values())

    def get_all_edges(self) -> List[Edge]:
        return list(self.edges.values())

    def get_node_count(self) -> int:
        return len(self.nodes)

    def search_nodes(
        self,
        query: str,
        node_types=None,
        limit: int = 50,
        include_archived: bool = False,
        match_mode: str = storage_search.MATCH_MODE_SUBSTRING,
    ) -> List[Node]:
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
        node_types=None,
        limit: int = 50,
        threshold: float = storage_search.DEFAULT_SEMANTIC_THRESHOLD,
        include_archived: bool = False,
    ) -> List[Node]:
        return storage_search.semantic_search_nodes(
            self.nodes,
            self.vector_store,
            query,
            node_types,
            limit,
            threshold,
            include_archived=include_archived,
        )

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types=None,
        depth: int = 1,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return storage_search.get_related_nodes(
            self.nodes,
            self.edges,
            self.graph,
            node_id,
            relationship_types,
            depth,
            include_archived=include_archived,
        )

    def find_similar_nodes(self, name, node_type=None, threshold=0.7, limit=5):
        return storage_search.find_similar_nodes(
            self.nodes, self.vector_store, name, node_type, threshold, limit
        )

    def find_similar_nodes_batch(self, names, node_type=None, threshold=0.7, limit=5):
        return storage_search.find_similar_nodes_batch(
            self.nodes, self.vector_store, names, node_type, threshold, limit
        )

    def get_incident_edges(self, node_ids: List[str]) -> List[Edge]:
        collected: Dict[str, Edge] = {}
        for node_id in node_ids:
            for adjacency in (self.graph._succ, self.graph._pred):
                for keydict in (adjacency.get(node_id) or {}).values():
                    for key, data in keydict.items():
                        collected[key] = data["data"]
        return list(collected.values())

    def get_edges_for_node(self, node_id: str) -> List[Edge]:
        return self.get_incident_edges([node_id])

    def get_edges_between_nodes(self, node_ids: List[str]) -> List[Edge]:
        wanted = set(node_ids)
        return [
            edge
            for edge in self.get_incident_edges(list(wanted))
            if edge.source in wanted and edge.target in wanted
        ]

    def get_stats(self):
        from datetime import datetime, timezone

        from backend.core import GraphStats

        nodes, edges = self.get_all_nodes(), self.get_all_edges()
        nodes_by_type: Dict[str, int] = {}
        for node in nodes:
            nodes_by_type[node.type_str] = nodes_by_type.get(node.type_str, 0) + 1
        edges_by_type: Dict[str, int] = {}
        for edge in edges:
            edges_by_type[edge.type_str] = edges_by_type.get(edge.type_str, 0) + 1
        return GraphStats(
            total_nodes=len(nodes),
            total_edges=len(edges),
            nodes_by_type=nodes_by_type,
            edges_by_type=edges_by_type,
            last_updated=datetime.now(timezone.utc),
        )

    def get_subtypes_by_node_type(self, node_type: Optional[str] = None):
        return subtypes_by_node_type(self.get_all_nodes(), node_type)

    def audit_relationship_applicability(self) -> List[Dict[str, Any]]:
        return applicability_violations(self.get_all_edges(), self.get_node)


def subtypes_by_node_type(
    nodes: List[Node], node_type: Optional[str] = None
) -> Dict[str, List[str]]:
    """``GraphStorage.get_subtypes_by_node_type`` over the given nodes."""
    result: Dict[str, set] = {}
    for node in nodes:
        if node_type and node.type_str != node_type:
            continue
        if node.subtypes:
            result.setdefault(node.type_str, set()).update(node.subtypes)
    return {k: sorted(v) for k, v in result.items()}


def applicability_violations(
    edges: List[Edge], get_node: Callable[[str], Optional[Node]]
) -> List[Dict[str, Any]]:
    """``GraphStorage.audit_relationship_applicability`` over the given edges."""
    violations = []
    for edge in edges:
        source, target = get_node(edge.source), get_node(edge.target)
        if source is None or target is None:
            message = "Edge references a missing source or target node."
        else:
            message = edge_applicability_error(edge.type_str, source, target)
            if not message:
                continue
        violations.append(
            {
                "edge_id": edge.id,
                "type": edge.type_str,
                "source": edge.source,
                "target": edge.target,
                "source_type": source.type_str if source else None,
                "target_type": target.type_str if target else None,
                "message": message,
            }
        )
    return violations


def _doc(entity: Any) -> Dict[str, Any]:
    data = entity.to_dict()
    data.pop("embedding", None)
    return data


def _state(entry: Dict[str, Any], main_doc: Optional[Dict[str, Any]]) -> str:
    action = entry.get("action")
    if action == "create":
        return STATE_CREATED
    if action == "update":
        return STATE_UPDATED if main_doc is not None else STATE_ORPHANED
    return STATE_NONE


# The keys a composing read's result carries entities under.
_NODE_LISTS = ("nodes",)
_EDGE_LISTS = ("edges",)


def annotate_staged_state(result: Any, composed: ComposedStorage) -> Any:
    """Mark every entity in a composing read's ``result`` with its
    ``staged_state`` (§8.1), in place."""
    if not isinstance(result, dict):
        return result

    def mark(kind: str, entity: Any) -> None:
        if isinstance(entity, dict) and isinstance(entity.get("id"), str):
            entity["staged_state"] = composed.state(kind, entity["id"])

    for key in _NODE_LISTS:
        if isinstance(result.get(key), list):
            for node in result[key]:
                mark("node", node)
    for key in _EDGE_LISTS:
        if isinstance(result.get(key), list):
            for edge in result[key]:
                mark("edge", edge)
    mark("node", result.get("node"))
    mark("edge", result.get("edge"))
    similar_lists = [result.get("similar_nodes")]
    if isinstance(result.get("results"), dict):
        similar_lists += [
            group.get("similar_nodes")
            for group in result["results"].values()
            if isinstance(group, dict)
        ]
    for similar in similar_lists:
        if isinstance(similar, list):
            for item in similar:
                if isinstance(item, dict):
                    mark("node", item.get("node"))
    return result
