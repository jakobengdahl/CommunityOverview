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
    _PROBE_AFTER_DECLINES,
    _REBUILD_WORTH_IT_AFTER,
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


def _let_the_index_rebuild(index):
    """Say that the last corpus earned its keep, so the next query rebuilds.

    A rebuild waits until the previous corpus has answered enough queries to be
    worth another one, which is what keeps the index out of the way on a load
    that writes before every read. A test fixture has answered none, so it
    would decline - correctly, but it would answer the wrong question. Tests
    about what the CORPUS holds say this first; tests about what a SEARCH
    returns must not, because declining is one of the right answers there.
    """
    index._served_by_the_last_corpus = _REBUILD_WORTH_IT_AFTER
    index._declines = 0


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
        _let_the_index_rebuild(index)
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
        _let_the_index_rebuild(index)

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


class TestTypeLabelsReachEveryType:
    """The type-text lookup is keyed on the schema's type name. Legacy enum
    types used to look themselves up as ``str(NodeType.ACTOR)`` -
    ``'NodeType.ACTOR'`` - and miss, while schema-only string types hit, so
    localized labels were reachable for some types and not others."""

    LABELLED = {
        "Actor": "actor aktör",
        "CustomerSegment": "customersegment kundsegment",
        "Market": "market marknad",
    }

    @pytest.mark.parametrize(
        "node_type,label",
        [
            (NodeType.ACTOR, "aktör"),
            # Schema-only types stay plain strings; "Actor" would be coerced to
            # the enum and repeat the case above.
            ("CustomerSegment", "kundsegment"),
            ("Market", "marknad"),
        ],
    )
    def test_the_localized_label_is_searchable(self, node_type, label):
        fields = build_match_fields(
            Node(id="a", type=node_type, name="x"), self.LABELLED
        )
        assert label in fields.text
        assert fields.type_text == self.LABELLED[fields.type_key]

    @pytest.mark.parametrize("node_type", ["CustomerSegment", "Market"])
    def test_the_schema_only_cases_really_are_strings(self, node_type):
        assert not isinstance(Node(id="a", type=node_type, name="x").type, NodeType)

    def test_an_enum_type_does_not_match_its_python_repr(self):
        fields = build_match_fields(
            Node(id="a", type=NodeType.ACTOR, name="x"), self.LABELLED
        )
        assert fields.type_name == "actor"
        assert "nodetype" not in fields.text


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


