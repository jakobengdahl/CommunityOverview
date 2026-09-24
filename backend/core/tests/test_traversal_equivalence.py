"""The two traversal implementations must answer identically.

`dec-oc-traversal-recursive-cte` accepted a second implementation in order to
remove the memory ceiling, on one condition: that the two are held to the same
answer by a differential test rather than by two people reading two pieces of
code. This is that test.

It is written as a fuzz against randomised graphs rather than as a list of
cases, because the failure it exists to catch is the one nobody thought of.
The cases that are enumerated beside it are the ones a reading of the in-memory
walk does NOT make obvious, or that the generator cannot produce. Four of them
were confirmed against the reference before the SQL was written:

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

from psycopg.conninfo import make_conninfo  # noqa: E402
from psycopg_pool import PoolTimeout  # noqa: E402

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


class _VanishingOnLookup(dict):
    """Says yes, then loses the key - exactly one id, exactly once."""

    def __init__(self, *args, victim=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.victim = victim

    def __contains__(self, key):
        present = super().__contains__(key)
        if key == self.victim:
            super().pop(key, None)
            self.victim = None
        return present


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
                # -1 because `mcp_tools.get_related_nodes` caps nothing and
                # the walk's `range(depth)` makes any negative depth the anchor
                # alone: the store has to clamp rather than take its absolute
                # value, which is a live edit that changes no other answer.
                for depth in (-1, 0, 1, 2, 3):
                    for types in (None, [RelationshipType.RELATES_TO], rel_types):
                        for archived in (False, True):
                            _compare(
                                backend, nodes, edges, anchor, depth, types, archived
                            )
        finally:
            backend.close()

    def test_an_edge_may_share_an_id_with_a_node(self, schema):
        """Node ids and edge ids are separate namespaces - separate dicts in
        memory, separate tables in the store - so they can collide, and the
        fuzz cannot produce one because it names nodes n{i} and edges e{i}.
        Tracking both in one set is a natural simplification and loses the
        node: the edge is recorded first, and the node of the same id then
        reads as already seen.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("a", "b")}
        edges = {
            "b": Edge(id="b", source="a", target="b", type=RelationshipType.RELATES_TO)
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("a", 1)
            assert set(got["node_ids"]) == {"a", "b"}, (
                "the node was lost to an edge of the same id: "
                f"{sorted(got['node_ids'])}"
            )
            assert set(got["edge_ids"]) == {"b"}
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

    def test_the_backend_coerces_an_enum_type_filter_itself(self, schema):
        """Both callers coerce with `.value` before calling, so the backend's
        own coercion has no caller that can exercise it - `str(t)` there,
        exactly the bug its comment warns about, passes the whole suite. The
        protocol asks for strings; this pins the defensive copy that accepts
        an enum anyway, since it is there.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("a", "b", "c")}
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            ),
            "ac": Edge(id="ac", source="a", target="c", type=RelationshipType.PART_OF),
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            # The enum itself, not its .value: str() of it is
            # "RelationshipType.RELATES_TO", which matches no stored document.
            got = backend.traverse(
                "a", 1, relationship_types=[RelationshipType.RELATES_TO]
            )
            assert set(got["node_ids"]) == {"a", "b"}, (
                "an enum filter matched nothing (anchor alone) or was ignored "
                f"(c present): {sorted(got['node_ids'])}"
            )
            assert set(got["edge_ids"]) == {"ab"}
        finally:
            backend.close()

    def test_a_document_without_an_archived_key_is_not_treated_as_archived(
        self, schema
    ):
        """Every fixture here round-trips through `model_dump()`, so every
        document carries `archived: false` and the two COALESCEs that handle
        its absence are never exercised - a mutation round removed both with
        the suite green. The shape is real: `Node.from_dict` and
        `Edge.from_dict` both `setdefault("archived", False)` for data that
        predates the flag, and the SQL is the one reader that does not go
        through them. Without the COALESCE, `NOT NULL` is NULL and every such
        row is filtered out - the store returns the anchor alone where the walk
        returns the neighbourhood.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            # Written as raw documents, deliberately: the point is a store
            # holding what an older release or a non-model writer put there.
            backend.save_graph_data(
                {
                    "nodes": [
                        {"id": "a", "type": "Actor", "name": "a"},
                        {"id": "b", "type": "Actor", "name": "b"},
                    ],
                    "edges": [
                        {
                            "id": "ab",
                            "source": "a",
                            "target": "b",
                            "type": "RELATES_TO",
                        }
                    ],
                    "metadata": {},
                }
            )
            got = backend.traverse("a", 1)
            assert set(got["node_ids"]) == {"a", "b"}, (
                "a document with no `archived` key was read as archived; got "
                f"{sorted(got['node_ids'])}"
            )
            assert set(got["edge_ids"]) == {"ab"}
        finally:
            backend.close()

    def test_a_traversal_can_be_a_backends_first_call(self, schema):
        """The contract pins this for writes (`test_an_entity_write_can_be_a
        _backends_first_call`) and had no traversal equivalent. Without the
        migration call, traversing an unprovisioned store raises UndefinedTable
        instead of answering - hidden in product terms, because the storage
        layer catches it and walks, which is also why no other test sees it.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            assert backend.traverse("nobody", 2) == {
                "node_ids": [],
                "edge_ids": [],
            }
        finally:
            backend.close()

    def test_an_anchor_that_is_not_in_the_graph_returns_nothing(self, schema):
        # Edges that NAME the absent anchor, because the anchor id alone is
        # the weaker half of this. Without the pre-check the walk still starts
        # from the id it was given and returns it as a node - which an empty
        # graph would catch too - but only edges naming it show the other
        # half: a ghost anchor expanding into the neighbourhood around it.
        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("a", "b")}
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            ),
            "ga": Edge(
                id="ga",
                source="nosuchnode",
                target="a",
                type=RelationshipType.RELATES_TO,
            ),
            "bg": Edge(
                id="bg",
                source="b",
                target="nosuchnode",
                type=RelationshipType.RELATES_TO,
            ),
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("nosuchnode", 2)
            assert got == {"node_ids": [], "edge_ids": []}, (
                "not an empty traversal but no traversal - the walk returns "
                f"nothing at all rather than a lone anchor, or its edges: {got}"
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


class TestTheStoreIsActuallyTheOneAnswering:
    """Every other test here would pass with the store branch deleted, because
    the walk it falls back to is correct. That makes the whole feature
    unfalsifiable: a mutation round confirmed the branch can be made inert
    (`if False:`) with the suite fully green. The only way to assert the store
    answered is to make the walk unable to.
    """

    def test_the_answer_survives_a_walk_that_cannot_run(self, schema):
        from backend.core import storage as storage_module
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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
            assert storage._store_traversal_is_current()

            original = storage_module.storage_search.get_related_nodes

            def _refuse(*args, **kwargs):
                raise AssertionError("the walk answered; the store did not")

            storage_module.storage_search.get_related_nodes = _refuse
            try:
                result = storage.get_related_nodes("a", depth=1)
            finally:
                storage_module.storage_search.get_related_nodes = original

            assert {n.id for n in result["nodes"]} == {"a", "b"}
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()

    def test_the_store_also_answers_under_include_archived(self, schema):
        """Same shape as the archived anchor, one exemption over: dropping
        `include_archived` from the visibility check makes every archived node
        in the store's answer look vanished, so the whole include_archived=True
        class falls through to the walk and returns the identical answer.
        """
        from backend.core import storage as storage_module
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b", archived=True),
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

            original = storage_module.storage_search.get_related_nodes

            def _refuse(*args, **kwargs):
                raise AssertionError("the walk answered; the store did not")

            storage_module.storage_search.get_related_nodes = _refuse
            try:
                result = storage.get_related_nodes("a", depth=1, include_archived=True)
            finally:
                storage_module.storage_search.get_related_nodes = original

            assert {n.id for n in result["nodes"]} == {"a", "b"}
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()

    def test_the_store_also_answers_for_an_archived_anchor(self, schema):
        """The anchor exemption is the one place where a wrong answer is not
        the failure mode: dropping it makes every archived-anchor traversal
        look like a vanished node, fall through to the walk, and return the
        same thing - the store branch quietly dead for a whole class of
        anchor, which is the unfalsifiability this class exists to close.
        """
        from backend.core import storage as storage_module
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a", archived=True),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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
            assert storage.nodes["a"].archived, "the anchor has to be archived"

            original = storage_module.storage_search.get_related_nodes

            def _refuse(*args, **kwargs):
                raise AssertionError("the walk answered; the store did not")

            storage_module.storage_search.get_related_nodes = _refuse
            try:
                result = storage.get_related_nodes("a", depth=1)
            finally:
                storage_module.storage_search.get_related_nodes = original

            assert {n.id for n in result["nodes"]} == {"a", "b"}
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()


class TestTheTraversalIsOneMoment:
    """N levels on one connection are N statements, and PostgreSQL's default
    isolation takes its snapshot per statement - so without an isolation level
    of its own, level 2 reads a graph level 1 never saw. `load_graph_data`
    makes this argument for itself in the same file, for the same reason.

    Measured before the fix, with another connection committing `DELETE ab`
    and `INSERT bc` between the two levels of `traverse("a", 2)`: the store
    returned nodes a,b,c and edges ab,bc - the deleted edge AND the new one,
    which is the answer for neither graph.
    """

    def test_a_write_between_levels_does_not_tear_the_answer(self, schema):
        import psycopg

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("a", "b", "c")}
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            )
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        original = psycopg.Connection.execute
        levels = {"n": 0}

        def _write_between_levels(self, query, *args, **kwargs):
            result = original(self, query, *args, **kwargs)
            if "far.id" in repr(query):
                levels["n"] += 1
                if levels["n"] == 1:
                    with psycopg.connect(DSN, autocommit=True) as writer:
                        writer.execute(
                            psycopg.sql.SQL(
                                "DELETE FROM {}.graph_edges WHERE id = %s"
                            ).format(psycopg.sql.Identifier(schema)),
                            ("ab",),
                        )
                        writer.execute(
                            psycopg.sql.SQL(
                                "INSERT INTO {}.graph_edges (id, doc) VALUES (%s, %s)"
                            ).format(psycopg.sql.Identifier(schema)),
                            (
                                "bc",
                                psycopg.types.json.Jsonb(
                                    {
                                        "id": "bc",
                                        "source": "b",
                                        "target": "c",
                                        "type": "RELATES_TO",
                                    }
                                ),
                            ),
                        )
            return result

        try:
            _load(backend, nodes, edges)
            psycopg.Connection.execute = _write_between_levels
            try:
                got = backend.traverse("a", 2)
            finally:
                psycopg.Connection.execute = original
            assert levels["n"] >= 2, (
                "the traversal did not reach a second level, so nothing was "
                "interleaved and this asserts nothing"
            )
            # The graph as the traversal began: a -> b, and c unconnected.
            assert set(got["node_ids"]) == {"a", "b"}, (
                "the answer mixes the graph before the write with the graph "
                f"after it: {sorted(got['node_ids'])}"
            )
            assert set(got["edge_ids"]) == {"ab"}, f"got {sorted(got['edge_ids'])}"
        finally:
            backend.close()


class TestTheTraversalHoldsOneConnection:
    """`pool_size=1` is a supported configuration - the constructor accepts it
    and the pool-size comment discusses it. Taking a second connection per
    level would be the natural way to write the loop and would deadlock there
    against its own pool, answering correctly on every larger pool and so on
    every other test in this file.

    This pins the narrower property, which is the one at risk: no NESTED
    checkout while the outer connection is held. A rewrite that took one
    connection per level and none around them would not deadlock and would
    pass. `load_graph_data` holds one connection for four statements and is
    the same shape, so it is not the only path that needs this care.
    """

    def test_a_traversal_completes_on_a_pool_of_one(self, schema):
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("a", "b", "c")}
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            ),
            "bc": Edge(
                id="bc", source="b", target="c", type=RelationshipType.RELATES_TO
            ),
        }
        # Seeded by a SEPARATE backend, so the one under test meets the
        # traversal as its very first call. Migrating from inside the held
        # connection needs a second one and deadlocks against its own pool -
        # measured at 30s to PoolTimeout - and loading here first would hide
        # that, because the load migrates.
        seeder = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(seeder, nodes, edges)
        finally:
            seeder.close()

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        try:
            # Several levels, so a per-level checkout would need a second
            # connection while the first is still held.
            got = backend.traverse("a", 3)
            assert set(got["node_ids"]) == {"a", "b", "c"}
            assert set(got["edge_ids"]) == {"ab", "bc"}
        finally:
            backend.close()


class TestTheTraversalTerminatesAndNotJustCorrectly:
    """Equivalence says the two engines return the same SET. It says nothing
    about what the query costs to get there, and a cyclic graph is where the
    difference lives: every level re-reaches nodes the previous ones already
    found, so an implementation that does not prune by `seen` revisits them
    for as many levels as it is given. This one is dense, cyclic and asked for
    5 levels - inside the REST API's cap, and the MCP tool caps nothing.

    The statement timeout is the point of the test, not scaffolding. It makes
    a regression here a cancelled query rather than a query that grows without
    bound: the implementation this replaced could take the backend process out
    on this graph, and an OOM kill takes the whole cluster with it rather than
    failing one test.
    """

    def test_a_dense_cyclic_graph_at_the_rest_api_depth_cap(self, schema):
        import random

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        rng = random.Random(7)
        nodes = {
            f"n{i}": Node(id=f"n{i}", type=NodeType.ACTOR, name=f"n{i}")
            for i in range(60)
        }
        edges = {
            f"e{k}": Edge(
                id=f"e{k}",
                source=f"n{rng.randrange(60)}",
                target=f"n{rng.randrange(60)}",
                type=RelationshipType.RELATES_TO,
            )
            for k in range(600)
        }
        # Built by psycopg, not by string concatenation. A DSN comes in two
        # shapes - a URI, and the keyword/value form CI passes - and appending
        # a query string works only on the first. Glued onto the second it
        # lands inside the dbname value, and the connection then asks for a
        # database called `communityoverview_test?options=...`, which is how
        # this passed locally and could never have passed in CI.
        bounded = make_conninfo(DSN, options="-c statement_timeout=10000")
        backend = PostgresGraphPersistenceBackend(bounded, schema=schema)
        try:
            _load(backend, nodes, edges)
            got = backend.traverse("n0", 5)
            assert len(got["node_ids"]) == 60
            assert len(got["edge_ids"]) == 600
        finally:
            backend.close()


class TestTheRoutingItselfIsCovered:
    """The equivalence fuzz calls `backend.traverse` directly, so everything
    `GraphStorage` does on the way there - coercing the type filter, deciding
    whether to ask at all, coping with a store that raises - was reachable only
    through two tests that used neither a filter nor a broken store. A mutation
    round found each of these survivable: the code was right, and nothing would
    have noticed if it stopped being.
    """

    def test_a_type_filter_survives_the_trip_to_the_store(self, schema):
        """`str()` of a str-Enum is "RelationshipType.RELATES_TO", not
        "RELATES_TO", so a filter built with it matches nothing and the
        traversal returns the anchor alone. The store path coerces with
        `.value`; without this test nothing exercised a filter through
        `get_related_nodes` at all.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
                    Node(id="c", type=NodeType.ACTOR, name="c"),
                ],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    ),
                    Edge(
                        id="ac",
                        source="a",
                        target="c",
                        type=RelationshipType.PART_OF,
                    ),
                ],
            )
            storage.flush()
            assert storage._store_traversal_is_current()

            result = storage.get_related_nodes(
                "a", relationship_types=[RelationshipType.RELATES_TO], depth=1
            )
            assert {n.id for n in result["nodes"]} == {"a", "b"}, (
                "the filter either matched nothing (anchor alone) or was not "
                f"applied (c present): {sorted(n.id for n in result['nodes'])}"
            )
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()

    def test_the_arguments_survive_the_trip_to_the_store(self, schema):
        """`depth` and `include_archived` are passed through to the backend
        and, until this test, were never exercised AS arguments: every
        store-path test used depth 1 or 2 on a graph at most two hops deep,
        and include_archived=False. A mutation round walked straight through
        both - `depth + 1` on a three-node chain, and hardcoding
        include_archived=False in the store call - each producing an answer
        the walk does not give, with the suite green.

        Compared against the walk directly rather than against a literal, so
        this is the same G1 question the fuzz asks, asked one layer up.
        """
        from backend.core import storage_search
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
                    Node(id="c", type=NodeType.ACTOR, name="c"),
                    Node(id="d", type=NodeType.ACTOR, name="d", archived=True),
                ],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    ),
                    Edge(
                        id="bc",
                        source="b",
                        target="c",
                        type=RelationshipType.RELATES_TO,
                    ),
                    Edge(
                        id="cd",
                        source="c",
                        target="d",
                        type=RelationshipType.RELATES_TO,
                    ),
                ],
            )
            storage.flush()
            assert storage._store_traversal_is_current()

            for depth in (1, 2, 3, 4):
                for include_archived in (False, True):
                    routed = storage.get_related_nodes(
                        "a", depth=depth, include_archived=include_archived
                    )
                    walked = storage_search.get_related_nodes(
                        storage.nodes,
                        storage.edges,
                        storage.graph,
                        "a",
                        None,
                        depth,
                        include_archived=include_archived,
                    )
                    assert {n.id for n in routed["nodes"]} == {
                        n.id for n in walked["nodes"]
                    }, (
                        f"depth={depth} include_archived={include_archived}: "
                        f"nodes {sorted(n.id for n in routed['nodes'])} vs "
                        f"walk {sorted(n.id for n in walked['nodes'])}"
                    )
                    assert {e.id for e in routed["edges"]} == {
                        e.id for e in walked["edges"]
                    }, (
                        f"depth={depth} include_archived={include_archived}: "
                        f"edges {sorted(e.id for e in routed['edges'])} vs "
                        f"walk {sorted(e.id for e in walked['edges'])}"
                    )
        finally:
            storage.flush()
            backend.close()

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(RuntimeError("the store is having a day"), id="runtime"),
            pytest.param(
                psycopg.OperationalError("connection dropped"), id="connection"
            ),
            pytest.param(
                psycopg.errors.QueryCanceled("statement timeout"), id="timeout"
            ),
            pytest.param(PoolTimeout("no connection available"), id="pool"),
        ],
    )
    def test_a_store_that_raises_is_answered_by_the_walk(self, schema, failure):
        """A store that cannot answer is not a failed request. Nothing made
        `traverse` fail before, so neither half of this was covered: letting
        the exception out, and swallowing it into an empty answer, both
        passed.

        Parametrised over what a store actually raises, not over a stand-in.
        With only `RuntimeError` here, narrowing the handler to `except
        RuntimeError` passed the whole suite - and a dropped connection, a
        statement timeout (which this file deliberately configures elsewhere)
        or an exhausted pool would then leave the fallback and come out of the
        API as a 500.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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

            def _explode(*args, **kwargs):
                raise failure

            backend.traverse = _explode
            result = storage.get_related_nodes("a", depth=1)
            assert {n.id for n in result["nodes"]} == {"a", "b"}, (
                "the walk answers when the store cannot; got "
                f"{sorted(n.id for n in result['nodes'])}"
            )
            assert {e.id for e in result["edges"]} == {"ab"}
        finally:
            storage.flush()
            backend.close()

    def test_a_backend_that_cannot_traverse_is_never_asked(self):
        """Not merely "the answer is the same either way": without the
        capability check every traversal on a backend that does not declare it
        raises inside the try, prints a warning and walks. Same answer, an
        exception and a log line per call.

        Assembled rather than constructed, so this touches no disk: a real
        `GraphStorage()` would build the default file backend in the working
        directory and answer for whatever graph and journal happen to be
        sitting there.
        """
        from backend.core.storage import GraphStorage
        from backend.core.storage_backends import BackendCapabilities

        storage = GraphStorage.__new__(GraphStorage)
        storage._backend_capabilities = BackendCapabilities(
            incremental_writes=True, transactions=True
        )
        storage._resync_pending = False
        storage._last_write = None

        assert storage._store_traversal_is_current() is False, (
            "a backend that has not declared store_traversal must not be "
            "asked to traverse"
        )

    def test_a_failed_write_closes_the_guard_even_though_the_future_is_done(
        self, schema
    ):
        """A write that raised leaves the Future `done()` while the store is
        genuinely behind, so `done()` alone is not enough - the resync flag is
        the only thing holding the freshness guarantee in that window.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [Node(id="a", type=NodeType.ACTOR, name="a")],
                [],
            )
            storage.flush()

            def _refuse(*args, **kwargs):
                raise RuntimeError("write refused")

            backend.apply_batch = _refuse
            storage.add_nodes(
                [Node(id="b", type=NodeType.ACTOR, name="b")],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    )
                ],
            )
            write = storage._last_write
            if write is not None:
                # Drain it; the exception is the point, not a problem here.
                try:
                    write.result()
                except Exception:
                    pass
                assert write.done()

            assert storage._resync_pending, (
                "the failed write should have flagged the store as owed the whole graph"
            )
            assert storage._store_traversal_is_current() is False, (
                "the Future is done and the store is still behind; only the "
                "resync flag can tell these apart"
            )
            result = storage.get_related_nodes("a", depth=1)
            assert {n.id for n in result["nodes"]} == {"a", "b"}, (
                "the walk still holds the write the store never got; got "
                f"{sorted(n.id for n in result['nodes'])}"
            )
        finally:
            backend.apply_batch = None
            backend.close()


