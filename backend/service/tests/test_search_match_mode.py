"""
Tests for the opt-in lexical ``match_mode`` on search_graph.

The default (``substring``) requires the whole query verbatim, so a multi-word
query returns nothing unless a node contains the phrase. ``any_term`` is the
opt-in OR over the query's terms. The default's results must not move — every
existing caller depends on them — so that invariant is pinned first.
"""

import os
import tempfile

import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.core.storage_search import MATCH_MODE_ANY_TERM, MATCH_MODE_SUBSTRING
from backend.service import GraphService


def _service():
    """A service whose embedding search finds nothing.

    That is the ML-free base install CI runs, and it keeps these tests about the
    lexical matcher: the zero-result semantic fallback is covered in
    ``test_search_semantic.py`` and must not silently fill in for a lexical
    miss here.
    """
    tmp = tempfile.mkdtemp()
    storage = GraphStorage(json_path=os.path.join(tmp, "g.json"))
    storage.vector_store.search = lambda **kwargs: []
    storage.add_nodes(
        [
            Node(
                id="plan",
                type=NodeType.INITIATIVE,
                name="Pricing plan rollout",
                description="Introduce the paid plan tiers.",
            ),
            Node(
                id="offering",
                type=NodeType.INITIATIVE,
                name="Offering catalogue",
                description="What we sell, per segment.",
            ),
            Node(
                id="unrelated",
                type=NodeType.ACTOR,
                name="Zeta",
                description="Unrelated party.",
            ),
        ],
        [],
    )
    return GraphService(storage)


def _two_name_tier_service():
    """Two nodes whose name-tier scores discriminate "best term" from "sum".

    ``exact`` scores 500 000 on one term; ``two_terms`` scores 400 000 and
    300 000 on two — a sum (700 000) would rank it first, the best single term
    (400 000) must not.
    """
    tmp = tempfile.mkdtemp()
    storage = GraphStorage(json_path=os.path.join(tmp, "g.json"))
    storage.vector_store.search = lambda **kwargs: []
    storage.add_nodes(
        [
            Node(id="exact", type=NodeType.INITIATIVE, name="Pricing"),
            Node(id="two_terms", type=NodeType.INITIATIVE, name="Pricing plan rollout"),
        ],
        [],
    )
    return GraphService(storage)


class TestDefaultIsUnchanged:
    def test_default_still_requires_the_whole_query_verbatim(self):
        """The pre-existing semantics: a phrase no node contains matches nothing."""
        service = _service()

        result = service.search_graph(query="plan pricing offering")

        assert result["total"] == 0
        assert result["match_mode"] == MATCH_MODE_SUBSTRING

    def test_default_matches_a_substring_across_fields_as_before(self):
        service = _service()

        ids = {n["id"] for n in service.search_graph(query="plan")["nodes"]}

        assert ids == {"plan"}

    def test_explicit_substring_mode_equals_the_default(self):
        service = _service()

        default = service.search_graph(query="pricing")
        explicit = service.search_graph(
            query="pricing", match_mode=MATCH_MODE_SUBSTRING
        )

        assert [n["id"] for n in default["nodes"]] == [
            n["id"] for n in explicit["nodes"]
        ]


class TestAnyTermMode:
    def test_multi_word_query_matches_nodes_carrying_any_term(self):
        service = _service()

        result = service.search_graph(
            query="plan pricing offering", match_mode=MATCH_MODE_ANY_TERM
        )

        assert {n["id"] for n in result["nodes"]} == {"plan", "offering"}
        assert result["match_mode"] == MATCH_MODE_ANY_TERM

    def test_a_single_term_query_behaves_like_substring(self):
        service = _service()

        assert [
            n["id"]
            for n in service.search_graph(query="plan", match_mode=MATCH_MODE_ANY_TERM)[
                "nodes"
            ]
        ] == [n["id"] for n in service.search_graph(query="plan")["nodes"]]

    def test_best_matching_term_decides_rank_not_the_number_of_terms(self):
        """A name hit outranks a node matching more terms only weakly.

        ``offering`` matches the node named "Offering catalogue" on its name
        tier; ``plan`` and ``paid`` both hit the other node's name/description.
        Accumulated weak matches must never overtake a stronger single match.
        """
        service = _service()

        result = service.search_graph(
            query="offering paid tiers", match_mode=MATCH_MODE_ANY_TERM
        )

        assert [n["id"] for n in result["nodes"]] == ["offering", "plan"]

    def test_two_name_tier_hits_do_not_outscore_one_better_hit(self):
        """The invariant a summed score would break, at the tier that matters.

        "Pricing" is an exact name match for one term (the top tier); "Pricing
        plan rollout" matches *both* terms, each a weaker name-tier hit. Adding
        the two up would beat the exact match — scoring by the single best term
        keeps the stronger node first.
        """
        service = _two_name_tier_service()

        result = service.search_graph(
            query="pricing plan", match_mode=MATCH_MODE_ANY_TERM
        )

        assert [n["id"] for n in result["nodes"]] == ["exact", "two_terms"]

    def test_more_matched_terms_break_a_tie_within_the_same_tier(self):
        service = _service()

        result = service.search_graph(
            query="sell segment paid", match_mode=MATCH_MODE_ANY_TERM
        )

        # Every hit here is description-tier, so the tie is broken by how many
        # terms matched: "offering" on two ("sell", "segment"), "plan" on one.
        # "offering" is also the *later* node in insertion order, so a stable
        # sort without the tie-break would put "plan" first.
        assert [n["id"] for n in result["nodes"]] == ["offering", "plan"]

    def test_repeating_a_term_does_not_change_the_ranking(self):
        """A term counts once however often the caller wrote it.

        The tie-break counts matched terms, so counting a repeated word once per
        occurrence let repetition alone reorder same-tier results — and
        repetition is normal in the natural-language queries this mode exists to
        serve. Here "paid" repeated three times would give "plan" three hits
        against "offering"'s two and flip the pair.
        """
        service = _service()

        baseline = service.search_graph(
            query="sell segment paid", match_mode=MATCH_MODE_ANY_TERM
        )
        repeated = service.search_graph(
            query="sell segment paid paid paid", match_mode=MATCH_MODE_ANY_TERM
        )

        assert [n["id"] for n in repeated["nodes"]] == [
            n["id"] for n in baseline["nodes"]
        ]
        assert [n["id"] for n in repeated["nodes"]] == ["offering", "plan"]


class TestValidation:
    def test_unknown_mode_is_rejected(self):
        service = _service()

        with pytest.raises(ValueError):
            service.search_graph(query="plan", match_mode="fuzzy")

    def test_unknown_mode_is_rejected_even_on_the_semantic_path(self):
        """Semantic ranking skips the lexical matcher, so validation is up front."""
        service = _service()

        with pytest.raises(ValueError):
            service.search_graph(query="plan", semantic=True, match_mode="fuzzy")
