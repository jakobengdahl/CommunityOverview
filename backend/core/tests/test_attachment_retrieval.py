"""Tests for query-time attachment passage retrieval.

Everything here runs without the optional ML extra: the embedding model is a
small fake with an ``encode`` method, as the real one is only ever used once a
caller has already loaded it.
"""

import math
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


# --- Residue: pins for the fused score, tail rank, embedding guards, ties ----

_RRF_K = 60


def _locator_order(result):
    return [p.locator for p in result.passages]


class VectorModel:
    """Returns the vector registered for each text; the query is [1, 0]."""

    def __init__(self, query, vectors):
        self.query = query
        self.vectors = vectors

    def encode(self, texts):
        return np.array(
            [[1.0, 0.0] if t == self.query else self.vectors[t] for t in texts]
        )


def _decreasing_policy_segments():
    # BM25 order is the input order's reverse: more "policy" => higher score.
    return [
        Segment("policy filler filler filler", "S0"),
        Segment("policy policy filler filler", "S1"),
        Segment("policy policy policy filler", "S2"),
        Segment("policy policy policy policy", "S3"),
    ]


def test_hybrid_score_is_the_fused_rrf_value_not_the_bm25_score():
    query = "policy"
    segments = [
        Segment("policy policy policy policy", "A"),
        Segment("policy policy policy filler", "B"),
        Segment("policy policy filler filler", "C"),
        Segment("policy filler filler filler", "D"),
    ]
    # The model reverses the BM25 order A, B, C, D.
    affinity = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}
    texts = {s.text: s.locator for s in segments}
    model = FakeModel(query, lambda t: affinity[texts[t]])
    result = rank_passages(query, segments, vector_store=StoreWith(model))

    bm25_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    emb_rank = {"D": 0, "C": 1, "B": 2, "A": 3}
    for p in result.passages:
        expected = 1 / (_RRF_K + bm25_rank[p.locator] + 1) + 1 / (
            _RRF_K + emb_rank[p.locator] + 1
        )
        assert p.score == pytest.approx(expected)
        assert p.score != pytest.approx(p.bm25_score)
    scores = [p.score for p in result.passages]
    assert scores == sorted(scores, reverse=True)
    assert _locator_order(result) == ["A", "D", "B", "C"]


def test_tail_scores_continue_the_rank_sequence_past_the_candidate_cap():
    # The tail offset is only visible once the candidate cap exceeds the
    # handful of ranks where fused and one-sided scores are close.
    query = "policy"
    segments = [Segment(f"policy {_words('f', i)}", f"p{i}") for i in range(100)]
    model = FakeModel(query, lambda t: 0.0)
    result = rank_passages(
        query,
        segments,
        vector_store=StoreWith(model),
        rerank_candidates=70,
        top_k=100,
    )

    assert result.ranking == RANKING_HYBRID
    assert len(result.passages) == 100
    for rank in range(70, 100):
        assert result.passages[rank].score == pytest.approx(1 / (_RRF_K + rank + 1))
    scores = [p.score for p in result.passages]
    assert all(a > b for a, b in zip(scores, scores[1:]))


def test_model_returning_extra_rows_is_rejected_as_a_wrong_shape():
    class TooMany:
        def encode(self, texts):
            return np.ones((len(texts) + 2, 2))

    segments = [Segment("policy a", "p1"), Segment("policy b", "p2")]
    result = rank_passages("policy", segments, vector_store=StoreWith(TooMany()))
    assert result.ranking == RANKING_BM25


def test_zero_norm_vectors_are_scored_zero_rather_than_failing_the_rerank():
    query = "policy"
    segments = _decreasing_policy_segments()
    vectors = {
        "policy policy policy policy": [0.0, 0.0],
        "policy policy policy filler": [1.0, 0.1],
        "policy policy filler filler": [0.5, 1.0],
        "policy filler filler filler": [0.0, 0.0],
    }
    result = rank_passages(
        query, segments, vector_store=StoreWith(VectorModel(query, vectors))
    )

    assert result.ranking == RANKING_HYBRID
    # Embedding order S2, S1, then the two zero vectors in BM25 order.
    assert _locator_order(result) == ["S2", "S3", "S1", "S0"]


def test_embedding_order_uses_cosine_similarity_not_the_raw_dot_product():
    query = "policy"
    segments = [
        Segment("policy policy policy x", "X"),
        Segment("policy policy x x", "Y"),
        Segment("policy x x x", "Z"),
    ]
    vectors = {
        # Largest dot product with the query but nearly orthogonal to it.
        "policy policy policy x": [10.0, 100.0],
        # Smallest dot product, but almost parallel to the query.
        "policy policy x x": [1.0, 0.1],
        "policy x x x": [5.0, 5.0],
    }
    result = rank_passages(
        query, segments, vector_store=StoreWith(VectorModel(query, vectors))
    )
    # Cosine order is Y, Z, X; a raw dot product would put X first.
    assert _locator_order(result) == ["Y", "X", "Z"]


