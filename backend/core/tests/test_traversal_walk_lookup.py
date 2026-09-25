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

    Four routes are instrumented - `in`, `.get()`, `[]` and `.keys()` - so a
    check-then-use spelled through any of them is caught: the first
    observation answers "present" and removes the key, and a second one misses.
    Iteration, `.items()` and `.values()` are not instrumented. `observations`
    counts how often the victim was observed while armed, present or not;
    `.keys()` observes every key.

    Unarmed, the dict is plain. The walk legitimately reads `nodes` while it
    traverses (to skip archived neighbours); only the resolution that follows
    is the window, so the tests arm it when the traversal ends.
    """

    def __init__(self, *args, victim=None, armed=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.victim = victim
        self.armed = armed
        self.observations = 0

    def _observe(self, key):
        if not self.armed or key != self.victim:
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

    def keys(self):
        snapshot = set(super().keys())
        self._observe(self.victim)
        return snapshot


class _ArmsWhenTheWalkEnds:
    """Stands in for the graph and arms the fixtures once the traversal is over.

    The walk reads the graph only through `out_edges` and `in_edges`, and only
    while it traverses. Once `expected` of those iterators have been drained,
    every hop is done and what follows is resolution. That assumes the walk
    consumes each iterator as it loops over it; a walk that materialised them
    up front would arm the fixture before its traversal-phase reads.
    """

    def __init__(self, graph, expected, *fixtures):
        self._graph = graph
        self._remaining = expected
        self._fixtures = fixtures

    def _watch(self, iterator):
        yield from iterator
        self._remaining -= 1
        if self._remaining == 0:
            for fixture in self._fixtures:
                fixture.armed = True

    def out_edges(self, *args, **kwargs):
        return self._watch(self._graph.out_edges(*args, **kwargs))

    def in_edges(self, *args, **kwargs):
        return self._watch(self._graph.in_edges(*args, **kwargs))


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
    """Depth 1 from `a` drains exactly two edge iterators, `a`'s out-edges and
    in-edges, so the fixture arms after two. `observations == 1` is also what
    keeps these from passing vacuously: a resolution the fixture never saw
    would leave it at zero.
    """

    def test_a_node_deleted_during_resolution_is_observed_once(self):
        nodes, edges = _fixture()
        vanishing = _VanishingOnLookup(nodes, victim="b", armed=False)
        graph = _ArmsWhenTheWalkEnds(_graph(nodes, edges), 2, vanishing)

        result = storage_search.get_related_nodes(vanishing, edges, graph, "a")

        assert vanishing.observations == 1
        assert None not in result["nodes"]
        assert {n.id for n in result["nodes"]} == {"a", "b"}
        assert {e.id for e in result["edges"]} == {"ab"}

    def test_an_edge_deleted_during_resolution_is_observed_once(self):
        nodes, edges = _fixture()
        vanishing = _VanishingOnLookup(edges, victim="ab", armed=False)
        graph = _ArmsWhenTheWalkEnds(_graph(nodes, edges), 2, vanishing)

        result = storage_search.get_related_nodes(nodes, vanishing, graph, "a")

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
