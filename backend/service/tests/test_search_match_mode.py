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

    def test_more_matched_terms_break_a_tie_within_the_same_tier(self):
        service = _service()

        result = service.search_graph(
            query="paid tiers sell", match_mode=MATCH_MODE_ANY_TERM
        )

        # Every hit here is description-tier, so the tie is broken by how many
        # terms matched: "plan" on two ("paid", "tiers"), "offering" on one.
        assert [n["id"] for n in result["nodes"]] == ["plan", "offering"]


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
