"""Two application instances writing one graph, which is the point of all of it.

The parent task's acceptance criterion, as an executable test rather than an
argument: two `GraphStorage` instances share one PostgreSQL store, write at the
same time, and neither one's work is lost.

The contract already drives two `GraphStorage` instances against one real
store - `test_two_storages_writing_one_store_do_not_wait_on_each_other`. What
it does not do is assert what they ended up holding, and it says so: it calls
`save()` on every iteration, so it exercises the WHOLE-GRAPH path, where two
writers overwrite each other by design, and its docstring states that content
is deliberately not asserted and only liveness is.

The entity path is what this work exists to provide, and on it content is
exactly what has to survive. That is what this module adds, and it is why
nothing here calls `save()`.

Needs a real server, for the same reason the backend's own tests do: what is
being asserted is the server's concurrency, and a fake would be asserting that
the fake has it.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import timedelta

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

    def test_a_rename_that_arrived_by_report_is_searchable_by_its_new_name(
        self, instances, schema
    ):
        """Convergence of the node dictionary is not convergence of the
        instance: lexical search reads a per-node cache.

        A RENAME, not a create, and that is the whole point. A cache MISS
        heals itself - `search_nodes` rebuilds the entry and finds the node
        anyway - so a create can never fail this way, and a test built on one
        would pass against a refresh that never touched the cache at all.
        Measured: popping the entry by hand changes nothing. A stale HIT does
        not heal. The old text stays matchable and the new text does not, and
        the instance answers a search with a name the node no longer has.
        """
        one, two = instances(), instances()
        one.add_nodes([_node("n", name="Alpha")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("n") is not None,
            "the second instance learned of the node",
        )

        two.update_node("n", {"name": "Renamed"})
        _drain(two)
        _assert_nothing_failed(one, two)

        _wait_until(
            lambda: one.get_node("n").name == "Renamed",
            "the rename reached the other instance",
        )
        assert [n.id for n in one.search_nodes("Renamed")] == ["n"]
        assert one.search_nodes("Alpha") == [], (
            "the old name still matches at the instance that was told about "
            "the rename: its searchable-text cache kept the stale entry"
        )


class TestTwoInstancesWritingTheSameEntity:
    """The same entity is the case the class above does not answer, and the
    one that decided how the seam arbitrates at all.

    What settles it is the store, not either instance's clock. An instance
    reads the contested entity back from the store AFTER its own queued writes
    have committed, and takes what it finds - so whichever write the store
    committed last is what every instance ends on.

    It did not start there. The report used to carry content gathered when the
    announcement was dispatched, which is before the receiving instance's own
    queued writes have landed, and the instance defended itself against that
    with `updated_at`: keep whichever version is stamped later. The stamp is
    taken in memory under the lock and the write commits asynchronously
    afterwards, so commit order is not stamp order - and when the write that
    committed LAST carried the EARLIER stamp, the instance holding the later
    stamp refused the store's value and went on refusing it. There is no
    further change to report, so the disagreement was permanent, and that
    instance served a value nothing else held. Measured before the fix, on an
    unloaded machine: 4 of 8 raced runs ended divergent, in both directions,
    with no failed write on either side. Under load it did not reproduce at
    all - which is why the two cases that pin the fix below are constructed
    rather than raced, and the raced one is here for what it is worth and not
    as the guard.
    """

    def test_a_delivered_write_is_superseded_by_a_later_one(self, instances, schema):
        """Sequenced, which is the easy case: the second write is made after
        the first has been delivered, so its stamp and its commit fall in the
        same order and nothing has to arbitrate."""
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

    def test_a_value_the_store_took_last_wins_over_a_later_stamp(
        self, instances, schema
    ):
        """The regression guard, and deliberately not a race.

        The interleaving that used to diverge is constructed here instead of
        hoped for: an instance holds a stamp, and the store then takes a value
        stamped EARLIER, exactly as it does when the write carrying the older
        stamp is the one that commits last. Raced, this happened about half
        the time on an idle machine and not at all under load, so a race would
        pass on a CI runner for the wrong reason. Constructed, it fails
        wherever the instance arbitrates by clock and passes wherever it takes
        the store's answer.
        """
        one, two = instances(), instances()
        one.add_nodes([_node("contested", name="Origin")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("contested") is not None,
            "the second instance learned of the node",
        )

        two.update_node("contested", {"name": "Stamped later"})
        _drain(two)
        _wait_until(
            lambda: one.get_node("contested").name == "Stamped later",
            "both instances hold the later stamp",
        )
        held = two.get_node("contested").updated_at

        # A third writer stands in for the commit that lands last carrying the
        # earlier stamp. Going through a backend rather than a GraphStorage is
        # what makes the stamp settable at all: GraphStorage stamps its own,
        # and stamping it is the very thing under test.
        stale = one.get_node("contested").to_dict()
        stale["name"] = "Committed last"
        stale["updated_at"] = (held - timedelta(seconds=60)).isoformat()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            writer.upsert_node(stale)
        finally:
            writer.close()

        for label, storage in (("one", one), ("two", two)):
            _wait_until(
                lambda s=storage: s.get_node("contested").name == "Committed last",
                f"instance {label} took the value the store committed last, "
                f"rather than keeping its own later stamp",
            )
        _assert_nothing_failed(one, two)

    def test_the_content_is_read_after_this_instance_has_settled(
        self, instances, schema
    ):
        """Why the guard above holds: the read is made late, not early.

        An instance that read the contested entity when the announcement
        arrived would read it without its own queued writes in it, and would
        then be arbitrating between two values with nothing to order them by.
        So the read is asked for by the application, once it has drained its
        own queue - and this pins that ordering directly, because a refactor
        could move the read back to dispatch and still satisfy the case above,
        which has nothing queued.
        """
        one, two = instances(), instances()
        one.add_nodes([_node("contested", name="Origin")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("contested") is not None,
            "the second instance learned of the node",
        )

        backend = one._persistence_backend
        real_resolve = type(backend)._resolve
        seen_at_read = []
        slow = threading.Event()

        def watch(self, pairs):
            operations = real_resolve(self, pairs)
            seen_at_read.append(
                [op.payload.get("name") for op in operations if op.kind == "node"]
            )
            return operations

        backend._resolve = watch.__get__(backend)

        real_upsert = type(backend).upsert_node
        released = []

        def stall(self, node):
            # Held until the test releases it, so the peer's announcement is
            # in hand while this instance's own write is still queued - which
            # is the whole interleaving. A timeout here would let the write
            # through early and fail the assertion below for a reason that has
            # nothing to do with the property, so whether the wait was
            # released or expired is recorded and checked.
            released.append(slow.wait(CONVERGE_TIMEOUT))
            return real_upsert(self, node)

        backend.upsert_node = stall.__get__(backend)
        one.update_node("contested", {"name": "Mine"})
        two.update_node("contested", {"name": "Theirs"})
        _drain(two)
        slow.set()
        _drain(one)
        _assert_nothing_failed(one, two)
        assert released == [True], (
            "the stalled write was released by its own timeout rather than by "
            "the test, so the interleaving this case is about did not happen"
        )

        _wait_until(
            lambda: seen_at_read and seen_at_read[-1] == ["Mine"],
            "the content was read with this instance's own write already in "
            f"the store; reads saw {seen_at_read}",
        )

    def test_reading_back_this_instances_own_value_changes_nothing(
        self, instances, schema
    ):
        """Reading after the settle means the answer is often our own write,
        and applying that is not free: the event would tell every subscriber a
        node changed when nothing about it did, and settling the vector would
        evict a description to regenerate the identical one. So an answer that
        agrees with what is held is applied as nothing at all.

        Constructed by writing the held value back to the store verbatim,
        which is the same thing the interleaving above produces and is
        deterministic."""
        one, two = instances(), instances()
        one.add_nodes(
            [Node(id="c", type=NodeType.ACTOR, name="Mine", embedding=[0.5, 0.25])], []
        )
        _drain(one)
        _wait_until(
            lambda: two.get_node("c") is not None,
            "the second instance learned of the node",
        )
        vector = one.vector_store.get_vector_list("c")
        assert vector is not None, "no vector to keep, so nothing to assert"

        emitted = []
        real_emit = type(one)._emit_event

        def record(self, **kwargs):
            if self is one:
                emitted.append(kwargs.get("entity_id"))
            return real_emit(self, **kwargs)

        GraphStorage._emit_event = record
        try:
            reader = PostgresGraphPersistenceBackend(DSN, schema=schema)
            try:
                doc = {n["id"]: n for n in reader.load_graph_data()["nodes"]}["c"]
                reader.upsert_node(doc)
                # The assertion is an ABSENCE, so it needs a barrier rather
                # than a wait: a second write, announced on the same channel
                # after the first, whose arrival proves the first was already
                # dealt with. A bounded sleep would only prove the machine was
                # slow.
                reader.upsert_node(_node("sentinel").to_dict())
            finally:
                reader.close()
            _wait_until(
                lambda: one.get_node("sentinel") is not None,
                "the report after the identical one arrived",
            )
        finally:
            GraphStorage._emit_event = real_emit

        assert emitted == ["sentinel"], (
            f"an answer identical to what this instance holds was applied "
            f"anyway; the only event should be the barrier's, got {emitted}"
        )
        assert one.vector_store.get_vector_list("c") == vector
        assert one.get_node("c").name == "Mine"

    def test_a_raced_write_leaves_every_party_on_the_same_value(
        self, instances, schema
    ):
        """Raced rather than constructed, and so it is the weaker of the two:
        it passed before the fix about half the time on an idle machine and
        every time under load. It is here because the constructed cases fix
        one interleaving each and this one fixes none - if the arbitration is
        wrong in a way neither of them named, this is what notices."""
        one, two = instances(), instances()
        one.add_nodes([_node("contested", name="Origin")], [])
        _drain(one)
        _wait_until(
            lambda: two.get_node("contested") is not None,
            "the second instance learned of the node",
        )

        errors = []
        written = {"Origin"}

        def rename(storage, tag):
            try:
                for i in range(5):
                    name = f"{tag}{i}"
                    written.add(name)
                    storage.update_node("contested", {"name": name})
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

        def stored_name():
            reader = PostgresGraphPersistenceBackend(DSN, schema=schema)
            try:
                rows = {n["id"]: n for n in reader.load_graph_data()["nodes"]}
            finally:
                reader.close()
            return rows["contested"]["name"]

        _wait_until(
            lambda: (
                one.get_node("contested").name
                == two.get_node("contested").name
                == stored_name()
            ),
            "the store and both instances settled on one value; "
            "one holds {!r}, two holds {!r}".format(
                one.get_node("contested").name, two.get_node("contested").name
            ),
        )
        assert stored_name() in written, (
            f"the store holds {stored_name()!r}, which neither instance wrote"
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
