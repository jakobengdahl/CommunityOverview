"""Tests for query-time attachment passage retrieval.

Everything here runs without the optional ML extra: the embedding model is a
small fake with an ``encode`` method, as the real one is only ever used once a
caller has already loaded it.
"""

import sys
from unittest.mock import patch

import numpy as np
import pytest

from backend.core.attachment_retrieval import (
    RANKING_BM25,
    RANKING_HYBRID,
    Segment,
    bm25_scores,
    chunk_segments,
    rank_passages,
    tokenize,
)
from backend.core.vector_store import VectorStore


class FakeModel:
    """Embeds the query as [1, 0] and a text as [affinity(text), 1].

    Cosine to the query is then monotonic in ``affinity``, so a test states
    the embedding order it wants as a plain function of the text.
    """

    def __init__(self, query, affinity):
        self.query = query
        self.affinity = affinity
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return np.array(
            [[1.0, 0.0] if t == self.query else [self.affinity(t), 1.0] for t in texts]
        )


class StoreWith:
    def __init__(self, model):
        self.model = model


def _words(prefix, n):
    return " ".join(f"{prefix}{i}" for i in range(n))


# --- G1: every passage carries its source locator --------------------------


def test_chunks_never_cross_a_segment_boundary_and_keep_locator_and_source():
    segments = [
        Segment(_words("alpha", 600), "page 1", {"node_id": "n1", "file": "a"}),
        Segment(_words("beta", 600), "page 2", {"node_id": "n1", "file": "a"}),
    ]
    chunks = chunk_segments(segments, chunk_size=300, overlap=50)

    assert len(chunks) > 4
    for chunk in chunks:
        owner = segments[0] if chunk.locator == "page 1" else segments[1]
        assert chunk.locator in ("page 1", "page 2")
        assert owner.text[chunk.start : chunk.start + len(chunk.text)] == chunk.text
        assert dict(chunk.source) == {"node_id": "n1", "file": "a"}
        prefix = "alpha" if owner is segments[0] else "beta"
        assert all(tok.startswith(prefix) for tok in tokenize(chunk.text))


