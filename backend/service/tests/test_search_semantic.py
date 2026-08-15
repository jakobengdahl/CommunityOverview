"""
Tests for semantic (embedding) ranking on search_graph.

The default lexical search matches substrings, so multi-word / natural-language
queries that no node contains verbatim return nothing. These tests cover the
opt-in ``semantic`` flag and the automatic lexical->semantic fallback, mocking
the embedding provider (VectorStore.search) exactly as the ML-free base install
degrades — no embedding model is loaded here.
"""

import os
import tempfile

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService


def _nodes():
    return [
        Node(
            id="sec",
            type=NodeType.INITIATIVE,
            name="Auth bypass remediation",
            description="Close the token validation gap flagged by CodeQL.",
        ),
        Node(
            id="ready",
            type=NodeType.INITIATIVE,
            name="Delivery status tag",
            description="Readiness is a maintained tag on each node.",
        ),
        Node(
            id="misc",
            type=NodeType.ACTOR,
            name="Zeta",
            description="Unrelated party.",
        ),
    ]


def _service(ranking):
    """Build a service whose vector store returns *ranking* for any text query.

    ``ranking`` is an ordered list of ``(node_id, score)`` tuples simulating
    cosine similarity. The fake honours the threshold and limit the production
    code passes, and records every call so tests can assert the semantic path
    was (or was not) taken.
    """
    tmp = tempfile.mkdtemp()
    storage = GraphStorage(
        json_path=os.path.join(tmp, "g.json"),
        embeddings_path=os.path.join(tmp, "e.pkl"),
    )
    storage.add_nodes(_nodes(), [])

    calls = []

    def fake_search(query_text=None, query_node=None, limit=5, threshold=0.0):
        calls.append(query_text)
        hits = [(nid, s) for nid, s in ranking if s >= threshold]
        return hits[:limit]

    storage.vector_store.search = fake_search
    return GraphService(storage), calls


def _service_with_tags(ranking):
    """Like :func:`_service` but the ``sec`` node carries a ``blocked`` tag, so a
    ``tags_none=["blocked"]`` filter removes a node that still matched lexically.
    """
    tmp = tempfile.mkdtemp()
    storage = GraphStorage(
        json_path=os.path.join(tmp, "g.json"),
        embeddings_path=os.path.join(tmp, "e.pkl"),
    )
    nodes = _nodes()
    nodes[0].tags = ["blocked"]  # the "Auth bypass remediation" node
    storage.add_nodes(nodes, [])

    calls = []

    def fake_search(query_text=None, query_node=None, limit=5, threshold=0.0):
        calls.append(query_text)
        hits = [(nid, s) for nid, s in ranking if s >= threshold]
        return hits[:limit]

    storage.vector_store.search = fake_search
    return GraphService(storage), calls


def _names(result):
    return {n["name"] for n in result["nodes"]}


class TestDefaultUnchanged:
    def test_lexical_hit_does_not_touch_semantic(self):
        # "Zeta" matches a node name lexically, so semantic must not run.
        service, calls = _service(ranking=[("sec", 0.9)])
        result = service.search_graph(query="Zeta")
        assert _names(result) == {"Zeta"}
        assert result["semantic"] is False
        assert calls == []  # embedding provider never consulted

    def test_empty_query_returns_all_without_semantic(self):
        service, calls = _service(ranking=[("sec", 0.9)])
        result = service.search_graph(query="")
        assert _names(result) == {
            "Auth bypass remediation",
            "Delivery status tag",
            "Zeta",
        }
        assert result["semantic"] is False
        assert calls == []

    def test_star_query_does_not_fall_back(self):
        service, calls = _service(ranking=[("sec", 0.9)])
        result = service.search_graph(query="*")
        assert result["semantic"] is False
        assert calls == []


class TestSemanticFlag:
    def test_flag_ranks_by_meaning(self):
        # Query shares no verbatim substring with any node.
        service, calls = _service(ranking=[("sec", 0.82), ("ready", 0.11)])
        result = service.search_graph(
            query="security hardening in the core", semantic=True
        )
        assert _names(result) == {"Auth bypass remediation"}  # below-threshold dropped
        assert result["semantic"] is True
        assert calls == ["security hardening in the core"]

    def test_flag_respects_node_type_filter(self):
        # Vector store would rank the Actor node, but the type filter excludes it.
        service, _ = _service(ranking=[("misc", 0.95), ("sec", 0.80)])
        result = service.search_graph(
            query="anything conceptual",
            semantic=True,
            node_types=["Initiative"],
        )
        assert _names(result) == {"Auth bypass remediation"}


class TestAutoFallback:
    def test_zero_lexical_hits_fall_back_to_semantic(self):
        service, calls = _service(ranking=[("ready", 0.77)])
        result = service.search_graph(query="how do we represent readiness")
        assert _names(result) == {"Delivery status tag"}
        assert result["semantic"] is True
        assert calls == ["how do we represent readiness"]

    def test_fallback_yielding_nothing_stays_empty(self):
        # Non-empty query, no lexical hit, and the provider also returns nothing.
        service, calls = _service(ranking=[])
        result = service.search_graph(query="something with no match at all")
        assert result["nodes"] == []
        assert result["semantic"] is False
        assert calls == ["something with no match at all"]

    def test_no_fallback_when_lexical_returns_hits(self):
        # "Auth" is a lexical substring of a node name; semantic must stay off
        # even though the provider could return a different ranking.
        service, calls = _service(ranking=[("ready", 0.99)])
        result = service.search_graph(query="Auth")
        assert _names(result) == {"Auth bypass remediation"}
        assert result["semantic"] is False
        assert calls == []

    def test_lexical_hit_narrowed_away_by_filter_does_not_fall_back(self):
        # The headline invariant: the fallback gates on RAW lexical matches, not
        # the post-filter set. "Auth" matches a node lexically, but a tag filter
        # excludes it — leaving zero visible results. Semantic must NOT widen the
        # query by meaning; the empty result is left to the (here absent)
        # federation path instead of surfacing meaning-related nodes.
        service, calls = _service_with_tags(ranking=[("ready", 0.99)])
        result = service.search_graph(query="Auth", tags_none=["blocked"])
        assert result["nodes"] == []  # the lexical hit was filtered out
        assert result["semantic"] is False  # not widened by meaning
        assert calls == []  # embedding provider never consulted

    def test_semantic_flag_with_match_all_query_falls_through_to_lexical(self):
        service, calls = _service(ranking=[("sec", 0.9)])
        for q in ("", "*"):
            result = service.search_graph(query=q, semantic=True)
            assert _names(result) == {
                "Auth bypass remediation",
                "Delivery status tag",
                "Zeta",
            }
            assert result["semantic"] is False
        assert calls == []  # no meaning to rank by; provider never consulted
