"""
Unit tests for storage_search.semantic_search_nodes.

The embedding provider (VectorStore.search) is faked, so no ML model is loaded —
mirroring the ML-free base install where query embedding is unavailable and the
vector search degrades to returning nothing.
"""

from backend.core import storage_search
from backend.core.models import Node, NodeType


class _FakeVectorStore:
    """Minimal VectorStore stand-in returning a fixed ranking."""

    def __init__(self, ranking):
        self.ranking = ranking
        self.calls = []

    def search(self, query_text=None, limit=5, threshold=0.0):
        self.calls.append((query_text, limit, threshold))
        hits = [(nid, s) for nid, s in self.ranking if s >= threshold]
        return hits[:limit]


def _nodes():
    return {
        "a": Node(id="a", type=NodeType.ACTOR, name="Alpha"),
        "i": Node(id="i", type=NodeType.INITIATIVE, name="Iota"),
        "arch": Node(id="arch", type=NodeType.ACTOR, name="Gamma", archived=True),
    }


def test_ranks_and_maps_ids_to_nodes():
    vs = _FakeVectorStore([("i", 0.9), ("a", 0.7)])
    results = storage_search.semantic_search_nodes(_nodes(), vs, "meaningful query")
    assert [n.id for n in results] == ["i", "a"]


def test_node_type_filter_excludes_others():
    vs = _FakeVectorStore([("a", 0.9), ("i", 0.8)])
    results = storage_search.semantic_search_nodes(
        _nodes(), vs, "query", node_types=[NodeType.INITIATIVE]
    )
    assert [n.id for n in results] == ["i"]


def test_archived_excluded_by_default():
    vs = _FakeVectorStore([("arch", 0.95), ("a", 0.6)])
    results = storage_search.semantic_search_nodes(_nodes(), vs, "query")
    assert [n.id for n in results] == ["a"]


def test_archived_included_when_requested():
    vs = _FakeVectorStore([("arch", 0.95), ("a", 0.6)])
    results = storage_search.semantic_search_nodes(
        _nodes(), vs, "query", include_archived=True
    )
    assert [n.id for n in results] == ["arch", "a"]


def test_limit_is_respected():
    vs = _FakeVectorStore([("i", 0.9), ("a", 0.8)])
    results = storage_search.semantic_search_nodes(_nodes(), vs, "query", limit=1)
    assert [n.id for n in results] == ["i"]


def test_empty_query_short_circuits_without_calling_provider():
    vs = _FakeVectorStore([("i", 0.9)])
    assert storage_search.semantic_search_nodes(_nodes(), vs, "") == []
    assert storage_search.semantic_search_nodes(_nodes(), vs, "   ") == []
    assert storage_search.semantic_search_nodes(_nodes(), vs, "*") == []
    assert vs.calls == []


def test_threshold_is_forwarded_to_provider():
    vs = _FakeVectorStore([("i", 0.9)])
    storage_search.semantic_search_nodes(_nodes(), vs, "query", threshold=0.5)
    assert vs.calls[0][2] == 0.5


def test_unknown_ids_from_provider_are_skipped():
    vs = _FakeVectorStore([("ghost", 0.9), ("a", 0.6)])
    results = storage_search.semantic_search_nodes(_nodes(), vs, "query")
    assert [n.id for n in results] == ["a"]