class TestAStaleCorpusIsNeverTheAnswer:
    """The corpus is rebuilt whole, so between a write and the next rebuild it
    does not describe the records. Every earlier version of this class got that
    window wrong in a different way: one cleared the staleness flag at the END
    of the rebuild and lost a write that landed inside it; the next cleared it
    at the START, which meant every OTHER thread spent the rebuild answering
    from the previous corpus - a node committed before the query came back as
    no match at all.

    The property that replaced both: there is no flag. A corpus is published
    only when it matches the records, and any write drops it. So a reader sees
    a corpus that is exactly right, or sees none and walks.
    """

    def test_a_write_drops_the_corpus_rather_than_leaving_it_readable(self):
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(5)])
        index.candidates("shared")
        assert index._built is not None

        index["late"] = build_match_fields(_node("late", "arrivedlate"), TYPE_TEXT)

        assert index._built is None, (
            "the previous corpus is still readable, and it does not contain "
            "the node that was just written"
        )

    def test_the_new_node_is_found_and_never_reported_missing(self):
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(5)])
        index.candidates("shared")

        index["late"] = build_match_fields(_node("late", "arrivedlate"), TYPE_TEXT)

        # Either answer is correct - rebuild and find it, or decline and let
        # the walk find it. `[]` is the one answer that is not.
        for _ in range(3):
            answer = index.candidates("arrivedlate")
            assert answer in (["late"], None), answer

    @pytest.mark.parametrize(
        "mutate,term,expected",
        [
            pytest.param(
                lambda i, n: i.__setitem__("x", n), "sentinel", ["x"], id="setitem"
            ),
            pytest.param(lambda i, n: i.pop("n1"), "node 1 ", [], id="pop"),
            pytest.param(lambda i, n: i.clear(), "shared", [], id="clear"),
            pytest.param(
                lambda i, n: i.update({"x": n}), "sentinel", ["x"], id="update"
            ),
        ],
    )
    def test_every_mutator_drops_the_corpus(self, mutate, term, expected):
        """All four, individually. A mutator that changed the records but left
        the corpus published would serve answers from text that no longer
        describes the graph."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(5)])
        index.candidates("shared")

        mutate(index, build_match_fields(_node("x", "x sentinel shared"), TYPE_TEXT))

        assert index._built is None
        _let_the_index_rebuild(index)
        assert index.candidates(term) == expected

    def test_a_reader_cannot_see_a_corpus_a_rebuild_has_not_finished(self):
        """The publish is one assignment of one tuple, so a reader that catches
        a rebuild mid-flight reads either the old value or the new - never a
        corpus from one build against offsets from another, which maps every
        hit to the wrong record and loses the node that really matched."""
        index = _index([_node(f"n{i}", f"node{i} needle{i:03d}") for i in range(40)])
        seen = []

        from backend.core import storage_search

        separator = storage_search._CORPUS_SEPARATOR
        real_join = separator.join

        class _ReadsWhileTheBuildIsHalfDone(str):
            def join(self, parts):
                seen.append(index._built)  # mid-rebuild, before the publish
                return real_join(parts)

        monkey = _ReadsWhileTheBuildIsHalfDone(separator)
        storage_search._CORPUS_SEPARATOR = monkey
        try:
            index["x"] = build_match_fields(_node("x", "x needle999"), TYPE_TEXT)
            _let_the_index_rebuild(index)
            index.candidates("needle999")
        finally:
            storage_search._CORPUS_SEPARATOR = separator

        assert seen == [None], (
            "a half-built corpus was readable; it must stay None until the "
            "whole triple is published at once"
        )
        ids, corpus, starts = index._built
        assert len(ids) == len(starts) == corpus.count("\x00") + 1


class TestACorpusIsOnlyBuiltForALoadThatWillReadIt:
    """One write drops the whole corpus, so charging a rebuild to the next
    query means one rebuild per write. With a write between every two queries
    that made this index 1.6x SLOWER than the plain walk at 30k nodes - it
    stopped being an optimisation and became an overhead. A rebuild now waits
    until the previous corpus has actually answered queries.

    Counted in queries, deliberately, not in elapsed time: a wall-clock budget
    passes on an idle gap, which repays nothing, so a write-then-query trickle
    rebuilt on every query and measured twice the walk.
    """

    def test_a_write_between_queries_does_not_buy_a_rebuild_each_time(self):
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(200)])
        index.candidates("shared")

        rebuilds = []
        real = type(index)._rebuild_if_worth_it

        def counting(self):
            before = self._built
            real(self)
            if self._built is not before:
                rebuilds.append(True)

        type(index)._rebuild_if_worth_it = counting
        try:
            # The corpus in this fixture has answered nothing, which is the
            # state a write-before-every-read load keeps it in.
            index._served_by_the_last_corpus = 0
            index._declines = 0
            for i in range(20):
                index[f"n{i}"] = build_match_fields(
                    _node(f"n{i}", f"node {i} shared changed"), TYPE_TEXT
                )
                index.candidates("shared")
        finally:
            type(index)._rebuild_if_worth_it = real

        assert rebuilds == [], (
            f"{len(rebuilds)} rebuilds for 20 writes - the budget is not "
            f"holding any of them back"
        )

    def test_declining_is_not_a_wrong_answer(self):
        """What the budget buys is paid for by the walk, so the query must
        still be answered correctly while the corpus is out of date."""
        nodes = [_node(f"n{i}", f"node {i} shared") for i in range(5)]
        by_id = {n.id: n for n in nodes}
        index = _index(nodes)
        index.candidates("shared")

        newcomer = _node("late", "late arrivedlate shared")
        by_id["late"] = newcomer
        index["late"] = build_match_fields(newcomer, TYPE_TEXT)
        index._served_by_the_last_corpus = 0  # force the decline
        index._declines = 0

        assert index.candidates("arrivedlate") is None
        found = search_nodes(by_id, index, TYPE_TEXT, query="arrivedlate", limit=10)
        assert [n.id for n in found] == ["late"]

    def test_it_probes_again_rather_than_declining_for_ever(self):
        """A load that was write-bound once must not keep the index switched
        off however quiet it later becomes."""
        index = _index([_node(f"n{i}", f"node {i} shared") for i in range(5)])
        index.candidates("shared")
        index["late"] = build_match_fields(_node("late", "arrivedlate"), TYPE_TEXT)
        index._served_by_the_last_corpus = 0

        declined = 0
        while index.candidates("arrivedlate") is None:
            declined += 1
            assert declined <= _PROBE_AFTER_DECLINES, "the index never probed again"
        assert declined == _PROBE_AFTER_DECLINES - 1
        assert index.candidates("arrivedlate") == ["late"]


class TestTheIndexAlwaysCoversTheNodesItAnswersFor:
    """The candidate path can only offer ids the index holds records for, so a
    node in `nodes` that the index has not got would simply be absent from the
    answer. GraphStorage keeps the index a superset instead of leaving it to a
    size check: adds write the index first, removals write it last.
    """

    def test_a_node_the_index_has_not_seen_is_still_found(self):
        nodes = [_node("a", "alpha findmehere"), _node("b", "beta findmehere")]
        by_id = {node.id: node for node in nodes}
        index = _index([nodes[0]])

        found = search_nodes(by_id, index, TYPE_TEXT, query="findmehere", limit=10)

        assert [n.id for n in found] == ["a", "b"]

    def test_a_record_for_a_departed_node_does_not_stop_the_index(self):
        """The mirror case: the index is a superset, which is the state the
        write ordering deliberately produces, and it must still answer."""
        nodes = [_node("a", "alpha findmehere")]
        by_id = {node.id: node for node in nodes}
        index = _index(nodes + [_node("gone", "gone findmehere")])
        consulted = []
        real = type(index).candidates
        type(index).candidates = lambda self, term: (
            consulted.append(term) or real(self, term)
        )
        try:
            found = search_nodes(by_id, index, TYPE_TEXT, query="findmehere", limit=10)
        finally:
            type(index).candidates = real

        assert [n.id for n in found] == ["a"]
        assert consulted, "the index declined; a superset must still answer"

    def test_every_storage_write_path_keeps_the_index_a_superset(self, tmp_path):
        """The invariant lives in storage.py, at six call sites, and a reader
        can land between any two statements there. Checked after each kind of
        write rather than trusting the ordering by eye."""
        import os

        from backend.config.config_loader import reset_loader
        from backend.core.storage import GraphStorage

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            storage = GraphStorage()
        finally:
            os.chdir(cwd)
            # Building a GraphStorage resolves the config loader against the
            # working directory and caches it process-wide. Restoring the
            # directory does not undo that, and the stale loader carries no
            # relationship types - so a later test's applicability check
            # silently passes everything. Whether that surfaces depends on
            # which file pytest runs next, which is not isolation.
            reset_loader()

        def covered(where):
            missing = set(storage.nodes) - set(storage._searchable_text_cache)
            assert not missing, f"{where}: index missing {sorted(missing)[:5]}"

        storage.add_nodes([_node("a", "alpha"), _node("b", "beta")], [])
        covered("after add_nodes")

        storage.update_node("a", {"name": "alpha renamed"})
        covered("after update_node")

        storage.delete_nodes(["b"], confirmed=True)
        covered("after delete_nodes")


class TestTheWalkDoesNotWriteBackIntoTheIndex:
    """`load` empties the index before refilling it, and the walk answers for
    that window. If the walk also STORED what it built, it would repopulate the
    index with only the ids that passed its filters, in its own order, and the
    refill would leave them there - so the index would stop iterating in step
    with `nodes`, which is what the `-index` tie-break needs to reproduce the
    old stable sort.
    """

    def test_a_walk_over_an_empty_index_leaves_it_empty(self):
        nodes = [
            _node(f"n{i}", f"n{i} widget", archived=(i % 2 == 1)) for i in range(20)
        ]
        by_id = {node.id: node for node in nodes}
        index = LexicalIndex()  # the state `load` leaves behind mid-swap

        search_nodes(by_id, index, TYPE_TEXT, query="widget", limit=50)

        assert len(index) == 0, (
            f"the walk wrote {len(index)} records back; a refill after this "
            f"would put them out of step with `nodes`"
        )

    def test_the_index_still_iterates_in_step_after_a_walk_in_the_window(self):
        """The consequence the previous test protects against."""
        nodes = [
            _node(f"n{i}", f"n{i} widget", archived=(i % 2 == 1)) for i in range(20)
        ]
        by_id = {node.id: node for node in nodes}
        index = LexicalIndex()

        search_nodes(by_id, index, TYPE_TEXT, query="widget", limit=50)
        for node in nodes:  # what `load` does next
            index[node.id] = build_match_fields(node, TYPE_TEXT)

        assert list(index) == list(by_id)


class TestOnlyAnsweredQueriesCountTowardsTheNextRebuild:
    """The rebuild policy asks how many queries the previous corpus answered.
    Counting the ones it REFUSED - a term matching most of the graph, which the
    selectivity gate declines - made a corpus that answered nothing at all look
    worth rebuilding, which is exactly the case where a rebuild buys nothing.
    """

    def test_a_query_the_gate_declines_is_not_an_answer(self):
        # every node matches, so the selectivity gate declines
        index = _index([_node(f"n{i}", "shared everywhere") for i in range(300)])
        _let_the_index_rebuild(index)
        assert index.candidates("shared") is None

        before = index._served
        for _ in range(20):
            index.candidates("shared")
        assert index._served == before, (
            f"{index._served - before} declined queries were counted as answers"
        )

    def test_a_query_the_corpus_does_answer_counts(self):
        index = _index([_node(f"n{i}", f"n{i} unique{i:03d}") for i in range(300)])
        _let_the_index_rebuild(index)

        before = index._served
        assert index.candidates("unique007") == ["n7"]
        assert index.candidates("nothingmatchesthis") == []
        assert index._served == before + 2, "a hit and a miss are both answers"


class TestTheRebuildPolicyDecidesForItself:
    """Deliberately touches none of the policy's counters.

    Every other test here injects `_served_by_the_last_corpus` to put the index
    in the state it wants, which tests the consumer of that value and not the
    production code that computes it. Delete the one line that records it and
    those tests still pass - against the value their own fixture wrote - while
    the index rebuilds after every single write. So this one drives real writes
    and real queries and asks only what came out.
    """

    @staticmethod
    def _fraction_answered(queries_per_write, rounds=140):
        index = _index(
            [_node(f"n{i}", f"n{i} unique{i:03d} shared") for i in range(400)]
        )
        answered = asked = 0
        for i in range(rounds):
            if i % queries_per_write == 0:
                node = _node(
                    f"n{i % 400}", f"n{i % 400} unique{i % 400:03d} shared edited"
                )
                index[node.id] = build_match_fields(node, TYPE_TEXT)
            asked += 1
            if index.candidates(f"unique{i % 400:03d}") is not None:
                answered += 1
        return answered / asked

    def test_a_read_heavy_load_gets_the_index(self):
        assert self._fraction_answered(20) > 0.8

    def test_a_write_heavy_load_does_not_pay_for_a_corpus_it_cannot_use(self):
        """One write per five queries is below the ratio at which a rebuild can
        repay itself, so the walk should be answering nearly everything."""
        assert self._fraction_answered(5) < 0.3

    def test_the_two_loads_are_told_apart(self):
        """The pair, in one assertion: a policy stuck in either position - off
        after every write, or rebuilding regardless - fails this even if it
        happens to satisfy one of the two above."""
        assert self._fraction_answered(20) > self._fraction_answered(5) + 0.5


class TestAReloadLeavesTheIndexIteratingInStepWithTheNodes:
    """The `-index` tie-break reproduces the old stable sort only while the two
    dicts iterate in the same order. A reload rebuilds `nodes` in the store's
    order, so the index has to be emptied and refilled rather than updated in
    place - `dict.update` would leave every surviving id in its old slot.

    The corpus has to be live for this to bite: below the rebuild threshold the
    query declines and the walk hides it. Hence the warm-up.
    """

    def test_equal_scoring_nodes_come_back_in_the_reloaded_order(self, tmp_path):
        import json
        import os

        from backend.config.config_loader import reset_loader
        from backend.core.storage import GraphStorage

        ids = ["a", "b", "c", "d", "e"]
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            storage = GraphStorage()
            storage.add_nodes([_node(i, f"{i} widget") for i in ids], [])

            for _ in range(15):  # make the corpus worth rebuilding
                storage.search_nodes(query="widget", limit=50)

            (tmp_path / "graph.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": i, "type": "Actor", "name": f"{i} widget"}
                            for i in reversed(ids)
                        ],
                        "edges": [],
                    }
                )
            )
            for stray in ("graph.journal.ndjson", "graph.history.ndjson"):
                if (tmp_path / stray).exists():
                    (tmp_path / stray).unlink()
            storage.load()

            assert list(storage._searchable_text_cache) == list(storage.nodes)
            found = [n.id for n in storage.search_nodes(query="widget", limit=2)]
            assert found == list(storage.nodes)[:2]
        finally:
            os.chdir(cwd)
            reset_loader()


class TestTwoWritesInFlightCannotHideEachOther:
    """Adds write the index before `nodes` and removals write it after, which
    keeps the index a superset of `nodes` through every incremental write.
    (`load` replaces everything at once and steps outside this deliberately -
    it empties the index first so the size test declines for the whole swap.)

    A single half-done write needs no such care: `nodes` first would leave the
    index shorter, and the size test sends the query to the walk. The ordering
    is for two writes at once, where the sizes cancel. An add and a delete that
    have both reached `nodes` and neither reached the index leave the two the
    same size with different ids - the one state the size test cannot tell from
    agreement - and the candidate path then answers for a graph that is missing
    the added node.
    """

    @staticmethod
    def _mixed_writes_half_done(index_first):
        nodes = {f"n{i}": _node(f"n{i}", f"n{i} filler{i:03d}") for i in range(200)}
        index = _index(list(nodes.values()))
        newcomer = _node("brandnew", "brandnew uniqueneedle")
        if index_first:
            index[newcomer.id] = build_match_fields(newcomer, TYPE_TEXT)
        nodes["brandnew"] = newcomer
        del nodes["n5"]  # the delete has reached `nodes`, not yet the index
        _let_the_index_rebuild(index)
        return nodes, index

    def test_the_shipped_ordering_still_finds_the_added_node(self):
        nodes, index = self._mixed_writes_half_done(index_first=True)
        assert len(index) >= len(nodes), "the index must stay a superset"

        found = search_nodes(nodes, index, TYPE_TEXT, query="uniqueneedle", limit=5)

        assert [n.id for n in found] == ["brandnew"]

    def test_the_reversed_ordering_is_what_loses_it(self):
        """Pins why the ordering is there, not just that it works. Without this
        the ordering can be reversed and no test in the repo objects."""
        nodes, index = self._mixed_writes_half_done(index_first=False)
        assert len(index) == len(nodes), (
            "this test is meaningless unless the sizes cancel out - that is "
            "the state the size test cannot detect"
        )

        found = search_nodes(nodes, index, TYPE_TEXT, query="uniqueneedle", limit=5)

        assert found == [], (
            "if this finds the node, the size test caught the interleaving "
            "after all and the ordering above is load-bearing for some other "
            "reason than the one it claims"
        )

    def test_the_add_path_writes_the_index_before_nodes(self, tmp_path):
        """The property above says why the ordering matters; this says where.
        Constructing the interleaved state by hand cannot catch a call site
        that has been reordered, so watch the real writes as they happen.

        Only a NEW id is checked. For one already in both dicts the ordering
        cannot matter, and asserting on it would fail `update_node` for no
        reason."""
        import os

        from backend.config.config_loader import reset_loader
        from backend.core.storage import GraphStorage
        from backend.core.storage_search import LexicalIndex

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            storage = GraphStorage()
            seen = []
            real = LexicalIndex.__setitem__

            def watching(self, node_id, fields):
                if self is storage._searchable_text_cache and node_id not in self:
                    seen.append((node_id, node_id in storage.nodes))
                return real(self, node_id, fields)

            LexicalIndex.__setitem__ = watching
            try:
                storage.add_nodes(
                    [_node("one", "one node"), _node("two", "two node")], []
                )
            finally:
                LexicalIndex.__setitem__ = real
        finally:
            os.chdir(cwd)
            reset_loader()

        assert [node_id for node_id, _ in seen] == ["one", "two"], (
            f"expected an index write per new node, saw {seen}"
        )
        late = [node_id for node_id, in_nodes in seen if in_nodes]
        assert not late, (
            f"{late} reached `nodes` before the index; a reader landing there, "
            f"with a delete also in flight, gets a graph that is missing them"
        )


class TestADeleteLandingMidScanDoesNotLoseTheNode:
    """Removals write `nodes` first and the index second, so a delete can land
    after `candidates()` has returned and before the scan reads the record -
    leaving the id in the candidate list with its record already popped. The
    scan builds the record rather than dropping the node.

    This is the one interleaving that reaches the on-demand build from the
    candidate path, and a comment here once claimed it could not happen.
    """

    def test_the_node_is_still_returned(self, monkeypatch):
        nodes = {f"n{i}": _node(f"n{i}", f"n{i} unique{i:03d}") for i in range(200)}
        index = _index(list(nodes.values()))
        _let_the_index_rebuild(index)

        real = LexicalIndex.candidates
        landed = []

        def _delete_lands_after_the_candidate_list(self, term):
            offered = real(self, term)
            if offered and not landed:
                landed.append(True)
                self.pop("n7")  # the delete's index write
            return offered

        monkeypatch.setattr(
            LexicalIndex, "candidates", _delete_lands_after_the_candidate_list
        )
        found = search_nodes(nodes, index, TYPE_TEXT, query="unique007", limit=5)

        assert landed, "the delete never landed - the test proves nothing"
        assert "n7" not in index, "the record should be gone by scan time"
        assert [n.id for n in found] == ["n7"]


class TestEveryWritePathOrdersTheIndexAgainstTheNodes:
    """The ordering rule is "index first on an add, index last on a removal",
    at every site. Only `add_nodes` was watched; reversing any of the other
    three passed the whole suite.
    """

    @staticmethod
    def _storage(tmp_path):
        import os

        from backend.config.config_loader import reset_loader
        from backend.core.storage import GraphStorage

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            return GraphStorage(), cwd, reset_loader
        except Exception:
            os.chdir(cwd)
            raise

    def test_the_external_upsert_path_writes_the_index_before_nodes(self, tmp_path):
        """The OTHER add site, on the persistence seam - a second instance's
        writes arriving here. Reversing it passed the whole suite: the watcher
        on `add_nodes` does not look at this one, and the add half is the half
        that loses a node a reader is owed."""
        import os

        from backend.core.storage_backends import EntityOperation, ExternalChange
        from backend.core.storage_search import LexicalIndex

        storage, cwd, reset = self._storage(tmp_path)
        try:
            seen = []
            real = LexicalIndex.__setitem__

            def watching(self, node_id, fields):
                if self is storage._searchable_text_cache and node_id not in self:
                    seen.append((node_id, node_id in storage.nodes))
                return real(self, node_id, fields)

            LexicalIndex.__setitem__ = watching
            try:
                storage.apply_external_change(
                    ExternalChange.entities(
                        [
                            EntityOperation.upsert_node(
                                {
                                    "id": "fromelsewhere",
                                    "type": "Actor",
                                    "name": "from elsewhere",
                                }
                            )
                        ]
                    )
                )
            finally:
                LexicalIndex.__setitem__ = real
        finally:
            os.chdir(cwd)
            reset()

        assert [node_id for node_id, _ in seen] == ["fromelsewhere"], (
            f"expected one index write for the external upsert, saw {seen}"
        )
        late = [node_id for node_id, in_nodes in seen if in_nodes]
        assert not late, (
            f"{late} reached `nodes` before the index; with a delete also in "
            f"flight the sizes cancel and the candidate path loses them"
        )

    def test_the_external_delete_path_removes_from_the_index_at_all(self, tmp_path):
        """The fourth site, and the only one with neither its ordering nor its
        content watched. Deleting its `pop` outright passed the whole suite:
        the search still answers correctly, because the scan drops a candidate
        that has left `nodes` - so the cost is not a wrong result but a record
        and its corpus text living for ever on any instance that receives
        deletes over the seam, behind a size count that stays inflated."""
        import os

        from backend.core.storage_backends import EntityOperation, ExternalChange
        from backend.core.storage_search import LexicalIndex

        storage, cwd, reset = self._storage(tmp_path)
        try:
            storage.add_nodes([_node("keep", "keep me"), _node("gone", "gone me")], [])

            popped = []
            real = LexicalIndex.pop

            def watching(self, node_id, default=None):
                if self is storage._searchable_text_cache:
                    popped.append((node_id, node_id in storage.nodes))
                return real(self, node_id, default)

            LexicalIndex.pop = watching
            try:
                storage.apply_external_change(
                    ExternalChange.entities([EntityOperation.delete_node("gone")])
                )
            finally:
                LexicalIndex.pop = real
        finally:
            os.chdir(cwd)
            reset()

        assert [node_id for node_id, _ in popped] == ["gone"], (
            f"the external delete never reached the index; the record and its "
            f"corpus text stay for ever. Saw: {popped}"
        )
        assert "gone" not in storage._searchable_text_cache
        early = [node_id for node_id, in_nodes in popped if in_nodes]
        assert not early, (
            f"{early} left the index while still in `nodes`, so the index is "
            f"no longer a superset - the state the size test assumes away"
        )

    def test_a_removal_writes_the_index_after_nodes(self, tmp_path):
        """Pins the invariant, not a query. Reversing this does lose a node
        from the candidate path, but only the one whose delete is already in
        flight - which a search racing that delete could miss anyway, so it is
        not a loss a reader is owed. What it really breaks is the index being a
        superset of `nodes` through every incremental write, which is the
        property the size
        test in `search_nodes` is a backstop for. Asserting the consequence
        instead would pin a guarantee the system does not make."""
        import os

        from backend.core.storage_search import LexicalIndex

        storage, cwd, reset = self._storage(tmp_path)
        try:
            storage.add_nodes([_node("keep", "keep me"), _node("drop", "drop me")], [])

            still_in_nodes = []
            real = LexicalIndex.pop

            def watching(self, node_id, default=None):
                if self is storage._searchable_text_cache:
                    still_in_nodes.append((node_id, node_id in storage.nodes))
                return real(self, node_id, default)

            LexicalIndex.pop = watching
            try:
                storage.delete_nodes(["drop"], confirmed=True)
            finally:
                LexicalIndex.pop = real
        finally:
            os.chdir(cwd)
            reset()

        assert still_in_nodes, "no index removal seen"
        early = [i for i, in_nodes in still_in_nodes if in_nodes]
        assert not early, (
            f"{early} left the index while still in `nodes`, so the index is "
            f"no longer a superset - the state the size test assumes away"
        )


class TestARenameLandingMidScanDoesNotReturnANonMatch:
    """The sibling of the delete case, in the opposite direction.

    `update_node` mutates the node under the storage lock and then writes the
    new record; `search_nodes` holds no lock. So a rename can land after the
    candidate list was computed and before the scan reads the record, leaving a
    candidate whose text no longer contains the term. The scan's own
    `single_term not in fields.text` re-check is the only thing that discards
    it - a comment here once described that test as already established on the
    candidate path, which invited removing it.
    """

    def test_the_renamed_node_is_not_returned(self, monkeypatch):
        nodes = {f"n{i}": _node(f"n{i}", f"n{i} unique{i:03d}") for i in range(200)}
        index = _index(list(nodes.values()))
        _let_the_index_rebuild(index)

        real = LexicalIndex.candidates
        landed = []

        def _rename_lands_after_the_candidate_list(self, term):
            offered = real(self, term)
            if offered and not landed:
                landed.append(True)
                renamed = _node("n7", "n7 renamedaway")
                nodes["n7"] = renamed
                self["n7"] = build_match_fields(renamed, TYPE_TEXT)
            return offered

        monkeypatch.setattr(
            LexicalIndex, "candidates", _rename_lands_after_the_candidate_list
        )
        found = search_nodes(nodes, index, TYPE_TEXT, query="unique007", limit=5)

        assert landed, "the rename never landed - the test proves nothing"
        assert "unique007" not in index["n7"].text, "the record was not replaced"
        assert found == [], (
            f"returned {[n.id for n in found]} for a term none of them contain"
        )


def _index_with(nodes, type_text):
    index = LexicalIndex()
    for node in nodes:
        index[node.id] = build_match_fields(node, type_text)
    return index


class _StorageIn:
    """A GraphStorage rooted in a scratch directory, with the process-wide
    config loader reset on the way out - see
    `test_every_storage_write_path_keeps_the_index_a_superset` for why."""

    def __init__(self, directory):
        self._directory = directory

    def __enter__(self):
        import os

        self._cwd = os.getcwd()
        os.chdir(self._directory)
        try:
            return GraphStorage()
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        import os

        from backend.config.config_loader import reset_loader

        os.chdir(self._cwd)
        reset_loader()
        return False


def _write_store(directory, nodes):
    import json

    (directory / "graph.json").write_text(json.dumps({"nodes": nodes, "edges": []}))
    for stray in ("graph.journal.ndjson", "graph.history.ndjson"):
        if (directory / stray).exists():
            (directory / stray).unlink()


def _watch_candidates(monkeypatch, index):
    """Record what `candidates` answered, for this index only."""
    answers = []
    real = LexicalIndex.candidates

    def watching(self, term):
        answer = real(self, term)
        if self is index:
            answers.append(answer)
        return answer

    monkeypatch.setattr(LexicalIndex, "candidates", watching)
    return answers


class TestTheBandBetweenTheFloorAndTheFraction:
    """At 200 nodes a quarter is 50, so the floor of 64 is what decides between
    51 and 64 hits. No fixture sat in that band: the small ones never reach 51
    hits and the big ones are far past 64. Dropping the floor outright is
    caught elsewhere, by a two-node graph; lowering it into the band, or
    cutting the candidate list short, passed every test."""

    @staticmethod
    def _graph(hits):
        # Hits spread through the graph, so a list cut short at either end is
        # visible, not just one that loses its tail.
        hit_slots = set(range(0, 200, 3)[:hits]) if hits <= 67 else set(range(hits))
        assert len(hit_slots) == hits
        return [
            _node(f"n{i:03d}", f"bandterm {i}" if i in hit_slots else f"filler {i}")
            for i in range(200)
        ]

    @pytest.mark.parametrize("hits", [51, 60, 64])
    def test_every_hit_in_the_band_is_offered(self, hits):
        nodes = self._graph(hits)
        index = _index(nodes)
        _let_the_index_rebuild(index)

        expected = [n.id for n in nodes if n.name.startswith("bandterm")]
        assert index.candidates("bandterm") == expected, (
            f"{hits} hits out of 200 sit under the floor and must be answered "
            f"in full, in insertion order"
        )

    @pytest.mark.parametrize("hits", [51, 60, 64])
    def test_a_search_in_the_band_returns_every_hit(self, hits, monkeypatch):
        nodes = self._graph(hits)
        index = _index(nodes)
        _let_the_index_rebuild(index)
        answers = _watch_candidates(monkeypatch, index)

        found = search_nodes(
            {node.id: node for node in nodes},
            index,
            TYPE_TEXT,
            query="bandterm",
            limit=200,
        )

        assert answers and answers[0] is not None, "the index was not the path"
        assert len(found) == hits

    def test_one_past_the_floor_is_declined(self):
        """The gate declines on `>`, so 64 is answered and 65 is not."""
        index = _index(self._graph(65))
        _let_the_index_rebuild(index)
        assert index.candidates("bandterm") is None


class TestGraphStorageSearchesThroughTheIndex:
    """`TestTheIndexIsActuallyConsulted` hands `search_nodes` an index it built
    itself, so GraphStorage could pass a plain copy, the read-only view, or
    nothing index-shaped at all - the results would be identical and nothing
    would fail."""

    def test_a_selective_storage_search_is_answered_by_its_index(
        self, tmp_path, monkeypatch
    ):
        with _StorageIn(tmp_path) as storage:
            storage.add_nodes(
                [_node(f"n{i}", f"node {i}") for i in range(100)]
                + [_node("needle", "findmehere")],
                [],
            )
            answers = _watch_candidates(monkeypatch, storage._searchable_text_cache)

            found = storage.search_nodes(query="findmehere")

        assert [n.id for n in found] == ["needle"]
        assert answers == [["needle"]], (
            f"GraphStorage's own index answered {answers}; the search did not "
            f"go through it"
        )


class TestAnyTermWithOneTermStillUsesTheIndex:
    """`any_term` with a single distinct term is the substring query, and takes
    the index. Gating the index on the match mode instead of on the number of
    terms would send it to the walk - same answer, the whole cost back."""

    @pytest.mark.parametrize("query", ["findmehere", "findmehere findmehere"])
    def test_a_single_term_any_term_query_does_not_walk(self, query):
        nodes = [_node(f"n{i}", f"node number {i}") for i in range(200)]
        nodes.append(_node("needle", "findmehere"))
        walked = []

        class _CountingNodes(dict):
            def values(self):
                walked.append(True)
                return dict.values(self)

        found = search_nodes(
            _CountingNodes({node.id: node for node in nodes}),
            _index(nodes),
            TYPE_TEXT,
            query=query,
            limit=10,
            match_mode="any_term",
        )

        assert [n.id for n in found] == ["needle"]
        assert not walked, f"{query!r} in any_term mode walked every node"


class TestAReloadIndexesArchivedNodesToo:
    """`nodes` holds archived nodes; the search filters them. If `load` left
    them out of the index, the index would be shorter than `nodes` for as long
    as one archived node existed, and the size test would switch the index off
    for good - silently, since the walk gives the same answer."""

    def test_the_index_covers_an_archived_node_and_still_answers(
        self, tmp_path, monkeypatch
    ):
        with _StorageIn(tmp_path) as storage:
            _write_store(
                tmp_path,
                [
                    {"id": f"n{i}", "type": "Actor", "name": f"node {i}"}
                    for i in range(100)
                ]
                + [
                    {
                        "id": "shelved",
                        "type": "Actor",
                        "name": "shelved findmehere",
                        "archived": True,
                    },
                    {"id": "live", "type": "Actor", "name": "live findmehere"},
                ],
            )
            storage.load()
            index = storage._searchable_text_cache
            assert storage.nodes["shelved"].archived, "the fixture lost its flag"
            assert set(index) == set(storage.nodes)

            answers = _watch_candidates(monkeypatch, index)
            found = storage.search_nodes(query="findmehere")
            with_archived = storage.search_nodes(
                query="findmehere", include_archived=True
            )

        assert [n.id for n in found] == ["live"]
        assert [n.id for n in with_archived] == ["shelved", "live"]
        assert answers == [["shelved", "live"]] * 2, (
            f"the index answered {answers} after a reload holding an archived node"
        )


class TestTheRecordViewIsLive:
    """The scan reads records through `records`. Read-only is half of what it
    must be; the other half is that it shows the records as they are NOW. A
    snapshot - `MappingProxyType(dict(...))` - would be read-only too, and every
    record written after construction would be missing from it, so the scan
    would rebuild each one per query. Same answers; the prepared-fields cache
    quietly gone."""

    def test_a_record_written_after_construction_is_visible_through_the_view(self):
        index = LexicalIndex()
        view = index.records
        fields = build_match_fields(_node("a", "alpha"), TYPE_TEXT)

        index["a"] = fields
        assert view.get("a") is fields

        index.pop("a")
        assert view.get("a") is None

        index.update({"b": fields})
        assert view.get("b") is fields

    def test_the_scan_does_not_rebuild_records_the_index_holds(self, monkeypatch):
        from backend.core import storage_search

        nodes = [_node(f"n{i}", f"widget {i}") for i in range(5)]
        index = LexicalIndex()
        for node in nodes:  # written after the view exists, like GraphStorage
            index[node.id] = build_match_fields(node, TYPE_TEXT)

        built = []
        real = storage_search.build_match_fields
        monkeypatch.setattr(
            storage_search,
            "build_match_fields",
            lambda node, text: built.append(node.id) or real(node, text),
        )
        found = search_nodes(
            {n.id: n for n in nodes}, index, TYPE_TEXT, query="widget", limit=10
        )

        assert len(found) == 5
        assert built == [], f"the scan rebuilt {built}; the view did not show them"


def _tier_node(node_id, **kwargs):
    kwargs.setdefault("name", f"plain {node_id}")
    kwargs.setdefault("type", NodeType.ACTOR)
    return Node(id=node_id, **kwargs)


class TestEveryTierBeatsTheOneBelowItWhenInsertedSecond:
    """Equal scores keep insertion order, so a pair inserted stronger-first
    still comes out right after the stronger tier collapses onto the one below
    it. Here every adjacent pair goes in weaker-first: the stronger node wins
    only by actually scoring higher."""

    @pytest.mark.parametrize(
        "query,weaker,stronger",
        [
            pytest.param(
                "widget",
                {"name": "widget tool"},
                {"name": "widget"},
                id="name exact over name prefix",
            ),
            pytest.param(
                "widget",
                {"name": "the widget"},
                {"name": "widget tool"},
                id="name prefix over name contains",
            ),
            pytest.param(
                "widget",
                {"aliases": ["widget"]},
                {"name": "the widget"},
                id="name contains over alias exact",
            ),
            pytest.param(
                "widget",
                {"aliases": ["widget tool"]},
                {"aliases": ["widget"]},
                id="alias exact over alias prefix",
            ),
            pytest.param(
                "widget",
                {"aliases": ["the widget"]},
                {"aliases": ["widget tool"]},
                id="alias prefix over alias contains",
            ),
            pytest.param(
                "widget",
                {"tags": ["widget"], "subtypes": ["widget"], "description": "widget"},
                {"aliases": ["the widget"]},
                id="alias contains over every secondary signal",
            ),
            pytest.param(
                "widget",
                {"tags": ["widgets"]},
                {"tags": ["widget"]},
                id="tag exact over tag contains",
            ),
            pytest.param(
                "widget",
                {"subtypes": ["widget"]},
                {"tags": ["widgets"]},
                id="tag contains over subtype",
            ),
            pytest.param(
                "widget",
                {"description": "a widget"},
                {"subtypes": ["widget"]},
                id="subtype over description",
            ),
            # A query that spans two fields matches the joined text and scores
            # nothing, which is the only way to put a node below 200.
            pytest.param(
                "alpha beta",
                {"name": "alpha", "description": "beta"},
                {"description": "alpha beta"},
                id="description over no field",
            ),
            pytest.param(
                "alpha beta",
                {"name": "alpha", "description": "beta"},
                {"summary": "alpha beta"},
                id="summary over no field",
            ),
            # Every field is lowered once when the record is prepared; each of
            # these only scores its tier if that lowering still happens.
            pytest.param(
                "widget",
                {"aliases": ["widget"]},
                {"name": "WIDGET"},
                id="unlowered name",
            ),
            pytest.param(
                "widget",
                {"tags": ["widget"]},
                {"aliases": ["WIDGET"]},
                id="unlowered alias",
            ),
            pytest.param(
                "widget",
                {"description": "widget"},
                {"tags": ["WIDGET"]},
                id="unlowered tag",
            ),
            pytest.param(
                "widget",
                {"description": "widget"},
                {"subtypes": ["WIDGET"]},
                id="unlowered subtype",
            ),
            pytest.param(
                "alpha beta",
                {"name": "alpha", "description": "beta"},
                {"description": "ALPHA BETA"},
                id="unlowered description",
            ),
            pytest.param(
                "alpha beta",
                {"name": "alpha", "description": "beta"},
                {"summary": "ALPHA BETA"},
                id="unlowered summary",
            ),
        ],
    )
    def test_the_stronger_node_ranks_first(self, query, weaker, stronger):
        weak = _tier_node("weaker", **weaker)
        strong = _tier_node("stronger", **stronger)
        by_id = {"weaker": weak, "stronger": strong}

        found = search_nodes(
            by_id, _index([weak, strong]), TYPE_TEXT, query=query, limit=10
        )

        assert [n.id for n in found] == ["stronger", "weaker"], (
            "the stronger tier did not outscore the weaker one; it only ever "
            "won on insertion order"
        )

    # Node types are schema-defined strings, so one type can carry the query
    # as its whole name, another as a prefix, and a third only in its label.
    TYPE_LABELS = {"Gadget": "gadget widget"}

    @pytest.mark.parametrize(
        "weaker,stronger",
        [
            pytest.param(
                {"type": "WidgetKind"},
                {"type": "Widget"},
                id="type exact over type prefix",
            ),
            pytest.param(
                {"type": "Gadget"},
                {"type": "WidgetKind"},
                id="type prefix over type label",
            ),
            pytest.param(
                {"tags": ["widget"]},
                {"type": "Gadget"},
                id="type label over tag exact",
            ),
        ],
    )
    def test_the_stronger_type_tier_ranks_first(self, weaker, stronger):
        weak = _tier_node("weaker", **weaker)
        strong = _tier_node("stronger", **stronger)
        by_id = {"weaker": weak, "stronger": strong}

        found = search_nodes(
            by_id,
            _index_with([weak, strong], self.TYPE_LABELS),
            self.TYPE_LABELS,
            query="widget",
            limit=10,
        )

        assert [n.id for n in found] == ["stronger", "weaker"], (
            "the stronger type tier did not outscore the weaker one"
        )


class TestLocalizedTypeLabelsRankEndToEnd:
    """Every `search_nodes` fixture in this file passed an empty
    `type_searchable_text`. `test_storage.py` ranks a label through
    GraphStorage, but nothing here pinned the pure function's side: a label
    reaching the record's text and `score_type` reading it off the record."""

    LABELS = {"Actor": "actor aktör"}

    def test_a_label_match_is_found_and_outranks_a_description_match(self):
        described = Node(
            id="described", type="Market", name="x", description="an aktör here"
        )
        labelled = Node(id="labelled", type=NodeType.ACTOR, name="y")
        by_id = {"described": described, "labelled": labelled}

        found = search_nodes(
            by_id,
            _index_with([described, labelled], self.LABELS),
            self.LABELS,
            query="aktör",
            limit=10,
        )

        assert [n.id for n in found] == ["labelled", "described"], (
            "the type label scores 600 and a description 200; the label match "
            "was not found, or not ranked by its type"
        )

    def test_a_label_is_searchable_through_graph_storage(self, tmp_path):
        """GraphStorage builds each record with its own label lookup; a record
        built without it has no label in its text."""
        with _StorageIn(tmp_path) as storage:
            storage._type_searchable_text.update(self.LABELS)
            storage.add_nodes(
                [
                    Node(id="market", type="Market", name="beta"),
                    _node("actor", "alpha"),
                ],
                [],
            )

            found = storage.search_nodes(query="aktör")

        assert [n.id for n in found] == ["actor"]


