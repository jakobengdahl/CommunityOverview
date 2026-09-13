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
        """The scan looks records up through this view. What it must stay is a
        view: a write that landed here would not mark the corpus stale, and
        that is the reason - the speed difference is small and fixture-
        dependent, which an earlier version of this docstring overstated."""
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


class TestTheGateAndTheCorpusAtSizesTheFixturesAboveDoNotReach:
    """Every fixture above is a handful of nodes, which leaves two things
    unexercised: the gate's declining branch (it needs more than 64 hits) and
    any offset arithmetic whose error is smaller than one node's text."""

    @staticmethod
    def _many(count, shared="shared", text_length=3):
        """Short texts on purpose. An off-by-one in the corpus offset step
        drifts by one character per node, so a fixture whose texts are longer
        than the node count hides it - every position still lands inside the
        right node. Short texts make the drift visible."""
        return [
            Node(id=f"m{i}", type=NodeType.ACTOR, name=f"{shared}{i:0{text_length}d}")
            for i in range(count)
        ]

    def test_a_term_matching_most_of_the_graph_still_returns_results(self):
        """The gate declines above 64 hits, and declining must mean "walk
        instead", not "no matches". Returning [] here instead of None costs
        every result: 200 nodes sharing a term come back as zero."""
        nodes = self._many(200)
        by_id = {node.id: node for node in nodes}
        index = _index(nodes)

        assert index.candidates("shared") is None, (
            "this fixture no longer exercises the declining branch"
        )
        found = search_nodes(by_id, index, TYPE_TEXT, query="shared", limit=50)
        assert len(found) == 50, (
            "the gate declined and the walk did not happen - a decline was "
            "read as an empty result"
        )

    def test_every_node_maps_back_to_itself_across_a_long_corpus(self):
        """The offset step adds one for the separator. Dropping that walks the
        mapping off by one character per node, which the small fixtures cannot
        see. Asserted over every node, not a sample - the head of the corpus is
        correct under every off-by-one here, so only the tail shows it."""
        nodes = self._many(200, shared="k")
        index = _index(nodes)

        for node in nodes:
            term = node.name
            assert index.candidates(term) == [node.id], (
                f"{term!r} mapped to the wrong node - the corpus offsets have drifted"
            )

    def test_a_reload_replaces_the_corpus(self):
        """`clear()` and `update()` are only ever called as a pair, on the
        reload path, so removing the invalidation from either one alone is
        invisible. Removing it from both leaves the pre-reload corpus live -
        a search after an external refresh returns the departed node and
        misses the arrived one."""
        index = _index([_node("old", "departed")])
        assert index.candidates("departed") == ["old"]

        index.clear()
        index.update({"new": build_match_fields(_node("new", "arrived"), TYPE_TEXT)})

        assert index.candidates("arrived") == ["new"]
        assert index.candidates("departed") == [], (
            "the corpus still holds the records the reload replaced"
        )


class TestWhatGoesIntoTheMatchedText:
    """`MatchFields.text` is what every term is tested against, so dropping a
    field from it silently stops that field being searchable, and changing how
    the fields are joined changes which cross-field substrings match."""

    @pytest.mark.parametrize(
        "kwargs,term",
        [
            ({"name": "uniquename"}, "uniquename"),
            ({"name": "x", "description": "uniquedescription"}, "uniquedescription"),
            ({"name": "x", "summary": "uniquesummary"}, "uniquesummary"),
            ({"name": "x", "tags": ["uniquetag"]}, "uniquetag"),
            ({"name": "x", "subtypes": ["uniquesubtype"]}, "uniquesubtype"),
            ({"name": "x", "aliases": ["uniquealias"]}, "uniquealias"),
        ],
    )
    def test_each_field_is_reachable_on_its_own(self, kwargs, term):
        node = Node(id="a", type=NodeType.ACTOR, **kwargs)
        assert term in build_match_fields(node, TYPE_TEXT).text, (
            f"a term only in {list(kwargs)[-1]} is not searchable"
        )

    def test_the_fields_are_joined_with_a_space(self):
        """Joining without one lets a term span two fields that are not
        adjacent in any node's text: `ab` + `cd` would start matching `abcd`.
        The space is what keeps the fields separate."""
        node = Node(id="a", type=NodeType.ACTOR, name="ab", description="cd")
        text = build_match_fields(node, TYPE_TEXT).text
        assert "abcd" not in text
        assert "ab cd" in text


