"""
Pure search, traversal, and similarity algorithms for GraphStorage.

All functions receive the graph state they need as explicit parameters —
no I/O, no locking, no event emission.  GraphStorage delegates its
search/similarity/related methods here and passes ``self.nodes``,
``self.edges``, ``self.graph``, etc. as arguments.
"""

from types import MappingProxyType
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from rapidfuzz.distance import Levenshtein

from .models import Node, Edge, NodeType, RelationshipType, SimilarNode
from .vector_store import VectorStore


# Default cosine-similarity floor for semantic search.  Embeddings from the
# all-MiniLM-L6-v2 model score unrelated text near 0 and topically related text
# well above this, so this keeps meaning-ranked results without hardcoding any
# domain-specific tuning.
DEFAULT_SEMANTIC_THRESHOLD = 0.3


# Lexical match modes for :func:`search_nodes`.  ``substring`` is the historical
# behaviour (the whole query must occur verbatim in a node's searchable text);
# ``any_term`` is an opt-in OR over the query's whitespace-separated terms, so a
# multi-word query no longer collapses to zero results when no node contains the
# phrase itself.
MATCH_MODE_SUBSTRING = "substring"
MATCH_MODE_ANY_TERM = "any_term"
MATCH_MODES = (MATCH_MODE_SUBSTRING, MATCH_MODE_ANY_TERM)
MAX_ANY_TERM_TERMS = 32


def validate_match_mode(match_mode: str) -> str:
    """Return *match_mode* unchanged, or raise ``ValueError`` if unsupported.

    Callers that may skip the lexical path entirely (semantic search) validate
    up front with this, so an unsupported mode is always rejected rather than
    silently ignored.
    """
    if match_mode not in MATCH_MODES:
        raise ValueError(
            f"unknown match_mode {match_mode!r}; expected one of {', '.join(MATCH_MODES)}"
        )
    return match_mode


# ---------------------------------------------------------------------------
# Searchable-text helpers
# ---------------------------------------------------------------------------


class MatchFields(NamedTuple):
    """Everything matching and ranking read off a node, lowered once.

    The scorer used to take a Node and lower its name, description, summary,
    tags, subtypes and aliases on every call - once per matched node per query
    - and call `str()` on the type enum alongside. Measured at 100k matched
    nodes that was 157 ms of the 447 a matches-everything query cost, with the
    enum's __str__ alone a sixth of the whole search.

    None of it varies with the query, so it is prepared where the searchable
    text was already prepared and cached in its place. `text` is that same flat
    string, built the same way, so what matches is unchanged.
    """

    text: str
    name: str
    aliases: Tuple[str, ...]
    type_key: str
    type_name: str
    type_text: str
    tags: Tuple[str, ...]
    subtypes: Tuple[str, ...]
    description: str
    summary: str


_CORPUS_SEPARATOR = "\x00"

# Below this many hits the index is used whatever fraction of the graph they
# are. See the selectivity gate in LexicalIndex.candidates.
_MIN_SELECTIVE_HITS = 64