class TestAReloadOfAnyShapeKeepsTheIndexInStep:
    """`TestAReloadLeavesTheIndexIteratingInStepWithTheNodes` pins one shape: the
    same ids, reversed. A reload can also drop ids, bring new ones in between
    survivors, and carry archived nodes - and pruning departed ids before a
    plain `update` gets the reversed case wrong and this one wrong differently,
    with the newcomers appended rather than in their slots."""

    def test_dropped_added_and_shuffled_ids_follow_the_store(self, tmp_path):
        with _StorageIn(tmp_path) as storage:
            storage.add_nodes([_node(i, f"{i} widget") for i in "abcde"], [])
            for _ in range(15):  # make the corpus worth rebuilding
                storage.search_nodes(query="widget", limit=50)

            _write_store(
                tmp_path,
                [
                    {"id": "c", "type": "Actor", "name": "c widget"},
                    {"id": "new1", "type": "Actor", "name": "new1 widget"},
                    {"id": "a", "type": "Actor", "name": "a widget", "archived": True},
                    {"id": "new2", "type": "Actor", "name": "new2 widget"},
                    {"id": "e", "type": "Actor", "name": "e widget"},
                ],
            )
            storage.load()

            assert list(storage._searchable_text_cache) == list(storage.nodes)
            assert list(storage.nodes) == ["c", "new1", "a", "new2", "e"]
            for _ in range(15):
                found = [n.id for n in storage.search_nodes(query="widget", limit=3)]
                assert found == ["c", "new1", "new2"]