@pytest.mark.parametrize(
    "exc", [TypeError, KeyError, OSError, ImportError, ValueError, AttributeError]
)
def test_any_model_exception_falls_back_to_the_bm25_order(exc):
    class Raises:
        def encode(self, texts):
            raise exc("boom")

    segments = _decreasing_policy_segments()
    plain = rank_passages("policy", segments)
    result = rank_passages("policy", segments, vector_store=StoreWith(Raises()))

    assert result.ranking == RANKING_BM25
    assert result.passages == plain.passages


# --- G3: tie-breaks ------------------------------------------------------------


def test_fused_ties_prefer_the_higher_bm25_score_over_the_lower_ordinal():
    query = "policy"
    segments = _decreasing_policy_segments()
    # Embedding order S3, S1, S2, S0 swaps the ranks of S2 and S1, so their
    # fused scores are equal while S2 has the higher BM25 score and the higher
    # ordinal.
    affinity = {"S3": 4.0, "S1": 3.0, "S2": 2.0, "S0": 1.0}
    texts = {s.text: s.locator for s in segments}
    model = FakeModel(query, lambda t: affinity[texts[t]])
    result = rank_passages(query, segments, vector_store=StoreWith(model))

    by_locator = {p.locator: p for p in result.passages}
    assert by_locator["S1"].score == pytest.approx(by_locator["S2"].score)
    assert by_locator["S2"].bm25_score > by_locator["S1"].bm25_score
    assert _locator_order(result) == ["S3", "S2", "S1", "S0"]


def _equal_score_segments_with_unequal_starts():
    # Chunk 1 of the first segment starts at 9 (ordinal 1); the second
    # segment's only chunk starts at 0 (ordinal 2). Both hold the word once.
    return [Segment("aaaa bbbb policy", "late-start"), Segment("policy", "early")]


def test_equal_bm25_scores_are_ordered_by_ordinal_not_by_chunk_start():
    segments = _equal_score_segments_with_unequal_starts()
    result = rank_passages("policy", segments, chunk_size=10, overlap=0)

    first, second = result.passages
    assert first.score == second.score
    assert (first.start, second.start) == (9, 0)
    assert (first.ordinal, second.ordinal) == (1, 2)
    assert _locator_order(result) == ["late-start", "early"]


def test_equal_fused_scores_are_ordered_by_ordinal_not_by_chunk_start():
    # Capitalised so no chunk text equals the query string. The model prefers
    # the second chunk, so each chunk is first in one ranking and second in the
    # other: their fused scores are equal.
    query = "Policy"
    segments = _equal_score_segments_with_unequal_starts()
    vectors = {" policy": [0.5, 1.0], "policy": [2.0, 1.0]}
    model = VectorModel(query, vectors)
    result = rank_passages(
        query,
        segments,
        vector_store=StoreWith(model),
        chunk_size=10,
        overlap=0,
    )
    assert result.ranking == RANKING_HYBRID
    assert result.passages[0].score == result.passages[1].score
    assert _locator_order(result) == ["late-start", "early"]


def test_repeated_query_term_scores_as_the_deduplicated_query():
    docs = [tokenize(t) for t in ("policy oversight", "policy policy", "other")]
    assert bm25_scores(["policy", "policy", "oversight"], docs) == bm25_scores(
        ["policy", "oversight"], docs
    )

    segments = [
        Segment("policy and oversight", "a"),
        Segment("policy policy policy", "b"),
        Segment("budget table", "c"),
    ]
    repeated = rank_passages("policy policy oversight", segments)
    deduplicated = rank_passages("policy oversight", segments)
    assert repeated.passages == deduplicated.passages


# --- G4: normalisation and BM25 quality ---------------------------------------


def test_tokenize_folds_full_width_compatibility_forms_to_ascii():
    assert tokenize("ＰＯＬＩＣＹ ｏｖｅｒｓｉｇｈｔ") == ["policy", "oversight"]
    assert tokenize("ﬁnance") == ["finance"]


def test_tokenize_uses_full_case_folding_not_lowercasing():
    assert tokenize("STRASSE") == tokenize("straße") == ["strasse"]
    result = rank_passages(
        "STRASSE", [Segment("Die straße ist lang", "hit"), Segment("Annat", "miss")]
    )
    assert _locator_order(result) == ["hit"]


def test_tokenize_keeps_non_ascii_letters_inside_their_words():
    assert tokenize("Årsredovisning 2024: människan!") == [
        "årsredovisning",
        "2024",
        "människan",
    ]