def test_chunks_respect_size_overlap_and_cover_every_word():
    text = _words("w", 1000)
    chunks = chunk_segments([Segment(text, "p1")], chunk_size=1200, overlap=200)

    assert all(len(c.text) <= 1200 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start < prev.start + len(prev.text)  # windows overlap
        assert nxt.start > prev.start  # and always advance
    covered = set()
    for c in chunks:
        covered.update(tokenize(c.text))
    assert covered == set(tokenize(text))


def test_chunks_do_not_cut_words_when_whitespace_is_available():
    text = _words("word", 800)
    for chunk in chunk_segments([Segment(text, "p")], chunk_size=500, overlap=100):
        assert set(tokenize(chunk.text)) <= set(text.split())


def test_unbroken_text_longer_than_a_chunk_is_still_chunked():
    text = "x" * 3000
    chunks = chunk_segments([Segment(text, "p")], chunk_size=1000, overlap=100)
    assert len(chunks) >= 3
    assert chunks[-1].start + len(chunks[-1].text) == len(text)


def test_empty_and_symbol_only_segments_yield_no_chunks():
    assert chunk_segments([Segment("", "p1"), Segment("  -- \n", "p2")]) == []


def test_every_ranked_passage_carries_its_segment_locator_and_source():
    segments = [
        Segment("The policy requires human oversight.", "page 3", {"att": "x"}),
        Segment("Unrelated budget table.", "sheet Budget", {"att": "y"}),
        Segment("Human review is mandatory.", "slide 2", {"att": "z"}),
    ]
    result = rank_passages("human", segments)

    by_locator = {p.locator: p for p in result.passages}
    assert set(by_locator) == {"page 3", "slide 2"}
    assert dict(by_locator["page 3"].source) == {"att": "x"}
    assert dict(by_locator["slide 2"].source) == {"att": "z"}


@pytest.mark.parametrize(
    "kwargs",
    [{"chunk_size": 0}, {"chunk_size": 100, "overlap": 100}, {"overlap": -1}],
)
def test_invalid_chunk_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        chunk_segments([Segment("text", "p")], **kwargs)


@pytest.mark.parametrize("kwargs", [{"top_k": 0}, {"rerank_candidates": 0}])
def test_invalid_rank_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        rank_passages("q", [Segment("q", "p")], **kwargs)


# --- G2: works with no ML extra and reports ranking='bm25' ------------------


def test_no_vector_store_ranks_with_bm25():
    result = rank_passages("policy", [Segment("AI policy", "p1")])
    assert result.ranking == RANKING_BM25
    assert [p.locator for p in result.passages] == ["p1"]


def test_real_vector_store_without_ml_extra_falls_back_to_bm25_without_loading():
    store = VectorStore()
    segments = [Segment("AI policy one", "p1"), Segment("AI policy two", "p2")]
    # A None entry in sys.modules makes the import raise ImportError, as on a
    # base install without sentence-transformers.
    with patch.dict(sys.modules, {"sentence_transformers": None}):
        result = rank_passages("policy", segments, vector_store=store)

    assert result.ranking == RANKING_BM25
    assert store.model is None
    assert len(result.passages) == 2


def test_unloaded_model_is_never_loaded_by_a_query():
    store = VectorStore()
    with patch.object(VectorStore, "_load_model", side_effect=AssertionError):
        result = rank_passages(
            "policy",
            [Segment("policy a", "p1"), Segment("policy b", "p2")],
            vector_store=store,
        )
    assert result.ranking == RANKING_BM25


def test_failing_model_falls_back_to_bm25_order():
    class Broken:
        def encode(self, texts):
            raise RuntimeError("model crashed")

    segments = [Segment("policy policy", "p1"), Segment("policy", "p2")]
    plain = rank_passages("policy", segments)
    result = rank_passages("policy", segments, vector_store=StoreWith(Broken()))

    assert result.ranking == RANKING_BM25
    assert result.passages == plain.passages


@pytest.mark.parametrize("bad", [np.zeros((1, 2)), np.array([[np.nan, 1.0]] * 3)])
def test_malformed_model_output_falls_back_to_bm25(bad):
    class Odd:
        def encode(self, texts):
            return bad

    segments = [Segment("policy a", "p1"), Segment("policy b", "p2")]
    result = rank_passages("policy", segments, vector_store=StoreWith(Odd()))
    assert result.ranking == RANKING_BM25


# --- G3: deterministic order for equal scores ------------------------------


def test_equal_scores_keep_input_order():
    segments = [Segment("same policy text", f"page {i}") for i in range(6)]
    result = rank_passages("policy", segments, top_k=6)
    assert [p.locator for p in result.passages] == [f"page {i}" for i in range(6)]
    assert len({p.score for p in result.passages}) == 1


def test_repeated_queries_return_identical_results():
    segments = [
        Segment(f"policy {_words('t', i)} oversight", f"p{i}") for i in range(20)
    ]
    first = rank_passages("policy oversight", segments)
    for _ in range(3):
        assert rank_passages("oversight policy", segments) == first


def test_hybrid_ties_are_broken_deterministically():
    segments = [Segment("same policy text", f"page {i}") for i in range(5)]
    query = "policy"
    model = FakeModel(query, lambda t: 0.5)
    result = rank_passages(query, segments, vector_store=StoreWith(model), top_k=5)
    assert result.ranking == RANKING_HYBRID
    assert [p.locator for p in result.passages] == [f"page {i}" for i in range(5)]


# --- G4: a verbatim query term ranks its chunk above chunks without it -----


@pytest.mark.parametrize(
    "term, text",
    [
        ("människan", "Beslut fattas alltid av människan i loopen."),
        ("oversight", "Decisions require human oversight at every step."),
    ],
)
def test_chunk_with_query_term_outranks_chunks_without_it(term, text):
    filler = [
        Segment(f"Annan text om budget och planering {i}.", f"p{i}") for i in range(8)
    ]
    segments = filler[:4] + [Segment(text, "hit")] + filler[4:]
    result = rank_passages(term, segments, top_k=20)
    assert [p.locator for p in result.passages] == ["hit"]


def test_matching_is_case_and_compatibility_insensitive():
    segments = [Segment("Other text", "p1"), Segment("MÄNNISKAN beslutar", "p2")]
    result = rank_passages("människan", segments)
    assert [p.locator for p in result.passages] == ["p2"]


def test_term_present_in_most_chunks_still_outranks_chunks_without_it():
    # With the classic Okapi IDF this term's weight is negative (it occurs in
    # more than half the chunks), which would rank its chunks last.
    segments = [Segment(f"policy clause {i}", f"p{i}") for i in range(9)]
    segments.append(Segment("annex without the word", "p9"))
    scores = bm25_scores(["policy"], [tokenize(s.text) for s in segments])
    assert all(s > 0 for s in scores[:9])
    assert scores[9] == 0
    result = rank_passages("policy", segments, top_k=20)
    assert "p9" not in {p.locator for p in result.passages}
    assert len(result.passages) == 9


def test_embedding_rerank_cannot_promote_a_chunk_without_the_term():
    query = "oversight"
    segments = [
        Segment("A note on oversight.", "hit"),
        Segment("Human supervision of every automated decision.", "semantic"),
    ]
    # The model strongly prefers the chunk with no lexical match.
    model = FakeModel(query, lambda t: 100.0 if "supervision" in t else -100.0)
    result = rank_passages(query, segments, vector_store=StoreWith(model))
    assert [p.locator for p in result.passages] == ["hit"]


def test_query_without_word_characters_returns_nothing():
    result = rank_passages("?!", [Segment("anything", "p1")])
    assert result.passages == []


# --- Hybrid re-rank and G5: bounded candidate set --------------------------


def test_hybrid_fuses_embedding_order_into_bm25_order():
    query = "policy"
    # BM25 order A, B, C, D (A has the term most often relative to length).
    segments = [
        Segment("policy policy policy policy", "A"),
        Segment("policy policy policy filler", "B"),
        Segment("policy policy filler filler", "C"),
        Segment("policy filler filler filler", "D"),
    ]
    assert [p.locator for p in rank_passages(query, segments).passages] == list("ABCD")
    # Embedding order B, C, D, A: RRF lifts B above A.
    affinity = {"B": 4.0, "C": 3.0, "D": 2.0, "A": 1.0}
    texts = {s.text: s.locator for s in segments}
    model = FakeModel(query, lambda t: affinity[texts[t]])
    result = rank_passages(query, segments, vector_store=StoreWith(model))

    assert result.ranking == RANKING_HYBRID
    assert [p.locator for p in result.passages] == ["B", "A", "C", "D"]


def test_rerank_never_sends_more_candidates_than_the_setting():
    query = "policy"
    segments = [Segment(f"policy item {i}", f"p{i}") for i in range(50)]
    model = FakeModel(query, lambda t: 0.0)
    result = rank_passages(
        query, segments, vector_store=StoreWith(model), rerank_candidates=5, top_k=20
    )

    assert result.ranking == RANKING_HYBRID
    assert len(model.calls) == 1
    sent_chunks = [t for t in model.calls[0] if t != query]
    assert len(sent_chunks) == 5
    assert len(result.passages) == 20


def test_hybrid_scores_stay_monotonic_past_the_candidate_cap():
    query = "policy"
    segments = [Segment(f"policy {_words('f', i)}", f"p{i}") for i in range(12)]
    model = FakeModel(query, lambda t: -len(t))
    result = rank_passages(
        query, segments, vector_store=StoreWith(model), rerank_candidates=4, top_k=12
    )
    scores = [p.score for p in result.passages]
    assert scores == sorted(scores, reverse=True)
    # Chunks past the cap keep their BM25 order.
    plain = [p.locator for p in rank_passages(query, segments, top_k=12).passages]
    assert [p.locator for p in result.passages][4:] == plain[4:]


def test_top_k_limits_the_result():
    segments = [Segment(f"policy {i}", f"p{i}") for i in range(10)]
    assert len(rank_passages("policy", segments, top_k=3).passages) == 3
