"""The two traversal implementations must answer identically.

`dec-oc-traversal-recursive-cte` accepted a second implementation in order to
remove the memory ceiling, on one condition: that the two are held to the same
answer by a differential test rather than by two people reading two pieces of
code. This is that test.

It is written as a fuzz against randomised graphs rather than as a list of
cases, because the failure it exists to catch is the one nobody thought of.
The four cases that are enumerated are the ones a reading of the in-memory walk
does NOT make obvious, and each was confirmed against the reference before the
SQL was written:

- an edge between two nodes that are both exactly `depth` away is not returned,
  because neither endpoint was ever expanded;
- an edge whose far endpoint is not in the graph IS returned, and the missing
  id is not;
- an archived node blocks the path through it, and the edge that would have
  reached it is dropped too;
- an archived anchor is still returned.

Order is deliberately not compared. The in-memory walk collects into sets, so
its order is an artefact rather than a contract.
"""

import os
import random
import uuid

import pytest

from backend.core.models import Edge, Node, NodeType, RelationshipType

REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES") == "1"

if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from backend.core import storage_search  # noqa: E402
from backend.core.postgres_backend import (  # noqa: E402
    PostgresGraphPersistenceBackend,
)

DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")


def _server_reachable() -> bool:
    if not DSN:
        if REQUIRE:
            raise RuntimeError(
                "CO_REQUIRE_POSTGRES=1 but CO_TEST_POSTGRES_DSN is unset"
            )
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception as exc:
        if REQUIRE:
            raise RuntimeError(
                f"CO_REQUIRE_POSTGRES=1 but the server at {DSN} is "
                f"unreachable: {type(exc).__name__}: {exc}"
            ) from exc
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason=(
        "set CO_TEST_POSTGRES_DSN to a PostgreSQL server to run these"
        if not DSN
        else f"no PostgreSQL server reachable at CO_TEST_POSTGRES_DSN ({DSN})"
    ),
)


@pytest.fixture
def schema():
    name = f"co_tr_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _reference(nodes, edges, anchor, depth, types, include_archived):
    """The in-memory walk, which is the contract the store must match."""
    import networkx as nx

    graph = nx.MultiDiGraph()
    for node in nodes.values():
        graph.add_node(node.id, data=node)
    for edge in edges.values():
        graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)
    result = storage_search.get_related_nodes(
        nodes, edges, graph, anchor, types, depth, include_archived=include_archived
    )
    return (
        {n.id for n in result["nodes"]},
        {e.id for e in result["edges"]},
    )


def _load(backend, nodes, edges):
    backend.save_graph_data(
        {
            "nodes": [n.model_dump(mode="json") for n in nodes.values()],
            "edges": [e.model_dump(mode="json") for e in edges.values()],
            "metadata": {},
        }
    )


def _compare(backend, nodes, edges, anchor, depth, types, include_archived):
    want_nodes, want_edges = _reference(
        nodes, edges, anchor, depth, types, include_archived
    )
    got = backend.traverse(
        anchor,
        depth,
        # `.value`, not `str()` - see the note in PostgresGraphPersistenceBackend.
        relationship_types=[getattr(t, "value", t) for t in types] if types else None,
        include_archived=include_archived,
    )
    assert set(got["node_ids"]) == want_nodes, (
        f"node sets differ for anchor={anchor} depth={depth} types={types} "
        f"archived={include_archived}: store-only={set(got['node_ids']) - want_nodes} "
        f"memory-only={want_nodes - set(got['node_ids'])}"
    )
    assert set(got["edge_ids"]) == want_edges, (
        f"edge sets differ for anchor={anchor} depth={depth} types={types} "
        f"archived={include_archived}: store-only={set(got['edge_ids']) - want_edges} "
        f"memory-only={want_edges - set(got['edge_ids'])}"
    )


