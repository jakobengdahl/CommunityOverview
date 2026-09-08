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

import itertools
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from backend.core.tests.persistence_contract import (
    PersistenceBackendContract,
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)

# Read before the import guard below, not after: `importorskip` would
# otherwise skip the whole module for a missing driver before anything got
# to ask whether a skip is acceptable here - the same silent green this
# variable exists to prevent, reached by the other door.
REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES") == "1"

if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    MIGRATION_LOCK_KEY,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage_backends import (  # noqa: E402
    BackendCapabilities,
    EntityOperation,
)

# Deliberately no default. This module creates and drops roles and schemas
# and, where PUBLIC holds it, revokes CREATE on the database - so a default
# pointing at a local server would do all of that to whatever a developer
# happens to be running, on a plain `pytest backend/ -q`. Opt in by naming
# the server; CI names it.
DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")


# The bound `docs/PERSISTENCE_BACKENDS.md` publishes, restated here so the
# tests below pin it from both sides rather than following it wherever it
# is moved.
DOCUMENTED_DEADLOCK_RETRIES = 3


class _RetryBoundExceeded(BaseException):
    """Raised by a test when a bounded retry loop turns out unbounded."""


def _dbname() -> str:
    return psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", "postgres")


def _dsn_as_role(user: str, password: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["user"] = user
    parts["password"] = password
    return psycopg.conninfo.make_conninfo(**parts)


# CO_REQUIRE_POSTGRES (read above, before the driver import): without it an
# unreachable server is a skip, so a developer with a stale variable is not
# blocked. With it, an unreachable server - or a missing driver - is an
# error, because CI always sets the DSN and a service container that failed
# to start would otherwise leave "Backend tests" green having run none of
# this backend at all.


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


def _statements_issued(action, into=None):
    """Every statement `action` issues, captured on the cursor.

    The cursor, not the connection: `Connection.execute` delegates to a
    cursor, so this sees both, where patching the connection alone would
    miss anything a backend ran on a cursor of its own.

    Pass `into` when the action is expected to raise: the return value is
    lost in that case, and what was issued before the failure is exactly
    what a test about a partly-applied batch needs to see.
    """
    issued = [] if into is None else into
    real_execute = psycopg.Cursor.execute
    real_many = psycopg.Cursor.executemany

    def spy_execute(cur, query, params=None, *args, **kwargs):
        issued.append((query, params))
        return real_execute(cur, query, params, *args, **kwargs)

    def spy_many(cur, query, params_seq, *args, **kwargs):
        # Materialised so the recorded parameters are usable: `EXPLAIN`
        # cannot plan a parameterised statement without them, and a
        # generator would be consumed by reading the first row. An empty
        # sequence records the sentinel rather than None, so "executed
        # zero times" stays distinguishable from "parameters unknown".
        rows = list(params_seq)
        issued.append((query, rows[0] if rows else _EXECUTED_NEVER))
        return real_many(cur, query, rows, *args, **kwargs)

    psycopg.Cursor.execute = spy_execute
    psycopg.Cursor.executemany = spy_many
    try:
        action()
    finally:
        psycopg.Cursor.execute = real_execute
        psycopg.Cursor.executemany = real_many
    return issued


# An executemany over an empty sequence: the statement was issued but ran
# no times, so it scans nothing and there is nothing to plan.
_EXECUTED_NEVER = object()


def _rendered(query):
    """The SQL a statement actually is - str() gives a repr."""
    if hasattr(query, "as_string"):
        with psycopg.connect(DSN) as conn:
            return " ".join(query.as_string(conn).split())
    return " ".join(str(query).split())


# Not only the three obvious verbs. This helper is the file's general
# answer to "did that write", and a statement that takes a row lock or
# empties a table writes as surely as an UPDATE does - `TRUNCATE` inside
# `checkpoint()` would be invisible to a three-verb filter. `FOR UPDATE`
# would match by accident on the word UPDATE; `FOR SHARE` would not.
_WRITING = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "MERGE", "COPY", "FOR SHARE")


def _writing_statements(issued):
    return [
        text
        for text in (_rendered(q) for q, _ in issued)
        if any(verb in text.upper() for verb in _WRITING)
    ]


def _tables_named(text):
    """Which of our tables a statement names, as a set."""
    return {
        table
        for table in ("graph_nodes", "graph_edges", "graph_metadata")
        if table in text
    }


def _sequential_scans(issued):
    """(statements naming a graph table, those whose plan scans one).

    EXPLAIN does not execute, so this is safe to run for every statement.
    """
    touched, scanning = [], []
    with psycopg.connect(DSN, autocommit=True) as conn:
        for query, params in issued:
            text = _rendered(query)
            if "graph_nodes" not in text and "graph_edges" not in text:
                continue
            if params is _EXECUTED_NEVER:
                continue
            touched.append(text)
            if params is None and "%s" in text:
                # Refused rather than skipped. A statement this helper
                # cannot plan is one it cannot answer for, and returning
                # it as "not scanning" would be an answer.
                raise AssertionError(
                    f"cannot plan a parameterised statement with no recorded "
                    f"parameters, so its cost is unknown: {text}"
                )
            rows = conn.execute(psycopg.sql.SQL("EXPLAIN ") + query, params).fetchall()
            if "Seq Scan" in "\n".join(r[0] for r in rows):
                scanning.append(text)
    return touched, scanning


def _wait_until_blocking(pid, timeout=15.0):
    """Wait until the session `pid` is blocking another one.

    Attributed on purpose. A fixed sleep plus `thread.is_alive()` cannot
    tell "blocked on the lock" from "has not reached it yet", so under
    load it passes while the interleaving never happened. Asking only
    whether *something* in this database waits on a lock replaces that
    weakness with a worse one: any unrelated blocked session satisfies
    it, including one no test created - two suite runs against one
    database, or a developer with a psql parked in an open transaction.

    `pg_blocking_pids` names the sessions doing the blocking, so the
    caller can ask about the one it holds open and can vouch for.
    """
    deadline = time.monotonic() + timeout
    with psycopg.connect(DSN, autocommit=True) as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database()"
                " AND %s = ANY(pg_blocking_pids(pid))",
                (pid,),
            ).fetchone()[0]
            if waiting:
                return True
            threading.Event().wait(0.05)
    return False


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

    def interrupt_next_append(self, backend, monkeypatch):
        """Fail a batch after its delete has reached the server.

        Armed on the STATEMENTS the batch issues, not on when its payloads
        happen to be serialised. Keying on the payload works only while
        `Jsonb()` is called lazily inside the transaction: build the
        parameter lists up front - an ordinary refactor that changes no SQL -
        and the hook fires before any statement is sent, so the store is
        untouched and the clause passes without having tested a rollback at
        all. Measured: that combination shipped a non-atomic batch green.

        Raising once a DELETE has been seen and an INSERT is starting is the
        property the clause actually needs, so it is what the hook checks.
        """
        seen_delete = []
        real_execute = psycopg.Connection.execute

        def spy(conn, query, *args, **kwargs):
            text = " ".join(str(query).split()).upper()
            if "DELETE" in text:
                seen_delete.append(text)
            elif "INSERT" in text and seen_delete:
                monkeypatch.setattr(psycopg.Connection, "execute", real_execute)
                raise OSError("connection lost mid-batch")
            return real_execute(conn, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", spy)


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

    def test_a_partly_provisioned_schema_gets_the_rest(self, schema, backends):
        """An operator who provisioned some of the tables, not all three.

        Every other existing-schema case here provisions all three together,
        so a migration that checked one table and assumed the others would
        look identical. It is not: the instance boots, then dies on the
        first statement touching a table nobody created.
        """
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(schema)
                )
            )
            conn.execute(
                psycopg.sql.SQL(
                    "CREATE TABLE {}.graph_nodes"
                    " (id text PRIMARY KEY, doc jsonb NOT NULL)"
                ).format(psycopg.sql.Identifier(schema))
            )

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert not backend.exists()
        backend.save_graph_data(snapshot([node_payload("a")]))
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]

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

    stall_errors: list = []

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
            except Exception as exc:
                # Without this a G7 violation landing on the FIRST writer is
                # silent: the thread dies raising, is_alive() is satisfied,
                # and only a pytest warning records it.
                self.stall_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                module.psycopg.types.json.Jsonb = real

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert seen.wait(30), "the stalled save never reached its metadata write"
        return thread

    def test_overlapping_saves_leave_one_writers_graph(self, schema, backends):
        # Different graph_name, same store: the lock must be keyed on what
        # identifies the store - the schema - not on a constructor argument
        # two instances of one graph need not agree on.
        first = PostgresGraphPersistenceBackend(DSN, schema=schema, graph_name="a")
        second = PostgresGraphPersistenceBackend(DSN, schema=schema, graph_name="b")
        backends.extend([first, second])
        self.stall_errors = []
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
        assert errors == [], f"the second save failed: {errors}"
        assert self.stall_errors == [], f"the first save failed: {self.stall_errors}"
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
        generations = set()

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
                generations.add(generation)
                if ids and f"n_{generation}" not in ids:
                    torn.append(
                        f"metadata says {generation!r} but the nodes are {sorted(ids)}"
                    )
        finally:
            stop.set()
            writing.join(30)

        assert errors == []
        assert torn == [], f"load returned a graph that never existed: {torn[:3]}"
        # A reader frozen on its first result is maximally consistent and
        # would satisfy every check above without ever reading the store
        # again.
        assert len(generations) > 1, f"the reader never advanced past {generations}"


