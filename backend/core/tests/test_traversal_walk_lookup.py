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


class _VanishingOnLookup(dict):
    """Loses one key on its first observation, and counts every observation.

    Seven routes are instrumented - `in`, `.get()`, `[]`, `.keys()`,
    iteration, `.items()` and `.values()` - so a check-then-use spelled through
    any of them is caught: the first observation answers "present" and removes
    the key, and a second one misses. `observations` counts how often the
    victim was observed, present or not; the four whole-dict routes observe
    every key, so each counts as one observation of the victim.
    """

    def __init__(self, *args, victim=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.victim = victim
        self.observations = 0

    def _observe(self, key):
        if key != self.victim:
            return
        self.observations += 1
        if self.observations == 1:
            super().pop(key, None)

    def __contains__(self, key):
        present = super().__contains__(key)
        self._observe(key)
        return present

    def get(self, key, default=None):
        value = super().get(key, default)
        self._observe(key)
        return value

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._observe(key)
        return value

    # The whole-dict routes snapshot before they observe: removing the victim
    # mid-iteration would raise "dictionary changed size", which is the
    # fixture failing rather than the code under test.
    def keys(self):
        snapshot = set(super().keys())
        self._observe(self.victim)
        return snapshot

    def __iter__(self):
        snapshot = list(super().__iter__())
        self._observe(self.victim)
        return iter(snapshot)

    def items(self):
        snapshot = list(super().items())
        self._observe(self.victim)
        return snapshot

    def values(self):
        snapshot = list(super().values())
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