class LexicalIndex:
    """The per-node match records, plus one joined copy of their text.

    Dict-like, because it replaces the plain dict this cache used to be and is
    written at six places in `GraphStorage`. Making it an object rather than a
    dict with a separate corpus beside it is the point: a derived structure
    invalidated at six call sites is invalidated correctly only as long as
    everyone remembers, and there is nowhere else to write to here.

    The corpus exists because scanning was the whole cost of a query that
    matches little: `term in text` per node is a Python-level call per node,
    and at 100k nodes that was 64 ms whether anything matched or not. One
    `str.find` over a joined copy is the same test at C speed - 3.8 ms over
    17.5 MB - and the same test, so what matches does not change.

    It is rebuilt lazily and whole. Incremental maintenance of a joined string
    means splicing at an offset and shifting every offset after it, which is
    the same O(total) as rebuilding; doing it on read means a burst of writes
    pays for one rebuild rather than one per write.
    """

    __slots__ = ("_fields", "_records", "_corpus", "_ids", "_starts", "_dirty")

    def __init__(self) -> None:
        self._fields: Dict[str, MatchFields] = {}
        # A read-only view for the scan to look records up through. Going via
        # this class's own `get` puts a Python-level call in the hot loop where
        # there used to be a C-level `dict.get`, which measured 75 ms over 100k
        # nodes - most of what the prepared fields had just saved. The proxy
        # delegates at C speed and cannot be written through, so the index
        # still sees every change that would invalidate its corpus.
        self._records = MappingProxyType(self._fields)
        self._corpus: Optional[str] = None
        self._ids: List[str] = []
        self._starts = None
        self._dirty = True

    # -- the dict surface this replaces ------------------------------------
    def __iter__(self):
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._fields

    def __getitem__(self, node_id: str) -> MatchFields:
        return self._fields[node_id]

    def __setitem__(self, node_id: str, fields: MatchFields) -> None:
        self._fields[node_id] = fields
        self._dirty = True

    def get(self, node_id: str, default=None):
        return self._fields.get(node_id, default)

    def pop(self, node_id: str, default=None):
        self._dirty = True
        return self._fields.pop(node_id, default)

    def clear(self) -> None:
        self._fields.clear()
        self._dirty = True

    def update(self, other) -> None:
        self._fields.update(other)
        self._dirty = True

    @property
    def records(self):
        """Read-only view of the records, for hot read paths. See __init__."""
        return self._records

    def values(self):
        return self._fields.values()

    def items(self):
        return self._fields.items()

    # -- the corpus --------------------------------------------------------
    def _rebuild(self) -> None:
        self._ids = list(self._fields)
        self._corpus = _CORPUS_SEPARATOR.join(
            self._fields[node_id].text for node_id in self._ids
        )
        starts, position = [], 0
        for node_id in self._ids:
            starts.append(position)
            position += len(self._fields[node_id].text) + 1
        try:
            import numpy as np

            self._starts = np.asarray(starts, dtype="int64")
        except ImportError:  # pragma: no cover - numpy is a base requirement
            self._starts = starts
        self._dirty = False

    def candidates(self, term: str) -> Optional[List[str]]:
        """Node ids whose text contains *term*, or None to say "scan instead".

        None rather than an empty list when the corpus cannot answer, so an
        empty result is never confused with an unavailable one - a term
        containing the separator could match across a node boundary, and there
        the caller has to fall back rather than get a wrong answer.
        """
        if _CORPUS_SEPARATOR in term or not term:
            return None
        if self._dirty:
            self._rebuild()
        if not self._ids:
            return []

        corpus = self._corpus
        # One C-level pass to decide whether to take this path at all. The
        # index wins by not visiting most nodes, so it stops winning when the
        # term matches most of them: measured at 100k nodes it costs 4.25 us a
        # hit against the walk's 1.64 us over a 93 ms head start, which crosses
        # over near 36%. Declining at a quarter keeps a margin - and declining
        # is cheap, one pass, where discovering it late is not.
        #
        # `count` counts occurrences, not nodes, so it over-counts a node that
        # holds the term twice. That makes this decline slightly more often
        # than it strictly must, which is the safe direction.
        occurrences = corpus.count(term)
        if occurrences == 0:
            return []
        # The floor matters as much as the fraction: a quarter of a five-node
        # index is one, so without it the index declined every query on a small
        # graph and was dead code below about eight nodes - which is most
        # graphs. Nothing can dominate a scan that short, so there is nothing
        # to decline for.
        if occurrences > max(_MIN_SELECTIVE_HITS, len(self._ids) // 4):
            return None

        found, position = [], corpus.find(term)
        while position != -1:
            found.append(position)
            position = corpus.find(term, position + 1)
        if not found:
            return []

        starts = self._starts
        ids = self._ids
        if isinstance(starts, list):
            from bisect import bisect_right

            indices = [bisect_right(starts, p) - 1 for p in found]
        else:
            import numpy as np

            indices = (
                np.searchsorted(starts, np.asarray(found, dtype="int64"), side="right")
                - 1
            )
        # One node can hold several occurrences. The positions come out
        # ascending, so the indices are non-decreasing and duplicates are
        # adjacent - dropping them needs no set and no membership test, and on
        # the numpy path no Python loop at all. Corpus order is insertion
        # order, which is the order the caller's stable ranking assumes.
        if isinstance(starts, list):
            out = []
            previous = -1
            for i in indices:
                if i != previous:
                    out.append(ids[i])
                    previous = i
            return out

        import numpy as np

        if len(indices) > 1:
            indices = indices[np.concatenate(([True], indices[1:] != indices[:-1]))]
        return [ids[i] for i in indices]


def build_match_fields(node: Node, type_searchable_text: Dict[str, str]) -> MatchFields:
    """Prepare a node's matching and ranking fields (used to populate cache)."""
    tags = tuple(node.tags) if getattr(node, "tags", None) else ()
    subtypes = tuple(node.subtypes) if getattr(node, "subtypes", None) else ()
    aliases = tuple(node.aliases) if getattr(node, "aliases", None) else ()
    type_key = str(node.type)
    type_name = type_key.lower()
    type_text = type_searchable_text.get(type_key, type_name)
    tags_text = " ".join(tags)
    subtypes_text = " ".join(subtypes)
    aliases_text = " ".join(aliases)
    text = f"{node.name} {node.description} {node.summary} {tags_text} {subtypes_text} {aliases_text} {type_text}".lower()
    return MatchFields(
        text=text,
        name=(node.name or "").lower(),
        aliases=tuple(a.lower() for a in aliases),
        type_key=type_key,
        type_name=type_name,
        type_text=type_text,
        tags=tuple(t.lower() for t in tags),
        subtypes=tuple(s.lower() for s in subtypes),
        description=(node.description or "").lower(),
        summary=(node.summary or "").lower(),
    )


def build_searchable_text(node: Node, type_searchable_text: Dict[str, str]) -> str:
    """The flat searchable string alone, for callers that want only that."""
    return build_match_fields(node, type_searchable_text).text


def score_type(type_name: str, type_text: str, query_lower: str) -> int:
    """The type tier's contribution, which depends only on the TYPE and the
    query - never on the node. There are a couple of dozen node types, so a
    scan over 100k nodes was recomputing a couple of dozen distinct answers
    100k times; :func:`search_nodes` memoises it per query instead."""
    if type_name == query_lower:
        return 700
    if type_name.startswith(query_lower):
        return 650
    if query_lower in type_text:
        return 600
    return 0


def score_fields(
    fields: MatchFields, query_lower: str, type_bonus: Optional[int] = None
) -> int:
    """Score how well a prepared node matches a query. Higher = better match.

    Name matches use large base values (300 000-500 000) so that any
    name-tier match always outranks secondary signals (type/tags/description)
    regardless of how many secondary signals accumulate.  Aliases are
    alternative names and score in a dedicated band (200 000-250 000) that
    sits just below real-name matches but above every secondary signal.
    Secondary signals use values up to ~1 850, well below the 100 000-point
    gap between tiers.

    This is the only implementation of the ranking; `score_node_match` prepares
    a node and calls it, so the cached path and the per-node path cannot drift
    apart.

    `type_bonus` is the memoised result of :func:`score_type` for this node's
    type and this query. Passing it changes nothing about the result - it is
    what would have been computed here - and lets a scan compute it once per
    distinct type rather than once per node.
    """
    name_lower = fields.name

    name_score = 0
    if name_lower == query_lower:
        name_score = 500_000
    elif name_lower.startswith(query_lower):
        name_score = 400_000
    elif query_lower in name_lower:
        name_score = 300_000

    alias_score = 0
    if fields.aliases:
        aliases_lower = fields.aliases
        if query_lower in aliases_lower:
            alias_score = 250_000
        elif any(a.startswith(query_lower) for a in aliases_lower):
            alias_score = 220_000
        elif any(query_lower in a for a in aliases_lower):
            alias_score = 200_000

    score = max(name_score, alias_score)

    score += (
        score_type(fields.type_name, fields.type_text, query_lower)
        if type_bonus is None
        else type_bonus
    )

    if fields.tags:
        tags_lower = fields.tags
        if query_lower in tags_lower:
            score += 500
        elif any(query_lower in t for t in tags_lower):
            score += 450

    if fields.subtypes:
        if any(query_lower in s for s in fields.subtypes):
            score += 400

    if query_lower in fields.description or query_lower in fields.summary:
        score += 200

    return score


def score_node_match(
    node: Node, query_lower: str, type_searchable_text: Dict[str, str]
) -> int:
    """Score a node against a query, preparing its fields first.

    Kept for callers that hold a Node rather than a cached record. The search
    path uses the cached record directly; both end in :func:`score_fields`.
    """
    return score_fields(build_match_fields(node, type_searchable_text), query_lower)


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
    match_mode: str = MATCH_MODE_SUBSTRING,
) -> List[Node]:
    """Text search over *nodes*.  Matches against name, description, summary,
    tags, subtypes, aliases and node type (including localized labels).
    Results are ranked so that name matches rank above type matches, which
    rank above description/tag matches.
    Empty query or ``'*'`` returns all nodes (subject to filtering and limit).

    Archived nodes are excluded unless ``include_archived`` is True. Excluding
    them here — before the ``limit`` slice below — keeps ``limit`` counting only
    visible results.

    ``match_mode`` selects how the query text is matched:

    - ``substring`` (default): the whole query must occur verbatim in a node's
      searchable text — the historical behaviour.
    - ``any_term``: the query is split on whitespace into capped, *distinct*
      terms and a node matches when it contains **any** of them.  Ranking stays
      tier-based: a node scores by its single best-matching term (so a name-tier
      match still outranks any pile of secondary signals), and the number of
      matched distinct terms only breaks an exact scoring tie.  Never added:
      terms are not summed across tiers.
    """
    validate_match_mode(match_mode)

    query_lower = query.lower().strip()
    results = []
    match_all = query_lower == "" or query_lower == "*"

    terms = [query_lower]
    if match_mode == MATCH_MODE_ANY_TERM and not match_all:
        # Deduplicated, order preserved: the tie-break below counts matched
        # terms, so a word the caller happened to repeat ("AI in the public
        # sector and AI in the private sector") would otherwise be counted once
        # per occurrence and could reorder same-tier results on repetition
        # alone.
        terms = list(dict.fromkeys(query_lower.split()))[:MAX_ANY_TERM_TERMS]

    # Ranked during the scan rather than by a sort key afterwards. The key was
    # `max(score(n, t) for t in matched_terms[n.id])` inside a lambda, so every
    # matched node paid a lambda call, a generator, a `max` and a dict lookup -
    # and in substring mode there is exactly one term, which makes the generator
    # and the `max` pure overhead. Measured at 100k matched nodes that framing
    # cost about 200 ms of a 447 ms query.
    #
    # The tuple carries a decreasing index so the sort never has to compare two
    # Nodes (they are not orderable) and, more importantly, so that equal keys
    # keep the order they were scanned in - which is what the stable sort this
    # replaces produced, and therefore what callers have been seeing.
    scored: List[tuple] = []
    single_term = terms[0] if len(terms) == 1 else None

    # Ask the index which nodes can match before walking anything. It answers
    # with the same `in` test, at C speed over one joined copy of the text, so
    # the walk below visits candidates rather than the whole graph. None means
    # it declined - no corpus, or a term it cannot answer safely - and then the
    # walk is over everything, exactly as it was.
    candidate_ids = None
    if (
        not match_all
        and single_term is not None
        and hasattr(searchable_text_cache, "candidates")
    ):
        candidate_ids = searchable_text_cache.candidates(single_term)

    # Note what this does NOT do: wrap both sources in one generator yielding
    # (id, node). That reads better and costs 0.7 us a node in iteration and
    # unpacking, which is 70 ms over a graph this size - paid by the fallback
    # walk, the path that is already the slow one. The candidate list is
    # materialised instead, which is affordable because `candidates` declines
    # when it would be long.
    if candidate_ids is not None:
        scan = [node for node in map(nodes.get, candidate_ids) if node is not None]
    else:
        scan = nodes.values()

    # Bound once: a plain dict when the caller passed one, the index's
    # read-only view when it passed an index. Either way `records_get` below is
    # a C-level lookup rather than a Python method call per node.
    records_get = getattr(searchable_text_cache, "records", searchable_text_cache).get
    # Memoised per type, keyed by the type key alone rather than by
    # (type, term): the term is fixed for the whole scan in the single-term
    # case, and building a tuple key per node cost more than the three string
    # comparisons it saved - measured, it made a matches-everything query 30%
    # SLOWER. The multi-term branch keeps its own dict per term.
    type_bonuses: Dict[str, int] = {}
    type_bonuses_by_term: Dict[str, Dict[str, int]] = {}

    for index, node in enumerate(scan):
        if node_types and node.type not in node_types:
            continue

        if not include_archived and getattr(node, "archived", False):
            continue

        if match_all:
            results.append(node)
            continue

        fields = records_get(node.id)
        if fields is None:
            fields = build_match_fields(node, type_searchable_text)
            searchable_text_cache[node.id] = fields

        if single_term is not None:
            # Already established when the candidates came from the index; the
            # test is kept for the fallback walk, and is cheap on a candidate.
            if single_term not in fields.text:
                continue
            bonus = type_bonuses.get(fields.type_key)
            if bonus is None:
                bonus = type_bonuses[fields.type_key] = score_type(
                    fields.type_name, fields.type_text, single_term
                )
            best = score_fields(fields, single_term, bonus)
            hit_count = 1
        else:
            best = -1
            hit_count = 0
            for term in terms:
                if term in fields.text:
                    hit_count += 1
                    per_type = type_bonuses_by_term.get(term)
                    if per_type is None:
                        per_type = type_bonuses_by_term[term] = {}
                    bonus = per_type.get(fields.type_key)
                    if bonus is None:
                        bonus = per_type[fields.type_key] = score_type(
                            fields.type_name, fields.type_text, term
                        )
                    term_score = score_fields(fields, term, bonus)
                    if term_score > best:
                        best = term_score
            if not hit_count:
                continue

        scored.append((best, hit_count, -index, node))

    if match_all:
        return results[:limit]

    scored.sort(reverse=True)
    return [entry[3] for entry in scored[:limit]]