class TestPostgresSaveFailsWholeOnAnEntityWrite:
    """Atomicity where the contract's hook cannot reach.

    `interrupt_next_snapshot` keys on the payload with no `id` - the
    metadata row - so the contract's atomicity clause only ever interrupts
    the save's LAST statement. A failure on a node is the untested half, and
    it is the reachable one: an unencodable value in a payload comes from
    the graph, not from a hook.
    """

    def test_a_node_that_cannot_be_serialised_takes_the_whole_save_with_it(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("keep")]))

        # A value json cannot encode, reached through the ordinary payload.
        doomed = node_payload("doomed")
        doomed["metadata"] = {"when": datetime.now(timezone.utc)}
        with pytest.raises(Exception):
            backend.save_graph_data(snapshot([node_payload("also_new"), doomed]))

        # Neither new node landed, and the previous graph is intact - not
        # "most of it", and not silently short of the node that failed.
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["keep"]


class TestPostgresSaveIsolation:
    """The save states READ COMMITTED rather than inheriting it.

    Asserting the level alone would prove nothing - READ COMMITTED is the
    default, so a save with no SET at all would pass. The connection here
    therefore carries a REPEATABLE READ default, which is what a server,
    database or role setting looks like from the backend's side. Under it,
    an inheriting save takes its snapshot at the advisory lock, before the
    lock blocks, and the writer that waited dies on a serialization failure
    instead of proceeding - the "never a failure" half of the guarantee the
    lock exists for.
    """

    def _dsn_defaulting_to_repeatable_read(self) -> str:
        parts = psycopg.conninfo.conninfo_to_dict(DSN)
        parts["options"] = "-c default_transaction_isolation=repeatable\\ read"
        return psycopg.conninfo.make_conninfo(**parts)

    def test_the_save_runs_read_committed_under_a_repeatable_read_default(
        self, schema, backends
    ):
        dsn = self._dsn_defaulting_to_repeatable_read()
        with psycopg.connect(dsn) as check:
            assert (
                check.execute("SHOW transaction_isolation").fetchone()[0]
                == "repeatable read"
            ), "the fixture did not establish a non-default level"

        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, pool_size=1)
        backends.append(backend)
        backend.exists()  # migrate first, so only the save is observed

        seen = []
        real_execute = psycopg.Connection.execute

        def spy(conn, query, *args, **kwargs):
            result = real_execute(conn, query, *args, **kwargs)
            # The save's own lock, told apart by its two-argument form: the
            # migration takes a one-argument advisory lock too, so keying on
            # "advisory" latches onto whichever fires first. That makes the
            # test red - with an actively false message - when the migration
            # memo is removed, which is a pure performance change. The
            # sibling load test documents this hazard; this one had not
            # inherited the defence.
            if "hashtext" in str(query).lower() and not seen:
                seen.append(
                    real_execute(conn, "SHOW transaction_isolation").fetchone()[0]
                )
            return result

        psycopg.Connection.execute = spy
        try:
            backend.save_graph_data(snapshot([node_payload("a")]))
        finally:
            psycopg.Connection.execute = real_execute

        assert seen == ["read committed"], (
            f"the save inherited the connection's isolation level: {seen}"
        )