class TestTheRankingKeptItsOrderAndItsTieBreak:
    """`test_the_whole_ranking_is_unchanged_by_going_through_the_index` cannot
    reach these: they change the index path and the walk path identically, so
    the two still agree with each other while both are wrong."""

    def test_equal_scores_come_back_in_insertion_order(self):
        """The sort this replaced was stable, so equal keys kept scan order.
        The rewrite carries a decreasing index to reproduce that; flipping its
        sign reverses every tie and nothing else notices."""
        nodes = [_node(f"n{i}", "identical", "identical text") for i in range(6)]
        by_id = {node.id: node for node in nodes}

        found = search_nodes(
            by_id, _index(nodes), TYPE_TEXT, query="identical", limit=10
        )
        assert [n.id for n in found] == [f"n{i}" for i in range(6)], (
            "equal-scoring results are no longer in insertion order"
        )

    def test_matching_more_terms_breaks_a_tie_in_any_term_mode(self):
        """The documented rule: a node scores by its single best term, and the
        number of matched terms only breaks an exact tie. Nothing asserted the
        second half, so `hit_count` could be dropped or pinned to 1."""
        both = _node("both", "zzz", "alpha beta")
        one = _node("one", "zzz", "alpha only")
        by_id = {"one": one, "both": both}

        found = search_nodes(
            by_id,
            _index([one, both]),
            TYPE_TEXT,
            query="alpha beta",
            limit=10,
            match_mode="any_term",
        )
        assert [n.id for n in found] == ["both", "one"], (
            "the node matching both terms did not win the tie"
        )

    def test_a_node_is_ranked_by_its_strongest_term_not_its_last(self):
        """`best` keeps the maximum over the matched terms. Assigning
        unconditionally ranks a node by whichever term happened to come last in
        the query, which reorders results on word order alone."""
        # The node has to match BOTH terms, at DIFFERENT tiers - otherwise max
        # and last-wins agree and the mutation is invisible. `strong` matches
        # "alpha" on its name (300k+) and "beta" in its description (200); a
        # node ranked by its last term therefore collapses to 200 and loses to
        # a rival that was inserted first.
        rival = _node("rival", "zzz", "alpha and beta both appear here")
        strong = _node("strong", "alpha", "beta appears in the description")
        by_id = {"rival": rival, "strong": strong}

        for query in ("alpha beta", "beta alpha"):
            found = search_nodes(
                by_id,
                _index([rival, strong]),
                TYPE_TEXT,
                query=query,
                limit=10,
                match_mode="any_term",
            )
            assert found[0].id == "strong", (
                f"for {query!r} the name-tier node lost to a description match "
                f"- the node was ranked by its last term, not its best"
            )


class TestTheIndexIsActuallyConsulted:
    """G4 - per-query cost not growing with the nodes that do not match - is
    the reason this change exists, and every assertion above passes just as
    well when the index is never asked. Spied rather than timed: a wall-clock
    assertion is a flake, and what matters is whether the walk was skipped."""

    def test_a_selective_query_does_not_visit_every_node(self, monkeypatch):
        nodes = [_node(f"n{i}", f"node number {i}") for i in range(200)]
        nodes.append(_node("needle", "findmehere"))
        by_id = {node.id: node for node in nodes}
        index = _index(nodes)

        looked_up = []
        walked = []

        class _CountingNodes(dict):
            def get(self, key, default=None):
                looked_up.append(key)
                return dict.get(self, key, default)

            def values(self):
                walked.append(True)
                return dict.values(self)

        counting = _CountingNodes(by_id)
        found = search_nodes(counting, index, TYPE_TEXT, query="findmehere", limit=10)

        assert [n.id for n in found] == ["needle"]
        # Both halves are needed. Counting only the lookups misses the case
        # that matters most - a scan that never consults the index walks
        # `values()` and never calls `get` at all, so the lookup count is ZERO
        # and any "fewer than ten" bound passes.
        assert not walked, "the search walked every node instead of asking the index"
        assert 0 < len(looked_up) < 10, (
            f"the search looked up {len(looked_up)} nodes to return one"
        )


