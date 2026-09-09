"""Two application instances writing one graph, which is the point of all of it.

The parent task's acceptance criterion, as an executable test rather than an
argument: two `GraphStorage` instances share one PostgreSQL store, write at the
same time, and neither one's work is lost.

This is deliberately the last slice, because it is the first thing that runs
the whole stack against itself. The backend's own contract tests drive the
backend; the seam's tests drive `GraphStorage` against a reference backend.
Only this drives a real application on a real store beside another one.

Why it is not an extension of the contract's existing two-writer clause: that
one calls `save()` on every iteration, so it exercises the WHOLE-GRAPH path,
where two writers overwrite each other by design - which is why its docstring
says content is not asserted and only liveness is. The entity path is the one
this work exists to provide, and on it content is exactly what must survive.
So nothing here calls `save()`.

Needs a real server, for the same reason the backend's own tests do: what is
being asserted is the server's concurrency, and a fake would be asserting that
the fake has it.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from backend.core.models import Edge, Node, NodeType, RelationshipType

REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES") == "1"

if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    DEFAULT_POOL_SIZE,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage import GraphStorage  # noqa: E402

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

CONVERGE_TIMEOUT = 60.0


@pytest.fixture
def schema():
    """A private schema per test, dropped afterwards."""
    name = f"co_mi_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def instances(schema):
    """Application instances on one store, torn down however the test ends.

    Torn down in the fixture rather than in each test because
    `shutdown_events()` is not a neutral act on a shared store: it heals a
    failed write by re-issuing this instance's whole graph, which would
    overwrite the other one. Every assertion here therefore runs while both
    instances are still up, and the teardown happens after.
    """
    built = []

    def start(pool_size=DEFAULT_POOL_SIZE):
        backend = PostgresGraphPersistenceBackend(
            DSN, schema=schema, pool_size=pool_size
        )
        storage = GraphStorage(persistence_backend=backend)
        built.append((storage, backend))
        return storage

    yield start
    for storage, backend in built:
        try:
            storage.shutdown_events()
        finally:
            backend.close()


def _node(node_id, name=None):
    return Node(id=node_id, type=NodeType.ACTOR, name=name or node_id)


def _drain(storage):
    """Wait for this instance's own queued writes, and nothing else.

    Not `flush()`: that also heals and checkpoints, and healing is a
    whole-graph write. On a shared store, waiting for your own work must not
    become re-asserting your whole image over someone else's.
    """
    storage._io_executor.submit(lambda: None).result(timeout=CONVERGE_TIMEOUT)


def _assert_nothing_failed(*storages):
    """No write failed on any of them.

    The reason this is not paranoia: `add_nodes` discards the future its write
    returns and swallows the exception with a print, so a failed write is
    invisible to the caller. It raises `_resync_pending`, and from that moment
    the instance silently drops every external report it is sent - so a
    convergence assertion could pass vacuously, or a lost write could be
    laundered into a whole-graph overwrite at the next flush. Read the flag
    before believing anything else in this file.
    """
    for i, storage in enumerate(storages):
        assert not storage._resync_pending, (
            f"instance {i} has a write that failed and is only in memory; it "
            f"has been dropping external reports since, so nothing below "
            f"means what it says"
        )


def _wait_until(predicate, what, timeout=CONVERGE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _ids(storage):
    return {n.id for n in storage.get_all_nodes()}


def _store_ids(schema):
    """What the store itself holds, read by neither instance.

    Two instances agreeing is not the whole property: they could agree on a
    graph a whole-graph heal had flattened. The store is the third opinion.
    """
    backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
    try:
        return {n["id"] for n in backend.load_graph_data()["nodes"]}
    finally:
        backend.close()


class TestTwoInstancesWritingDistinctEntities:
    """The parent task's acceptance criterion in one sentence: two instances
    write the same graph at the same time and neither one's work is lost.

    Distinct entities, deliberately. Writes to the SAME entity are a different
    property with a different answer - last writer wins by wall clock - and
    the class below says so rather than letting this one imply more than the
    system promises.
    """

    WRITES = 15

    def test_neither_instances_writes_are_lost(self, instances, schema):
        one, two = instances(), instances()
        errors = []

        def write(storage, prefix):
            try:
                for i in range(self.WRITES):
                    storage.add_nodes([_node(f"{prefix}{i:02d}")], [])
            except Exception as exc:  # reported, not raised off-thread
                errors.append(exc)

        threads = [
            threading.Thread(target=write, args=(one, "a"), daemon=True),
            threading.Thread(target=write, args=(two, "b"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        assert not [t for t in threads if t.is_alive()], "a writer never finished"
        assert errors == []
        _drain(one)
        _drain(two)
        _assert_nothing_failed(one, two)

        expected = {f"a{i:02d}" for i in range(self.WRITES)} | {
            f"b{i:02d}" for i in range(self.WRITES)
        }
        assert _store_ids(schema) == expected, (
            "the store lost a write that neither instance reported failing"
        )
        _wait_until(
            lambda: _ids(one) == expected and _ids(two) == expected,
            "both instances converged on every node both of them wrote",
        )
        _assert_nothing_failed(one, two)

    def test_what_each_instance_learns_is_searchable_there(self, instances, schema):
        """Convergence of the node dictionary is not convergence of the
        instance. Lexical search reads a per-node cache built at write time,
        and a node that arrived by report has to be in it - otherwise the
        instance holds the node and cannot find it."""
        one, two = instances(), instances()

        one.add_nodes([_node("mine", name="Alpha")], [])
        two.add_nodes([_node("theirs", name="Beacon")], [])
        _drain(one)
        _drain(two)
        _assert_nothing_failed(one, two)

        _wait_until(
            lambda: _ids(one) == {"mine", "theirs"} == _ids(two),
            "both instances hold both nodes",
        )
        assert [n.id for n in one.search_nodes("Beacon")] == ["theirs"]
        assert [n.id for n in two.search_nodes("Alpha")] == ["mine"]


class TestTwoInstancesWritingTheSameEntity:
    """The same entity is a different property with a weaker answer, and this
    class exists so the class above cannot be read as promising it.

    `GraphStorage` resolves a contested node as last writer wins by
    `updated_at` - a wall clock the two instances share no other ordering
    behind. What holds unconditionally is that they CONVERGE: both end on the
    same value, and it is the one the store holds. Which of the two writes
    wins is the clock's answer, not this system's, and the sequential case
    below is the only one that can name it.
    """

    def test_the_later_write_wins_and_the_earlier_instance_adopts_it(
        self, instances, schema
    ):
        one, two = instances(), instances()
        one.add_nodes([_node("contested", name="First")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("contested") is not None,
            "the second instance learned of the node",
        )

        two.update_node("contested", {"name": "Second"})
        _drain(two)
        _assert_nothing_failed(one, two)

        _wait_until(
            lambda: one.get_node("contested").name == "Second",
            "the instance that wrote first adopted the later write",
        )
        assert two.get_node("contested").name == "Second"

    def test_a_contested_write_leaves_both_instances_agreeing_with_the_store(
        self, instances, schema
    ):
        """Raced rather than sequenced, so no assertion is made about WHICH
        write wins - only that the disagreement ends, and ends on what the
        store holds. An instance that quietly kept its own value would look
        healthy and serve a graph nobody else has."""
        one, two = instances(), instances()
        one.add_nodes([_node("contested", name="Origin")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("contested") is not None,
            "the second instance learned of the node",
        )

        errors = []

        def rename(storage, name):
            try:
                for i in range(5):
                    storage.update_node("contested", {"name": f"{name}{i}"})
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=rename, args=(one, "A"), daemon=True),
            threading.Thread(target=rename, args=(two, "B"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        assert not [t for t in threads if t.is_alive()]
        assert errors == []
        _drain(one)
        _drain(two)
        _assert_nothing_failed(one, two)

        def settled():
            here, there = one.get_node("contested"), two.get_node("contested")
            if here is None or there is None:
                return False
            return here.name == there.name

        _wait_until(settled, "the two instances stopped disagreeing")
        agreed = one.get_node("contested").name
        stored = {
            n["id"]: n
            for n in PostgresGraphPersistenceBackend(
                DSN, schema=schema
            ).load_graph_data()["nodes"]
        }
        assert stored["contested"]["name"] == agreed, (
            "the instances agreed on a value the store does not hold"
        )


class TestADeleteByOneInstanceReachesTheOther:
    """A delete is the case with no payload to arbitrate, so it applies
    whatever the receiving instance last did - and it has to take the edges
    and the vector with it. A node that survives in one structure and not
    another is worse than one that survives in none: a read path walking the
    graph trips over the half that is left.
    """

    def test_the_node_its_edges_and_its_vector_all_go(self, instances, schema):
        one, two = instances(), instances()
        one.add_nodes(
            [
                _node("a"),
                Node(
                    id="b",
                    type=NodeType.ACTOR,
                    name="Beacon",
                    # Supplied rather than generated: without a model there is
                    # no vector to lose, and an assertion that it is gone
                    # would pass against a delete that took nothing.
                    embedding=[0.5, 0.25],
                ),
            ],
            [Edge(id="ab", source="a", target="b", type=RelationshipType.RELATES_TO)],
        )
        _drain(one)
        _wait_until(
            lambda: (
                _ids(two) == {"a", "b"}
                and {e.id for e in two.get_all_edges()} == {"ab"}
            ),
            "the second instance learned of both nodes and the edge",
        )
        # The three things the delete has to take with it must be there first.
        assert one.vector_store.get_vector_list("b") is not None
        assert [n.id for n in one.search_nodes("Beacon")] == ["b"]
        assert {e.id for e in one.get_all_edges()} == {"ab"}

        two.delete_nodes(["b"], confirmed=True)
        _drain(two)
        _assert_nothing_failed(one, two)

        _wait_until(
            lambda: one.get_node("b") is None,
            "the deleting instance's work reached the other one",
        )
        assert one.search_nodes("Beacon") == []
        assert one.vector_store.get_vector_list("b") is None
        assert not one.graph.has_node("b")
        assert one.get_all_edges() == [], (
            "the node went and its edge stayed: a read path walking the graph "
            "trips over the half that is left"
        )
        assert _store_ids(schema) == {"a"}


class TestTheConnectionBudgetIsWhatTheDocumentSays:
    """`instance_count x (pool_size + 1)` is the number an operator sizes
    `max_connections` against, and it was prose.

    The `+ 1` is the listening connection, which cannot go back to a pool and
    still be listening. Getting this wrong is not a slow deployment: the
    instance that cannot get a connection fails to boot, and it fails at the
    instance count the operator was scaling TO rather than the one they tested
    at.
    """

    def _connections(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            return conn.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database()"
            ).fetchone()[0]

    @pytest.mark.parametrize("pool_size", [1, 2])
    def test_an_instance_at_full_stretch_holds_pool_size_plus_one(
        self, pool_size, instances
    ):
        baseline = self._connections()
        storage = instances(pool_size=pool_size)
        # A write opens the pool's connections; without one the pool is at
        # min_size 0 and the count says nothing about the budget.
        storage.add_nodes([_node("a")], [])
        _drain(storage)
        held = []
        pool = storage._persistence_backend._pool
        try:
            for _ in range(pool_size):
                conn = pool.getconn(timeout=30)
                conn.execute("SELECT 1")
                held.append(conn)
            at_full_stretch = self._connections() - baseline
        finally:
            for conn in held:
                pool.putconn(conn)

        assert at_full_stretch == pool_size + 1, (
            f"one instance at pool_size={pool_size} holds {at_full_stretch} "
            f"connections, not the documented {pool_size + 1}"
        )