class TestPostgresDeclaresWhatItImplements:
    """Under-declaring is invisible to the contract, by construction.

    Every clause reads the backend's own declaration and skips itself when
    the flag is absent, so dropping a flag turns tests into skips and the
    suite stays green - while GraphStorage quietly reverts to whole-graph
    writes, which is the regression this slice exists to prevent. Only an
    assertion outside the contract can see it.
    """

    def test_the_declaration_is_exactly_incremental_and_transactional(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert backend.capabilities() == BackendCapabilities(
            incremental_writes=True, transactions=True
        )

    def test_an_entity_write_can_be_a_backends_first_call(self, schema, backends):
        """Migration is every other method's first act; this one too.

        GraphStorage happens to call exists() before it mutates, so the
        omission would not surface there - but the guard is uniform in this
        class and nothing was holding it in place on the entity path.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)

        backend.upsert_node(node_payload("a"))

        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]


class TestPostgresEntityWritesTouchOneRow:
    """What makes an entity write worth having, and what the contract misses.

    Every contract clause checks the resulting graph, which a backend whose
    `upsert_node` quietly rewrote the whole store would satisfy perfectly -
    while destroying exactly the property this slice exists for. These check
    the cost and the blast radius rather than the outcome.

    Every case runs for edges as well as nodes, and for a multi-operation
    batch as well as the single-operation wrappers. That is not symmetry
    for its own sake: `GraphStorage._do_apply` reaches `apply_batch` only
    when there is more than one operation, and `delete_nodes` builds
    exactly that - edge deletes followed by node deletes - so the wrappers
    these tests once covered alone are the ones production uses least.
    """

    # Each entity write, with the tables its writing statements may name,
    # in order. The batch is the one production actually issues.
    WRITES = {
        "upsert_node": (
            lambda b: b.upsert_node(node_payload("n0", name="Renamed")),
            [{"graph_nodes"}],
        ),
        "delete_node": (lambda b: b.delete_node("n0"), [{"graph_nodes"}]),
        "upsert_edge": (
            lambda b: b.upsert_edge(edge_payload("e0", "n0", "n1", name="Renamed")),
            [{"graph_edges"}],
        ),
        "delete_edge": (lambda b: b.delete_edge("e0"), [{"graph_edges"}]),
        "batch": (
            lambda b: b.apply_batch(
                [
                    EntityOperation.delete_edge("e0"),
                    EntityOperation.delete_node("n0"),
                ]
            ),
            [{"graph_edges"}, {"graph_nodes"}],
        ),
        # Removing something that is not there is not an error, and is the
        # natural place for a fallback that rewrites the table.
        "delete_absent_node": (
            lambda b: b.delete_node("no-such-node"),
            [{"graph_nodes"}],
        ),
        "delete_absent_edge": (
            lambda b: b.delete_edge("no-such-edge"),
            [{"graph_edges"}],
        ),
    }

    # Lengths, not a length. Pinning one batch size pins a fencepost: this
    # suite has now been extended from one operation to two and from two
    # to five, and each time a mutation gated just above the new size
    # walked past every case. `GraphStorage.delete_nodes` builds one edge
    # delete per edge plus one node delete per node, so the length is
    # whatever the caller deleted - a property, not a number. Testing a
    # spread of them means a threshold gate has nowhere plausible to hide;
    # it cannot make "gated above 40" impossible, only absurd.
    BATCH_LENGTHS = (2, 5, 40)

    # Runs of two, cycling edge-delete, node-delete, edge-upsert,
    # node-upsert. Runs rather than strict alternation because
    # `GraphStorage.delete_nodes` emits every edge delete and then every
    # node delete, so consecutive same-table operations are the ordinary
    # shape - and a backend that merged adjacent ones into a single
    # statement would be invisible to a batch that never has two in a row.
    # Both actions appear from length 5; a shorter length truncates the
    # cycle, which is what makes 2 worth running as well as 40.
    _RUN = 2

    @classmethod
    def _long_batch(cls, length):
        """Both kinds and both actions, in runs, at a caller-chosen length."""
        make = (
            lambda i: EntityOperation.delete_edge(f"e{i}"),
            lambda i: EntityOperation.delete_node(f"n{i}"),
            lambda i: EntityOperation.upsert_edge(
                edge_payload(f"e{i}", f"n{i}", "n0", name="Renamed")
            ),
            lambda i: EntityOperation.upsert_node(
                node_payload(f"n{i}", name="Renamed")
            ),
        )
        return [make[(i // cls._RUN) % 4](i) for i in range(length)]

    @classmethod
    def _long_batch_slots(cls, length):
        return [(i // cls._RUN) % 4 for i in range(length)]

    @classmethod
    def _long_batch_tables(cls, length):
        return [
            {"graph_edges"} if slot in (0, 2) else {"graph_nodes"}
            for slot in cls._long_batch_slots(length)
        ]

    # Large enough that the planner prefers the index whether or not the
    # table has been analysed. Measured on this server: at 40 rows an
    # unanalysed table plans an Index Scan and an analysed one plans a Seq
    # Scan, so a 40-row seed makes the assertion below a property of
    # missing statistics rather than of the backend, and one ANALYZE turns
    # it red for correct code. At 200 it is an Index Scan either way.
    SEED = 200

    @classmethod
    def _seeded(cls, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(
            snapshot(
                [node_payload(f"n{i}") for i in range(cls.SEED)],
                [edge_payload(f"e{i}", f"n{i}", "n0") for i in range(cls.SEED)],
            )
        )
        with psycopg.connect(DSN, autocommit=True) as conn:
            # So the plan asserted below is the one a live store gets, not
            # the one a never-analysed table happens to get.
            conn.execute(
                psycopg.sql.SQL("ANALYZE {}.graph_nodes, {}.graph_edges").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(schema)
                )
            )
        return backend

    @pytest.mark.parametrize("write", sorted(WRITES))
    def test_an_entity_write_reads_no_more_of_the_graph_as_it_grows(
        self, schema, backends, write
    ):
        """The economy, as the cost of the plan rather than a count of texts.

        A backend that satisfied the contract by re-saving the whole graph
        would be correct and useless, and counting statements catches that
        shape. It does not catch every shape: one statement with constant
        text can still carry an unbounded scan, and a read routed through a
        cursor is invisible to a spy on the connection. So this asserts what
        the guarantee actually says - that nothing the write issues reads a
        table sequentially - and pins the shape as well.

        A delete is where the two diverge: `WHERE doc->>'id' = %s` reads the
        same as `WHERE id = %s` and plans as a sequential scan.
        """
        action, expected = self.WRITES[write]
        backend = self._seeded(schema, backends)

        issued = _statements_issued(lambda: action(backend))

        writes = _writing_statements(issued)
        named = [_tables_named(text) for text in writes]
        assert named == expected, (
            f"{write} should issue {len(expected)} writing statement(s) naming "
            f"{expected}, and issued {named}: {writes}"
        )
        if write.startswith("upsert"):
            assert "ON CONFLICT" in writes[0]
            assert "DELETE" not in writes[0].upper()
        if write.startswith("delete"):
            assert all("DELETE" in text.upper() for text in writes)

        touched, scanning = _sequential_scans(issued)
        assert touched, f"no statement of {write} touched a graph table"
        assert not scanning, (
            f"{write} reads a graph table sequentially, so its cost grows "
            f"with the graph: {scanning}"
        )

    @pytest.mark.parametrize("length", BATCH_LENGTHS)
    def test_a_batch_reads_no_more_of_the_graph_as_it_grows(
        self, schema, backends, length
    ):
        """The same two questions, at several batch lengths.

        A mutation gated on `len(operations) >= N`, or on the operation at
        index N, is invisible to any single fixed length. This is the one
        test that walks the whole batch: one writing statement per
        operation, naming that operation's own table, in the caller's
        order, and no plan that scans.
        """
        backend = self._seeded(schema, backends)

        issued = _statements_issued(
            lambda: backend.apply_batch(self._long_batch(length))
        )

        writes = _writing_statements(issued)
        named = [_tables_named(text) for text in writes]
        assert named == self._long_batch_tables(length), (
            f"a batch of {length} should issue one writing statement per "
            f"operation, naming its own table, in order: {named}"
        )
        for index, (slot, text) in enumerate(
            zip(self._long_batch_slots(length), writes)
        ):
            if slot in (0, 1):
                assert "DELETE" in text.upper(), f"operation {index}: {text}"
            else:
                assert "ON CONFLICT" in text.upper(), f"operation {index}: {text}"

        touched, scanning = _sequential_scans(issued)
        assert touched, "no statement of the batch touched a graph table"
        assert not scanning, (
            f"a batch of {length} reads a graph table sequentially: {scanning}"
        )

    def test_a_delete_leaves_the_edges_no_operation_named(self, schema, backends):
        """The seam does not cascade, so neither may the backend.

        A delete is the natural place to write rows nobody asked for -
        removing the edges that reference the node - which would eat an
        edge another instance committed a moment earlier. `delete_node` is
        documented as removing one node, and `GraphStorage.delete_nodes`
        names the edges it wants gone, edges first.
        """
        backend = self._seeded(schema, backends)

        backend.delete_node("n0")

        loaded = backend.load_graph_data()
        assert "n0" not in by_id(loaded, "nodes")
        assert "n1" in by_id(loaded, "nodes")
        assert "e0" in by_id(loaded, "edges"), (
            "deleting a node removed an edge no operation named"
        )

    @pytest.mark.parametrize(
        "write", ["upsert_node", "upsert_edge", "batch", "batch_many"]
    )
    def test_an_entity_write_does_not_lose_a_row_written_while_it_runs(
        self, schema, backends, write
    ):
        """The property a read-modify-write quietly breaks.

        Writing the foreign row *before* the write proves nothing: a
        backend that loaded the graph, edited it and saved it whole would
        read that row and put it back. The lost update only appears when the
        row lands between such a backend's read and its write - which is the
        ordinary case on a shared store, not a contrived one. So the row is
        inserted from another connection after the first writing statement.

        The batch case is the one that matters most and was missing longest:
        "batches are complicated, just re-save" is a plausible thing for a
        later reader to write, and it passes every clause of the contract.
        """
        backend = self._seeded(schema, backends)

        real_execute = psycopg.Connection.execute
        injected = []

        def spy(conn, query, *args, **kwargs):
            # Just before the first WRITING statement, which is the only
            # point that separates the two shapes: a row write has done no
            # reading by then, while a read-modify-write has already read
            # and is about to overwrite what it read.
            text = " ".join(str(query).split()).upper()
            if not injected and any(
                verb in text for verb in ("INSERT", "DELETE", "UPDATE")
            ):
                injected.append(text)
                with psycopg.connect(DSN, autocommit=True) as other:
                    other.execute(
                        psycopg.sql.SQL(
                            "INSERT INTO {}.graph_nodes (id, doc) VALUES (%s, %s)"
                        ).format(psycopg.sql.Identifier(schema)),
                        (
                            "elsewhere",
                            psycopg.types.json.Jsonb(node_payload("elsewhere")),
                        ),
                    )
            return real_execute(conn, query, *args, **kwargs)

        psycopg.Connection.execute = spy
        try:
            if write == "upsert_node":
                backend.upsert_node(node_payload("n0", name="Renamed"))
            elif write == "upsert_edge":
                backend.upsert_edge(edge_payload("e0", "n0", "n0", name="Renamed"))
            elif write == "batch":
                backend.apply_batch(
                    [
                        EntityOperation.upsert_node(node_payload("n0", name="Renamed")),
                        EntityOperation.upsert_node(node_payload("n1", name="Renamed")),
                    ]
                )
            else:
                backend.apply_batch(
                    [
                        EntityOperation.upsert_node(node_payload("n0", name="Renamed")),
                        EntityOperation.upsert_node(node_payload("n1", name="Renamed")),
                        EntityOperation.upsert_node(node_payload("n2", name="Renamed")),
                        EntityOperation.upsert_edge(
                            edge_payload("e0", "n0", "n1", name="Renamed")
                        ),
                    ]
                )
        finally:
            psycopg.Connection.execute = real_execute

        assert injected, "the other instance's write never happened"
        loaded = backend.load_graph_data()
        assert "elsewhere" in by_id(loaded, "nodes"), (
            f"a row written during {write} was lost, so {write} is not a row write"
        )
        # The exact sets, not membership: a write that also removed or
        # added an unrelated row would otherwise pass here.
        assert set(by_id(loaded, "nodes")) == {f"n{i}" for i in range(self.SEED)} | {
            "elsewhere"
        }
        assert set(by_id(loaded, "edges")) == {f"e{i}" for i in range(self.SEED)}

        if write == "upsert_node":
            assert by_id(loaded, "nodes")["n0"]["name"] == "Renamed"
        elif write == "upsert_edge":
            assert by_id(loaded, "edges")["e0"]["name"] == "Renamed"
        else:
            nodes = by_id(loaded, "nodes")
            assert nodes["n0"]["name"] == "Renamed"
            assert nodes["n1"]["name"] == "Renamed"
            if write == "batch_many":
                assert nodes["n2"]["name"] == "Renamed"
                assert by_id(loaded, "edges")["e0"]["name"] == "Renamed"

    def test_a_batch_carrying_an_unstorable_value_raises(self, schema, backends):
        """The entity twin of the save's own unstorable-value test.

        `save_graph_data` has one; the entity path had none, and a batch
        is where it matters more. A backend with no vector sidecar
        receives every embedding inline, so one degenerate vector is
        enough - and the documented contract is that the error propagates,
        because GraphStorage answers a failed entity write by re-issuing
        the whole graph. Swallowing it tells the caller the mutation is
        stored, the whole-graph fallback never fires, and the write is
        lost with nothing failing. Several operations, with the offending
        one in the middle: a swallow gated on batch length walks past a
        single-operation case.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("before")]))

        batch = [
            EntityOperation.upsert_node(node_payload("ok1")),
            EntityOperation.upsert_node(node_payload("bad", embedding=[float("nan")])),
            EntityOperation.upsert_node(node_payload("ok2")),
        ]
        # `psycopg.Error`, not bare `Exception`: the point is that the
        # SERVER rejected the value part way through a started batch. A
        # client-side rejection - a serialiser configured allow_nan=False,
        # say - would raise before any statement was issued, and "left
        # part of itself behind" would then be asserted about a batch that
        # never began.
        issued = []
        with pytest.raises(psycopg.Error):
            _statements_issued(lambda: backend.apply_batch(batch), into=issued)

        assert any("ok1" in str(params) for _, params in issued), (
            "the batch failed before it wrote anything, so this says "
            "nothing about a partly-applied one"
        )
        assert set(by_id(backend.load_graph_data(), "nodes")) == {"before"}, (
            "a batch carrying an unstorable value left part of itself behind"
        )

    def test_a_checkpoint_writes_nothing(self, schema, backends):
        """The docstring's claim, as an assertion.

        `checkpoint()` is a no-op here because a database has no deferred
        state - but the contract only asks that entity writes survive one,
        which a full load-and-save round trip satisfies too. That shape is
        not merely wasteful: GraphStorage calls `checkpoint()` at shutdown,
        so it would reintroduce the whole-graph overwrite this slice exists
        to remove, on the one path nobody is watching.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a"), node_payload("b")]))
        backend.upsert_node(node_payload("c"))

        issued = _statements_issued(backend.checkpoint)

        # The positive control. Without it the only assertions below are
        # that lists are empty, which they would also be if the spy had
        # stopped capturing anything at all.
        observed = _statements_issued(lambda: backend.upsert_node(node_payload("d")))
        assert _writing_statements(observed), (
            "the spy captured nothing even for a known write"
        )

        # Not "issues no statement matching a write verb": that is a
        # keyword filter, and a checkpoint can rewrite the store without
        # matching one - `CREATE TABLE AS` plus `DROP` plus `ALTER ...
        # RENAME` replaces both tables whole with no INSERT, UPDATE or
        # DELETE in sight. The question worth asking is stricter and
        # simpler: does it touch a graph table at all.
        touching = [
            text for text in (_rendered(q) for q, _ in issued) if _tables_named(text)
        ]
        assert touching == [], (
            f"a checkpoint should not touch a graph table at all: {touching}"
        )

    def test_an_upsert_carries_the_vector_a_snapshot_would_have_held(
        self, schema, backends
    ):
        """The silent-loss shape, pinned.

        A backend with no vector sidecar receives every embedding inline, in
        the node payload, for a snapshot and an upsert alike. If an entity
        write dropped it, vectors would survive a whole-graph save and
        disappear on any single-node edit - with nothing failing.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        backend.upsert_node(node_payload("a", embedding=[0.5, 0.25]))

        stored = by_id(backend.load_graph_data(), "nodes")["a"]
        assert stored["embedding"] == [0.5, 0.25]


