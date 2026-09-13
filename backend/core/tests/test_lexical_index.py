"""The lexical index: what it must not change, and what it must notice.

`search_nodes` used to walk every node and test `term in text` per node in
Python. It now asks a `LexicalIndex` which nodes can match - the same test,
run once at C speed over a joined copy of the same text - and walks only
those. Ranking is a product surface, so the whole point is that nothing about
the answer changes; these tests are mostly about that, and about the index
noticing every way its corpus can go stale.
"""

import pytest

from backend.core.models import Node, NodeType
from backend.core.storage import GraphStorage
from backend.core.storage_search import (
    LexicalIndex,
    build_match_fields,
    score_fields,
    score_node_match,
    search_nodes,
)


TYPE_TEXT = {}


def _node(node_id, name, description="", **kwargs):
    return Node(
        id=node_id,
        type=NodeType.ACTOR,
        name=name,
        description=description,
        **kwargs,
    )


def _index(nodes):
    index = LexicalIndex()
    for node in nodes:
        index[node.id] = build_match_fields(node, TYPE_TEXT)
    return index


class TestTheIndexAnswersWhatTheWalkWouldHave:
    """The index is an optimisation, so its answer has to be the walk's."""

    @staticmethod
    def _corpus_nodes():
        return [
            _node("a", "Statistics Sweden", "national statistical office"),
            _node("b", "statistical programme", "a programme about statistics"),
            _node("c", "Eurostat", "the European statistical office", tags=["eu"]),
            _node("d", "unrelated", "nothing to see"),
            _node("e", "STATISTICS uppercase", "case is folded before indexing"),
        ]

    @pytest.mark.parametrize(
        "term",
        ["stat", "statistics", "office", "eu", "nothing", "zzz", "s", "programme"],
    )
    def test_candidates_match_a_plain_walk_of_the_same_text(self, term):
        nodes = self._corpus_nodes()
        index = _index(nodes)

        walked = {
            node.id
            for node in nodes
            if term in build_match_fields(node, TYPE_TEXT).text
        }
        offered = index.candidates(term)
        # None means the index declined and the caller walks - which is
        # always correct, so it is only a failure if it offers a WRONG set.
        if offered is not None:
            assert set(offered) == walked, (
                f"the index and the walk disagree on {term!r}"
            )

    def test_the_whole_ranking_is_unchanged_by_going_through_the_index(self):
        nodes = self._corpus_nodes()
        by_id = {node.id: node for node in nodes}
        index = _index(nodes)
        plain = {node_id: index[node_id] for node_id in index}

        for term in ("stat", "statistics", "office", "eu", "programme"):
            through_index = search_nodes(by_id, index, TYPE_TEXT, query=term, limit=50)
            through_walk = search_nodes(
                by_id, dict(plain), TYPE_TEXT, query=term, limit=50
            )
            assert [n.id for n in through_index] == [n.id for n in through_walk], (
                f"ranking differs for {term!r} depending on the path taken"
            )

    def test_a_term_holding_the_separator_is_declined_rather_than_answered(self):
        """A match spanning two nodes' text is only possible through the
        separator, so the one query that could produce one is refused. None -
        not an empty list - because the caller must walk, not conclude."""
        index = _index([_node("a", "first"), _node("b", "second")])
        assert index.candidates("t\x00s") is None

    def test_declining_is_not_an_empty_answer(self):
        """The two are different and a caller that confuses them silently
        returns nothing for a perfectly good query."""
        index = _index([_node("a", "alpha")])
        assert index.candidates("zzz") == []
        assert index.candidates("") is None


class TestTheCorpusNoticesEveryWayItCanGoStale:
    """A derived structure is only correct while it is invalidated, and this
    one is derived from a dict that is written at six places in GraphStorage.
    That is why the index owns the dict rather than sitting beside it."""

    def test_a_node_added_after_the_first_search_is_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        storage = GraphStorage()
        storage.add_nodes([_node("a", "first node")], [])
        assert [n.id for n in storage.search_nodes(query="first")] == ["a"]

        storage.add_nodes([_node("b", "second node")], [])
        assert [n.id for n in storage.search_nodes(query="second")] == ["b"]

    def test_a_renamed_node_is_found_under_its_new_name_and_not_its_old(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        storage = GraphStorage()
        storage.add_nodes([_node("a", "before")], [])
        assert [n.id for n in storage.search_nodes(query="before")] == ["a"]

        storage.update_node("a", {"name": "after"})
        assert [n.id for n in storage.search_nodes(query="after")] == ["a"]
        assert storage.search_nodes(query="before") == []

    def test_a_deleted_node_stops_being_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        storage = GraphStorage()
        storage.add_nodes([_node("a", "doomed"), _node("b", "surviving")], [])
        assert [n.id for n in storage.search_nodes(query="doomed")] == ["a"]

        # confirmed=True, or the call is refused and this test would be
        # asserting that a deletion which never happened changed nothing.
        result = storage.delete_nodes(["a"], confirmed=True)
        assert result.success, result.message

        assert storage.search_nodes(query="doomed") == []
        assert [n.id for n in storage.search_nodes(query="surviving")] == ["b"]

    def test_a_removed_record_leaves_the_corpus(self):
        """Deleting a node is caught by `search_nodes` whatever the corpus
        says, because a candidate that is no longer in `nodes` is dropped - so
        the test above passes even with the invalidation on `pop` removed, and
        cannot stand in for this one.

        What the invalidation actually buys is that the dead text stops being
        scanned and stops counting towards the selectivity gate. A graph that
        deletes heavily would otherwise keep declining the index over text
        nobody can match."""
        index = _index([_node("a", "doomed"), _node("b", "surviving")])
        assert index.candidates("doomed") == ["a"]

        index.pop("a")
        assert index.candidates("doomed") == [], (
            "the corpus still holds the removed node's text"
        )
        assert index.candidates("surviving") == ["b"]

    def test_the_read_only_view_cannot_be_written_through(self):
        """The scan looks records up through this view because a Python-level
        `get` in that loop cost 75 ms over 100k nodes. It must stay a view: a
        write that landed here would not mark the corpus stale."""
        index = _index([_node("a", "alpha")])
        with pytest.raises(TypeError):
            index.records["b"] = None


class TestRankingHasOneImplementation:
    def test_scoring_a_node_is_scoring_its_prepared_fields(self):
        """`score_node_match` exists for callers holding a Node; the scan uses
        the cached record. They cannot drift apart because the first is
        defined as the second - this asserts that is still true."""
        nodes = [
            _node("a", "esam", "about esam", tags=["esam"], aliases=["ESAM"]),
            _node("b", "something else", "mentions esam once"),
            _node("c", "Esamverkan", "prefix match", subtypes=["esam-related"]),
        ]
        for node in nodes:
            for term in ("esam", "es", "verkan", "zzz"):
                assert score_node_match(node, term, TYPE_TEXT) == score_fields(
                    build_match_fields(node, TYPE_TEXT), term
                )

    def test_the_memoised_type_bonus_is_what_would_have_been_computed(self):
        """The scan computes the type tier once per type rather than once per
        node and passes it in. Passing it must change nothing."""
        from backend.core.storage_search import score_type

        node = _node("a", "actor", "an actor node")
        fields = build_match_fields(node, TYPE_TEXT)
        for term in ("actor", "act", "node", "zzz"):
            bonus = score_type(fields.type_name, fields.type_text, term)
            assert score_fields(fields, term, bonus) == score_fields(fields, term)
