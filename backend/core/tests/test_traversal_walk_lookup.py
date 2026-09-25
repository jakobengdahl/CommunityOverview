"""The in-memory walk resolves each result id with one observation.

The walk is also the traversal fallback, and it reads `nodes` and `edges`
without the lock every mutator holds. Resolving a result with a membership test
and then an index observes the dict twice; a delete landing between the two
raised KeyError out of the traversal. These tests drive
`storage_search.get_related_nodes` directly, so no store can answer in its
place and hide the window, and they need no database: they belong in the plain
`pytest backend/` run.
"""

import networkx as nx

from backend.core import storage_search
from backend.core.models import Edge, Node, NodeType, RelationshipType


_ABSENT = object()


class _VanishingOnLookup(dict):
    """Loses one key on one observation, and counts every observation.

    Eight routes are instrumented - `in`, `.get()`, `[]`, `.keys()`,
    iteration, `.items()`, `.values()` and `.copy()`. Observation number
    `vanish_at` (the first, by default) answers "present" and removes the key,
    and a later one misses, so a single-key check-then-use is caught; one whose
    check is a whole-dict read is not (see below). `observations` counts how often the victim was observed,
    present or not; the whole-dict routes observe every key, so each counts as
    one observation of the victim.

    A whole-dict read is one observation, and a snapshot of the dict at that
    instant. `dict(d)` and `{**d}` on a dict subclass that overrides iteration
    are carried out as `d.keys()` and then `d[k]` per key, so a `d[victim]`
    right after a whole-dict read is answered from that read's snapshot, as
    part of the same observation - otherwise the fixture raises KeyError out
    of a copy that on a plain dict is atomic. A Python loop doing keys-then-
    index, or `d[k] for k in ids if k in d.keys()`, looks the same from here
    and is treated the same, so it is not caught; single-key check-then-use
    (`in` or `.get()`, then `[]`) still is.
    """

    def __init__(self, *args, victim=None, vanish_at=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.victim = victim
        self.vanish_at = vanish_at
        self.observations = 0
        self._snapshot = _ABSENT

    def _observe(self, key):
        if key != self.victim:
            return
        self._snapshot = _ABSENT
        self.observations += 1
        if self.observations == self.vanish_at:
            super().pop(key, None)

    def _observe_whole(self):
        value = super().get(self.victim, _ABSENT)
        self._observe(self.victim)
        self._snapshot = value

    def __contains__(self, key):
        present = super().__contains__(key)
        self._observe(key)
        return present

    def get(self, key, default=None):
        value = super().get(key, default)
        self._observe(key)
        return value

    def __getitem__(self, key):
        if key == self.victim and self._snapshot is not _ABSENT:
            value, self._snapshot = self._snapshot, _ABSENT
            return value
        value = super().__getitem__(key)
        self._observe(key)
        return value

    # The whole-dict routes snapshot before they observe: removing the victim
    # mid-iteration would raise "dictionary changed size", which is the
    # fixture failing rather than the code under test.
    def keys(self):
        snapshot = set(super().keys())
        self._observe_whole()
        return snapshot

    def __iter__(self):
        snapshot = list(super().__iter__())
        self._observe_whole()
        return iter(snapshot)

    def items(self):
        snapshot = list(super().items())
        self._observe_whole()
        return snapshot

    def values(self):
        snapshot = list(super().values())
        self._observe_whole()
        return snapshot

    def copy(self):
        snapshot = dict(super().items())
        self._observe(self.victim)
        return snapshot


def _graph(nodes, edges):
    graph = nx.MultiDiGraph()
    for node in nodes.values():
        graph.add_node(node.id, data=node)
    for edge in edges.values():
        graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)
    return graph


def _fixture():
    nodes = {
        "a": Node(id="a", type=NodeType.ACTOR, name="a"),
        "b": Node(id="b", type=NodeType.ACTOR, name="b"),
    }
    edges = {
        "ab": Edge(id="ab", source="a", target="b", type=RelationshipType.RELATES_TO)
    }
    return nodes, edges