class TestPostgresConcurrentEntityWrites:
    def test_two_instances_writing_different_entities_both_land(self, schema, backends):
        """The deployment this whole initiative is for.

        Under whole-graph saves these two would overwrite each other by
        design; as row writes they do not contend at all.
        """
        first = PostgresGraphPersistenceBackend(DSN, schema=schema)
        second = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([first, second])
        first.save_graph_data(snapshot())

        errors = []

        def write(backend, prefix):
            try:
                for i in range(10):
                    backend.upsert_node(node_payload(f"{prefix}{i}"))
            except Exception as exc:
                errors.append(f"{prefix}: {type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=write, args=(first, "a"), daemon=True),
            threading.Thread(target=write, args=(second, "b"), daemon=True),
        ]
        deadline = time.monotonic() + 60
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        assert not [t for t in threads if t.is_alive()], "a writer never finished"
        assert errors == [], f"a concurrent entity write failed: {errors}"
        landed = {n["id"] for n in first.load_graph_data()["nodes"]}
        assert landed == {f"a{i}" for i in range(10)} | {f"b{i}" for i in range(10)}, (
            f"writes were lost: {sorted(landed)}"
        )

    @pytest.mark.parametrize("write", ["single", "batch"])
    def test_the_same_entity_from_two_instances_resolves_last_writer_wins(
        self, schema, backends, write
    ):
        """Neither writer fails, and the row holds one of the two payloads.

        Under a REPEATABLE READ default the second writer would abort with a
        serialization failure rather than wait; the save and the batch both
        state READ COMMITTED so that they wait and win instead.
        """
        parts = psycopg.conninfo.conninfo_to_dict(DSN)
        parts["options"] = "-c default_transaction_isolation=repeatable\\ read"
        hostile = psycopg.conninfo.make_conninfo(**parts)

        first = PostgresGraphPersistenceBackend(hostile, schema=schema)
        second = PostgresGraphPersistenceBackend(hostile, schema=schema)
        backends.extend([first, second])
        first.save_graph_data(snapshot())

        first.upsert_node(node_payload("contested", name="Original"))

        # Forced, not hoped for. Two threads racing collide only sometimes,
        # and measured, a regression removing the isolation statement went
        # green on about two runs in five - which a CI re-run would then
        # "fix". A blocker holding the row makes the second writer wait every
        # time.
        errors = []
        contended = False
        blocker = psycopg.connect(hostile, autocommit=False)
        try:
            blocker.execute(
                psycopg.sql.SQL(
                    "UPDATE {}.graph_nodes SET doc = doc WHERE id = %s"
                ).format(psycopg.sql.Identifier(schema)),
                ("contested",),
            )

            def contend():
                try:
                    if write == "single":
                        second.upsert_node(node_payload("contested", name="Second"))
                    else:
                        # The isolation statement stated only for short
                        # batches would leave this one aborting on a
                        # serialization failure instead of waiting.
                        second.apply_batch(
                            [
                                EntityOperation.upsert_node(node_payload(f"filler{i}"))
                                for i in range(5)
                            ]
                            + [
                                EntityOperation.upsert_node(
                                    node_payload("contested", name="Second")
                                )
                            ]
                        )
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")

            writer = threading.Thread(target=contend, daemon=True)
            writer.start()
            # Asked of the server about the blocker we hold, not guessed at
            # with a sleep and not asked of the database at large.
            contended = _wait_until_blocking(blocker.info.backend_pid)
            contended = contended and writer.is_alive()
            blocker.commit()
            writer.join(30)
        finally:
            blocker.close()

        assert contended, "the writer never contended for the row"
        assert not writer.is_alive(), "the contended write never finished"
        assert errors == [], f"a contended entity write failed: {errors}"
        assert by_id(first.load_graph_data(), "nodes")["contested"]["name"] == (
            "Second"
        )


