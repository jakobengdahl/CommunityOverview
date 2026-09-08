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
import secrets
import threading
import time
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

# Deliberately no default. This module creates and drops roles and schemas
# and, where PUBLIC holds it, revokes CREATE on the database - so a default
# pointing at a local server would do all of that to whatever a developer
# happens to be running, on a plain `pytest backend/ -q`. Opt in by naming
# the server; CI names it.
DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")


def _dbname() -> str:
    return psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", "postgres")


def _dsn_as_role(user: str, password: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["user"] = user
    parts["password"] = password
    return psycopg.conninfo.make_conninfo(**parts)


def _server_reachable() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
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
        # Joined against one shared deadline rather than a timeout each: ten
        # hung boots at sixty seconds apiece would blow the suite's own
        # per-test ceiling first, and a killed test says far less than the
        # assertion below.
        deadline = time.monotonic() + 60
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        # A boot that blocks forever adds to neither list, and join() with a
        # timeout returns either way - so without this the test is green on
        # exactly the migration bug that hangs rather than raises.
        assert not [t for t in threads if t.is_alive()], "a boot never finished"
        assert failures == [], f"instances failed to boot: {failures}"

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
        # Joined against one shared deadline rather than a timeout each: ten
        # hung boots at sixty seconds apiece would blow the suite's own
        # per-test ceiling first, and a killed test says far less than the
        # assertion below.
        deadline = time.monotonic() + 60
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        # A boot that blocks forever adds to neither list, and join() with a
        # timeout returns either way - so without this the test is green on
        # exactly the migration bug that hangs rather than raises.
        assert not [t for t in threads if t.is_alive()], "a boot never finished"
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

        assert not first.is_alive() and not second.is_alive(), (
            "a thread never finished - a migration that hangs rather than "
            "raises would otherwise pass here"
        )
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

        # A join with a timeout returns whether or not the thread finished,
        # and a save that hangs raises nothing - so without this the test
        # passes just as happily when the second writer never returns. Not
        # hypothetical: a session-scoped advisory lock in place of the
        # transaction-scoped one hangs every writer but the first, and this
        # test reported green on that for thirty seconds.
        assert not stalled.is_alive(), "the first save never finished"
        assert not other.is_alive(), "the second save never finished"
        assert errors == [], f"a concurrent save failed: {errors}"
        landed = {n["id"] for n in second.load_graph_data()["nodes"]}
        assert landed in (
            {"shared", "only_first"},
            {"shared", "only_second"},
        ), f"the store holds a graph neither writer saved: {sorted(landed)}"


class TestPostgresBootsForALeastPrivilegeRole:
    """The role a managed deployment actually runs as.

    An operator provisions the schema and the tables and grants the app role
    DML on them, nothing more. Both `CREATE SCHEMA IF NOT EXISTS` and
    `CREATE TABLE IF NOT EXISTS` check the caller's CREATE privilege *before*
    the existence short-circuit, so an unguarded migration raises for that
    role - at boot, against a store it has every permission it needs on.
    """

    @pytest.fixture(params=["plain", "MixedCase"], ids=["plain", "needs-quoting"])
    def lowpriv(self, request):
        """A role with no CREATE anywhere, and a schema it does not own.

        Parametrised over the schema's *name*, not for completeness: a name
        that only survives quoted is what tells an exact catalog lookup apart
        from one that parses the name and case-folds it. The parsing kind
        reports a table that exists as missing, which drops the guard for
        precisely this role.
        """
        suffix = uuid.uuid4().hex[:12]
        name = f"co_low_{suffix}"
        schema = (
            f"co_low_{suffix}_sch" if request.param == "plain" else f"CoLow_{suffix}"
        )
        # Generated per run rather than written down: the role lives for one
        # test, and a fixed one would be a credential in the source tree.
        password = secrets.token_hex(16)
        database = psycopg.sql.Identifier(_dbname())
        created_role = False
        public_had_create = False
        try:
            with psycopg.connect(DSN, autocommit=True) as conn:
                try:
                    conn.execute(
                        psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            psycopg.sql.Identifier(name),
                            psycopg.sql.Literal(password),
                        )
                    )
                except psycopg.errors.InsufficientPrivilege:
                    pytest.skip("the test role may not create roles")
                created_role = True
                # The fixture's whole premise is a role that cannot CREATE.
                # PUBLIC may hold CREATE on this database, in which case the
                # role inherits it and the guard under test is never reached
                # - the test would pass while covering nothing.
                public_had_create = conn.execute(
                    "SELECT has_database_privilege('public', %s, 'CREATE')",
                    (_dbname(),),
                ).fetchone()[0]
                if public_had_create:
                    conn.execute(
                        psycopg.sql.SQL(
                            "REVOKE CREATE ON DATABASE {} FROM PUBLIC"
                        ).format(database)
                    )
                if conn.execute(
                    "SELECT has_database_privilege(%s, %s, 'CREATE')",
                    (name, _dbname()),
                ).fetchone()[0]:
                    pytest.skip("cannot take CREATE on the database from the role")
                conn.execute(
                    psycopg.sql.SQL("CREATE SCHEMA {}").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                conn.execute(
                    psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name)
                    )
                )
            yield name, schema, password
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                if public_had_create:
                    conn.execute(
                        psycopg.sql.SQL("GRANT CREATE ON DATABASE {} TO PUBLIC").format(
                            database
                        )
                    )
                if created_role:
                    conn.execute(
                        psycopg.sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                            database, psycopg.sql.Identifier(name)
                        )
                    )
                    conn.execute(
                        psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                            psycopg.sql.Identifier(name)
                        )
                    )

    def test_a_role_with_only_dml_on_existing_tables_can_boot(self, lowpriv, backends):
        name, schema, password = lowpriv
        # The operator's half: the tables exist and the role may use them.
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table, columns in (
                ("graph_nodes", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                ("graph_edges", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                (
                    "graph_metadata",
                    "only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),"
                    " doc jsonb NOT NULL",
                ),
            ):
                conn.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.SQL(columns),
                    )
                )
            conn.execute(
                psycopg.sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE"
                    " ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name))
            )

        low_dsn = _dsn_as_role(name, password)
        backend = PostgresGraphPersistenceBackend(low_dsn, schema=schema)
        backends.append(backend)

        assert not backend.exists()
        backend.save_graph_data(snapshot([node_payload("a")]))
        assert backend.exists()
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]


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
                # Metadata is the third of the moment. Each save stamps its
                # generation into graph_name, so a load that mixes two shows
                # up here even when the edges happen to line up.
                generation = loaded["metadata"].get("graph_name")
                if ids and f"n_{generation}" not in ids:
                    torn.append(
                        f"metadata says {generation!r} but the nodes are {sorted(ids)}"
                    )
        finally:
            stop.set()
            writing.join(30)

        assert errors == []
        assert torn == [], f"load returned a graph that never existed: {torn[:3]}"