class TestTheFixtureCopiesLikeADict:
    """A walk that snapshots the dict and resolves from the copy is correct,
    so the fixture must let every copying spelling through as one observation.
    """

    def test_a_copy_sees_the_victim_once_and_keeps_it(self):
        for copy in (dict, lambda d: {**d}, lambda d: d.copy()):
            vanishing = _VanishingOnLookup({"a": 1, "b": 2}, victim="b")

            snapshot = copy(vanishing)

            assert snapshot == {"a": 1, "b": 2}
            assert type(snapshot) is dict
            assert vanishing.observations == 1
            assert "b" not in vanishing

    def test_check_then_index_still_misses_after_a_copy(self):
        vanishing = _VanishingOnLookup({"a": 1, "b": 2}, victim="b", vanish_at=2)
        dict(vanishing)

        assert "b" in vanishing
        try:
            vanishing["b"]
        except KeyError:
            pass
        else:
            raise AssertionError("check-then-index was not caught")


class TestTheWalkResolvesEachIdOnce:
    """The fixtures are armed from the start, so nothing here depends on how
    the walk reads the graph - lazily or up front, through `out_edges` or
    `succ`. That needs the resolution to be the walk's only observation of the
    victim. The walk never reads `edges` while it traverses, so an edge victim
    qualifies as is. It does read `nodes` - to skip archived neighbours - but
    not with `include_archived=True`, so the node test passes that; the
    resolution after the traversal is the same line either way.

    `observations == 1` is also what keeps these from passing vacuously: a
    resolution the fixture never saw would leave it at zero.
    """

    def test_a_node_deleted_during_resolution_is_observed_once(self):
        nodes, edges = _fixture()
        vanishing = _VanishingOnLookup(nodes, victim="b")

        result = storage_search.get_related_nodes(
            vanishing, edges, _graph(nodes, edges), "a", include_archived=True
        )

        assert vanishing.observations == 1
        assert None not in result["nodes"]
        assert {n.id for n in result["nodes"]} == {"a", "b"}
        assert {e.id for e in result["edges"]} == {"ab"}

    def test_an_edge_deleted_during_resolution_is_observed_once(self):
        nodes, edges = _fixture()
        vanishing = _VanishingOnLookup(edges, victim="ab")

        result = storage_search.get_related_nodes(
            nodes, vanishing, _graph(nodes, edges), "a"
        )

        assert vanishing.observations == 1
        assert None not in result["edges"]
        assert {n.id for n in result["nodes"]} == {"a", "b"}
        assert {e.id for e in result["edges"]} == {"ab"}

    def test_an_id_that_is_already_gone_is_dropped_not_returned_as_none(self):
        nodes, edges = _fixture()
        graph = _graph(nodes, edges)
        del nodes["b"]
        del edges["ab"]

        result = storage_search.get_related_nodes(nodes, edges, graph, "a")

        assert [n.id for n in result["nodes"]] == ["a"]
        assert result["edges"] == []

    def test_a_node_vanishing_at_any_observation_never_breaks_the_default_walk(
        self,
    ):
        """The default path also reads `nodes` while it traverses, to skip
        archived neighbours, so the resolution is not the victim's first
        observation there. Sweeping the vanishing point across every
        observation the walk makes puts it on the resolution's too, whatever
        the traversal's shape. The sweep ends at the first `vanish_at` the
        walk never reaches, which is what shows it covered them all.
        """
        for vanish_at in range(1, 20):
            nodes, edges = _fixture()
            vanishing = _VanishingOnLookup(nodes, victim="b", vanish_at=vanish_at)

            result = storage_search.get_related_nodes(
                vanishing, edges, _graph(nodes, edges), "a"
            )

            assert None not in result["nodes"]
            assert {n.id for n in result["nodes"]} <= {"a", "b"}
            assert "a" in {n.id for n in result["nodes"]}
            if vanishing.observations < vanish_at:
                assert "b" in {n.id for n in result["nodes"]}
                assert vanish_at > 1
                break
        else:
            raise AssertionError("the walk observed the victim 19+ times")