def test_bm25_score_matches_the_okapi_formula_with_k1_and_b_applied():
    # tf=2 in a 3-token document, average length 2: neither tf nor the length
    # ratio is 1, so k1 and b both change the value.
    scores = bm25_scores(["a"], [["a", "a", "x"], ["y"]])
    idf = math.log(1 + 1.5 / 1.5)
    norm = 1.2 * (1 - 0.75 + 0.75 * 3 / 2)
    assert scores[0] == pytest.approx(idf * 2 * 2.2 / (2 + norm))
    assert scores[1] == 0


def test_bm25_more_occurrences_score_higher():
    scores = bm25_scores(["a"], [["a", "x", "x"], ["a", "a", "x"]])
    assert scores[1] > scores[0] > 0


def test_bm25_shorter_document_scores_higher_for_equal_term_frequency():
    scores = bm25_scores(["a"], [["a", "x"], ["a", "x", "x", "x", "x", "x"]])
    assert scores[0] > scores[1] > 0


def test_bm25_rarer_terms_weigh_more_than_common_ones():
    docs = [["rare", "x"], ["common", "x"], ["common", "y"], ["common", "z"]]
    rare = bm25_scores(["rare"], docs)[0]
    common = bm25_scores(["common"], docs)[1]
    assert rare > common > 0


def test_bm25_unmatched_empty_and_all_empty_inputs_score_zero():
    assert bm25_scores(["a"], []) == []
    assert bm25_scores([], [["a"], ["b"]]) == [0.0, 0.0]
    assert bm25_scores(["a"], [["b"], ["c"]]) == [0.0, 0.0]
    # Zero average length must not divide by zero.
    assert bm25_scores(["a"], [[], []]) == [0.0, 0.0]


# --- G1: chunk boundaries -----------------------------------------------------


def _slice_of(segment, chunk):
    return segment.text[chunk.start : chunk.start + len(chunk.text)]


def test_chunk_text_is_an_exact_slice_when_the_segment_starts_with_whitespace():
    segment = Segment("  \n " + _words("lead", 120) + "  ", "p")
    chunks = chunk_segments([segment], chunk_size=80, overlap=20)

    assert len(chunks) > 2
    assert chunks[0].start == 0
    for chunk in chunks:
        assert chunk.text == _slice_of(segment, chunk)


def test_unbroken_text_respects_size_and_overlap_exactly():
    segment = Segment("x" * 500, "p")
    chunks = chunk_segments([segment], chunk_size=50, overlap=10)

    assert all(len(c.text) <= 50 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.start + len(prev.text) - nxt.start == 10
    assert chunks[-1].start + len(chunks[-1].text) == 500


def test_overlap_never_exceeds_the_setting_on_spaced_text():
    segment = Segment(_words("tok", 200), "p")
    chunks = chunk_segments([segment], chunk_size=60, overlap=15)

    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.start + len(prev.text) - nxt.start <= 15
        assert nxt.start > prev.start


def test_zero_overlap_chunks_tile_the_text_without_gaps_or_repeats():
    segment = Segment(_words("tok", 200), "p")
    chunks = chunk_segments([segment], chunk_size=60, overlap=0)

    assert len(chunks) > 3
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start == prev.start + len(prev.text)
    assert "".join(c.text for c in chunks) == segment.text


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_non_final_chunks_are_at_least_half_the_chunk_size(separator):
    # A break near the start of every window must not shorten the window.
    segment = Segment(("ab" + separator + "x" * 200 + separator) * 3, "p")
    chunks = chunk_segments([segment], chunk_size=50, overlap=5)

    assert len(chunks) > 3
    assert all(len(c.text) >= 25 for c in chunks[:-1])


def test_no_chunk_is_without_word_characters():
    segment = Segment("first" + "-" * 100 + "second" + " " * 100 + "third", "p")
    chunks = chunk_segments([segment], chunk_size=20, overlap=5)

    assert chunks
    assert all(tokenize(c.text) for c in chunks)
    assert {t for c in chunks for t in tokenize(c.text)} >= {"first", "third"}


@pytest.mark.timeout(5)
def test_tiny_chunks_with_maximal_overlap_terminate():
    segment = Segment("ab cd ef gh ij kl " * 5, "p")
    chunks = chunk_segments([segment], chunk_size=4, overlap=3)

    starts = [c.start for c in chunks]
    assert starts == sorted(set(starts))
    assert all(len(c.text) <= 4 for c in chunks)
    assert chunks[-1].start + len(chunks[-1].text) == len(segment.text)


def test_overlap_is_kept_when_the_only_whitespace_in_it_ends_the_window():
    # Doubled space: the first chunk ends right after a space. Restarting at
    # the chunk end would silently drop the requested overlap.
    segment = Segment("xxxxxxx  " + "y" * 30, "p")
    chunks = chunk_segments([segment], chunk_size=10, overlap=3)

    assert (chunks[0].start, len(chunks[0].text)) == (0, 8)
    assert chunks[1].start == 5