class TestTheGuardCoversEveryRouteAWriteCanTake:
    """The first version watched only `_persist`'s incremental branch, and
    `_persist` itself falls back to `save()` in four documented cases - so the
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

    def test_an_edge_archived_after_the_store_answered_is_not_returned(self, schema):
        """The node half of this was covered from the start and the edge half
        was not, which a mutation round found: the edge check could be deleted
        with the suite green, and an archived edge plus the node behind it came
        back under include_archived=False.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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

            storage.edges["ab"].archived = True

            result = storage.get_related_nodes("a", depth=1)
            assert {e.id for e in result["edges"]} == set(), (
                "returned an edge whose own payload says archived=True under "
                f"include_archived=False: {[e.id for e in result['edges']]}"
            )
            assert {n.id for n in result["nodes"]} == {"a"}, (
                "and b was reachable only across that edge; got "
                f"{sorted(n.id for n in result['nodes'])}"
            )
            assert all(isinstance(e, Edge) for e in result["edges"]), (
                "edges resolved out of the wrong dictionary"
            )
        finally:
            storage.flush()
            backend.close()

    def test_the_answer_does_not_depend_on_the_order_the_store_returns(self, schema):
        """The protocol says order is not part of the contract, and nothing
        held that sentence to anything: `PostgresGraphPersistenceBackend`
        always seeds `node_ids` with the anchor, so every id-vs-position
        question in this layer has the same answer for the only implementation
        in the tree. A second backend ordering its answer differently would
        find out the hard way - keying the anchor exemption on
        `node_ids[0]` rather than on the anchor id passes the whole suite and
        returns an archived node under include_archived=False.
        """
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

            # Archived after the store decided, so the answer has to be the
            # walk's - and the walk's answer does not depend on any order.
            storage.nodes["c"].archived = True

            straight = backend.traverse

            def _reversed(*args, **kwargs):
                got = straight(*args, **kwargs)
                return {
                    "node_ids": list(reversed(got["node_ids"])),
                    "edge_ids": list(reversed(got["edge_ids"])),
                }

            backend.traverse = _reversed
            try:
                result = storage.get_related_nodes("a", depth=1)
            finally:
                backend.traverse = straight

            assert {n.id for n in result["nodes"]} == {"a"}, (
                "an archived node came back because the anchor was recognised "
                "by its position rather than by its id: "
                f"{sorted(n.id for n in result['nodes'])}"
            )
            assert {e.id for e in result["edges"]} == set()
        finally:
            storage.flush()
            backend.close()

    def test_the_edge_check_does_not_borrow_the_anchor_exemption(self, schema):
        """The node rule exempts the anchor: an archived anchor is still
        returned. Edges have no such rule - an archived edge is dropped
        whatever it is called - but because ids are separate namespaces, an
        edge can share the anchor's id, and an edge check written by analogy
        with the node one would exempt it. Every other test here names its
        edge `ab` against an anchor `a`, so none of them can tell.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
                ],
                [
                    # The edge is called "a", like the anchor node.
                    Edge(
                        id="a",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    )
                ],
            )
            storage.flush()

            storage.edges["a"].archived = True

            result = storage.get_related_nodes("a", depth=1)
            assert {e.id for e in result["edges"]} == set(), (
                "an archived edge was exempted for sharing the anchor's id: "
                f"{[e.id for e in result['edges']]}"
            )
            assert {n.id for n in result["nodes"]} == {"a"}, (
                f"got {sorted(n.id for n in result['nodes'])}"
            )
            assert all(isinstance(e, Edge) for e in result["edges"])
        finally:
            storage.flush()
            backend.close()

    def test_a_node_deleted_after_the_store_answered_does_not_crash(self, schema):
        """Archiving is the case the comment names; deleting is the other one,
        and it is the one that used to raise KeyError out of the API rather
        than fall back - the branch resolved the ids in a second pass over
        dictionaries it had already checked.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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

            del storage.nodes["b"]

            result = storage.get_related_nodes("a", depth=1)
            assert {n.id for n in result["nodes"]} == {"a"}, (
                f"got {sorted(n.id for n in result['nodes'])}"
            )
        finally:
            storage.flush()
            backend.close()

    def test_a_delete_between_the_check_and_the_lookup_does_not_raise(self, schema):
        """The delete-based test above removes the node BEFORE the call, which
        a check-then-use survives: the membership test already fails. What
        that shape cannot reach is the window the comment is actually about -
        a delete landing between the `in` and the `[]`, which this path is
        exposed to because it takes no lock while every mutator holds one.
        A dict whose membership test is true and whose lookup then misses is
        that window, made deterministic.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
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

            storage.nodes = _VanishingOnLookup(storage.nodes, victim="b")
            # Must not raise. The answer may come from either engine - what
            # this pins is that a traversal does not turn into a KeyError out
            # of the API.
            result = storage.get_related_nodes("a", depth=1)
            assert "a" in {n.id for n in result["nodes"]}
        finally:
            backend.close()

    def test_what_was_reachable_only_through_it_goes_too(self, schema):
        """Dropping the archived node on its own is not enough. Whatever was
        behind it was reachable only through it, and returning that leaves a
        node the walk would never have reached - the same "state neither
        engine held" the drop was meant to avoid. Answering from the walk is
        the only self-consistent way out; recomputing reachability here would
        be reimplementing it beside itself.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
        from backend.core.storage import GraphStorage

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="a"),
                    Node(id="b", type=NodeType.ACTOR, name="b"),
                    Node(id="z", type=NodeType.ACTOR, name="z"),
                ],
                [
                    Edge(
                        id="ab",
                        source="a",
                        target="b",
                        type=RelationshipType.RELATES_TO,
                    ),
                    Edge(
                        id="bz",
                        source="b",
                        target="z",
                        type=RelationshipType.RELATES_TO,
                    ),
                ],
            )
            storage.flush()

            # a -> b -> z, and b is archived after the store decided.
            storage.nodes["b"].archived = True

            result = storage.get_related_nodes("a", depth=2)
            returned = {n.id for n in result["nodes"]}
            assert returned == {"a"}, (
                "z was reachable only through the archived b, so the walk "
                f"returns the anchor alone; got {returned}"
            )
            assert {e.id for e in result["edges"]} == set()
        finally:
            storage.flush()
            backend.close()