class TestPostgresLoadIsolation:
    """The load's isolation level, asserted rather than inferred.

    The tearing test infers it: if the level were wrong, some load would
    tear. That is true but weak. `SET SESSION CHARACTERISTICS` in place of
    `SET TRANSACTION` sets the default for *future* transactions and leaves
    the current one alone, so only the first load on each pooled connection
    is exposed - and a test that loads forty times through one connection
    gets no extra chances at it. Asserting the level directly kills that
    whole family in one go, and pins the docstring's claim that nothing is
    left on the connection afterwards.
    """

    def test_the_load_runs_repeatable_read_and_leaves_nothing_behind(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

        seen = []
        real_execute = psycopg.Connection.execute

        def spy(conn, query, *args, **kwargs):
            result = real_execute(conn, query, *args, **kwargs)
            # Keyed on the statement, not on "the first one seen": the load's
            # SET is only first because a preceding save memoised the
            # migration, so removing that memo - a pure performance change -
            # would otherwise turn this test red for the wrong reason.
            if "ISOLATION LEVEL" in str(query).upper() and not seen:
                seen.append(
                    real_execute(conn, "SHOW transaction_isolation").fetchone()[0]
                )
            return result

        psycopg.Connection.execute = spy
        try:
            backend.load_graph_data()
        finally:
            psycopg.Connection.execute = real_execute

        assert seen == ["repeatable read"], (
            f"the load did not run at REPEATABLE READ: {seen}"
        )

        # The same pooled connection, next transaction: back to the default.
        with backend._pool.connection() as conn:
            after = conn.execute("SHOW transaction_isolation").fetchone()[0]
        assert after == "read committed", (
            f"the isolation level leaked onto the pooled connection: {after}"
        )


class TestPostgresLoadOnAVirginStore:
    def test_loading_before_anything_else_migrates_first(self, schema, backends):
        """A read-only instance's first call is a load, not an exists().

        Every other test reaches `exists()` or a save before it loads, so a
        backend that migrated only on those paths would look fine here while
        raising UndefinedTable for an export script or an instance booting
        read-only against a store it expects to be there.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)

        assert backend.load_graph_data() == {"nodes": [], "edges": [], "metadata": {}}


class TestPostgresSaveWritesMetadataLast:
    """The ordering two other tests silently depend on.

    `interrupt_next_snapshot` and `_stalled_save` both key on "the payload
    dict with no id" - the metadata row - to place their hook at the end of
    the save. Nothing asserted that it *is* the end. Move the upsert to the
    front of the transaction and both hooks fire before any write: the
    interrupt no longer exercises rollback, and the overlapping-save test
    stops arming at all. Measured: with the upsert moved AND the save
    advisory lock deleted, the whole module still passed - so the guarantee
    the lock exists for was left undefended by an unrelated refactor.
    """

    def test_the_metadata_upsert_is_the_last_statement_of_a_save(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.exists()  # migrate first, so only the save is recorded

        statements = []
        real_execute = psycopg.Connection.execute
        real_many = psycopg.Cursor.executemany

        def note(query):
            statements.append(" ".join(str(query).split()))

        def spy_execute(conn, query, *args, **kwargs):
            note(query)
            return real_execute(conn, query, *args, **kwargs)

        def spy_many(cur, query, *args, **kwargs):
            note(query)
            return real_many(cur, query, *args, **kwargs)

        psycopg.Connection.execute = spy_execute
        psycopg.Cursor.executemany = spy_many
        try:
            backend.save_graph_data(snapshot([node_payload("a")]))
        finally:
            psycopg.Connection.execute = real_execute
            psycopg.Cursor.executemany = real_many

        writes = [q for q in statements if "advisory" not in q.lower()]
        assert writes, "the save issued no statements"
        assert "graph_metadata" in writes[-1] and "ON CONFLICT" in writes[-1], (
            "the metadata upsert is no longer the save's last statement, "
            "which is what the interrupt and stall hooks rely on to land "
            f"after the writes: {writes}"
        )


class TestPostgresGuardsAreCaseSensitive:
    """Two schemas differing only by case are two schemas.

    A guard that compared case-insensitively would see the *other* schema's
    tables, skip creating its own, and leave the instance running against a
    schema with nothing in it. The least-privilege parametrisation cannot
    catch this - its two names differ by more than case.
    """

    def test_a_sibling_schema_differing_only_by_case_is_not_this_store(
        self, schema, backends
    ):
        sibling = schema.upper()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(sibling)
                )
            )
            for table in ("graph_nodes", "graph_edges", "graph_metadata"):
                conn.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.{} (id text PRIMARY KEY)").format(
                        psycopg.sql.Identifier(sibling),
                        psycopg.sql.Identifier(table),
                    )
                )
        try:
            backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
            backends.append(backend)
            # Would raise UndefinedColumn against the sibling's shape if the
            # guard had matched case-insensitively and skipped creating ours.
            backend.save_graph_data(snapshot([node_payload("a")]))
            assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(sibling)
                    )
                )


class TestPostgresMigrationBuildsTheDocumentedSchema:
    def test_every_table_gets_its_primary_key(self, schema, backends):
        """The DDL the document publishes for an operator to provision.

        The keys are not decoration. `graph_metadata.only_row` is named in
        the save's ON CONFLICT, so without it every save fails; the entity
        keys are what turn a lost save lock into a loud duplicate-key error
        instead of a silent union of two graphs.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.exists()

        with psycopg.connect(DSN, autocommit=True) as conn:
            keyed = {
                row[0]
                for row in conn.execute(
                    "SELECT c.relname FROM pg_constraint k"
                    " JOIN pg_class c ON c.oid = k.conrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND k.contype = 'p'",
                    (schema,),
                )
            }
        assert keyed == {"graph_nodes", "graph_edges", "graph_metadata"}


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
        # An EMPTY graph, deliberately: with a node saved, an exists() that
        # wrongly read graph_nodes would pass this too, and the test would
        # not carry its own name.
        backend.save_graph_data(snapshot())
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