class TestPostgresEntityWritesSpendTheConnectionBudget:
    """Connections are the scarce resource this backend is sized around.

    The pool size is documented against a server's `max_connections`
    divided by the instance count, and the migration is memoised so a
    boot-time advisory lock is not re-taken on every call. Both are
    load-bearing for the multi-instance case and neither was pinned: a
    write path opening its own connection, or a memo that never sets,
    leaves every functional test green while quietly turning the
    connection budget into a fiction.
    """

    def test_repeated_entity_writes_take_no_new_connections(self, schema, backends):
        """Counted by what the write asks for, not by what is open.

        A write that opens its own connection and closes it again leaves
        no trace in `pg_stat_activity` by the time anything looks, so the
        question has to be asked of the driver: once the pool is warm, an
        entity write must open no connection at all.

        Both spellings are spied, because they are two independent
        handles to the same function: `psycopg.connect` is a bound
        classmethod captured at import, so replacing
        `psycopg.Connection.connect` does not change what it calls, and
        replacing `psycopg.connect` does not change what the pool calls.
        Measured, in both directions: a bypass written one way survives a
        spy on the other. The pool opens through the class method; a
        hand-rolled bypass is likelier to use the module helper.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())
        backend.upsert_node(node_payload("warm"))  # fill the pool first

        opened = []
        real_module = psycopg.connect
        real_class = psycopg.Connection.connect

        def spy_module(conninfo="", **kwargs):
            opened.append(("psycopg.connect", conninfo))
            return real_module(conninfo, **kwargs)

        def spy_class(conninfo="", **kwargs):
            opened.append(("Connection.connect", conninfo))
            return real_class(conninfo, **kwargs)

        psycopg.connect = spy_module
        psycopg.Connection.connect = spy_class
        try:
            for i in range(12):
                backend.upsert_node(node_payload(f"n{i}"))
        finally:
            psycopg.connect = real_module
            psycopg.Connection.connect = real_class

        assert opened == [], (
            f"entity writes opened {len(opened)} connection(s) rather than "
            "taking one from the pool: the documented pool size, and the "
            "connection budget derived from it, no longer bound what one "
            "instance costs"
        )

    def test_the_migration_is_not_re_run_on_every_call(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        issued = _statements_issued(lambda: backend.upsert_node(node_payload("a")))
        texts = [_rendered(q).upper() for q, _ in issued]

        assert not [t for t in texts if "PG_NAMESPACE" in t or "PG_CLASS" in t], (
            "an entity write re-ran the migration's catalog probes, so the "
            "memo is not doing its job"
        )
        # The key is a *parameter*, never part of the statement text: the
        # backend issues `pg_advisory_xact_lock(%s)`. Looking for it in the
        # rendered SQL is a search that cannot succeed, so the assertion
        # would hold no matter what the code did.
        took_it = [
            params
            for _, params in issued
            if isinstance(params, (tuple, list)) and MIGRATION_LOCK_KEY in params
        ]
        assert took_it == [], (
            "an entity write re-took the migration advisory lock, which is "
            "global rather than per-schema, so every instance in the "
            "deployment serialises on it"
        )


class TestPostgresBatchesSurviveADeadlock:
    """The retry bound, pinned by injection rather than by hammering.

    A batch holds one row lock per operation until it commits, so opposite
    orderings deadlock - and the order is the caller's, which cannot be
    sorted away: a delete followed by an upsert of one id is not the same
    batch reordered. Left to propagate, the failure would be worse than one
    lost mutation, because GraphStorage answers a failed entity write by
    re-issuing the whole graph: a transient, retryable error would turn into
    the overwrite this slice exists to eliminate.

    Real contention is exercised below, but it cannot pin the bound: two
    threads racing produce a server-side cycle only sometimes, so the
    hammering test passed with the retry removed in 5 runs out of 12 -
    measured, and the same shape as the probabilistic detector removed
    earlier in this work. The two injected cases below pin what the
    hammering cannot: that a deadlock is retried, that exhaustion *raises*
    rather than returning as if it had written, and that the retry is
    bounded at all.
    """

    def test_a_deadlocked_batch_is_retried_and_then_lands(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        real_apply = backend._apply_batch_once
        attempts = []

        def flaky(operations):
            attempts.append(1)
            # The documented count, not `backend.DEADLOCK_RETRIES`: a test
            # that reads the bound off the thing it is pinning passes for
            # every value of it, including zero.
            if len(attempts) <= DOCUMENTED_DEADLOCK_RETRIES:
                raise psycopg.errors.DeadlockDetected("injected")
            return real_apply(operations)

        backend._apply_batch_once = flaky
        backend.apply_batch([EntityOperation.upsert_node(node_payload("a"))])

        assert len(attempts) == DOCUMENTED_DEADLOCK_RETRIES + 1
        assert "a" in by_id(backend.load_graph_data(), "nodes"), (
            "the retry returned without applying the batch"
        )

    @pytest.mark.parametrize("length", [5, 12, 40])
    def test_a_long_batch_interrupted_part_way_lands_nothing(
        self, schema, backends, length
    ):
        """One transaction, whatever the length.

        The contract's atomicity clause drives a two-operation batch
        through the `interrupt_next_append` hook. A backend that opened a
        fresh transaction every few operations - committing the first
        chunk and failing on a later one - would satisfy that clause and
        still leave a partly-applied batch behind, which is G1 broken on
        exactly the length production issues. Several lengths, and the
        failure on the LAST operation, so a chunk boundary anywhere
        earlier has already committed something by the time it happens.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("keep")]))

        real_apply_one = backend._apply_one
        applied = []

        def fail_on_the_last(conn, operation):
            applied.append(operation)
            if len(applied) == length:
                raise RuntimeError("injected, at the end of the batch")
            real_apply_one(conn, operation)

        backend._apply_one = fail_on_the_last
        with pytest.raises(RuntimeError):
            backend.apply_batch(
                [
                    EntityOperation.upsert_node(node_payload(f"a{i}"))
                    for i in range(length)
                ]
            )

        assert len(applied) == length, "the batch stopped somewhere unexpected"
        loaded = backend.load_graph_data()
        assert set(by_id(loaded, "nodes")) == {"keep"}, (
            "a batch that failed part way through left its earlier "
            "operations behind, so it is not one transaction"
        )
        assert by_id(loaded, "edges") == {}

    def test_a_retried_batch_keeps_the_callers_order(self, schema, backends):
        """The invariant `apply_batch` documents and `GraphStorage` relies on.

        Sorting a batch is the obvious way to make deadlocks rarer, and it
        is wrong: a delete after an upsert of one id is not the same batch
        reordered. `GraphStorage.delete_nodes` builds edge deletes before
        node deletes for the same reason - an edge must never outlive an
        endpoint in the store. The first attempt keeps the order whatever
        the retry does, so only a retried batch can show the difference.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        real_apply = backend._apply_batch_once
        real_apply_one = backend._apply_one
        attempts = []
        applied = []

        def flaky(operations):
            attempts.append(1)
            # Twice, not once: a re-sort applied only from the second
            # retry onward survives a single injected deadlock.
            if len(attempts) <= 2:
                raise psycopg.errors.DeadlockDetected("injected")
            return real_apply(operations)

        def record(conn, operation):
            applied.append((operation.action, operation.kind, operation.entity_id))
            real_apply_one(conn, operation)

        backend._apply_batch_once = flaky
        backend._apply_one = record
        ordered = [
            EntityOperation.upsert_node(node_payload("z")),
            EntityOperation.upsert_node(node_payload("a")),
            EntityOperation.upsert_edge(edge_payload("e", "z", "z")),
            EntityOperation.delete_node("a"),
        ]
        backend.apply_batch(ordered)

        assert len(attempts) == 3, "the batch was not retried"
        # The order the surviving attempt APPLIED, not the graph it left.
        # The graph cannot answer this question: operations on distinct
        # ids commute here - there is no foreign key between the two
        # tables - and `sorted` is stable, so same-id operations keep
        # their relative order. A re-sort by id or by kind is therefore
        # invisible in the result, and only one that moves a delete ahead
        # of a same-id upsert would show. Asking what the attempt issued
        # catches every sort key, which is what the docstring claims.
        # `_apply_batch_once` is stubbed for the first two attempts and
        # never reaches the database, so what is recorded here is the
        # third attempt alone.
        assert applied == [(o.action, o.kind, o.entity_id) for o in ordered], (
            "the retry reordered or dropped the caller's batch"
        )

        loaded = backend.load_graph_data()
        assert set(by_id(loaded, "nodes")) == {"z"}
        assert set(by_id(loaded, "edges")) == {"e"}

    def test_a_batch_that_keeps_deadlocking_raises_rather_than_vanishing(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())
        # Several operations, not one: a mutation that swallows exhaustion
        # only for a batch it considers worth retrying walks past the
        # single-operation case, and a batch is what production issues.
        batch = [
            EntityOperation.upsert_node(node_payload("a")),
            EntityOperation.upsert_node(node_payload("b")),
            EntityOperation.upsert_edge(edge_payload("e", "a", "b")),
        ]

        cap = DOCUMENTED_DEADLOCK_RETRIES + 5
        attempts = []

        def always(operations):
            attempts.append(1)
            if len(attempts) > cap:
                # BaseException on purpose: an `except Exception` in the
                # retry loop would swallow this and hang the suite instead
                # of failing it.
                raise _RetryBoundExceeded("apply_batch retried without a bound")
            raise psycopg.errors.DeadlockDetected("injected")

        backend._apply_batch_once = always
        with pytest.raises(psycopg.errors.DeadlockDetected):
            backend.apply_batch(batch)

        assert len(attempts) == DOCUMENTED_DEADLOCK_RETRIES + 1, (
            "exhaustion must propagate after exactly one attempt per retry: "
            "swallowing it tells the caller a mutation was stored when none was"
        )
        # Not an atomicity check: `_apply_batch_once` is stubbed here and
        # never reaches the database, so this can only fail if apply_batch
        # itself writes something outside it.
        loaded = backend.load_graph_data()
        assert by_id(loaded, "nodes") == {} and by_id(loaded, "edges") == {}

    def test_opposite_orderings_under_real_contention_all_land(self, schema, backends):
        """The real path, opportunistically: a cycle may or may not form."""
        first = PostgresGraphPersistenceBackend(DSN, schema=schema)
        second = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([first, second])
        first.save_graph_data(snapshot([node_payload("x"), node_payload("y")]))

        errors = []
        start = threading.Barrier(2)

        def hammer(backend, name, ids):
            try:
                start.wait(timeout=30)
                for _ in range(15):
                    backend.apply_batch(
                        [
                            EntityOperation.upsert_node(node_payload(entity, name=name))
                            for entity in ids
                        ]
                    )
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(
                target=hammer, args=(first, "First", ["x", "y"]), daemon=True
            ),
            threading.Thread(
                target=hammer, args=(second, "Second", ["y", "x"]), daemon=True
            ),
        ]
        deadline = time.monotonic() + 90
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        assert not [t for t in threads if t.is_alive()], "a batch never finished"
        assert errors == [], f"a batch failed rather than retrying: {errors}"
        landed = by_id(first.load_graph_data(), "nodes")
        assert set(landed) == {"x", "y"}


class TestPostgresEntityWritesDoNotSerialiseAgainstEachOther:
    """The lock's *mode*, which nothing else in the suite pins.

    Substituting the exclusive `pg_advisory_xact_lock` for the shared form
    leaves every other test here green: writes that serialise still land,
    and still land last-writer-wins. What it destroys is the reason the
    entity path exists at all - one instance's open batch would make every
    other instance's write wait, whatever entity it touched, so ten
    instances would write no faster than one.

    Parametrised over what the holder holds, because the substitution can
    be made for one kind of write and not another: an exclusive lock taken
    only for edges, or only for multi-operation batches, is invisible to a
    probe that only ever holds a single node upsert open.
    """

    HOLDS = {
        "node": lambda b: b.upsert_node(node_payload("held", name="Held")),
        "edge": lambda b: b.upsert_edge(edge_payload("e", "held", "free")),
        "batch": lambda b: b.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("held", name="Held")),
                EntityOperation.upsert_edge(edge_payload("e", "held", "free")),
            ]
        ),
        # Long, and built rather than written out: a mutation gated just
        # above whatever length happens to be spelled here walks past it.
        "batch_many": lambda b: b.apply_batch(
            [EntityOperation.upsert_node(node_payload("held", name="Held"))]
            + [
                EntityOperation.upsert_edge(edge_payload(f"e{i}", "held", "free"))
                for i in range(40)
            ]
        ),
    }

    @pytest.mark.parametrize("holds", sorted(HOLDS))
    def test_a_second_instance_writes_while_another_batch_is_open(
        self, schema, backends, holds
    ):
        holder = PostgresGraphPersistenceBackend(DSN, schema=schema)
        other = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([holder, other])
        holder.save_graph_data(snapshot([node_payload("held"), node_payload("free")]))
        other.exists()  # migrate before the timing matters

        paused, release = threading.Event(), threading.Event()
        real_apply_one = holder._apply_one

        def hold_open(conn, operation):
            real_apply_one(conn, operation)
            paused.set()
            release.wait(30)

        holder._apply_one = hold_open
        holding = threading.Thread(
            target=lambda: self.HOLDS[holds](holder), daemon=True
        )
        holding.start()
        assert paused.wait(30), "the holder never reached its first operation"

        done = threading.Event()

        def write_elsewhere():
            other.upsert_node(node_payload("free", name="Free"))
            done.set()

        writing = threading.Thread(target=write_elsewhere, daemon=True)
        writing.start()
        landed = done.wait(10)
        # Release before asserting anything. A guard that fails while the
        # holder is still parked leaves it holding a pooled connection, an
        # open transaction and the advisory lock for the rest of its wait,
        # which reddens the next test with an unrelated message.
        still_holding = holding.is_alive()
        release.set()
        holding.join(30)
        writing.join(30)

        assert still_holding, "the holder let go before the probe could run"
        assert landed, (
            f"a write to a different entity waited for an open {holds} write: "
            "the batch lock is exclusive rather than shared, so every "
            "instance's entity writes serialise against every other's"
        )
        assert by_id(other.load_graph_data(), "nodes")["free"]["name"] == "Free"


class TestPostgresEntityWritesLeaveNoLockBehind:
    """`pg_advisory_lock_shared` for `pg_advisory_xact_lock_shared`.

    Five characters apart in the source (`xact_`) and a permanent outage in
    operation: the session-scoped form is never released, so the lock
    stays on the pooled connection for the life of the process and the
    next whole-graph save waits for it forever. Measured. The whole suite
    stays green under that substitution, because no other test does an
    entity write and *then* a save on a different connection.

    Every kind of write, for the same reason as the class above: the
    substitution can be made for edges alone, or for batches alone.
    """

    WRITES = {
        "node": lambda b: b.upsert_node(node_payload("a")),
        "edge": lambda b: b.upsert_edge(edge_payload("e", "a", "a")),
        "batch": lambda b: b.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("a")),
                EntityOperation.upsert_edge(edge_payload("e", "a", "a")),
            ]
        ),
        "batch_many": lambda b: b.apply_batch(
            [EntityOperation.upsert_node(node_payload(f"n{i}")) for i in range(40)]
        ),
    }

    @pytest.mark.parametrize("write", sorted(WRITES))
    def test_no_advisory_lock_survives_an_entity_write(self, schema, backends, write):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        self.WRITES[write](backend)

        with psycopg.connect(DSN) as conn:
            held = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND database = (SELECT oid FROM pg_database"
                " WHERE datname = current_database())"
            ).fetchone()[0]

        assert held == 0, (
            f"{held} advisory lock(s) outlived the {write} write: a "
            "session-scoped lock on a pooled connection blocks the next "
            "whole-graph save for the life of the process"
        )


class TestPostgresEntityWritesAgainstAWholeGraphSave:
    """The interleaving no contract clause reaches, and where a delete broke.

    A save replaces a row rather than updating it, so a `DELETE ... WHERE id`
    that waited on the save's row lock unblocks to find its target tuple dead
    and the replacement outside its own statement snapshot. It removes
    nothing and raises nothing, and the caller is told the entity is gone
    while it is still there. An upsert of an existing id survives the same
    interleaving, because `ON CONFLICT` sees the new row - which is exactly
    why testing only that missed it.

    The second face is the save's: an entity write that inserts an id the
    save is about to insert lands between the save's DELETEs and its
    INSERTs, and the save aborts on the duplicate key. Same missing lock,
    the other party paying.

    An edge delete is the same anomaly on the half the tests did not walk,
    and it is not hypothetical: with the lock skipped for edge-only
    batches, a `delete_edge` here removes nothing and raises nothing.
    """

    def _save_paused_before_commit(
        self, backend, graph, paused, release, errors, holder
    ):
        """Run a whole-graph save, held open after its DELETEs.

        `holder` receives the paused connection's backend pid, so the
        caller can ask the server whether the entity write is blocked on
        *this* save rather than on anything at all.
        """
        real_execute = psycopg.Connection.execute

        def spy(conn, query, *args, **kwargs):
            result = real_execute(conn, query, *args, **kwargs)
            text = " ".join(str(query).split())
            if "DELETE" in text.upper() and "graph_edges" in text:
                holder.append(conn.info.backend_pid)
                paused.set()
                release.wait(30)
            return result

        def run():
            psycopg.Connection.execute = spy
            try:
                backend.save_graph_data(graph)
            except Exception as exc:
                errors.append(f"save: {type(exc).__name__}: {exc}")
            finally:
                psycopg.Connection.execute = real_execute

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert paused.wait(30), "the save never reached its DELETEs"
        return thread

    @pytest.mark.parametrize(
        "operation", ["delete", "upsert", "insert_new", "delete_edge", "batch_many"]
    )
    def test_an_entity_write_committing_after_a_save_is_not_swallowed(
        self, schema, backends, operation
    ):
        saver = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([saver, writer])
        saver.save_graph_data(
            snapshot(
                [node_payload("a"), node_payload("b")], [edge_payload("e", "a", "b")]
            )
        )
        writer.exists()  # migrate before the timing matters

        errors = []
        holder = []
        paused, release = threading.Event(), threading.Event()
        saving = self._save_paused_before_commit(
            saver,
            snapshot(
                [node_payload("a", name="Saved"), node_payload("b"), node_payload("c")],
                [edge_payload("e", "a", "b")],
            ),
            paused,
            release,
            errors,
            holder,
        )
        assert holder, "the save never reported the connection it paused on"

        def write():
            try:
                if operation == "delete":
                    writer.delete_node("a")
                elif operation == "upsert":
                    writer.upsert_node(node_payload("a", name="Written"))
                elif operation == "delete_edge":
                    writer.delete_edge("e")
                elif operation == "batch_many":
                    # The length production issues, and long enough that a
                    # save lock skipped above some threshold has nowhere
                    # plausible to hide. The filler ids are absent, which
                    # is not an error and keeps the assertion about the
                    # three that matter.
                    writer.apply_batch(
                        [
                            EntityOperation.delete_edge("e"),
                            EntityOperation.delete_node("a"),
                            EntityOperation.delete_node("b"),
                        ]
                        + [EntityOperation.delete_node(f"absent{i}") for i in range(40)]
                    )
                else:
                    # An id the paused save is about to insert: without the
                    # lock this lands first and the save dies on the key.
                    writer.upsert_node(node_payload("c", name="Written"))
            except Exception as exc:
                errors.append(f"write: {type(exc).__name__}: {exc}")

        writing = threading.Thread(target=write, daemon=True)
        writing.start()
        # Asked of the server rather than guessed at with a sleep, and
        # asked about *this* save: a fixed wait plus is_alive() cannot tell
        # "blocked behind the save" from "has not got there yet", and an
        # unattributed "is anything waiting" is satisfied by any stray
        # blocked session in the database. Both together are stronger than
        # either: the writer is still running AND this save is what it is
        # waiting for.
        blocked = _wait_until_blocking(holder[0]) and writing.is_alive()
        release.set()
        saving.join(30)
        writing.join(30)

        assert not saving.is_alive() and not writing.is_alive(), (
            "a thread never finished"
        )
        assert blocked, (
            "the entity write never waited for the save, so this proves "
            "nothing about the interleaving"
        )
        assert errors == [], f"a writer failed: {errors}"

        loaded = writer.load_graph_data()
        nodes, edges = by_id(loaded, "nodes"), by_id(loaded, "edges")
        if operation == "delete":
            assert "a" not in nodes, (
                "a delete that committed after the save was swallowed: the "
                "caller was told the node was gone and it is still there"
            )
        elif operation == "upsert":
            assert nodes["a"]["name"] == "Written", (
                "an upsert that committed after the save was swallowed"
            )
        elif operation == "delete_edge":
            assert "e" not in edges, (
                "an edge delete that committed after the save was swallowed: "
                "the caller was told the edge was gone and it is still there"
            )
        elif operation == "batch_many":
            assert not ({"a", "b"} & set(nodes)) and "e" not in edges, (
                "a multi-operation batch that committed after the save was "
                "swallowed: the caller was told the entities were gone and "
                f"they are still there (nodes {sorted(nodes)}, "
                f"edges {sorted(edges)})"
            )
        else:
            assert nodes["a"]["name"] == "Saved", "the save did not land whole"
            assert nodes["c"]["name"] == "Written", (
                "an insert of an id the save also wrote was swallowed"
            )


class TestPostgresInstancesStayInSync:
    """Two long-lived instances, which is the whole point of a shared store.

    Every other cross-instance read in the suite happens on a *fresh*
    object's first load, so an instance that cached its first result and
    never looked again would pass all of them - including the tearing test,
    because a frozen reader is maximally self-consistent. That is not a
    hypothetical shape: caching a load is an obvious optimisation, and it
    would silently turn a shared store back into a private one.
    """

    def test_a_reader_sees_a_later_save_by_another_instance(self, schema, backends):
        reader = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([reader, writer])

        writer.save_graph_data(snapshot([node_payload("first")]))
        assert [n["id"] for n in reader.load_graph_data()["nodes"]] == ["first"]

        writer.save_graph_data(snapshot([node_payload("second")]))
        # The same reader object, not a new one.
        assert [n["id"] for n in reader.load_graph_data()["nodes"]] == ["second"]

        writer.save_graph_data(snapshot())
        assert reader.load_graph_data()["nodes"] == []


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
        jsonb_calls = []
        first_statement_at = None
        clock = itertools.count()
        real_execute = psycopg.Connection.execute
        real_many = psycopg.Cursor.executemany
        real_jsonb = psycopg.types.json.Jsonb

        def note(query):
            nonlocal first_statement_at
            tick = next(clock)
            if first_statement_at is None:
                first_statement_at = tick
            statements.append(" ".join(str(query).split()))

        def spy_execute(conn, query, *args, **kwargs):
            note(query)
            return real_execute(conn, query, *args, **kwargs)

        def spy_many(cur, query, *args, **kwargs):
            note(query)
            return real_many(cur, query, *args, **kwargs)

        def spy_jsonb(value):
            jsonb_calls.append(next(clock))
            return real_jsonb(value)

        psycopg.Connection.execute = spy_execute
        psycopg.Cursor.executemany = spy_many
        psycopg.types.json.Jsonb = spy_jsonb
        try:
            backend.save_graph_data(snapshot([node_payload("a")]))
        finally:
            psycopg.Connection.execute = real_execute
            psycopg.Cursor.executemany = real_many
            psycopg.types.json.Jsonb = real_jsonb

        writes = [q for q in statements if "advisory" not in q.lower()]
        assert writes, "the save issued no statements"
        # Where the payload is serialised, not just where the statements go.
        # Hoisting the Jsonb() construction above the transaction - an
        # ordinary "build the parameter lists up front" refactor that changes
        # no SQL and no statement order - moves both hooks outside the
        # connection entirely. Measured: the interrupted save then issues
        # zero statements, so the atomicity test becomes an assertion that
        # cannot fail.
        assert jsonb_calls, "no payload was serialised"
        assert first_statement_at is not None, "the save issued no statements"
        assert min(jsonb_calls) > first_statement_at, (
            "a payload was serialised before the save opened its transaction, "
            "which moves the interrupt and stall hooks outside it"
        )
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
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT c.relname, a.attname FROM pg_constraint k"
                    " JOIN pg_class c ON c.oid = k.conrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " JOIN pg_attribute a"
                    "   ON a.attrelid = c.oid AND a.attnum = ANY(k.conkey)"
                    " WHERE n.nspname = %s AND k.contype = 'p'",
                    (schema,),
                )
            }
        # Which column, not merely that some key exists: a surrogate key
        # added beside `id` would satisfy "a primary key is present" while
        # removing the uniqueness that turns a lost save lock into a loud
        # error, and would diverge from the DDL the document publishes.
        assert keyed == {
            ("graph_nodes", "id"),
            ("graph_edges", "id"),
            ("graph_metadata", "only_row"),
        }

        # And the columns themselves. An operator provisions from the DDL in
        # docs/PERSISTENCE_BACKENDS.md; a migration that quietly built
        # `varchar(80)` or `json` would leave the two shapes different while
        # every key assertion above still passed - and a long node id could
        # then never be saved.
        with psycopg.connect(DSN, autocommit=True) as conn:
            columns = {
                (row[0], row[1], row[2], row[3])
                for row in conn.execute(
                    "SELECT table_name, column_name, data_type, is_nullable"
                    " FROM information_schema.columns WHERE table_schema = %s",
                    (schema,),
                )
            }
        assert columns == {
            ("graph_nodes", "id", "text", "NO"),
            ("graph_nodes", "doc", "jsonb", "NO"),
            ("graph_edges", "id", "text", "NO"),
            ("graph_edges", "doc", "jsonb", "NO"),
            ("graph_metadata", "only_row", "boolean", "NO"),
            ("graph_metadata", "doc", "jsonb", "NO"),
        }


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