class TestTheStoreAnswersWhatTheWalkWould:
    @pytest.mark.parametrize("seed", range(6))
    def test_randomised_graphs_agree_on_every_query_shape(self, schema, seed):
        rng = random.Random(seed)
        n_nodes = rng.choice([1, 2, 5, 20, 60])
        nodes = {}
        for i in range(n_nodes):
            nodes[f"n{i}"] = Node(
                id=f"n{i}",
                type=NodeType.ACTOR,
                name=f"node {i}",
                archived=rng.random() < 0.2,
            )
        rel_types = [RelationshipType.RELATES_TO, RelationshipType.PART_OF]
        edges = {}
        for i in range(int(n_nodes * rng.uniform(0.5, 3))):
            src, tgt = rng.choice(list(nodes)), rng.choice(list(nodes))
            if rng.random() < 0.1:  # a dangling edge, which is a real shape
                tgt = f"ghost{i}"
            edges[f"e{i}"] = Edge(
                id=f"e{i}",
                source=src,
                target=tgt,
                type=rng.choice(rel_types),
                archived=rng.random() < 0.2,
            )

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            for anchor in list(nodes)[:6]:
                for depth in (0, 1, 2, 3):
                    for types in (None, [RelationshipType.RELATES_TO], rel_types):
                        for archived in (False, True):
                            _compare(
                                backend, nodes, edges, anchor, depth, types, archived
                            )
        finally:
            backend.close()

    def test_an_edge_between_two_frontier_nodes_is_not_returned(self, schema):
        """Neither endpoint was ever expanded. Reading the walk suggests the
        opposite, which is why this is pinned separately from the fuzz."""
        nodes = {i: Node(id=i, type=NodeType.ACTOR, name=i) for i in ("a", "b", "c")}
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            ),
            "ac": Edge(
                id="ac", source="a", target="c", type=RelationshipType.RELATES_TO
            ),
            "bc": Edge(
                id="bc", source="b", target="c", type=RelationshipType.RELATES_TO
            ),
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("a", 1)
            assert set(got["node_ids"]) == {"a", "b", "c"}
            assert set(got["edge_ids"]) == {"ab", "ac"}, (
                "'bc' joins two nodes that are both exactly one hop away; the "
                "walk never expands either, so the edge is not part of the answer"
            )
            _compare(backend, nodes, edges, "a", 1, None, False)
        finally:
            backend.close()

    def test_an_edge_to_a_node_that_is_not_there_is_still_an_edge(self, schema):
        nodes = {"a": Node(id="a", type=NodeType.ACTOR, name="a")}
        edges = {
            "ax": Edge(
                id="ax", source="a", target="ghost", type=RelationshipType.RELATES_TO
            )
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("a", 1)
            assert set(got["edge_ids"]) == {"ax"}
            assert set(got["node_ids"]) == {"a"}, "the missing id is not a node"
            _compare(backend, nodes, edges, "a", 1, None, False)
        finally:
            backend.close()

    def test_an_archived_node_blocks_the_path_through_it(self, schema):
        nodes = {
            "a": Node(id="a", type=NodeType.ACTOR, name="a"),
            "b": Node(id="b", type=NodeType.ACTOR, name="b", archived=True),
            "c": Node(id="c", type=NodeType.ACTOR, name="c"),
        }
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            ),
            "bc": Edge(
                id="bc", source="b", target="c", type=RelationshipType.RELATES_TO
            ),
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("a", 2)
            assert set(got["node_ids"]) == {"a"}
            assert set(got["edge_ids"]) == set(), (
                "the edge that would have reached the archived node goes too"
            )
            _compare(backend, nodes, edges, "a", 2, None, False)
            _compare(backend, nodes, edges, "a", 2, None, True)
        finally:
            backend.close()

    def test_an_archived_anchor_is_still_returned(self, schema):
        nodes = {
            "a": Node(id="a", type=NodeType.ACTOR, name="a", archived=True),
            "b": Node(id="b", type=NodeType.ACTOR, name="b"),
        }
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            )
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("a", 1)
            assert "a" in set(got["node_ids"])
            _compare(backend, nodes, edges, "a", 1, None, False)
        finally:
            backend.close()

    def test_an_anchor_that_is_not_in_the_graph_returns_nothing(self, schema):
        nodes = {"a": Node(id="a", type=NodeType.ACTOR, name="a")}
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, {})
            got = backend.traverse("nosuchnode", 2)
            assert got == {"node_ids": [], "edge_ids": []}, (
                "not an empty traversal but no traversal - the walk returns "
                "nothing at all rather than a lone anchor"
            )
        finally:
            backend.close()


class TestTheStoreOnlyAnswersWhenItIsCurrent:
    """The one property the equivalence test cannot reach.

    The two engines agree about a given graph. They do not agree about a given
    MOMENT: writes are handed to a background executor, so between a mutation
    and its write landing the in-memory dictionaries are ahead of the store. A
    traversal answered there would miss the caller's own write - not a
    disagreement, a different question. `GraphStorage` therefore sends the
    query to the walk while a write is in flight.
    """

    def test_a_traversal_straight_after_a_write_sees_the_write(self, schema):
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="anchor"),
                    Node(id="b", type=NodeType.ACTOR, name="neighbour"),
                ],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    )
                ],
            )
            # Deliberately no flush(). This is the window.
            result = storage.get_related_nodes("a", depth=1)
            assert {n.id for n in result["nodes"]} == {"a", "b"}, (
                "a traversal answered by the store in the write window would "
                "have returned the anchor alone, or nothing at all"
            )
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()

    def test_the_store_answers_once_the_write_has_landed(self, schema):
        """The other half: the guard must not switch the store off for good."""
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="anchor"),
                    Node(id="b", type=NodeType.ACTOR, name="neighbour"),
                ],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    )
                ],
            )
            storage.flush()
            assert storage._store_traversal_is_current(), (
                "with nothing pending the store should be the one answering"
            )
            result = storage.get_related_nodes("a", depth=1)
            assert {n.id for n in result["nodes"]} == {"a", "b"}
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()


