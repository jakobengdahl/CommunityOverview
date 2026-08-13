"""
Pure search, traversal, and similarity algorithms for GraphStorage.

All functions receive the graph state they need as explicit parameters —
no I/O, no locking, no event emission.  GraphStorage delegates its
search/similarity/related methods here and passes ``self.nodes``,
``self.edges``, ``self.graph``, etc. as arguments.
"""

from typing import Any, Dict, List, Optional

from rapidfuzz.distance import Levenshtein

from .models import Node, Edge, NodeType, RelationshipType, SimilarNode
from .vector_store import VectorStore


# ---------------------------------------------------------------------------
# Searchable-text helpers
# ---------------------------------------------------------------------------


def build_searchable_text(node: Node, type_searchable_text: Dict[str, str]) -> str:
    """Build the flat searchable string for a node (used to populate cache)."""
    tags_text = " ".join(node.tags) if hasattr(node, "tags") and node.tags else ""
    subtypes_text = (
        " ".join(node.subtypes) if hasattr(node, "subtypes") and node.subtypes else ""
    )
    aliases_text = (
        " ".join(node.aliases) if hasattr(node, "aliases") and node.aliases else ""
    )
    type_text = type_searchable_text.get(str(node.type), str(node.type).lower())
    return f"{node.name} {node.description} {node.summary} {tags_text} {subtypes_text} {aliases_text} {type_text}".lower()


def score_node_match(
    node: Node, query_lower: str, type_searchable_text: Dict[str, str]
) -> int:
    """Score how well a node matches a query. Higher = better match.

    Name matches use large base values (300 000–500 000) so that any
    name-tier match always outranks secondary signals (type/tags/description)
    regardless of how many secondary signals accumulate.  Aliases are
    alternative names and score in a dedicated band (200 000–250 000) that
    sits just below real-name matches but above every secondary signal.
    Secondary signals use values up to ~1 850, well below the 100 000-point
    gap between tiers.
    """
    score = 0
    name_lower = (node.name or "").lower()

    name_score = 0
    if name_lower == query_lower:
        name_score = 500_000
    elif name_lower.startswith(query_lower):
        name_score = 400_000
    elif query_lower in name_lower:
        name_score = 300_000

    alias_score = 0
    if node.aliases:
        aliases_lower = [a.lower() for a in node.aliases]
        if query_lower in aliases_lower:
            alias_score = 250_000
        elif any(a.startswith(query_lower) for a in aliases_lower):
            alias_score = 220_000
        elif any(query_lower in a for a in aliases_lower):
            alias_score = 200_000

    score += max(name_score, alias_score)

    type_key = str(node.type)
    type_name_lower = type_key.lower()
    type_text = type_searchable_text.get(type_key, type_name_lower)
    if type_name_lower == query_lower:
        score += 700
    elif type_name_lower.startswith(query_lower):
        score += 650
    elif query_lower in type_text:
        score += 600

    if node.tags:
        tags_lower = [t.lower() for t in node.tags]
        if query_lower in tags_lower:
            score += 500
        elif any(query_lower in t for t in tags_lower):
            score += 450

    if node.subtypes:
        if any(query_lower in s.lower() for s in node.subtypes):
            score += 400

    if (
        query_lower in (node.description or "").lower()
        or query_lower in (node.summary or "").lower()
    ):
        score += 200

    return score


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_nodes(
    nodes: Dict[str, Node],
    searchable_text_cache: Dict[str, str],
    type_searchable_text: Dict[str, str],
    query: str,
    node_types: Optional[List[NodeType]] = None,
    limit: int = 50,
    include_archived: bool = False,
) -> List[Node]:
    """Text search over *nodes*.  Matches against name, description, summary,
    tags, subtypes, aliases and node type (including localized labels).
    Results are ranked so that name matches rank above type matches, which
    rank above description/tag matches.
    Empty query or ``'*'`` returns all nodes (subject to filtering and limit).

    Archived nodes are excluded unless ``include_archived`` is True. Excluding
    them here — before the ``limit`` slice below — keeps ``limit`` counting only
    visible results.
    """
    query_lower = query.lower().strip()
    results = []
    match_all = query_lower == "" or query_lower == "*"

    for node in nodes.values():
        if node_types and node.type not in node_types:
            continue

        if not include_archived and getattr(node, "archived", False):
            continue

        if not match_all:
            searchable_text = searchable_text_cache.get(node.id)
            if searchable_text is None:
                searchable_text = build_searchable_text(node, type_searchable_text)
                searchable_text_cache[node.id] = searchable_text

            if query_lower not in searchable_text:
                continue

        results.append(node)

    if not match_all:
        results.sort(
            key=lambda n: score_node_match(n, query_lower, type_searchable_text),
            reverse=True,
        )

    return results[:limit]