class TestAWriteThatLandsWhileTheCorpusIsBeingRebuilt:
    """`search_nodes` holds no lock; all fifteen writers in GraphStorage do.
    So a write genuinely overlaps a rebuild, and the rebuild takes ~100 ms at
    100k nodes. Clearing `_dirty` unconditionally at the end of a rebuild
    discarded that writer's flag: the corpus was marked clean without the new
    node in it, and the node stayed invisible to the candidate path until some
    unrelated write happened to dirty the index again.

    These drive the overlap deterministically - no threads, no timing - by
    letting a write land inside the corpus join, which is the widest part of
    the rebuild window.
    """

    @staticmethod
    def _during_rebuild(monkeypatch, index, land):
        """Run `land()` once, from inside the rebuild's corpus join."""
        from backend.core import storage_search

        separator = storage_search._CORPUS_SEPARATOR
        real_join = separator.join
        fired = []

        class _LandsMidJoin(str):
            def join(self, parts):
                joined = real_join(parts)
                if not fired:
                    fired.append(True)
                    land()
                return joined

        monkeypatch.setattr(
            storage_search, "_CORPUS_SEPARATOR", _LandsMidJoin(separator)
        )
        index._rebuild()
        assert fired, "the write never landed - the test proves nothing"

    def test_a_node_written_mid_rebuild_is_still_found_afterwards(self, monkeypatch):
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(10)])
        index._dirty = True

        def land():
            index["late"] = build_match_fields(
                _node("late", "arrivedlate needle"), TYPE_TEXT
            )

        self._during_rebuild(monkeypatch, index, land)

        # The corpus that was just built cannot contain `late`; the point is
        # that the index knows it, so the next question rebuilds.
        assert index._dirty, "the rebuild cleared the flag the writer had set"
        assert index.candidates("arrivedlate") == ["late"]

    def test_the_lost_node_stays_lost_which_is_why_the_flag_matters(self, monkeypatch):
        """Pin the consequence, not just the flag. Without the version check
        the node is invisible on every later query, not merely the next one -
        nothing re-dirties the index on its own."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(10)])
        index._dirty = True

        def land():
            index["late"] = build_match_fields(
                _node("late", "arrivedlate needle"), TYPE_TEXT
            )

        self._during_rebuild(monkeypatch, index, land)

        for _ in range(3):
            assert index.candidates("arrivedlate") == ["late"]

    def test_a_record_removed_mid_rebuild_does_not_break_the_rebuild(self, monkeypatch):
        """The old rebuild snapshotted the ids and then looked each one up
        again, so a `pop` in between raised KeyError out of an ordinary
        search. Building from a snapshot of the items cannot."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(10)])
        index._dirty = True

        self._during_rebuild(monkeypatch, index, lambda: index.pop("n3"))

        assert index._dirty
        assert "n3" not in index.candidates("shared")
        assert len(index.candidates("shared")) == 9

    def test_a_rebuild_with_no_write_in_it_still_settles(self, monkeypatch):
        """The counter must not leave the index permanently dirty - that would
        rebuild the corpus on every single query."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(10)])
        index._dirty = True

        assert len(index.candidates("shared")) == 10
        assert not index._dirty

        # and a second query must not rebuild again
        before = index._corpus
        index.candidates("shared")
        assert index._corpus is before

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda i, n: i.__setitem__("x", n), id="setitem"),
            pytest.param(lambda i, n: i.pop("n1"), id="pop"),
            pytest.param(lambda i, n: i.clear(), id="clear"),
            pytest.param(lambda i, n: i.update({"x": n}), id="update"),
        ],
    )
    def test_every_mutator_moves_the_counter_a_rebuild_reads(self, mutate):
        """Each of the four must bump it individually. A mutator that only set
        `_dirty` would be invisible to a rebuild already in flight, which is
        exactly the hole these tests exist to close."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(3)])
        index.candidates("shared")

        before = index._version
        mutate(index, build_match_fields(_node("x", "x shared"), TYPE_TEXT))
        assert index._version != before
