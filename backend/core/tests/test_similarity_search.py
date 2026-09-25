"""Unit tests for storage_search similarity helpers."""

from backend.core import storage_search
from backend.core.models import Node, NodeType


class _CountingNodes(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def values(self):
        self.values_calls += 1
        return super().values()


class _FakeVectorStore:
    def __init__(self, ranking=None):
        self.ranking = ranking or []
        self.calls = []

    def search(self, query_text=None, limit=5, threshold=0.0):
        self.calls.append((query_text, limit, threshold))
        return [
            (node_id, score) for node_id, score in self.ranking if score >= threshold
        ][:limit]


def _nodes():
    return _CountingNodes(
        {
            "a1": Node(id="a1", type=NodeType.ACTOR, name="Alpha Council"),
            "a2": Node(id="a2", type=NodeType.ACTOR, name="Alpha Collective"),
            "i1": Node(id="i1", type=NodeType.INITIATIVE, name="Beta Project"),
        }
    )


def test_batch_similarity_lexical_pass_walks_nodes_once_for_many_names():
    nodes = _nodes()
    vector_store = _FakeVectorStore()

    results = storage_search.find_similar_nodes_batch(
        nodes,
        vector_store,
        ["Alpha Council", "Beta Project", "Gamma"],
        threshold=0.6,
    )

    assert nodes.values_calls == 1
    assert [match.node.id for match in results["Alpha Council"]] == ["a1", "a2"]
    assert [match.node.id for match in results["Beta Project"]] == ["i1"]
    assert results["Gamma"] == []


def test_batch_similarity_matches_individual_results_for_lexical_and_semantic_hits():
    nodes = _nodes()
    vector_store = _FakeVectorStore([("i1", 0.85), ("a1", 0.8), ("missing", 0.9)])
    names = ["Alpha Council", "Beta Project"]

    individual = {
        name: storage_search.find_similar_nodes(
            nodes,
            vector_store,
            name,
            node_type=NodeType.ACTOR,
            threshold=0.6,
            limit=2,
        )
        for name in names
    }

    vector_store.calls.clear()
    batch = storage_search.find_similar_nodes_batch(
        nodes,
        vector_store,
        names,
        node_type=NodeType.ACTOR,
        threshold=0.6,
        limit=2,
    )

    assert {
        name: [
            (match.node.id, match.similarity_score, match.match_reason)
            for match in matches
        ]
        for name, matches in batch.items()
    } == {
        name: [
            (match.node.id, match.similarity_score, match.match_reason)
            for match in matches
        ]
        for name, matches in individual.items()
    }
    assert vector_store.calls == [
        ("Alpha Council", 2, 0.4),
        ("Beta Project", 2, 0.4),
    ]