# ---------------------------------------------------------------------------
# Graph traversal
# ---------------------------------------------------------------------------


def get_related_nodes(
    nodes: Dict[str, Node],
    edges: Dict[str, Edge],
    graph: Any,  # networkx.MultiDiGraph
    node_id: str,
    relationship_types: Optional[List[RelationshipType]] = None,
    depth: int = 1,
    include_archived: bool = False,
) -> Dict[str, Any]:
    """BFS traversal from *node_id* up to *depth* hops.  Returns nodes and
    edges that are reachable, filtered by *relationship_types* when given.

    Unless ``include_archived`` is True, archived edges are not traversed and
    archived neighbour nodes are not visited (nor reached through), so an
    archived node cannot re-enter the result set via a later hop. The starting
    node is always included as the anchor, even when it is itself archived.
    """
    if node_id not in nodes:
        return {"nodes": [], "edges": []}

    visited_nodes = {node_id}
    visited_edges: set = set()
    current_layer = {node_id}

    def _neighbor_blocked(neighbor_id: str) -> bool:
        if include_archived or neighbor_id == node_id:
            # The anchor is always part of the result, so an edge that reconnects
            # to it (e.g. a cycle at depth >= 2) must not be dropped even when the
            # anchor itself is archived.
            return False
        neighbor = nodes.get(neighbor_id)
        return neighbor is not None and getattr(neighbor, "archived", False)

    for _ in range(depth):
        next_layer: set = set()

        for curr_id in current_layer:
            for _, target, edge_id, edge_data in graph.out_edges(
                curr_id, keys=True, data=True
            ):
                edge = edge_data["data"]
                if relationship_types and edge.type not in relationship_types:
                    continue
                if not include_archived and getattr(edge, "archived", False):
                    continue
                if _neighbor_blocked(target):
                    continue
                visited_edges.add(edge_id)
                if target not in visited_nodes:
                    visited_nodes.add(target)
                    next_layer.add(target)

            for source, _, edge_id, edge_data in graph.in_edges(
                curr_id, keys=True, data=True
            ):
                edge = edge_data["data"]
                if relationship_types and edge.type not in relationship_types:
                    continue
                if not include_archived and getattr(edge, "archived", False):
                    continue
                if _neighbor_blocked(source):
                    continue
                visited_edges.add(edge_id)
                if source not in visited_nodes:
                    visited_nodes.add(source)
                    next_layer.add(source)

        current_layer = next_layer

    return {
        "nodes": [nodes[nid] for nid in visited_nodes if nid in nodes],
        "edges": [edges[eid] for eid in visited_edges if eid in edges],
    }


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def find_similar_nodes(
    nodes: Dict[str, Node],
    vector_store: VectorStore,
    name: str,
    node_type: Optional[NodeType] = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> List[SimilarNode]:
    """Find similar nodes using Levenshtein distance AND vector embeddings.
    Used for duplicate detection.
    """
    results = []
    seen_node_ids: set = set()

    name_lower = name.lower()
    for node in nodes.values():
        if node_type and node.type != node_type:
            continue

        node_name_lower = node.name.lower()
        distance = Levenshtein.distance(name_lower, node_name_lower)
        max_len = max(len(name_lower), len(node_name_lower))
        similarity = 1.0 if max_len == 0 else 1.0 - (distance / max_len)

        if similarity >= threshold:
            results.append(
                SimilarNode(
                    node=node,
                    similarity_score=round(similarity, 2),
                    match_reason=f"Name similarity: {int(similarity * 100)}%",
                )
            )
            seen_node_ids.add(node.id)

    vector_threshold = max(0.4, threshold - 0.2)
    vector_results = vector_store.search(
        query_text=name, limit=limit, threshold=vector_threshold
    )

    for node_id, score in vector_results:
        if node_id in seen_node_ids:
            continue
        node = nodes.get(node_id)
        if not node:
            continue
        if node_type and node.type != node_type:
            continue
        results.append(
            SimilarNode(
                node=node,
                similarity_score=round(score, 2),
                match_reason=f"Semantic similarity: {int(score * 100)}%",
            )
        )
        seen_node_ids.add(node_id)

    results.sort(key=lambda x: x.similarity_score, reverse=True)
    return results[:limit]


def find_similar_nodes_batch(
    nodes: Dict[str, Node],
    vector_store: VectorStore,
    names: List[str],
    node_type: Optional[NodeType] = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> Dict[str, List[SimilarNode]]:
    """Batch variant of :func:`find_similar_nodes`.  More efficient than
    calling it repeatedly when many names need to be checked at once.
    """
    return {
        name: find_similar_nodes(
            nodes,
            vector_store,
            name,
            node_type=node_type,
            threshold=threshold,
            limit=limit,
        )
        for name in names
    }