class TestTheGuardReadsItsSignalsInASafeOrder:
    def test_done_is_read_before_the_resync_flag(self):
        """_do_apply sets _resync_pending and THEN raises, so the flag is set
        before the Future finishes. Reading the flag first admits: flag False
        -> the worker sets it and completes -> done() True -> the guard calls
        a store it has just been told is stale. Reading done() first makes
        that impossible, because done() implies the flag write happened.
        """
        from backend.core.storage import GraphStorage
        from backend.core.storage_backends import BackendCapabilities

        storage = GraphStorage.__new__(GraphStorage)
        storage._backend_capabilities = BackendCapabilities(store_traversal=True)

        reads: list = []

        class _Flag:
            def __bool__(self):
                reads.append("flag")
                return False

        class _Write:
            def done(self):
                reads.append("done")
                return True

        storage._resync_pending = _Flag()
        storage._last_write = _Write()

        assert storage._store_traversal_is_current() is True
        assert reads == ["done", "flag"], (
            f"the guard read its signals in the order {reads}; reading the "
            f"flag before done() leaves the stale-store window open"
        )


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

        class _AlmostLiar:
            """Worse than the one above, because `hasattr` is satisfied. A
            declaration checked with `hasattr` instead of `callable` accepts
            this and then warns-and-walks for the life of the process, which
            is the failure the check exists to prevent."""

            traverse = None

            def capabilities(self):
                return BackendCapabilities(store_traversal=True)

        with pytest.raises(TypeError, match="store_traversal"):
            capabilities_of(_AlmostLiar())

        with pytest.raises(TypeError, match="store_traversal"):
            capabilities_of(_Liar())