class TestAReaderInsideTheReloadSwapIsNotAnsweredFromTheOldGraph:
    """`load` empties the index BEFORE it refills `nodes`, so for the whole swap
    the index is shorter and the size test sends a reader to the walk. Emptying
    it after instead leaves the old index - and its live corpus - beside the new
    `nodes` at the same size, and the candidate path answers for a graph that
    is no longer there. Nothing asserted what a reader sees in that window."""

    def test_a_search_between_the_nodes_swap_and_the_index_refill(self, tmp_path):
        from backend.core import storage_search

        ids = [f"n{i}" for i in range(20)]
        mid_swap = []

        with _StorageIn(tmp_path) as storage:
            storage.add_nodes(
                [_node(i, f"{i} widget") for i in ids] + [_node("r", "before")], []
            )
            for _ in range(15):  # a live corpus for the OLD graph
                storage.search_nodes(query="widget", limit=50)

            class _ReaderLandsAfterTheNodesSwap(dict):
                def update(self, *args, **kwargs):
                    dict.update(self, *args, **kwargs)
                    # Lock-free, as search_nodes is; the storage lock is held.
                    mid_swap.append(
                        storage_search.search_nodes(
                            storage.nodes,
                            storage._searchable_text_cache,
                            storage._type_searchable_text,
                            "afterward",
                        )
                    )

            storage.nodes = _ReaderLandsAfterTheNodesSwap(storage.nodes)
            _write_store(
                tmp_path,
                [{"id": i, "type": "Actor", "name": f"{i} widget"} for i in ids]
                + [{"id": "r", "type": "Actor", "name": "afterward"}],
            )
            storage.load()

        assert len(mid_swap) == 1, "the reader never landed inside the swap"
        assert [n.id for n in mid_swap[0]] == ["r"], (
            "a reader inside the reload was answered from the previous graph's "
            "corpus and missed the node as it now is"
        )