class TestTheGuardCoversEveryRouteAWriteCanTake:
    """The first version watched only `_persist`'s incremental branch, and
    `_persist` itself falls back to `save()` in three documented cases - so the
    guard was open on exactly the writes that had already gone wrong once. A
    backend declaring `store_traversal` without `incremental_writes`, which the
    capability flags explicitly permit, never set the marker at all.
    """

    def test_a_snapshot_save_is_a_write_the_guard_can_see(self, schema):
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="a")], [])
            storage.flush()
            assert storage._store_traversal_is_current()

            storage.save()  # the whole-graph route, not _persist's
            assert not storage._store_traversal_is_current(), (
                "a snapshot save is a write like any other; a traversal "
                "answered by the store before it lands would be reading the "
                "graph as it was"
            )
        finally:
            storage.flush()
            backend.close()


class TestWhatTheStoreDecidedIsFilteredByWhatWeReturn:
    """Membership comes from the store's moment and the payloads from ours, so
    a node archived in between would be returned while its own payload says
    archived - a state neither engine ever held. The walk cannot produce it: it
    decides and resolves from the same dictionary in one pass.
    """

    def test_a_node_archived_after_the_store_answered_is_not_returned(self, schema):
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="c", type=NodeType.ACTOR, name="c"),
                ],
                [
                    Edge(
                        id="ac",
                        source="a",
                        target="c",
                        type=RelationshipType.RELATES_TO,
                    )
                ],
            )
            storage.flush()

            # The store still says `c` is visible; memory already knows better.
            storage.nodes["c"].archived = True

            result = storage.get_related_nodes("a", depth=1)
            returned = {n.id for n in result["nodes"]}
            assert "c" not in returned, (
                "returned a node whose own payload says archived=True under "
                f"include_archived=False: {returned}"
            )
            assert {e.id for e in result["edges"]} == set(), (
                "and the edge that reached it goes with it"
            )
        finally:
            storage.flush()
            backend.close()


class TestADeclaredCapabilityMustBeImplemented:
    def test_declaring_store_traversal_without_traverse_is_refused(self):
        """It fails quietly otherwise: every traversal warns and walks, for the
        life of the process, with nothing to say the declaration was wrong."""
        from backend.core.storage_backends import (
            BackendCapabilities,
            capabilities_of,
        )

        class _Liar:
            def capabilities(self):
                return BackendCapabilities(store_traversal=True)

        with pytest.raises(TypeError, match="store_traversal"):
            capabilities_of(_Liar())


class TestDepthIsBoundedByTheGraphNotByTheCaller:
    """`reach` is keyed on (id, d), so the recursion never converges early: it
    runs the full `depth` iterations however small the graph. REST caps depth
    at 5, the MCP tool does not cap it at all, so the bound lives in the store.
    """

    def test_a_huge_depth_costs_no_more_than_the_graph_allows(self, schema):
        import time

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        # 40 nodes at depth 200_000 was chosen by measuring both sides. With
        # the clamp: 0.006s. Without it: 20.4s on one run, and on a second the
        # backend process was OOM-killed, which takes the whole cluster down
        # with it. A smaller shape does not separate the two -- 12 nodes at
        # depth 50_000 costs 1.4s unclamped, under any threshold loose enough
        # not to be flaky.
        nodes = {
            f"n{i}": Node(id=f"n{i}", type=NodeType.ACTOR, name=f"n{i}")
            for i in range(40)
        }
        edges = {
            f"e{i}": Edge(
                id=f"e{i}",
                source=f"n{i}",
                target=f"n{i + 1}",
                type=RelationshipType.RELATES_TO,
            )
            for i in range(39)
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            start = time.perf_counter()
            deep = backend.traverse("n0", 200_000)
            elapsed = time.perf_counter() - start
            assert elapsed < 2.0, (
                f"depth 200000 on a 40-node graph took {elapsed:.1f}s; the "
                f"recursion is running the caller's number of levels rather "
                f"than the graph's"
            )
            # and the answer is still the whole chain
            assert set(deep["node_ids"]) == set(nodes)
            assert set(deep["node_ids"]) == set(backend.traverse("n0", 39)["node_ids"])
        finally:
            backend.close()