class TestDepthIsBoundedByTheGraphNotByTheCaller:
    """REST caps depth at 5; `mcp_tools.get_related_nodes` caps it nowhere, so
    what a caller asks for is unbounded and the store has to stop on its own.
    It stops by converging: a level that reaches nothing new ends the walk.
    """

    def test_the_bound_counts_edges_because_a_path_runs_through_non_nodes(self, schema):
        """A traversal steps THROUGH an id that is not a node - the protocol's
        dangling-endpoint rule says the edge is returned and the missing id is
        not, and the walk adds that id to its frontier all the same. So a path
        can be longer than there are nodes, and a bound taken from the node
        count truncates it. Here: two nodes, joined by a three-hop chain of
        ids that are not nodes.

        The statement count is asserted here and not only in the huge-depth
        test, because that test cannot see this: its graph is dense and every
        endpoint is a real node, so an implementation that records only NODE
        ids as seen converges there and looks correct. Give it a dangling pair
        and the frontier oscillates between them forever - identical answer,
        one round trip per level the caller asked for. Measured on this graph
        with that edit: 4 levels became 400 at depth 400.
        """
        import psycopg

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        nodes = {n: Node(id=n, type=NodeType.ACTOR, name=n) for n in ("anchor", "far")}
        edges = {
            "e1": Edge(
                id="e1",
                source="anchor",
                target="ghost1",
                type=RelationshipType.RELATES_TO,
            ),
            "e2": Edge(
                id="e2",
                source="ghost1",
                target="ghost2",
                type=RelationshipType.RELATES_TO,
            ),
            "e3": Edge(
                id="e3",
                source="ghost2",
                target="far",
                type=RelationshipType.RELATES_TO,
            ),
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            _load(backend, nodes, edges)
            original = psycopg.Connection.execute
            counted = []

            def _counting(self, query, *args, **kwargs):
                text = str(query)
                # The graph-identity guard may run once at the start of the
                # first traversal in a fresh backend. These tests measure
                # traversal convergence only, so ignore that guard query.
                if "graph_metadata" not in text:
                    counted.append(query)
                return original(self, query, *args, **kwargs)

            # Every depth from the exact one to far beyond it: a bound taken
            # from the node count (2) would cut the path short at each.
            for depth in (3, 4, 100, 5_000):
                psycopg.Connection.execute = _counting
                try:
                    counted.clear()
                    got = backend.traverse("anchor", depth)
                    issued = len(counted)
                finally:
                    psycopg.Connection.execute = original
                # One SET ISOLATION LEVEL, one anchor check, then a level per
                # hop, plus the one that expands the far node and finds
                # nothing new. Four levels is where this graph stops, so every
                # depth beyond it costs the same six statements.
                assert issued == min(depth, 4) + 2, (
                    f"depth {depth}: this graph is crossed in four levels, so "
                    f"it costs {min(depth, 4) + 2} statements however deep the "
                    f"caller asks; issued {issued}"
                )
                assert "far" in got["node_ids"], (
                    f"depth {depth}: the far node is 3 hops away through two "
                    f"ids that are not nodes; something bounded the walk by "
                    f"the node count. Got {sorted(got['node_ids'])}"
                )
                assert set(got["edge_ids"]) == {"e1", "e2", "e3"}
        finally:
            backend.close()

    def test_a_huge_depth_costs_no_more_than_the_graph_allows(self, schema):
        """Counted, not timed. A wall-clock budget is the obvious way to write
        this and a bad one: it measures the server rather than the property,
        it is the first thing to go flaky on a loaded runner, and a budget
        loose enough not to be flaky is loose enough to miss the regression -
        an earlier version of this test allowed 3 s at depth 1000, where
        removing the convergence check costs 0.15 s and only becomes visible
        at depths the test does not use. Meanwhile `mcp_tools` caps depth
        nowhere, so the depth a caller can ask for is unbounded.

        The property is that the number of levels walked is the graph's, not
        the caller's, and that is exactly the number of queries issued. On a
        500-node / 5000-edge graph, complete at depth 5, asking for 1000
        levels must cost the same queries as asking for 5.
        """
        import random

        import psycopg

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        rng = random.Random(11)
        nodes = {
            f"n{i}": Node(id=f"n{i}", type=NodeType.ACTOR, name=f"n{i}")
            for i in range(500)
        }
        edges = {
            f"e{k}": Edge(
                id=f"e{k}",
                source=f"n{rng.randrange(500)}",
                target=f"n{rng.randrange(500)}",
                type=RelationshipType.RELATES_TO,
            )
            for k in range(5_000)
        }
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        original = psycopg.Connection.execute
        counted = []

        def _counting(self, query, *args, **kwargs):
            text = str(query)
            # The graph-identity guard may run once at the start of the first
            # traversal in a fresh backend. These tests measure traversal
            # convergence only, so ignore that guard query.
            if "graph_metadata" not in text:
                counted.append(query)
            return original(self, query, *args, **kwargs)

        try:
            _load(backend, nodes, edges)

            psycopg.Connection.execute = _counting
            try:
                counted.clear()
                shallow = backend.traverse("n0", 5)
                shallow_queries = len(counted)
                counted.clear()
                deep = backend.traverse("n0", 1_000)
                deep_queries = len(counted)
            finally:
                psycopg.Connection.execute = original

            assert deep_queries == shallow_queries, (
                f"depth 1000 issued {deep_queries} statements where depth 5 "
                f"issued {shallow_queries}, on a graph that answers completely "
                f"at depth 5; the walk is running the caller's number of "
                f"levels rather than the graph's"
            )
            # Absolute as well as relative. Equality alone passes for any
            # constant multiple per level - re-querying the anchor at every
            # level took this from 6 statements to 10 and the assertion above
            # did not move. One SET ISOLATION LEVEL, one anchor pre-check,
            # then one query per level crossed, and this graph is crossed in
            # four.
            assert shallow_queries == 6, (
                f"a depth-5 traversal of a graph 4 levels across should be one "
                f"isolation statement, one anchor check and four levels; "
                f"issued {shallow_queries}"
            )
            # and the answer is the same one the shallow traversal gave
            assert set(deep["node_ids"]) == set(shallow["node_ids"])
            assert set(deep["edge_ids"]) == set(shallow["edge_ids"])
        finally:
            psycopg.Connection.execute = original
            backend.close()


class TestTheWalkResolvesEachIdOnce:
    """The walk is also the fallback, and it reads `nodes` and `edges` without
    the lock every mutator holds. Resolving a result with a membership test and
    then an index observes the dict twice; a delete landing between the two
    raised KeyError out of the traversal. These drive the walk directly, so the
    store cannot answer in its place and hide the window.
    """

    @staticmethod
    def _graph(nodes, edges):
        import networkx as nx

        graph = nx.MultiDiGraph()
        for node in nodes.values():
            graph.add_node(node.id, data=node)
        for edge in edges.values():
            graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)
        return graph

    @staticmethod
    def _fixture():
        nodes = {
            "a": Node(id="a", type=NodeType.ACTOR, name="a"),
            "b": Node(id="b", type=NodeType.ACTOR, name="b"),
        }
        edges = {
            "ab": Edge(
                id="ab", source="a", target="b", type=RelationshipType.RELATES_TO
            )
        }
        return nodes, edges

    def test_a_node_deleted_between_check_and_lookup_does_not_raise(self):
        nodes, edges = self._fixture()
        graph = self._graph(nodes, edges)
        vanishing = _VanishingOnLookup(nodes, victim="b")

        result = storage_search.get_related_nodes(vanishing, edges, graph, "a")

        assert {n.id for n in result["nodes"]} == {"a", "b"}
        assert {e.id for e in result["edges"]} == {"ab"}

    def test_an_edge_deleted_between_check_and_lookup_does_not_raise(self):
        nodes, edges = self._fixture()
        graph = self._graph(nodes, edges)
        vanishing = _VanishingOnLookup(edges, victim="ab")

        result = storage_search.get_related_nodes(nodes, vanishing, graph, "a")

        assert {n.id for n in result["nodes"]} == {"a", "b"}
        assert {e.id for e in result["edges"]} == {"ab"}

    def test_an_id_that_is_already_gone_is_dropped_not_returned_as_none(self):
        nodes, edges = self._fixture()
        graph = self._graph(nodes, edges)
        del nodes["b"]
        del edges["ab"]

        result = storage_search.get_related_nodes(nodes, edges, graph, "a")

        assert [n.id for n in result["nodes"]] == ["a"]
        assert result["edges"] == []