class TestTheSmallerStepsEachHaveATest:
    """One test per step, so each fails on its own name. Sort-then-truncate and
    the replacing `update` were unpinned before these; the guard, the
    searchsorted side and the offset step were already caught, but only
    indirectly, by tests about something else."""

    def test_the_ranking_sorts_before_it_truncates(self):
        """Cutting to `limit` in scan order and sorting what is left returns
        the first few matches, not the best ones."""
        nodes = [_node(f"weak{i}", "plain", f"widget {i}") for i in range(5)]
        nodes.append(_node("strong", "widget"))
        by_id = {node.id: node for node in nodes}

        for match_mode in ("substring", "any_term"):
            found = search_nodes(
                by_id,
                _index(nodes),
                TYPE_TEXT,
                query="widget",
                limit=1,
                match_mode=match_mode,
            )
            assert [n.id for n in found] == ["strong"], match_mode

    def test_update_replaces_a_record_it_already_holds(self):
        """`update` must replace, like `dict.update`, for any caller. `load`
        does not depend on it today - it empties the index before refilling -
        so this pins the method rather than a reload: with `setdefault`
        semantics an id already present would keep matching its old text."""
        index = _index([_node("a", "oldtext")])
        index.update({"a": build_match_fields(_node("a", "newtext"), TYPE_TEXT)})
        _let_the_index_rebuild(index)

        assert "newtext" in index["a"].text
        assert index.candidates("newtext") == ["a"]
        assert index.candidates("oldtext") == []

    def test_an_empty_answer_from_the_index_is_not_a_walk(self):
        """`[]` is an answer - nothing matches - and `None` is a refusal. A
        truthiness test in place of `is not None` would walk every node for
        every query that matches nothing: same result, the whole cost back."""
        nodes = [_node(f"n{i}", f"node number {i}") for i in range(200)]
        index = _index(nodes)
        walked = []

        class _CountingNodes(dict):
            def values(self):
                walked.append(True)
                return dict.values(self)

        found = search_nodes(
            _CountingNodes({node.id: node for node in nodes}),
            index,
            TYPE_TEXT,
            query="nothingmatchesthis",
            limit=10,
        )

        assert found == []
        assert index._built is not None, "the index declined; nothing was tested"
        assert not walked, "an empty answer from the index was walked anyway"

    def test_a_term_at_the_very_start_of_a_node_maps_to_that_node(self):
        """Each node's text starts exactly at its offset, so a hit there sits
        ON the boundary. `searchsorted(side='left')` puts it in the node
        before - and the first node's hits in the last node."""
        nodes = [_node(f"n{i}", f"head{i:02d} tail") for i in range(30)]
        index = _index(nodes)

        for i in (0, 1, 15, 29):
            assert index.candidates(f"head{i:02d}") == [f"n{i}"]
        assert index.candidates("tail") == [n.id for n in nodes]

    def test_the_offset_step_counts_the_separator(self):
        """Offsets advance by the text plus one separator. Short texts, so an
        error of one character per node reaches the next node within a few
        nodes - at a term in the MIDDLE of the text, which the start-of-text
        test above does not probe."""
        nodes = [_node(f"n{i}", f"a{i:02d}") for i in range(60)]
        index = _index(nodes)

        for node in nodes:
            fields = index[node.id]
            middle = fields.text[1:4]
            expected = [n.id for n in nodes if middle in index[n.id].text]
            assert index.candidates(middle) == expected, middle
