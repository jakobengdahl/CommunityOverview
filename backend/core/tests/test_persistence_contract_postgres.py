"""The PostgreSQL backend against the persistence contract.

Needs a real server: the properties this backend exists for - a transaction
that rolls back, an advisory lock that serialises concurrent boots - are the
server's, and a fake would be asserting that the fake has them.

The whole module skips when there is no server, so `pytest backend/ -q` on a
clone with nothing installed stays green. Point `CO_TEST_POSTGRES_DSN` at a
server to run it; CI provides one as a service container.

Each case gets a schema of its own rather than a database of its own, which
is what lets one server serve the whole suite.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from backend.core.tests.persistence_contract import (
    PersistenceBackendContract,
    node_payload,
    snapshot,
)

psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    MIGRATION_LOCK_KEY,
    PostgresGraphPersistenceBackend,
)

DSN = os.environ.get(
    "CO_TEST_POSTGRES_DSN", "host=127.0.0.1 user=postgres dbname=postgres"
)


def _server_reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason=f"no PostgreSQL server reachable at CO_TEST_POSTGRES_DSN ({DSN})",
)


@pytest.fixture
def schema():
    """A private schema per test, dropped afterwards."""
    name = f"co_test_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def backends():
    """Every backend a test made, so the pools are closed even on failure."""
    made = []
    yield made
    for backend in made:
        backend.close()


class TestPostgresBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self, schema, backends):
        def make():
            backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
            backends.append(backend)
            return backend

        return make

    def interrupt_next_snapshot(self, backend, monkeypatch):
        """Fail the save at its last write, inside the open transaction.

        The metadata row is written after the deletes and after the node and
        edge inserts, so failing there is the case worth testing: everything
        the save was going to do has been done and none of it is committed.
        What the contract then checks is that the previous graph is still
        readable, which is the rollback.

        Keyed on the payload rather than on a call count: every node and edge
        carries an `id`, the metadata dict does not. A count would move the
        failure somewhere else the moment the contract changed how many nodes
        it writes here.
        """
        import backend.core.postgres_backend as module

        real = module.psycopg.types.json.Jsonb

        def exploding(value):
            if isinstance(value, dict) and "id" not in value:
                monkeypatch.setattr(module.psycopg.types.json, "Jsonb", real)
                raise OSError("connection lost mid-snapshot")
            return real(value)

        monkeypatch.setattr(module.psycopg.types.json, "Jsonb", exploding)


class TestPostgresSchemaIsSafeToMigrateConcurrently:
    """Autoscaling boots N instances at once, and every one migrates.

    Not a contract clause: it is a property of this backend's own boot, and
    the file backend has no equivalent. It is here because the failure it
    prevents is an instance that will not start, which under a Recreate-style
    rollout is an outage rather than a degraded pod.
    """

    def test_ten_instances_booting_at_once_all_migrate(self, schema, backends):
        failures = []
        barrier = threading.Barrier(10)

        def boot():
            try:
                backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
                backends.append(backend)
                barrier.wait(timeout=30)
                backend.exists()  # first call migrates
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=boot) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        assert failures == [], f"instances failed to boot: {failures}"
        assert not [t for t in threads if t.is_alive()]

    def test_the_migration_holds_the_advisory_lock(self, schema, backends):
        """The lock is what makes the test above pass rather than luck.

        Held by another session, the migration cannot proceed - so a
        migration that completes while the lock is held would be one that
        never took it, and the concurrency test above would be passing on a
        narrow window rather than on the lock.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        blocker = psycopg.connect(DSN, autocommit=False)
        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            done = threading.Event()

            def migrate():
                backend.exists()
                done.set()

            thread = threading.Thread(target=migrate, daemon=True)
            thread.start()
            assert not done.wait(timeout=2), (
                "the migration completed while the lock was held elsewhere, "
                "so it does not take the lock"
            )
            blocker.rollback()  # releases the transaction-scoped lock
            assert done.wait(timeout=30), "the migration did not resume"
        finally:
            blocker.close()

    def test_instances_race_on_the_tables_when_the_schema_already_exists(
        self, schema, backends
    ):
        """The race production actually runs.

        Each test gets a fresh schema name, so a bare ten-way boot races on
        `CREATE SCHEMA` and never reaches the tables. A deployment points at
        a schema that is already there - `public` by default - where the
        contended catalog entry is the table. Pre-creating the schema is what
        puts the documented case under test.
        """
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')

        failures = []
        barrier = threading.Barrier(10)

        def boot():
            try:
                backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
                backends.append(backend)
                barrier.wait(timeout=30)
                backend.exists()
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=boot) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        assert failures == [], f"instances failed to boot: {failures}"

    def test_a_second_thread_waits_for_the_migration_rather_than_the_memo(
        self, schema, backends
    ):
        """One backend object serves many request threads.

        The `_migrated` memo must be set once the tables are actually there,
        not on the way in: set early, a thread taking the lock-free fast path
        would sail past a migration still in progress and query a table that
        does not exist yet. The window is exactly as wide as the migration -
        and the migration blocks on the advisory lock whenever another
        instance is migrating, which is the autoscale boot this guards.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        blocker = psycopg.connect(DSN, autocommit=False)
        errors = []

        def use():
            try:
                backend.exists()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            first = threading.Thread(target=use, daemon=True)
            first.start()
            # Long enough that a memo set on the way in would have released
            # the second thread while the first is still blocked on the lock.
            second = threading.Thread(target=use, daemon=True)
            threading.Event().wait(1.0)
            second.start()
            threading.Event().wait(1.0)
            blocker.rollback()
            first.join(30)
            second.join(30)
        finally:
            blocker.close()

        assert errors == [], (
            f"a thread reached the tables before the migration created them: {errors}"
        )


class TestPostgresConcurrentSavesDoNotMerge:
    """Two whole-graph saves at once must not leave a graph neither wrote.

    They race for last place by nature - that is what a whole-graph write
    is - but the store must end holding one writer's snapshot. Without
    serialisation it does not: the default isolation takes each statement's
    snapshot when the statement starts, so the second writer's DELETE skips
    the rows the first deleted and cannot see the rows it inserted. The
    result is the union of two saves, or a duplicate-key failure on any id
    they share - and sharing ids is what two instances of the same graph do.
    """

    def _stalled_save(self, backend, nodes, released):
        """Save from `backend`, holding its transaction open until released."""
        import backend.core.postgres_backend as module

        real = module.psycopg.types.json.Jsonb
        seen = threading.Event()

        def stalling(value):
            if isinstance(value, dict) and "id" not in value:  # the metadata row
                seen.set()
                released.wait(30)
            return real(value)

        def run():
            module.psycopg.types.json.Jsonb = stalling
            try:
                backend.save_graph_data(snapshot(nodes))
            finally:
                module.psycopg.types.json.Jsonb = real

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert seen.wait(30), "the stalled save never reached its metadata write"
        return thread

    def test_overlapping_saves_leave_one_writers_graph(self, schema, backends):
        first = PostgresGraphPersistenceBackend(DSN, schema=schema)
        second = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([first, second])
        first.save_graph_data(snapshot([node_payload("seed")]))

        released = threading.Event()
        stalled = self._stalled_save(
            first, [node_payload("shared"), node_payload("only_first")], released
        )

        errors = []

        def save_second():
            try:
                second.save_graph_data(
                    snapshot([node_payload("shared"), node_payload("only_second")])
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        other = threading.Thread(target=save_second, daemon=True)
        other.start()
        # The second save must actually reach its first statement while the
        # first is still holding its transaction open - that is the whole
        # interleaving. Releasing straight away lets the first commit before
        # the second has begun, which is two saves in sequence and proves
        # nothing.
        threading.Event().wait(1.5)
        released.set()
        stalled.join(30)
        other.join(30)

        assert errors == [], f"a concurrent save failed: {errors}"
        landed = {n["id"] for n in second.load_graph_data()["nodes"]}
        assert landed in (
            {"shared", "only_first"},
            {"shared", "only_second"},
        ), f"the store holds a graph neither writer saved: {sorted(landed)}"


class TestPostgresLoadIsOneMomentInTime:
    """A load taken while another instance saves must not tear.

    Nodes, edges and metadata read as three statements are three moments,
    and PostgreSQL's default isolation takes its snapshot per statement. A
    save landing between them hands the reader edges whose endpoints are not
    in the nodes it got - a graph that never existed. For a backend whose
    whole purpose is several instances on one store, that interleaving is
    the normal case rather than a race to engineer.
    """

    def test_loading_while_another_instance_saves_never_tears(self, schema, backends):
        reader = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([reader, writer])

        def graph(tag):
            return {
                "nodes": [node_payload(f"n_{tag}")],
                "edges": [
                    {
                        "id": f"e_{tag}",
                        "source": f"n_{tag}",
                        "target": f"n_{tag}",
                        "type": "RELATES_TO",
                    }
                ],
                "metadata": {"version": "1.0", "graph_name": tag},
            }

        writer.save_graph_data(graph("first"))
        stop = threading.Event()
        torn, errors = [], []

        def save_repeatedly():
            tag = 0
            while not stop.is_set():
                tag += 1
                try:
                    writer.save_graph_data(graph(f"g{tag}"))
                except Exception as exc:
                    errors.append(f"writer: {type(exc).__name__}: {exc}")
                    return

        writing = threading.Thread(target=save_repeatedly, daemon=True)
        writing.start()
        try:
            for _ in range(40):
                loaded = reader.load_graph_data()
                ids = {n["id"] for n in loaded["nodes"]}
                dangling = [e["id"] for e in loaded["edges"] if e["source"] not in ids]
                if dangling:
                    torn.append(
                        f"edges {dangling} have no endpoint among {sorted(ids)}"
                    )
        finally:
            stop.set()
            writing.join(30)

        assert errors == []
        assert torn == [], f"load returned a graph that never existed: {torn[:3]}"


class TestPostgresStoreIdentity:
    def test_the_tables_existing_is_not_a_graph_existing(self, schema, backends):
        """`exists()` must answer for the graph, not for the migration.

        Every boot creates the tables, so a backend that read table presence
        would report a store that was never written as existing - and
        GraphStorage would then load an empty graph instead of bootstrapping
        one.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert not backend.exists()
        backend.save_graph_data(snapshot([node_payload("a")]))
        assert backend.exists()

    def test_two_schemas_are_two_stores(self, schema, backends):
        """One database serves several independent graphs."""
        other = f"{schema}_other"
        first = PostgresGraphPersistenceBackend(DSN, schema=schema)
        second = PostgresGraphPersistenceBackend(DSN, schema=other)
        backends.extend([first, second])
        try:
            first.save_graph_data(snapshot([node_payload("a")]))
            assert not second.exists()
            second.save_graph_data(snapshot([node_payload("b")]))

            assert [n["id"] for n in first.load_graph_data()["nodes"]] == ["a"]
            assert [n["id"] for n in second.load_graph_data()["nodes"]] == ["b"]
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')