# ---------------------------------------------------------------------------
# Semantic (embedding) search
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    nodes: Dict[str, Node],
    vector_store: VectorStore,
    query: str,
    node_types: Optional[List[NodeType]] = None,
    limit: int = 50,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    include_archived: bool = False,
) -> List[Node]:
    """Rank nodes by embedding (cosine) similarity to *query*.

    Reuses the same VectorStore embedding path as :func:`find_similar_nodes`:
    the query text is embedded and compared against the stored node embeddings
    (built from name + summary + description + tags on create/update). Returns
    nodes ordered by descending similarity, keeping only those at or above
    *threshold*.

    When the embedding model or the stored embeddings are unavailable — e.g. the
    ML-free base install where ``VectorStore.search`` cannot embed the query —
    ``search`` returns nothing and this yields an empty list, so callers can keep
    their lexical result unchanged.
    """
    query_text = (query or "").strip()
    if not query_text or query_text == "*":
        return []

    # Over-fetch so the node-type / archived filtering below cannot starve the
    # requested limit when the top hits are filtered out.
    fetch_limit = max(limit * 4, limit)
    ranked = vector_store.search(
        query_text=query_text, limit=fetch_limit, threshold=threshold
    )

    results: List[Node] = []
    for node_id, _score in ranked:
        node = nodes.get(node_id)
        if node is None:
            continue
        if node_types and node.type not in node_types:
            continue
        if not include_archived and getattr(node, "archived", False):
            continue
        results.append(node)
        if len(results) >= limit:
            break
    return results


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
