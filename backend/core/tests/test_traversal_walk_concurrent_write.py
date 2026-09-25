"""The in-memory walk survives a write landing while it traverses.

`storage_search.get_related_nodes` takes no lock, and every mutator holds one,
so an edge can be added or a node removed while the walk is between two steps
of iterating the graph's adjacency. Iterating networkx's live `out_edges` /
`in_edges` views then raised "dictionary changed size during iteration" out of
the traversal. The walk now copies each adjacency before reading it.
"""

import sys
import threading
import time

import networkx as nx

from backend.core import storage_search
from backend.core.models import Edge, Node, NodeType, RelationshipType


def _node(nid):
    return Node(id=nid, type=NodeType.ACTOR, name=nid)


def _edge(eid, source, target):
    return Edge(id=eid, source=source, target=target, type=RelationshipType.RELATES_TO)


def _add(graph, edges, edge):
    edges[edge.id] = edge
    graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)


class _WritesOnLookup(dict):
    """Adds a fresh edge into and out of the hub on every neighbour lookup.

    The walk looks each neighbour up to skip archived ones, from inside its
    loops over the hub's adjacency, so this lands a write at exactly the point
    a concurrent writer can: between two steps of those loops. It does so in
    both the outgoing and the incoming loop.
    """

    def __init__(self, *args, graph, edges, hub, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph = graph
        self.edges = edges
        self.hub = hub
        self.writes = 0

    def get(self, key, default=None):
        if key != self.hub:
            self.writes += 1
            other = f"late{self.writes}"
            _add(self.graph, self.edges, _edge(f"{other}-out", self.hub, other))
            _add(self.graph, self.edges, _edge(f"{other}-in", other, self.hub))
        return super().get(key, default)


def _hub_graph():
    nodes = {nid: _node(nid) for nid in ("hub", "out", "in")}
    edges: dict = {}
    graph = nx.MultiDiGraph()
    for node in nodes.values():
        graph.add_node(node.id, data=node)
    _add(graph, edges, _edge("hub-out", "hub", "out"))
    _add(graph, edges, _edge("in-hub", "in", "hub"))
    return nodes, edges, graph


class TestAWriteMidWalk:
    def test_a_write_between_two_adjacency_steps_does_not_break_the_walk(self):
        nodes, edges, graph = _hub_graph()
        writing = _WritesOnLookup(nodes, graph=graph, edges=edges, hub="hub")

        result = storage_search.get_related_nodes(writing, edges, graph, "hub")

        # Both loops ran a lookup, so both saw a write land mid-iteration.
        assert writing.writes >= 2
        assert {"hub", "out", "in"} <= {n.id for n in result["nodes"]}
        assert {"hub-out", "in-hub"} <= {e.id for e in result["edges"]}

    def test_a_concurrent_writer_never_breaks_a_walk(self):
        """A real writer thread churning the hub's adjacency.

        The deterministic test above lands a write between loop steps; this
        one also covers a write landing while an adjacency is being copied,
        which a copy made by iterating a networkx view (rather than copying
        the dict itself) leaves open.
        """
        nodes = {"hub": _node("hub")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        graph.add_node("hub", data=nodes["hub"])
        for i in range(200):
            nodes[f"n{i}"] = _node(f"n{i}")
            _add(graph, edges, _edge(f"e{i}", "hub", f"n{i}"))
            _add(graph, edges, _edge(f"r{i}", f"n{i}", "hub"))

        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                other = f"churn{i % 50}"
                _add(graph, {}, _edge(f"c{i}-out", "hub", other))
                _add(graph, {}, _edge(f"c{i}-in", other, "hub"))
                graph.remove_node(other)
                i += 1

        errors = []
        walks = 0
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not errors:
                walks += 1
                try:
                    storage_search.get_related_nodes(nodes, edges, graph, "hub")
                except Exception as exc:  # noqa: BLE001 - any failure is the finding
                    errors.append(exc)
        finally:
            stop.set()
            thread.join()
            sys.setswitchinterval(interval)

        assert walks > 1
        assert errors == []
