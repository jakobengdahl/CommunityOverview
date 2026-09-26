"""The in-memory walk survives a write landing while it traverses.

`storage_search.get_related_nodes` takes no lock, and every mutator holds one,
so an edge can be added or a node removed while the walk is between two steps
of iterating the graph's adjacency. Iterating networkx's live `out_edges` /
`in_edges` views then raised "dictionary changed size during iteration" out of
the traversal. The walk now copies each adjacency before reading it.
"""

import gc
import sys
import threading
import time

import networkx as nx
import pytest

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
        the dict itself) leaves open. It churns both levels of that adjacency:
        whole neighbours come and go, and so do parallel edges to neighbours
        that stay, which changes the per-pair key dicts in place.
        """
        nodes = {"hub": _node("hub")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        graph.add_node("hub", data=nodes["hub"])
        for i in range(200):
            nodes[f"n{i}"] = _node(f"n{i}")
            _add(graph, edges, _edge(f"e{i}", "hub", f"n{i}"))
            _add(graph, edges, _edge(f"r{i}", f"n{i}", "hub"))
        # Wide key dicts on the pairs the writer churns, so copying one of
        # them takes long enough for a write to land mid-copy.
        for i in range(100):
            _add(graph, edges, _edge(f"pe{i}", "hub", "n0"))
            _add(graph, edges, _edge(f"pr{i}", "n1", "hub"))

        stop = threading.Event()
        writes = []
        writer_errors = []

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    other = f"churn{i % 50}"
                    _add(graph, {}, _edge(f"c{i}-out", "hub", other))
                    _add(graph, {}, _edge(f"c{i}-in", other, "hub"))
                    graph.remove_node(other)
                    _add(graph, {}, _edge(f"p{i}-out", "hub", "n0"))
                    _add(graph, {}, _edge(f"p{i}-in", "n1", "hub"))
                    graph.remove_edge("hub", "n0", key=f"p{i}-out")
                    graph.remove_edge("n1", "hub", key=f"p{i}-in")
                    i += 1
                    writes.append(i)
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert
                writer_errors.append(exc)

        errors = []
        walks = 0
        interval = sys.getswitchinterval()
        thread = threading.Thread(target=writer, daemon=True)
        try:
            sys.setswitchinterval(1e-6)
            thread.start()
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not errors:
                walks += 1
                try:
                    storage_search.get_related_nodes(nodes, edges, graph, "hub")
                except Exception as exc:  # noqa: BLE001 - any failure is the finding
                    errors.append(exc)
        finally:
            stop.set()
            if thread.is_alive():
                thread.join()
            sys.setswitchinterval(interval)

        assert writer_errors == []
        assert walks > 1
        assert writes
        assert errors == []

    @pytest.mark.parametrize(
        "level, write, min_retries",
        [
            ("neighbours", "grow", 100),
            ("parallel_keys", "grow", 100),
            ("parallel_keys", "swap", 1),
            ("narrow_keys", "grow", 100),
        ],
    )
    def test_a_write_from_a_collection_during_the_copy_does_not_break_the_walk(
        self, level, write, min_retries
    ):
        """The copy runs in C, but each item it copies allocates a tuple, and
        on CPython 3.11 an allocation can run a garbage collection, and with it
        Python code that can hand the GIL to a writer. A `gc.callbacks` hook
        is that Python code here, standing in for the writer: with the
        collection threshold at 1, and armed by the adjacency lookup that
        precedes the copy, it lands writes from inside the copy itself - on
        the hub's neighbour dict, or on the key dict of one pair - a wide
        one, or one holding a single edge, so the copy is not skipped for a
        dict too small to be written mid-copy.

        A "grow" write adds an entry, which the copy reports as "dictionary
        changed size". A "swap" write removes an entry the copy has already
        passed and adds another, keeping the size, which it reports as
        "dictionary keys changed" instead, and only once the copy reaches
        the end of the dict - so a swap forces one retry, where a grow forces
        one per write or two.
        """
        budget_size = 1000
        base = range(1, 2) if level == "narrow_keys" else range(1, 50)
        nodes = {"hub": _node("hub"), "n0": _node("n0")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        for node in nodes.values():
            graph.add_node(node.id, data=node)
        if level == "neighbours":
            for i in base:
                nodes[f"n{i}"] = _node(f"n{i}")
                _add(graph, edges, _edge(f"e{i}", "hub", f"n{i}"))
            entries = [{} for _ in range(budget_size)]
        else:
            if write == "swap":
                # Ahead of the edges the walk must return, in insertion order,
                # so each swap removes one of these, never one of those.
                for i in range(budget_size):
                    old = _edge(f"old{i}", "hub", "n0")
                    graph.add_edge("hub", "n0", key=old.id, data=old)
            for i in base:
                _add(graph, edges, _edge(f"e{i}", "hub", "n0"))
            entries = [
                {"data": _edge(f"gc{i}", "hub", "n0")} for i in range(budget_size)
            ]

        copies = []

        class _CountsCopies(dict):
            def items(self):
                copies.append(len(self))
                return super().items()

        if level == "neighbours":
            written = graph._succ["hub"] = _CountsCopies(graph._succ["hub"])
        else:
            written = _CountsCopies(graph._succ["hub"]["n0"])
            graph._succ["hub"]["n0"] = graph._pred["n0"]["hub"] = written
            if write == "swap":
                # A same-size write that also resizes the dict can move the
                # entries under the copy without either error firing. Grow
                # it until it resizes, which leaves room for more inserts
                # than it holds, so no swap below resizes it.
                unresized = sys.getsizeof(written)
                pad = 0
                while sys.getsizeof(written) == unresized:
                    written[f"pad{pad}"] = entries[0]
                    pad += 1
                for i in range(pad):
                    del written[f"pad{i}"]
                assert len(written) >= budget_size
        size = sys.getsizeof(written)
        budget = []
        writes = []
        # 2-tuples come from a free list that bypasses the collector's
        # allocation count, so the copy only reaches a collection once that
        # list is empty. Holding more pairs than it keeps empties it.
        drained = []

        class _ArmsOnLookup(dict):
            def get(self, key, default=None):
                if key == "hub":
                    drained[:] = [(i, i) for i in range(5000)]
                    budget[:] = [None] * budget_size
                return super().get(key, default)

        def write_during_collection(phase, info):
            if phase == "start" and budget:
                budget.pop()
                writes.append(phase)
                if write == "swap":
                    del written[next(iter(written))]
                written[f"gc{len(writes)}"] = entries[len(writes) - 1]

        graph._succ = graph._adj = _ArmsOnLookup(graph._succ)
        threshold = gc.get_threshold()
        gc.callbacks.append(write_during_collection)
        try:
            gc.set_threshold(1)
            result = storage_search.get_related_nodes(nodes, edges, graph, "hub")
        finally:
            gc.set_threshold(*threshold)
            gc.callbacks.remove(write_during_collection)

        assert drained
        # The walk copies this dict once; every copy after the first is a
        # retry, which only an interrupted copy starts. A grow forces enough
        # of them that a retry giving up after a few dozen attempts fails.
        assert len(copies) - 1 >= min_retries, copies
        if write == "swap":
            assert sys.getsizeof(written) == size
        assert {f"e{i}" for i in base} <= {e.id for e in result["edges"]}


class TestTheCopyKeepsTheWalksAnswer:
    """With no write in flight, the copied adjacency must answer exactly as
    the live one did: every parallel edge, and nothing for an anchor the
    graph does not hold."""

    def _parallel(self):
        nodes = {nid: _node(nid) for nid in ("a", "b")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        for node in nodes.values():
            graph.add_node(node.id, data=node)
        _add(graph, edges, _edge("ab1", "a", "b"))
        _add(
            graph,
            edges,
            Edge(id="ab2", source="a", target="b", type=RelationshipType.PART_OF),
        )
        _add(graph, edges, _edge("ba1", "b", "a"))
        return nodes, edges, graph

    def test_every_parallel_edge_between_a_pair_is_returned(self):
        nodes, edges, graph = self._parallel()

        result = storage_search.get_related_nodes(nodes, edges, graph, "a")

        assert {e.id for e in result["edges"]} == {"ab1", "ab2", "ba1"}

    def test_a_type_filter_picks_the_matching_parallel_edge(self):
        nodes, edges, graph = self._parallel()

        result = storage_search.get_related_nodes(
            nodes, edges, graph, "a", relationship_types=[RelationshipType.PART_OF]
        )

        assert {e.id for e in result["edges"]} == {"ab2"}
        assert {n.id for n in result["nodes"]} == {"a", "b"}

    def test_a_second_hop_follows_the_incoming_edges_of_the_first(self):
        # c reaches b only through an incoming edge, so it is found only if
        # the second hop reads b's incoming adjacency, not the anchor's.
        nodes = {nid: _node(nid) for nid in ("a", "b", "c")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        for node in nodes.values():
            graph.add_node(node.id, data=node)
        _add(graph, edges, _edge("ab", "a", "b"))
        _add(graph, edges, _edge("cb", "c", "b"))

        result = storage_search.get_related_nodes(nodes, edges, graph, "a", depth=2)

        assert {n.id for n in result["nodes"]} == {"a", "b", "c"}
        assert {e.id for e in result["edges"]} == {"ab", "cb"}

    def test_an_anchor_the_graph_does_not_hold_returns_only_itself(self):
        # networkx's out_edges("ab") on a graph without "ab" iterates the
        # string, so the live views walked from "a" and "b" instead.
        nodes = {nid: _node(nid) for nid in ("a", "b", "ab")}
        edges: dict = {}
        graph = nx.MultiDiGraph()
        graph.add_node("a", data=nodes["a"])
        graph.add_node("b", data=nodes["b"])
        _add(graph, edges, _edge("e", "a", "b"))

        result = storage_search.get_related_nodes(nodes, edges, graph, "ab")

        assert [n.id for n in result["nodes"]] == ["ab"]
        assert result["edges"] == []
