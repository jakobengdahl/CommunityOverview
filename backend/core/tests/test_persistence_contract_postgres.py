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
import json
import os
import secrets
import threading
import time
import uuid

import pytest

from backend.core.tests.persistence_contract import (
    PersistenceBackendContract,
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)


def _env_flag(name: str) -> bool:
    """Whether `name` is set to a value that means "on".

    Docs and CI both write "1", but matching only that exact string means a
    developer's own habit - "true", "yes" - silently reads as unset instead
    of raising the loud error this variable exists to produce, which is a
    worse failure than the one it guards against.
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Read before the import guard below, not after: `importorskip` would
# otherwise skip the whole module for a missing driver before anything got
# to ask whether a skip is acceptable here - the same silent green this
# variable exists to prevent, reached by the other door.
REQUIRE = _env_flag("CO_REQUIRE_POSTGRES")

if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from psycopg_pool import ConnectionPool  # noqa: E402  (after importorskip)

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    DEFAULT_POOL_SIZE,
    MIGRATION_LOCK_KEY,
    NOTIFY_PAYLOAD_LIMIT,
    PostgresGraphPersistenceBackend,
    _channel_for,
)
from backend.core.storage import GraphStorage  # noqa: E402
from backend.core.storage_backends import ExternalChangeRefused  # noqa: E402
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

# The window the reconnect-pacing test watches, and the count above which it
# calls the loop unpaced. Wide on purpose: the property is the difference
# between a paced loop and an unpaced one - two orders of magnitude - not the
# exact schedule, which depends on how fast the killer thread gets scheduled.
_MEASURE_SECONDS = 5.0
_UNPACED_FLOOR = 30


class _RetryBoundExceeded(BaseException):
    """Raised by a test when a bounded retry loop turns out unbounded."""


def _dbname() -> str:
    return psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", "postgres")


def _dsn_as_role(user: str, password: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["user"] = user
    parts["password"] = password
    return psycopg.conninfo.make_conninfo(**parts)


def _redacted_dsn(dsn: str) -> str:
    """`dsn` with its password removed, for a message that reaches a log.

    CI already prints this DSN in plaintext elsewhere (ci.yml), so this is
    hygiene rather than a leak - but an error message manufactured here is
    not the place to make that worse.
    """
    try:
        parts = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:
        return dsn
    if parts.get("password"):
        parts["password"] = "***"
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
                f"CO_REQUIRE_POSTGRES=1 but the server at {_redacted_dsn(DSN)} is "
                f"unreachable: {type(exc).__name__}: {exc}"
            ) from exc
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason=(
        "set CO_TEST_POSTGRES_DSN to a PostgreSQL server to run these"
        if not DSN
        else f"no PostgreSQL server reachable at CO_TEST_POSTGRES_DSN ({_redacted_dsn(DSN)})"
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


def _wait_until_blocking(pid, timeout=15.0, count=1):
    """Wait until `count` distinct sessions are blocked on the session `pid`.

    Attributed on purpose. A fixed sleep plus `thread.is_alive()` cannot
    tell "blocked on the lock" from "has not reached it yet", so under
    load it passes while the interleaving never happened. Asking only
    whether *something* in this database waits on a lock replaces that
    weakness with a worse one: any unrelated blocked session satisfies
    it, including one no test created - two suite runs against one
    database, or a developer with a psql parked in an open transaction.

    `pg_blocking_pids` names the sessions doing the blocking, so the
    caller can ask about the one it holds open and can vouch for. `count`
    above 1 is for a caller that needs to tell "a second waiter joined"
    apart from "the first one is still there": the first blocking already
    satisfies count=1 on its own, so re-calling with the default would
    return immediately without the second ever having been checked.
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
            if waiting >= count:
                return True
            threading.Event().wait(0.05)
    return False


class _ObservableBackend(PostgresGraphPersistenceBackend):
    """The shipped backend, with the changes it applied recorded.

    `settle_notifications` needs a barrier: it must return only once every
    instance on this store has *applied* what the writer just wrote. Nothing
    in the transport answers that. The server hands a notification to the
    client library, which buffers it, long before the application sees it, so
    a quiet server queue proves nothing - and polling the reader's graph until
    it agrees would make every clause below assert its own precondition.

    So the count is taken at the one place the application actually is: the
    call into the listener. Only that callable is wrapped. The connection, the
    channel, the payload, the read-back and the thread are the shipped ones,
    and a defect in any of them fails these clauses exactly as it would fail
    an instance in production. Every class below it drives the unwrapped
    class directly, for the same reason.

    Recording a report normally costs no read of its own: the content is
    asked for after the listener has already asked, and a report is read at
    most once. Where the listener declined to ask - a failed local write, or a
    report refused as coming from a writing thread - the recording is the
    first read, and is made after that listener returned.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []
        self._applied_change = threading.Condition()

    def start_change_notification(self, listener) -> None:
        def recording(change):
            try:
                listener(change)
            finally:
                # Recorded with its content in hand. The shipped backend hands
                # over a change whose content is read when the application
                # asks - after it has settled its own writes - so a report
                # observed at delivery names nothing yet. Asked for AFTER the
                # listener has had it, and a report is read at most once, so
                # this returns the listener's own answer rather than making a
                # second read the shipped backend would not make.
                with self._applied_change:
                    self.seen.append(change.with_content())
                    self._applied_change.notify_all()

        super().start_change_notification(recording)

    def wait_for_barrier(self, after: int, timeout: float = 60.0) -> None:
        """Wait for the barrier change itself, never for a count.

        A count read before the barrier was issued is not a mark. The writes
        being waited for may not have been applied yet when it was taken, so
        `mark + 1` is satisfied by the first of them rather than by the
        barrier behind them - and the clause then asserts against a model that
        has applied some of the writes it is about. Measured: the external
        delete clause passed on its own and failed under the whole module's
        load, which is the load that decides how much of the backlog has
        drained by the time the mark is read.

        So the barrier is identified by what it is rather than by where it
        falls: the only change that carries no operations.
        """
        with self._applied_change:
            if not self._applied_change.wait_for(
                lambda: any(change.operations == () for change in self.seen[after:]),
                timeout=timeout,
            ):
                raise AssertionError(
                    f"the barrier was not applied in {timeout}s; "
                    f"{len(self.seen) - after} changes arrived after the mark"
                )


class TestPostgresBackendContract(PersistenceBackendContract):
    @pytest.fixture
    def factory(self, schema, backends):
        def make():
            backend = _ObservableBackend(DSN, schema=schema)
            backends.append(backend)
            return backend

        return make

    @pytest.fixture(autouse=True)
    def _made(self, backends):
        self._backends = backends

    def settle_notifications(self, backend) -> None:
        """Wait for every instance to have applied what `backend` wrote.

        The barrier is an empty batch through the ordinary write path, not a
        crafted notification: the server delivers on one connection in the
        order the transactions committed, so an instance that has applied the
        barrier has applied everything announced before it. Nothing about the
        barrier is special-cased in the backend, which is the point - a
        transport that dropped the barrier would drop the writes too.
        """
        listening = [
            other
            for other in self._backends
            if other is not backend and other._listen_thread is not None
        ]
        marks = [len(other.seen) for other in listening]
        backend.apply_batch([])
        for other, mark in zip(listening, marks):
            other.wait_for_barrier(mark)

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
        second_returned = threading.Event()

        def use(done: threading.Event | None = None) -> None:
            try:
                backend.exists()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                if done is not None:
                    done.set()

        try:
            blocker.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            first = threading.Thread(target=use, daemon=True)
            first.start()
            # Asked of the server, not guessed at with a sleep: a wait long
            # enough on a fast run is not long enough on a slow one, and too
            # short here starts the second thread before the first has even
            # reached the lock - which would not exercise the memo-set-too-
            # early bug this test is for.
            assert _wait_until_blocking(blocker.info.backend_pid), (
                "the first thread never blocked on the migration lock"
            )
            # The first thread now holds `backend._migrate_lock` - it only
            # reaches the advisory-lock wait below after acquiring it - and
            # `_ensure_schema` holds that Python lock across its whole
            # critical section, including the wait inside Postgres. So a
            # second thread calling `exists()` from here blocks on that
            # Python lock, in pure Python, before it ever opens a connection
            # of its own: it can never become a second blocked backend in
            # `pg_stat_activity`, which is why that is the wrong place to
            # look for it - the assertion below checks the Python-level
            # exclusion directly instead.
            second = threading.Thread(target=use, args=(second_returned,), daemon=True)
            second.start()
            # A memo set on the way in (`self._migrated = True` before the
            # tables actually exist) would let this thread sail past the
            # unlocked fast-path check and return almost immediately,
            # instead of blocking on `_migrate_lock` until the migration
            # finishes. Confirming it has NOT returned yet is what actually
            # exercises that bug.
            assert not second_returned.wait(timeout=1), (
                "the second thread's exists() call returned before the "
                "migration finished - it may have taken the memo's "
                "lock-free fast path instead of waiting on the migrate lock"
            )
            # Confirm the first thread is still genuinely blocked, rather
            # than assumed to still be: the wait above proves nothing about
            # ordering if the first thread had somehow already finished.
            assert _wait_until_blocking(blocker.info.backend_pid), (
                "the first thread stopped blocking on the migration lock "
                "while the second thread was still waiting on it"
            )
            blocker.rollback()  # releases the transaction-scoped lock
            first.join(30)
            second.join(30)
        finally:
            blocker.close()

        assert not first.is_alive() and not second.is_alive(), (
            "a thread never finished - a migration that hangs rather than "
            "raises would otherwise pass here"
        )
        assert second_returned.is_set(), (
            "the second thread's exists() call never returned once the "
            "migration lock was released"
        )
        assert errors == [], (
            f"a thread reached the tables before the migration created them: {errors}"
        )


class TestPostgresMigrationToleratesPartialProvisioning:
    """An operator who provisioned some of the tables, not all three.

    Not a concurrency case - a single instance against a schema someone
    else set up by hand - so it lives apart from the boot-race class above
    rather than inside it.
    """

    def test_a_partly_provisioned_schema_gets_the_rest(self, schema, backends):
        """Every other existing-schema case here provisions all three
        tables together, so a migration that checked one table and assumed
        the others would look identical. It is not: the instance boots,
        then dies on the first statement touching a table nobody created.
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

    @pytest.fixture(autouse=True)
    def _fresh_stall_errors(self):
        # An instance attribute set here, not a mutable class-level list: a
        # class attribute default is shared by every test method that never
        # reassigns it, so a second test added to this class would silently
        # inherit whatever the first one appended.
        self.stall_errors = []

    def _stalled_save(self, backend, nodes, released, monkeypatch):
        """Save from `backend`, holding its transaction open until released.

        Returns the thread and the backend pid of the connection it
        stalled on, so a caller can confirm a second writer is actually
        blocked on *this* save's lock rather than guessing with a fixed
        wait.

        `monkeypatch`, not a bare try/finally on this daemon thread: the
        save can sit inside `released.wait(30)` for up to 30s, so a
        failure in the OUTER test before `released` is ever set would
        otherwise leave the global patch live under that wait, into
        whatever test runs next. Restoring at THIS test's teardown -
        whichever thread is still running - is what monkeypatch buys here
        that a plain `finally` inside `run()` does not.
        """
        import backend.core.postgres_backend as module

        real = module.psycopg.types.json.Jsonb
        real_execute = module.psycopg.Connection.execute
        seen = threading.Event()
        holder = []

        def spy_execute(conn, query, *args, **kwargs):
            # The save's own lock statement, so `holder` names the session
            # actually holding the save open rather than some other
            # connection the backend happens to use.
            if "hashtext" in str(query).lower() and not holder:
                holder.append(conn.info.backend_pid)
            return real_execute(conn, query, *args, **kwargs)

        def stalling(value):
            if isinstance(value, dict) and "id" not in value:  # the metadata row
                seen.set()
                released.wait(30)
            return real(value)

        monkeypatch.setattr(module.psycopg.Connection, "execute", spy_execute)
        monkeypatch.setattr(module.psycopg.types.json, "Jsonb", stalling)

        def run():
            try:
                backend.save_graph_data(snapshot(nodes))
            except Exception as exc:
                # Without this a G7 violation landing on the FIRST writer is
                # silent: the thread dies raising, is_alive() is satisfied,
                # and only a pytest warning records it.
                self.stall_errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert seen.wait(30), "the stalled save never reached its metadata write"
        assert holder, "the stalled save never issued its own advisory lock statement"
        return thread, holder[0]

    def test_overlapping_saves_leave_one_writers_graph(
        self, schema, backends, monkeypatch
    ):
        # Different graph_name, same store: this falsifies a lock keyed on
        # graph_name specifically, which two instances of one graph need
        # not agree on. It does not prove the lock is keyed on the schema
        # - a lock keyed on a bare constant would pass this too, and
        # closing that needs a timing assertion against a second schema,
        # not attempted here.
        first = PostgresGraphPersistenceBackend(DSN, schema=schema, graph_name="a")
        second = PostgresGraphPersistenceBackend(DSN, schema=schema, graph_name="b")
        backends.extend([first, second])
        first.save_graph_data(snapshot([node_payload("seed")]))

        released = threading.Event()
        stalled, holder_pid = self._stalled_save(
            first,
            [node_payload("shared"), node_payload("only_first")],
            released,
            monkeypatch,
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
        # Asked of the server rather than guessed at with a fixed wait: a
        # wait long enough on a fast run degrades into two sequential
        # saves on a slow one, which is a vacuous pass, not a flake - the
        # interleaving this test is for never happened, and nothing said
        # so. `_wait_until_blocking` confirms the second save actually
        # reached the lock and is waiting on THIS save specifically.
        blocked = _wait_until_blocking(holder_pid) and other.is_alive()
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
        assert blocked, (
            "the second save never waited on the first save's lock, so "
            "this proves nothing about the interleaving"
        )
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

        # One of the two failures the module's own docstring names as
        # actually reachable from a graph, not one `json.dumps` rejects
        # client-side: Python writes bare NaN and reads it back, so this
        # value reaches the server and is rejected there, which is the
        # rollback path this test is for. A `datetime` would raise a
        # TypeError before any statement was ever sent, testing nothing
        # about the transaction at all.
        doomed = node_payload("doomed")
        doomed["metadata"] = {"score": float("nan")}
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
        self, schema, backends, monkeypatch
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

        # monkeypatch, not a manual save/restore: it undoes the patch at
        # this test's teardown regardless of how the test exits, where a
        # bare try/finally only restores once this function's own body
        # returns.
        monkeypatch.setattr(psycopg.Connection, "execute", spy)
        backend.save_graph_data(snapshot([node_payload("a")]))

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

    def test_the_declaration_names_all_three_capabilities(self, schema, backends):
        """Equality, not three flag reads: a capability added to the dataclass
        and left undeclared here would pass every `is True` in the file."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert backend.capabilities() == BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
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

    Cases below run for edges as well as nodes, and for multi-operation
    batches as well as the single-operation wrappers; each says which in
    its own parametrisation, and the narrower ones say so in their names.
    That breadth is not symmetry for its own sake: `GraphStorage._do_apply`
    reaches `apply_batch` only
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

    # Several lengths, and no claim that this pins a property: a
    # threshold gate can still sit in a gap between them, or above them.
    # Closing that family would need one shared length source, randomised
    # per run, driving every length-sensitive test; that is recorded as
    # follow-up rather than done here.
    #
    # `GraphStorage.delete_nodes` builds one edge delete per edge plus one
    # node delete per node, so the length is whatever the caller deleted.
    # These three are chosen for what each exercises, not for being new to
    # the module - none of them is:
    #
    # - 2 truncates the cycle below, which is the point of running it;
    # - 7 is the only mid-length under the cost and blast-radius
    #   assertions - the module's other seven-operation batch is a
    #   contract clause that checks the resulting graph and not the
    #   statements;
    # - 41 is the lock probe's holder length, so that length is also
    #   reached by a test asking a different question: about the lock's
    #   mode rather than about what a write costs.
    #
    # Why this comment says so little about which lengths the rest of the
    # module drives: counts of that here have repeatedly gone wrong or
    # gone stale. The measurements live in the branch history, where a
    # reader can check one against the commit that made it; a comment
    # that restates them has to be re-verified on every edit.
    BATCH_LENGTHS = (2, 7, 41)

    # Runs of two, cycling edge-delete, node-delete, edge-upsert,
    # node-upsert. Runs rather than strict alternation because
    # `GraphStorage.delete_nodes` emits every edge delete and then every
    # node delete, so consecutive same-table operations are the ordinary
    # shape - and a backend that merged adjacent ones into a single
    # statement would be invisible to a batch that never has two in a row.
    # Both actions appear from length 5; a shorter length truncates the
    # cycle, which is what makes 2 worth running as well as 41.
    _RUN = 2

    @classmethod
    def _long_batch(cls, length):
        """A cycle of both kinds and both actions, in runs of two, cut off
        at a caller-chosen length - so a short length gets only the start
        of it, which is what the comment above says 2 is for.
        """
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
        index N, is invisible to any single fixed length, which is what
        each case above is. This one asks the same of a whole batch at
        each of several: one writing statement per operation, naming that
        operation's own table, in the caller's order, and no plan that
        scans.
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
        inserted from another connection immediately before the first
        writing statement runs - which is where a read-modify-write has
        already read and has not yet written.

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
    divided by the instance count; the migration is memoised so a
    boot-time advisory lock is not re-taken on every call; and a
    connection is given back whether the batch succeeded or failed. Each
    is load-bearing for the multi-instance case and none of them was
    pinned: a write path opening its own connection, a memo that never
    sets, or a connection kept after a failure, leaves every functional
    test green while quietly turning the connection budget into a
    fiction - the last of them by emptying the pool one bad payload at a
    time.
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
        # The descriptor out of the class dict, not the bound method that
        # attribute access returns: assigning the latter back would leave
        # `Connection.connect` a plain bound method for the rest of the
        # process, so a subclass would silently get `Connection` objects.
        real_class_attr = psycopg.Connection.__dict__["connect"]
        real_class = psycopg.Connection.connect

        def spy_module(conninfo="", **kwargs):
            opened.append(("psycopg.connect", conninfo))
            return real_module(conninfo, **kwargs)

        def spy_class(conninfo="", **kwargs):
            opened.append(("Connection.connect", conninfo))
            return real_class(conninfo, **kwargs)

        pools = []
        real_pool_init = ConnectionPool.__init__

        def spy_pool(self, *args, **kwargs):
            pools.append(args[0] if args else kwargs.get("conninfo"))
            return real_pool_init(self, *args, **kwargs)

        psycopg.connect = spy_module
        psycopg.Connection.connect = spy_class
        ConnectionPool.__init__ = spy_pool
        try:
            for i in range(12):
                backend.upsert_node(node_payload(f"n{i}"))
        finally:
            psycopg.connect = real_module
            psycopg.Connection.connect = real_class_attr
            ConnectionPool.__init__ = real_pool_init

        # A second pool of its own would be warmed by the same write that
        # warms the first, so the connect spies alone cannot see it.
        assert pools == [], (
            f"an entity write built {len(pools)} further connection pool(s); "
            "one instance then costs a multiple of its documented pool size"
        )
        assert opened == [], (
            f"entity writes opened {len(opened)} connection(s) rather than "
            "taking one from the pool, so what one instance costs is no "
            "longer bounded by the pool it declares"
        )
        # The ceiling itself, because the assertions above only watch what
        # is opened. A write that widened the pool instead of going around
        # it would pass them while making the same claim false - and that
        # claim is what the connection budget in the module docstring is
        # derived from.
        assert (backend._pool.max_size, backend._pool.min_size) == (
            DEFAULT_POOL_SIZE,
            0,
        ), (
            "an entity write changed the pool's own bounds "
            f"(max {backend._pool.max_size}, min {backend._pool.min_size}), "
            f"where the backend declared max {DEFAULT_POOL_SIZE} and min 0"
        )

    def test_a_failed_batch_gives_its_connection_back(self, schema, backends):
        """The failure path, which is where a pooled connection is lost.

        Every other test that fails a batch builds a fresh backend and
        never writes through it again, so a connection returned only on
        the success path costs nothing anywhere else in this suite - and wedges a
        real instance permanently: the pool empties one bad payload at a
        time and the next good write blocks for ever. A pool of one makes
        it show up on the first retry rather than the fourth.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        for attempt in range(3):
            with pytest.raises(psycopg.Error):
                backend.apply_batch(
                    [
                        EntityOperation.upsert_node(
                            node_payload(f"bad{attempt}", embedding=[float("nan")])
                        )
                    ]
                )
            # The write after the failure is the whole test: with the
            # connection leaked this blocks until the pool times out.
            backend.upsert_node(node_payload(f"after{attempt}"))

        assert set(by_id(backend.load_graph_data(), "nodes")) == {
            "after0",
            "after1",
            "after2",
        }

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
    earlier in this work. The injected cases below pin what the hammering
    cannot: that a deadlock is retried, that exhaustion *raises* rather
    than returning as if it had written, that the retry is bounded at
    all, that the batch it replays is still the caller's and in the
    caller's order, and that a batch interrupted part way through leaves
    nothing behind whatever its length.
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

        # postgres_backend.py documents that the production bound (pinned at
        # 3 by the injected tests above) can occasionally be exhausted
        # under heavy contention - that is accepted behaviour, not a bug. Two
        # threads is much lighter than the "many instances" load that note is
        # about, but 30 rapid opposite-order batches still hit that rare tail
        # often enough to flake this assertion (observed in CI). Raise just
        # these two instances' retry headroom so the stress test exercises
        # real contention without asserting on the tail of a distribution the
        # production default was never meant to eliminate.
        first.DEADLOCK_RETRIES = 20
        second.DEADLOCK_RETRIES = 20

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
        # Long - 41 operations - because the short holders above cannot
        # see a lock made exclusive only for longer batches. It is still
        # one length: a gate that opens between it and the next-longest
        # holder walks past this too, which is why the follow-up asks for
        # one shared length source rather than more hand-picked values.
        "batch_many": lambda b: b.apply_batch(
            [EntityOperation.upsert_node(node_payload("held", name="Held"))]
            + [
                EntityOperation.upsert_edge(edge_payload(f"e{i}", "held", "free"))
                for i in range(40)
            ]
        ),
        # Delete-leading, and of middling length. Every holder above
        # begins with an upsert, so a lock made exclusive for batches
        # that START with a delete - which is exactly the shape
        # `GraphStorage.delete_nodes` builds, edges first - held open
        # here would have been invisible.
        "batch_deletes_first": lambda b: b.apply_batch(
            [EntityOperation.delete_edge(f"gone{i}") for i in range(8)]
            + [EntityOperation.upsert_node(node_payload("held", name="Held"))]
        ),
        # Deletes and nothing else, which is what `delete_nodes` emits:
        # a lock keyed on "no operation in this batch is an upsert" is
        # invisible to every other holder here, the delete-leading one
        # included, because they all end with an upsert.
        "batch_all_deletes": lambda b: b.apply_batch(
            [EntityOperation.delete_edge(f"gone{i}") for i in range(6)]
            + [EntityOperation.delete_node(f"absent{i}") for i in range(6)]
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
                    # A length production issues. It is ONE length, not a
                    # spread, so it says nothing about thresholds either
                    # side of it: a save lock skipped for batches of 2 to
                    # 42 survives this and the whole suite - measured. An
                    # earlier version of this comment claimed the
                    # opposite, in the same words the length comment on
                    # TestPostgresEntityWritesTouchOneRow had to withdraw.
                    # What this case buys is that the interleaving is
                    # exercised for a batch at all. The filler ids are
                    # absent, which is not an error and keeps the
                    # assertion about the three that matter.
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
        self, schema, backends, monkeypatch
    ):
        # The server's own default, not assumed: a role or database with a
        # non-default `default_transaction_isolation` would otherwise fail
        # this test for nothing having leaked, which is what a hardcoded
        # "read committed" expectation would do.
        with psycopg.connect(DSN) as check:
            baseline = check.execute("SHOW transaction_isolation").fetchone()[0]

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

        # monkeypatch, not a manual save/restore: it undoes the patch at
        # this test's teardown regardless of how the test exits, where a
        # bare try/finally only restores once this function's own body
        # returns.
        monkeypatch.setattr(psycopg.Connection, "execute", spy)
        backend.load_graph_data()

        assert seen == ["repeatable read"], (
            f"the load did not run at REPEATABLE READ: {seen}"
        )

        # The same pooled connection, next transaction: back to whatever it
        # started at.
        with backend._pool.connection() as conn:
            after = conn.execute("SHOW transaction_isolation").fetchone()[0]
        assert after == baseline, (
            f"the isolation level leaked onto the pooled connection: "
            f"{after} != {baseline}"
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
    dict with no id" - the metadata row - to place their hook after every
    row the save writes. Nothing asserted that it *is* the last of them.
    Move the upsert to the front of the transaction and both hooks fire
    before any write: the
    interrupt no longer exercises rollback, and the overlapping-save test
    stops arming at all. Measured: with the upsert moved AND the save
    advisory lock deleted, the whole module still passed - so the guarantee
    the lock exists for was left undefended by an unrelated refactor.
    """

    def test_the_metadata_upsert_is_the_saves_last_write_to_a_graph_table(
        self, schema, backends, monkeypatch
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

        # monkeypatch, not a manual save/restore: it undoes all three
        # patches at this test's teardown regardless of how the test
        # exits, where a bare try/finally only restores once this
        # function's own body returns.
        monkeypatch.setattr(psycopg.Connection, "execute", spy_execute)
        monkeypatch.setattr(psycopg.Cursor, "executemany", spy_many)
        monkeypatch.setattr(psycopg.types.json, "Jsonb", spy_jsonb)
        backend.save_graph_data(snapshot([node_payload("a")]))

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
        # Among the statements that touch a graph table, not among all of
        # them. The save's own last statement is its announcement, which
        # writes no row and is not what either hook keys on - a hook fires
        # while a payload is being serialised, and the announcement carries
        # none. Narrowed when the announcement was added, rather than
        # relaxed: what the hooks depend on is that every row this save
        # writes has been written by the time the metadata upsert runs.
        row_writes = [q for q in writes if _tables_named(q)]
        assert row_writes, "the save wrote no graph table"
        assert "graph_metadata" in row_writes[-1] and "ON CONFLICT" in row_writes[-1], (
            "the metadata upsert is no longer the save's last write to a "
            "graph table, which is what the interrupt and stall hooks rely "
            f"on to land after the writes: {writes}"
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
    def test_every_table_matches_the_documented_ddl(self, schema, backends):
        """The DDL the document publishes for an operator to provision.

        The keys are not decoration. `graph_metadata.only_row` is named in
        the save's ON CONFLICT, so without it every save fails; the entity
        keys are what turn a lost save lock into a loud duplicate-key error
        instead of a silent union of two graphs. `DEFAULT true` and
        `CHECK (only_row)` are checked too - dropping either leaves the
        table shape (and every assertion above it) green, and is harmless
        only because the save always supplies the value explicitly; an
        operator who provisioned from a DDL missing them would not be so
        lucky against a future save that omitted it.
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

        # DEFAULT true and CHECK (only_row): neither is exercised by the
        # key or column assertions above, so a migration that dropped one
        # would still pass every one of them.
        with psycopg.connect(DSN, autocommit=True) as conn:
            default = conn.execute(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = 'graph_metadata'"
                " AND column_name = 'only_row'",
                (schema,),
            ).fetchone()[0]
            checked = conn.execute(
                "SELECT count(*) FROM pg_constraint k"
                " JOIN pg_class c ON c.oid = k.conrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = %s AND c.relname = 'graph_metadata'"
                " AND k.contype = 'c'",
                (schema,),
            ).fetchone()[0]
        assert default is not None and "true" in default.lower(), (
            f"graph_metadata.only_row lost its DEFAULT true: {default!r}"
        )
        assert checked > 0, "graph_metadata lost its CHECK (only_row) constraint"


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


class _Collector:
    """A listener that records what it was handed, for tests about the report.

    Deliberately not a GraphStorage. The contract clauses already drive a real
    one and assert the graph that comes out; what is missing is the shape of
    the report itself - how many changes a batch became, which operations they
    carried and in what order - and a storage answers none of that, because it
    applies the report and discards it.
    """

    def __init__(self):
        self.changes = []
        self.arrivals = []
        self._arrived = threading.Condition()

    def __call__(self, change):
        # This collector is the whole application, so reading the content here
        # is the read a real one makes once it has settled - it has nothing
        # queued to settle.
        change = change.with_content()
        with self._arrived:
            self.changes.append(change)
            self.arrivals.append(time.monotonic())
            self._arrived.notify_all()

    def wait_for(self, count, timeout=30.0):
        with self._arrived:
            if not self._arrived.wait_for(
                lambda: len(self.changes) >= count, timeout=timeout
            ):
                raise AssertionError(
                    f"{len(self.changes)} of {count} changes arrived in {timeout}s"
                )
            return list(self.changes)

    def stays_at(self, count, seconds=2.0):
        """Assert no further change arrives. The only way to test a negative
        here is to wait, so the wait is named and bounded rather than a bare
        sleep."""
        with self._arrived:
            self._arrived.wait_for(lambda: len(self.changes) > count, timeout=seconds)
            assert len(self.changes) == count, (
                f"expected no change beyond {count}, got {len(self.changes)}"
            )


class _ReadingCollector(_Collector):
    """A collector that records what the read raised instead of failing on it.

    `_Collector` reads the content the way a settled application does, which
    is exactly the read the class below makes fail. Left to propagate it would
    be caught by the backend's own generic handler and the test would see an
    absence, which is indistinguishable from a change that never arrived.
    """

    def __init__(self):
        super().__init__()
        self.failures = []

    def __call__(self, change):
        try:
            super().__call__(change)
        except Exception as exc:
            with self._arrived:
                self.failures.append(exc)
                self.changes.append(change)
                self.arrivals.append(time.monotonic())
                self._arrived.notify_all()


@pytest.fixture
def listening(schema, backends):
    """A backend listening on `schema`, plus the collector it reports to.

    Stopped by the fixture rather than by each test, so a test that fails
    mid-way still leaves no thread reading a connection behind it.
    """
    started = []

    def start(**kwargs):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, **kwargs)
        backends.append(backend)
        collector = _Collector()
        backend.start_change_notification(collector)
        started.append(backend)
        return backend, collector

    yield start
    for backend in started:
        backend.stop_change_notification()


class TestPostgresReportsOneChangePerTransaction:
    """The acceptance property of this slice, and the one the batch path in
    GraphStorage was built for: what the store applied together is reported
    together.

    A backend that reported per entity would satisfy every contract clause -
    the graph that comes out is identical - while making the vector index
    rebuild once per node instead of once per batch, and emitting N events
    where the store performed one transaction.
    """

    def test_a_batch_of_many_operations_arrives_as_one_change(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("a")),
                EntityOperation.upsert_node(node_payload("b")),
                EntityOperation.upsert_edge(edge_payload("ab", "a", "b")),
            ]
        )

        (change,) = collector.wait_for(1)
        assert [(op.kind, op.action, op.entity_id) for op in change.operations] == [
            ("node", "upsert", "a"),
            ("node", "upsert", "b"),
            ("edge", "upsert", "ab"),
        ]
        collector.stays_at(1)

    def test_the_reported_order_is_the_order_the_store_applied(
        self, listening, schema, backends
    ):
        """Not cosmetic. GraphStorage drops an external edge whose endpoint is
        not present yet and says so in a warning, so a report that grouped by
        kind - edges first, or nodes read back in one query and edges in
        another - would silently lose every edge created alongside its
        endpoints. Which is the ordinary shape of a create."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("a")),
                EntityOperation.upsert_edge(edge_payload("aa", "a", "a")),
                EntityOperation.upsert_node(node_payload("b")),
                EntityOperation.upsert_edge(edge_payload("ab", "a", "b")),
            ]
        )

        (change,) = collector.wait_for(1)
        assert [op.entity_id for op in change.operations] == ["a", "aa", "b", "ab"]

    def test_a_single_entity_write_arrives_as_one_operation(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.upsert_node(node_payload("solo", name="Solo"))

        (change,) = collector.wait_for(1)
        (op,) = change.operations
        assert (op.kind, op.action, op.entity_id) == ("node", "upsert", "solo")
        assert op.payload["name"] == "Solo"

    def test_a_whole_graph_save_reports_that_it_cannot_say_what_changed(
        self, listening, schema, backends
    ):
        """A save replaced rows it never named, including rows it deleted.
        Naming what changed would mean diffing the store against itself."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.save_graph_data(snapshot([node_payload("z")]))

        (change,) = collector.wait_for(1)
        assert change.operations is None


class TestPostgresDoesNotReportAnInstanceToItself:
    """The server delivers a notification to the connection that sent it as
    readily as to any other - measured, not assumed - so the origin marker is
    load bearing rather than an optimisation.

    Without it every instance re-applies its own writes: harmless-looking,
    because the values agree, but it emits a second event for every mutation
    to every subscriber and every agent, and an agent that answers a change by
    writing would then answer itself.
    """

    def test_an_instance_is_not_told_about_its_own_write(self, listening):
        backend, collector = listening()

        backend.apply_batch([EntityOperation.upsert_node(node_payload("mine"))])

        collector.stays_at(0)
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["mine"]

    def test_an_instance_is_not_told_about_its_own_whole_graph_save(self, listening):
        backend, collector = listening()

        backend.save_graph_data(snapshot([node_payload("mine")]))

        collector.stays_at(0)

    def test_an_instance_is_not_told_about_its_own_oversized_write(self, listening):
        """The degraded announcement carries the origin too.

        `_encode` has two returns that name only the writer, and they are the
        same expression. The whole-graph one is pinned by the case above; this
        one - the batch too large to describe - was pinned by nothing, because
        every oversized write in this file is made by a separate writer. Drop
        the origin from it and a busy instance answers its own large batches
        with a whole-graph reload, on exactly the batches that cost most.
        """
        backend, collector = listening()
        long_id = "n" * 240
        batch = [
            EntityOperation.upsert_node(node_payload(f"{long_id}{i:04d}"))
            for i in range(60)
        ]
        # This case is about the DEGRADED return, and the id length and count
        # that get it there are chosen here rather than derived from the cap.
        # Raise NOTIFY_PAYLOAD_LIMIT - the obvious future change, since the
        # comment beside it tracks a server measurement - and without this
        # assertion the batch quietly stops degrading, the test stops
        # exercising the return it exists for, and becomes a duplicate of the
        # case above it with the suite still green.
        assert json.loads(backend._encode(batch)).get("ops") is None, (
            "this batch no longer takes the degraded return, so the case no "
            "longer covers the second of _encode's two origin markers"
        )

        backend.apply_batch(batch)

        assert len(backend.load_graph_data()["nodes"]) == 60
        collector.stays_at(0)

    def test_two_backends_in_one_process_are_two_instances(
        self, listening, schema, backends
    ):
        """Per object, not per process. Nothing stops one process holding two
        backends on one store, the tests here do it constantly, and an origin
        keyed on the process would make each of them deaf to the other."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.upsert_node(node_payload("theirs"))

        (change,) = collector.wait_for(1)
        assert [op.entity_id for op in change.operations] == ["theirs"]


class TestPostgresAnnouncesOnlyWhatCommitted:
    """The announcement is issued inside the writing transaction, so the
    server holds it until commit and discards it on rollback.

    That is the whole reason there is no bookkeeping here. Announce after the
    commit instead and there is a window in which the write is visible and
    unannounced - and a process that dies in it leaves every other instance
    permanently behind, with nothing to notice it. Announce before, outside
    the transaction, and a rolled-back batch tells everyone about a change
    that never happened.
    """

    def test_a_failed_batch_announces_nothing(self, listening, schema, backends):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        with pytest.raises(Exception):
            writer.apply_batch(
                [
                    EntityOperation.upsert_node(node_payload("ok")),
                    # jsonb cannot hold a non-finite float; the batch dies.
                    EntityOperation.upsert_node(
                        node_payload("bad", embedding=[float("nan")])
                    ),
                ]
            )

        collector.stays_at(0)
        assert writer.load_graph_data()["nodes"] == []

    def test_a_batch_that_fails_after_announcing_announces_nothing(
        self, listening, schema, backends, monkeypatch
    ):
        """The stronger half. Above, the failure happens before the
        announcement is even issued, so it would pass against an
        implementation that announced outside the transaction too. Here the
        announcement reaches the server and the transaction then rolls back,
        which is the property the design actually rests on.
        """
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        real_announce = type(writer)._announce
        real_apply_one = type(writer)._apply_one
        rows_written = []

        def note_row(self, conn, operation):
            rows_written.append(operation)
            return real_apply_one(self, conn, operation)

        def announce_then_die(self, conn, operations):
            real_announce(self, conn, operations)
            raise OSError("connection lost after announcing")

        monkeypatch.setattr(type(writer), "_apply_one", note_row)
        monkeypatch.setattr(type(writer), "_announce", announce_then_die)

        with pytest.raises(OSError):
            writer.apply_batch([EntityOperation.upsert_node(node_payload("ghost"))])

        collector.stays_at(0)
        assert writer.load_graph_data()["nodes"] == []
        # What makes this the stronger half is that a row was written before
        # the announcement was issued and the rollback then took both. Move
        # the announcement to the top of the transaction - which changes
        # nothing about the guarantee, since the server holds it to commit -
        # and this case degenerates into a copy of the weaker one above
        # without saying so.
        assert rows_written, (
            "the announcement was issued before the batch wrote anything, so "
            "the injected failure no longer rolls back a row and this case is "
            "not the stronger half it calls itself"
        )

    def test_a_save_that_fails_after_announcing_announces_nothing(
        self, listening, schema, backends, monkeypatch
    ):
        """The save path's own half of this class, not the batch path's.

        Every other case here drives `apply_batch`, and both paths call the
        same helper - so the save's half of G1 rested entirely on that sharing.
        Replace the save's announcement with one on a connection of its own
        and the whole class still passes, while a listener is told about a
        graph the save had not committed: under READ COMMITTED it reloads,
        sees the graph as it was, and stays there with nothing further coming.
        """
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        writer.save_graph_data(snapshot([node_payload("before")]))
        collector.wait_for(1)
        real_announce = type(writer)._announce

        def announce_then_die(self, conn, operations):
            real_announce(self, conn, operations)
            raise OSError("connection lost after announcing")

        monkeypatch.setattr(type(writer), "_announce", announce_then_die)

        with pytest.raises(OSError):
            writer.save_graph_data(snapshot([node_payload("after")]))

        collector.stays_at(1)
        assert [n["id"] for n in writer.load_graph_data()["nodes"]] == ["before"]

    def test_a_deadlocked_batch_announces_once_from_the_attempt_that_committed(
        self, listening, schema, backends, monkeypatch
    ):
        """A retried batch must not announce per attempt. Announcing outside
        the transaction would send one per try, and every other instance would
        read the same change back from the store two and three times."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        real_announce = type(writer)._announce
        attempts = []

        def announce_then_deadlock(self, conn, operations):
            attempts.append(1)
            # Announce first, then abort - so all three attempts reach the
            # server with a notification and only the last one commits. An
            # attempt that failed before announcing would prove nothing here:
            # it is the discarding of an announcement already sent that this
            # test is about.
            real_announce(self, conn, operations)
            if len(attempts) <= DOCUMENTED_DEADLOCK_RETRIES - 1:
                raise psycopg.errors.DeadlockDetected("injected")

        monkeypatch.setattr(type(writer), "_announce", announce_then_deadlock)

        writer.apply_batch([EntityOperation.upsert_node(node_payload("retried"))])

        assert len(attempts) == DOCUMENTED_DEADLOCK_RETRIES
        (change,) = collector.wait_for(1)
        assert [op.entity_id for op in change.operations] == ["retried"]
        collector.stays_at(1)


class TestPostgresAnnouncementFitsThePayloadLimit:
    """The server caps a NOTIFY payload at 8000 bytes and *raises* past it -
    InvalidParameterValue, inside the writing transaction.

    So the cap cannot be left to the server. An oversized announcement would
    not merely fail to arrive: it would abort the batch it describes, and a
    mutation would be lost to the act of announcing it. Which makes it a
    payload-size bug that presents as data loss, on exactly the large batches
    a busy instance produces.
    """

    # Long enough that a modest batch overruns the limit: ids come from the
    # graph's own data and are not bounded by anything this backend controls.
    LONG_ID = "n" * 240

    def _batch(self, count):
        return [
            EntityOperation.upsert_node(node_payload(f"{self.LONG_ID}{i:04d}"))
            for i in range(count)
        ]

    @pytest.mark.parametrize("count", [1, 2, 7, 20, 32, 33, 40, 200])
    def test_no_announcement_is_ever_too_long_for_the_server(
        self, count, schema, backends
    ):
        """Both halves, over a range that crosses the limit rather than
        approaching it: what `_encode` produces is under the cap, and the
        server accepts it. Asserting only the first would pass against a
        limit constant that is simply wrong, and asserting only the second
        would pass against a `_encode` that never degrades at all - because
        the server is what would raise, and the raise would be the bug.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        payload = backend._encode(self._batch(count))

        assert len(payload.encode("utf-8")) < NOTIFY_PAYLOAD_LIMIT

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (backend._channel, payload))

    def test_a_batch_too_large_to_describe_still_lands(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        batch = self._batch(60)

        writer.apply_batch(batch)

        assert len(writer.load_graph_data()["nodes"]) == 60
        (change,) = collector.wait_for(1)
        assert change.operations is None, (
            "an announcement that cannot name what changed must say so, "
            "not name part of it"
        )

    def test_a_batch_small_enough_to_describe_is_described(
        self, listening, schema, backends
    ):
        """The other side of the same fence. Without it, degrading *every*
        batch to a whole-graph reload would pass the test above and quietly
        undo the entire slice."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        batch = self._batch(4)

        writer.apply_batch(batch)

        (change,) = collector.wait_for(1)
        assert [op.entity_id for op in change.operations] == [
            op.entity_id for op in batch
        ]

    def _largest_described(self, backend):
        """The biggest announcement this backend will ever actually send.

        Found by growing one id a byte at a time until `_encode` stops naming
        the entities, then taking the step before. Measured through the
        behaviour rather than by re-deriving the payload here: a test that
        rebuilt the encoding to measure it would be asserting against its own
        copy of the thing under test, and the first version of this did the
        other wrong thing - it measured `_encode`'s OUTPUT, which is the
        degraded form, so the search never converged.

        The parametrised test above steps by a whole entry, about 250 bytes,
        so it can straddle the limit without ever landing on it. This is what
        finds the byte where it flips.
        """
        base = self._batch(30)
        pad = 0
        described = None
        while True:
            grown = list(base)
            grown[-1] = EntityOperation.upsert_node(
                node_payload(f"{self.LONG_ID}{'p' * pad}")
            )
            payload = backend._encode(grown)
            if json.loads(payload).get("ops") is None:
                assert described is not None, "even the smallest batch degraded"
                return described
            described = payload
            pad += 1
            assert pad < 2000, "the announcement never degraded"

    def test_the_largest_announcement_it_will_send_is_one_the_server_accepts(
        self, schema, backends
    ):
        """The boundary, from the only side that matters.

        The cap is a comparison against a length the server rejects, and off
        by one it is worse than useless: the payload it lets through is
        exactly the one `pg_notify` refuses, inside the transaction the write
        is in, so the batch dies of being described. Asserting the largest
        payload the backend will ever send is one the server takes says that
        without restating the comparison.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)

        payload = self._largest_described(backend)

        assert len(payload.encode("utf-8")) < NOTIFY_PAYLOAD_LIMIT
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (backend._channel, payload))

    def test_a_graph_whose_ids_are_not_ascii_announces_and_reads_back(
        self, listening, schema, backends
    ):
        """Entity ids are graph data, not an ASCII-bounded namespace.

        This is not the byte-versus-character test it looks like it should be.
        `json.dumps` escapes non-ASCII by default, so what `_encode` produces
        is always pure ASCII and the two counts can never differ - measured:
        an a-umlaut becomes a six-character escape sequence in the payload,
        costing six characters and six bytes alike. Writing the cap over the
        encoded length is still right, because bytes are the unit the server's
        own check uses, but it is not currently distinguishable and no test
        can make it so.

        (Not written with the character itself: this docstring is not raw, so
        an escape written here would be collapsed back into the one character
        the sentence is about, and the sentence would read as nonsense.)

        What IS worth pinning, and was covered nowhere: such a graph works.
        The escaping costs six bytes per character, so these ids reach the cap
        six times sooner and this batch announces as a reload rather than by
        name - and the entities still have to arrive.
        """
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        wide = ["\u00e4\u00f6\u5b57" * 60 + f"{i:04d}" for i in range(20)]

        writer.apply_batch(
            [EntityOperation.upsert_node(node_payload(i, name="Wide")) for i in wide]
        )

        assert sorted(by_id(writer.load_graph_data(), "nodes")) == sorted(wide)
        (change,) = collector.wait_for(1)
        assert change.operations is None

    def test_a_non_ascii_id_reads_back_by_name(self, listening, schema, backends):
        """The read-back with a non-ASCII id, which is the half above cannot
        reach: that batch degrades, so nothing looks the ids up in the store.
        A short one stays under the cap and goes through `_resolve`."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        entity_id = "\u00e4\u00f6\u5b57-\u00e9\u00e8"

        writer.upsert_node(node_payload(entity_id, name="Named"))

        (change,) = collector.wait_for(1)
        (op,) = change.operations
        assert (op.action, op.entity_id) == ("upsert", entity_id)
        assert op.payload["name"] == "Named"

    def test_the_limit_is_the_servers_and_is_measured_here(self):
        """The constant restated against the server that enforces it, so a
        version that moved the cap fails here rather than in production.
        `pg_notify` is asked directly: nothing of this backend is involved.
        """
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_notify('co_limit_probe', %s)",
                ("x" * (NOTIFY_PAYLOAD_LIMIT - 1),),
            )
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                conn.execute(
                    "SELECT pg_notify('co_limit_probe', %s)",
                    ("x" * NOTIFY_PAYLOAD_LIMIT,),
                )


class TestPostgresListensOutsideThePool:
    """The listening connection is the instance's, not the pool's.

    LISTEN registers on the session, so a pooled connection would stop
    listening the moment it was returned - and the thread reading it blocks
    for the instance's lifetime, which would hold a pooled connection out of
    circulation for good. At the default pool size that is a quarter of the
    instance's write capacity; at pool_size=1 it is all of it, and the
    instance deadlocks on its first write.
    """

    def test_a_single_connection_pool_can_still_write_while_listening(
        self, listening, schema, backends
    ):
        backend, _ = listening(pool_size=1)

        backend.upsert_node(node_payload("a"))

        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]

    def test_the_listening_connection_is_not_one_of_the_pools(self, listening):
        backend, _ = listening(pool_size=1)

        with backend._pool.connection(timeout=10) as conn:
            pooled = conn.info.backend_pid
        assert backend._listen_conn is not None
        assert backend._listen_conn.info.backend_pid != pooled

    def test_the_listener_runs_with_no_pool_connection_held(self, schema, backends):
        """The deadlock this ordering exists to prevent. A refresh waits for
        the application's write queue, and that queue's writes need this pool.
        Read the content back and keep the connection while calling the
        listener, and the refresh waits for a writer that is waiting for the
        connection the refresh is holding - at pool_size=1, for ever.

        Stands in for the write queue with the pool itself: whether a pool
        connection is available during the listener call is exactly the
        question, and it is asked without needing a GraphStorage.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        outcome = []
        done = threading.Event()

        def listener(change):
            try:
                with backend._pool.connection(timeout=5) as conn:
                    conn.execute("SELECT 1")
                outcome.append("free")
            except Exception as exc:
                outcome.append(f"{type(exc).__name__}: {exc}")
            finally:
                done.set()

        backend.start_change_notification(listener)
        try:
            writer.upsert_node(node_payload("a"))
            assert done.wait(30), "the listener was never called"
        finally:
            backend.stop_change_notification()
        assert outcome == ["free"], (
            "a pool connection was still held while the listener ran"
        )


class TestPostgresStartsListeningBeforeItReturns:
    """A gap between returning and listening loses every write made in it.

    And it is the worst possible gap to have: GraphStorage starts
    notification as the last act of its constructor, so the caller's next
    move is to serve traffic. A write arriving there is announced to a
    connection that is not listening yet and is gone for good - there is no
    catch-up, only the next write.
    """

    def test_a_write_immediately_after_start_is_not_lost(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()

        backend.start_change_notification(collector)
        try:
            # No settling, no sleep: the next statement after start returns.
            writer.upsert_node(node_payload("immediate"))
            (change,) = collector.wait_for(1)
        finally:
            backend.stop_change_notification()
        assert [op.entity_id for op in change.operations] == ["immediate"]

    def test_a_second_start_is_refused_rather_than_leaking_a_thread(self, listening):
        backend, _ = listening()
        with pytest.raises(RuntimeError):
            backend.start_change_notification(_Collector())


class TestPostgresStopsWhenAsked:
    """`stop_change_notification` promises the listener is not called again.

    A storage that has shut down has torn down the executor a refresh would
    wait for, and what it holds is nobody's view any more. Signalling the
    thread without joining it would leave a refresh already inside the
    listener running against exactly that.
    """

    def test_no_change_arrives_after_stop_returns(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()
        backend.start_change_notification(collector)
        writer.upsert_node(node_payload("before"))
        collector.wait_for(1)

        backend.stop_change_notification()
        writer.upsert_node(node_payload("after"))

        collector.stays_at(1)

    def test_stop_waits_for_a_listener_call_already_running(self, schema, backends):
        """The promise, asked directly rather than through a proxy.

        "No change arrives after stop returns" is satisfied by the flag alone,
        because _deliver re-reads it before calling - so a stop that only
        signalled passed that test, and the whole suite, while returning with
        a refresh still running against a model the caller was about to tear
        down. What the docstring promises is about calls in flight, so this
        holds one in flight and watches stop from outside.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        inside = threading.Event()
        release = threading.Event()
        left = threading.Event()

        def slow_listener(change):
            inside.set()
            release.wait(60)
            left.set()

        backend.start_change_notification(slow_listener)
        returned = threading.Event()
        try:
            writer.upsert_node(node_payload("a"))
            assert inside.wait(30), "the listener was never called"

            threading.Thread(
                target=lambda: (backend.stop_change_notification(), returned.set()),
                daemon=True,
            ).start()

            assert not returned.wait(2), (
                "stop returned while a listener call was still running"
            )
            release.set()
            assert returned.wait(60), "stop did not return once the listener left"
            assert left.is_set()
        finally:
            release.set()
            backend.stop_change_notification()

    def test_stopping_releases_the_listening_connection(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.start_change_notification(_Collector())
        pid = backend._listen_conn.info.backend_pid

        backend.stop_change_notification()

        with psycopg.connect(DSN, autocommit=True) as conn:
            assert not conn.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone(), "the listening connection outlived the listener"

    def test_stopping_twice_is_not_an_error(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.start_change_notification(_Collector())
        backend.stop_change_notification()
        backend.stop_change_notification()

    def test_stopping_one_that_never_started_is_not_an_error(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.stop_change_notification()

    def test_close_stops_the_listener(self, schema, backends):
        """A script or a test that only closes the backend would otherwise
        leave a thread reading a connection whose pool is gone."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backend.start_change_notification(_Collector())
        thread = backend._listen_thread

        backend.close()

        assert thread is not None
        thread.join(30)
        assert not thread.is_alive()


class TestPostgresRecoversItsListeningConnection:
    """A dropped listening connection is the quiet failure this capability
    exists to remove, arrived at by another road.

    The instance goes on answering reads, its health check passes, and it
    never hears another write for as long as it runs. Nothing else notices:
    the writers' announcements succeed, because NOTIFY does not care whether
    anyone is listening. So reconnecting is not a nicety here - without it a
    single server restart, failover or idle-connection reaper puts an
    instance permanently and silently out of step.
    """

    @staticmethod
    def _terminate(pid):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))

    def test_a_write_after_the_connection_dies_still_reaches_the_listener(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()
        backend.start_change_notification(collector)
        try:
            self._terminate(backend._listen_conn.info.backend_pid)
            # Announced into the gap or after it - either way the instance
            # must not be left behind.
            writer.upsert_node(node_payload("survivor"))

            changes = collector.wait_for(1, timeout=60)
        finally:
            backend.stop_change_notification()

        assert any(change.operations is None for change in changes), (
            "a reconnect must report that changes may have been missed: "
            "the announcements sent while the connection was gone are not "
            "replayed by the server"
        )
        assert "survivor" in by_id(backend.load_graph_data(), "nodes")

    def test_the_listener_is_still_live_after_a_reconnect(self, schema, backends):
        """Recovery is not one report and then silence: the point is that the
        instance goes on hearing writes."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()
        backend.start_change_notification(collector)
        try:
            self._terminate(backend._listen_conn.info.backend_pid)
            collector.wait_for(1, timeout=60)  # the reconnect's own report
            settled = len(collector.changes)

            writer.upsert_node(node_payload("later"))
            changes = collector.wait_for(settled + 1, timeout=60)
        finally:
            backend.stop_change_notification()

        named = [c for c in changes[settled:] if c.operations is not None]
        assert named, "nothing was reported after the reconnect"
        assert any(op.entity_id == "later" for op in named[-1].operations)

    def test_a_connection_that_never_opens_fails_the_start(self, schema, backends):
        """Boot loudly rather than into permanent staleness. An instance that
        starts without listening looks healthy and is silently wrong, which is
        the whole failure mode above - reached at boot instead of at runtime.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        # Outside the raises block: an assignment that raised would otherwise
        # satisfy it, and the test would pass without calling start at all.
        backend.conninfo = psycopg.conninfo.make_conninfo(
            **{
                **psycopg.conninfo.conninfo_to_dict(DSN),
                "dbname": f"co_absent_{uuid.uuid4().hex[:12]}",
            }
        )
        with pytest.raises(psycopg.OperationalError):
            backend.start_change_notification(_Collector())
        assert backend._listen_thread is None, "a failed start left a thread behind"


class TestPostgresChannelsAreOnePerStore:
    """Two graphs in one database must not hear each other, and the channel
    name is what keeps them apart.

    A channel is an SQL identifier: capped at 63 bytes where a schema name may
    itself be 63, and case-folded unless quoted - so an interpolated name
    would truncate one schema into another's channel, or have `LISTEN` and
    `pg_notify` disagree about which channel a mixed-case schema meant.
    """

    def test_a_write_to_one_schema_is_not_reported_to_another(self, schema, backends):
        other = f"{schema}_other"
        listener_backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=other)
        backends.extend([listener_backend, writer])
        collector = _Collector()
        listener_backend.start_change_notification(collector)
        try:
            writer.upsert_node(node_payload("elsewhere"))
            collector.stays_at(0)
        finally:
            listener_backend.stop_change_notification()
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')

    @pytest.mark.parametrize(
        "name",
        [
            "public",
            "MixedCase",
            "mixedcase",
            "with.a.dot",
            "x" * 63,
            "x" * 62 + "y",
            'quo"te',
        ],
    )
    def test_every_schema_name_yields_a_usable_channel(self, name):
        channel = _channel_for(name)
        assert len(channel.encode("utf-8")) <= 63
        assert channel == channel.lower(), (
            "an unquoted identifier is case-folded, so a channel with upper "
            "case in it is a different channel to LISTEN than to pg_notify"
        )
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(channel))
            )
            conn.execute("SELECT pg_notify(%s, %s)", (channel, "probe"))
            assert [n.payload for n in conn.notifies(timeout=5, stop_after=1)] == [
                "probe"
            ]

    def test_schemas_that_differ_at_all_get_different_channels(self):
        """Including past the point an identifier would have truncated: two
        63-byte schemas differing only in their last byte are two stores."""
        assert _channel_for("x" * 63) != _channel_for("x" * 62 + "y")
        assert _channel_for("MixedCase") != _channel_for("mixedcase")

    def test_the_channel_space_is_wide_enough_not_to_collide(self):
        """Two hard-coded pairs say nothing about a space that is too small.

        G11 is "for any schema name", and a digest shortened to fit some
        future constraint stays lower-case, stays under 63 bytes and still
        round-trips through LISTEN - so every other assertion in this class
        survives it. What does not survive is two tenants in one database:
        they hear each other's writes and each reads the other's ids out of
        its own schema, reporting deletes for entities that exist elsewhere.
        Measured at four hex characters: a collision inside 211 names.
        """
        names = [f"co_tenant_{i}" for i in range(20_000)]
        channels = {_channel_for(name) for name in names}
        assert len(channels) == len(names), (
            f"{len(names) - len(channels)} of {len(names)} schema names share "
            f"a channel with another"
        )


class TestPostgresReadsContentFromTheStore:
    """The announcement names identifiers; the store says what happened to
    them.

    Content cannot travel in the announcement - one node with an inline
    embedding can exceed the server's whole payload allowance - so the
    listener reads it back. That turns out to be the stronger design as well
    as the necessary one: between the commit and the read a third instance may
    have written the same entity again, and the announced action would then
    apply something the store contradicts.
    """

    def test_an_upsert_carries_the_content_that_is_in_the_store(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.upsert_node(
            node_payload("a", name="Alpha", tags=["x"], embedding=[0.5, 0.25])
        )

        (change,) = collector.wait_for(1)
        (op,) = change.operations
        assert op.payload["name"] == "Alpha"
        assert op.payload["tags"] == ["x"]
        assert op.payload["embedding"] == [0.5, 0.25]

    def test_an_entity_the_store_no_longer_holds_is_reported_as_a_delete(
        self, schema, backends
    ):
        """The race the read-back exists to survive. The announcement says an
        entity was written; by the time it is read the entity is gone. Naming
        it as an upsert with no payload would be a report the application
        cannot apply; trusting the announcement would resurrect a row the
        store does not have.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()
        removed = threading.Event()
        real_resolve = type(backend)._resolve

        def resolve_after_a_delete(self, named):
            # Between the announcement and the read, exactly where the race
            # is. Once only: the second call is the delete's own report.
            if not removed.is_set():
                removed.set()
                writer.delete_node("a")
            return real_resolve(self, named)

        backend._resolve = resolve_after_a_delete.__get__(backend)
        backend.start_change_notification(collector)
        try:
            writer.upsert_node(node_payload("a", name="Alpha"))
            (change, *_) = collector.wait_for(1)
        finally:
            backend.stop_change_notification()

        assert [(op.kind, op.action, op.entity_id) for op in change.operations] == [
            ("node", "delete", "a")
        ]

    def test_a_delete_is_reported_without_a_payload(self, listening, schema, backends):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        writer.upsert_node(node_payload("a"))
        collector.wait_for(1)

        writer.delete_node("a")

        changes = collector.wait_for(2)
        (op,) = changes[1].operations
        assert (op.action, op.entity_id, op.payload) == ("delete", "a", None)

    def test_edges_and_nodes_are_read_from_their_own_tables(
        self, listening, schema, backends
    ):
        """One id can name a node and an edge at once. Reading both from one
        table - or matching on the id alone - would report the wrong content
        for one of them, and there is nothing in the payload to catch it."""
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("same", name="TheNode")),
                EntityOperation.upsert_edge(
                    edge_payload("same", "same", "same", label="TheEdge")
                ),
            ]
        )

        (change,) = collector.wait_for(1)
        node_op, edge_op = change.operations
        assert (node_op.kind, node_op.payload["name"]) == ("node", "TheNode")
        assert (edge_op.kind, edge_op.payload["label"]) == ("edge", "TheEdge")


class TestPostgresTreatsAnUnreadableAnnouncementAsAReload:
    """A store is shared with instances that may run a version this one
    predates - that is what a rolling deploy is.

    An announcement this build cannot read is still an announcement. Ignoring
    it would leave the instance stale for exactly as long as the newer one
    keeps writing, and nothing would ever put it right: there is no catch-up,
    only the next announcement, which this build cannot read either.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            "",
            "[]",
            '{"ops": []}',  # no origin: a newer shape, not ours to guess at
            '{"o": "other", "ops": "not a list"}',
            '{"o": "other", "ops": [["x", "a"]]}',  # a kind this build lacks
            '{"o": "other", "ops": [["n", 7]]}',  # an id that is not a string
            '{"o": "other", "ops": [["n"]]}',  # an entry of the wrong shape
            '{"o": "other", "ops": [null]}',
            # A two-character string unpacks as readily as a pair does, so
            # this used to arrive as ("node", "e") - the one malformed shape
            # that produced a confident delete instead of a reload.
            '{"o": "other", "ops": ["ne"]}',
            '{"o": "other", "ops": [["n", "a", "extra"]]}',
            # null is the one non-string psycopg adapts without complaint: it
            # matches nothing and comes back as a delete of None, where every
            # other wrong type dies in the query. So the id's type has to be
            # checked here rather than left to the server.
            '{"o": "other", "ops": [["n", null]]}',
        ],
    )
    def test_an_announcement_this_build_cannot_read_reloads_the_graph(
        self, payload, listening
    ):
        backend, collector = listening()
        before = backend._listen_conn.info.backend_pid

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (backend._channel, payload))

        (change,) = collector.wait_for(1)
        assert change.operations is None
        # The reload must come from reading the announcement, not from the
        # reconnect that follows an exception thrown out of the reading
        # thread. Both end in a reload, so the connection is what tells them
        # apart. Measured against the shape that checked nothing until the
        # read-back: most of these payloads reached this assertion having
        # killed the listening thread, and the reload arrived only as a side
        # effect of reconnecting - which costs the connection, costs the
        # backoff wait a drop now takes, and is reported as a lost connection
        # naming a KeyError. The one that did not crash was worse: an unknown
        # kind read as "edge" looked its id up in the wrong table and
        # reported a confident delete of an entity nothing had announced.
        assert backend._listen_conn is not None
        assert backend._listen_conn.info.backend_pid == before, (
            "the announcement was not read - it crashed the listening thread "
            "and the reload arrived as a side effect of reconnecting"
        )

    def test_an_announcement_with_no_operations_key_reloads_the_graph(self, listening):
        """The shape a whole-graph save sends, arriving from anywhere."""
        backend, collector = listening()

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_notify(%s, %s)",
                (backend._channel, '{"o": "someone-else"}'),
            )

        (change,) = collector.wait_for(1)
        assert change.operations is None

    def test_a_listener_that_raises_does_not_end_the_reporting(self, schema, backends):
        """One bad refresh must not silence the instance for good: the thread
        that dies here is the only one that would ever hear another write."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        seen = _Collector()

        def angry(change):
            seen(change)
            if len(seen.changes) == 1:
                raise RuntimeError("the application refused this one")

        backend.start_change_notification(angry)
        try:
            writer.upsert_node(node_payload("first"))
            seen.wait_for(1)
            writer.upsert_node(node_payload("second"))
            changes = seen.wait_for(2)
        finally:
            backend.stop_change_notification()

        assert [op.entity_id for op in changes[1].operations] == ["second"]


class TestPostgresBacksOffWhenTheConnectionKeepsDropping:
    """A reconnect is not free, and the loop that performs it must be paced.

    Every reconnect reports `unknown()`, which costs each instance a drain of
    its own write queue and a whole-graph reload. A flapping server - a
    failover loop, an idle reaper, a connection limit being hit - therefore
    turns into a reload storm driven by the recovery rather than by the
    fault, and it is the recovering instance that pays.

    The failure this pins is specific and was measured, not imagined: the
    backoff was reset by the fact of having *connected*, which is the one
    thing a flapping server does reliably, so the ceiling was unreachable on
    exactly the failure it was written for. A dropped connection reconnected
    76 times a second, and each of those was a whole-graph reload.
    """

    def test_a_connection_killed_as_fast_as_it_appears_does_not_spin(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        collector = _Collector()
        backend.start_change_notification(collector)
        stop = threading.Event()
        terminations = []

        def keep_killing():
            with psycopg.connect(DSN, autocommit=True) as killer:
                while not stop.is_set():
                    conn = backend._listen_conn
                    if conn is not None:
                        try:
                            killer.execute(
                                "SELECT pg_terminate_backend(%s)",
                                (conn.info.backend_pid,),
                            )
                            terminations.append(1)
                        except Exception:
                            pass  # it went away on its own; try again
                    time.sleep(0.001)

        killer_thread = threading.Thread(target=keep_killing, daemon=True)
        killer_thread.start()
        try:
            time.sleep(_MEASURE_SECONDS)
        finally:
            stop.set()
            killer_thread.join(30)
            backend.stop_change_notification()

        # A ceiling with room to spare rather than a tight bound: the point is
        # the difference between a paced loop and an unpaced one, which is two
        # orders of magnitude, not the exact schedule. Unpaced, this window
        # produced hundreds.
        assert len(collector.changes) <= _UNPACED_FLOOR, (
            f"{len(collector.changes)} reloads in {_MEASURE_SECONDS}s from "
            f"{len(terminations)} terminations: the reconnect is not backing "
            f"off, so every drop costs every instance a whole-graph reload"
        )
        assert terminations, "the killer never caught a listening connection"

        # The count alone pins only half of it. The defect had two halves -
        # the drop path did not wait AT ALL, and the wait it would have used
        # was reset by the mere fact of having connected - and a loop that
        # waits a fixed minimum satisfies the ceiling above while never
        # escalating. Measured: with the escalation removed and the wait
        # kept, this window produced 18 reloads against a floor of 30, so the
        # assertion passed against half the defect it names.
        #
        # The intervals are what tells the two apart: escalating they double,
        # fixed they do not.
        gaps = [b - a for a, b in zip(collector.arrivals, collector.arrivals[1:])]
        assert len(gaps) >= 3, (
            f"only {len(gaps) + 1} reconnects in {_MEASURE_SECONDS}s - too "
            f"few to say anything about how they are spaced"
        )
        assert gaps[-1] >= 2 * gaps[0], (
            f"reconnect intervals {[round(g, 3) for g in gaps]} are not "
            f"growing: the wait is a fixed floor rather than a backoff, so a "
            f"server that keeps dropping is never given room to recover"
        )

    def test_the_backoff_comes_back_down_after_a_connection_that_held(
        self, schema, backends, monkeypatch
    ):
        """Escalating is half of it; coming back down is the other half.

        Without the reset, an instance that flapped once during a failover
        stays at the ceiling for the rest of its life - so the next drop, an
        isolated one years later, costs thirty seconds of staleness for no
        reason. Nothing pinned it: the flapping test asserts the intervals
        grow, which a backoff that only ever grows satisfies perfectly.

        `_NOTIFY_STABLE_SECONDS` is lowered so "a connection that stayed up"
        fits in a test rather than in a minute. That is the constant's whole
        meaning, so lowering it is what makes the case reachable, not what
        makes it pass.
        """
        import backend.core.postgres_backend as module

        monkeypatch.setattr(module, "_NOTIFY_STABLE_SECONDS", 0.5)
        monkeypatch.setattr(module, "NOTIFY_RECONNECT_MAX_SECONDS", 4.0)
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        collector = _Collector()
        backend.start_change_notification(collector)

        def drop():
            # Wait for one to exist: between a drop and the reconnect there is
            # no connection to drop, and the wait is exactly the backoff this
            # test is about.
            deadline = time.monotonic() + 30
            conn = backend._listen_conn
            while conn is None and time.monotonic() < deadline:
                time.sleep(0.01)
                conn = backend._listen_conn
            assert conn is not None, "no listening connection appeared in 30s"
            with psycopg.connect(DSN, autocommit=True) as killer:
                killer.execute(
                    "SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,)
                )

        try:
            # Three drops in quick succession: the backoff escalates.
            for _ in range(3):
                drop()
                time.sleep(0.05)
            collector.wait_for(3, timeout=60)
            escalated = len(collector.changes)

            # Let the next connection hold well past _NOTIFY_STABLE_SECONDS,
            # then drop it once. The reconnect after a connection that held
            # must be prompt again, not at the escalated delay.
            deadline = time.monotonic() + 30
            while backend._listen_conn is None and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(1.5)
            before = time.monotonic()
            drop()
            collector.wait_for(escalated + 1, timeout=60)
            recovered_in = collector.arrivals[escalated] - before
        finally:
            backend.stop_change_notification()

        assert recovered_in < 1.0, (
            f"the reconnect after a connection that stayed up took "
            f"{recovered_in:.2f}s: the backoff never comes back down, so one "
            f"flap costs this instance the ceiling for the rest of its life"
        )

    def test_it_is_still_listening_after_the_flapping_stops(self, schema, backends):
        """Backing off must not mean giving up: an instance that paced itself
        through a failover and then went deaf is worse than one that spun."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _Collector()
        backend.start_change_notification(collector)
        try:
            for _ in range(3):
                conn = backend._listen_conn
                if conn is not None:
                    with psycopg.connect(DSN, autocommit=True) as killer:
                        killer.execute(
                            "SELECT pg_terminate_backend(%s)",
                            (conn.info.backend_pid,),
                        )
                time.sleep(0.05)
            collector.wait_for(1, timeout=60)
            settled = len(collector.changes)

            writer.upsert_node(node_payload("after_the_storm"))
            changes = collector.wait_for(settled + 1, timeout=60)
        finally:
            backend.stop_change_notification()

        named = [c for c in changes[settled:] if c.operations is not None]
        assert named, "nothing was reported once the connection settled"
        assert any(op.entity_id == "after_the_storm" for op in named[-1].operations)


class TestPostgresDoesNotStartASecondListener:
    """A start that fails must not leave a live thread behind it.

    The handle to the listening thread is what the next start consults. Clear
    it while the thread is still running - which a stop whose join timed out
    used to do - and the next start finds nothing running, clears the stop
    flag, and revives the abandoned thread beside the new one. Measured: two
    listening connections, every change delivered to the application twice,
    and an instance costing pool_size + 2 connections against a budget
    written for pool_size + 1.
    """

    def test_a_start_that_times_out_does_not_leave_a_startable_backend(
        self, schema, backends, monkeypatch
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        import backend.core.postgres_backend as module

        released = threading.Event()
        real_connect = module.psycopg.connect

        def hang_once(*args, **kwargs):
            # A server that accepts the connection and never answers: the
            # shape the connect timeout exists for, and the one that made
            # start's own bound expire against a thread still running.
            released.wait(60)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(module, "_LISTEN_START_TIMEOUT", 1.0)
        monkeypatch.setattr(module, "_LISTEN_STOP_TIMEOUT", 1.0)
        monkeypatch.setattr(module.psycopg, "connect", hang_once)

        try:
            with pytest.raises(TimeoutError):
                backend.start_change_notification(_Collector())

            live = [t for t in threading.enumerate() if t.name.startswith("pg-notify")]
            # The thread is still there - that is the situation, not the bug.
            # The bug was the backend claiming to have none.
            with pytest.raises(RuntimeError):
                backend.start_change_notification(_Collector())
            assert len(
                [t for t in threading.enumerate() if t.name.startswith("pg-notify")]
            ) <= len(live), "a second listener was started beside the abandoned one"
            assert backend._listen_stop.is_set(), (
                "a start that failed left the abandoned thread un-signalled: "
                "when the hang clears it will connect, LISTEN, and call a "
                "listener the caller believes was never installed"
            )
        finally:
            released.set()
            backend.stop_change_notification()

    def test_a_start_that_times_out_never_calls_the_listener_it_was_given(
        self, schema, backends, monkeypatch
    ):
        """The other half, and the one a thread count cannot see.

        A start that raises has told its caller no listener is installed. The
        thread it abandoned does not know that: when whatever held it up
        clears, it connects, registers LISTEN and starts delivering to a
        callable the application has already forgotten - into a model that,
        for a GraphStorage, may be half torn down.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        import backend.core.postgres_backend as module

        released = threading.Event()
        real_connect = module.psycopg.connect

        def hang_once(*args, **kwargs):
            released.wait(60)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(module, "_LISTEN_START_TIMEOUT", 1.0)
        monkeypatch.setattr(module, "_LISTEN_STOP_TIMEOUT", 1.0)
        monkeypatch.setattr(module.psycopg, "connect", hang_once)
        forgotten = _Collector()

        try:
            with pytest.raises(TimeoutError):
                backend.start_change_notification(forgotten)
            released.set()
            # Long enough for the abandoned thread to get through connect and
            # LISTEN, if it were going to.
            time.sleep(1.0)
            writer.upsert_node(node_payload("after_the_failed_start"))
            forgotten.stays_at(0)
        finally:
            released.set()
            backend.stop_change_notification()


class TestPostgresSurvivesAReadBackItCannotComplete:
    """The store not answering for an announcement is not the announcement's
    fault, and must cost neither the listening connection nor the change.

    The read-back runs on the pool, which the instance's own writers are
    using: a pool timeout under contention, or a connection dropped between
    the announcement and the read. It is asked for inside the listener now
    rather than before it, so the failure surfaces where the application can
    answer for it - still on the reading thread, since a report is applied
    inline, but no longer on the path that owns the listening connection.

    The change is real whatever the read did, so the application may not drop
    it: it re-reads the whole graph instead. The two halves are asserted
    separately because they are separately breakable - a listener that
    swallowed the failure would keep the connection and lose the change.
    """

    # Not one class, and not one family. Only PoolTimeout and QueryCanceled
    # are OperationalError subclasses; InterfaceError, InsufficientPrivilege
    # and UndefinedTable are not - measured against the installed driver. A
    # containment narrowed to OperationalError therefore looks right, passes a
    # test that injects one, and lets a revoked grant or a dropped socket out
    # of the reading thread. The plain ValueError is there so the case does
    # not quietly become "any driver error": what must survive is anything at
    # all, because whatever it is, the change was real.
    READ_BACK_FAILURES = [
        psycopg.OperationalError("injected: the pool would not answer"),
        psycopg.InterfaceError("injected: the connection was already closed"),
        psycopg.errors.InsufficientPrivilege("injected: SELECT was revoked"),
        psycopg.errors.UndefinedTable("injected: the table went away"),
        ValueError("injected: something no one predicted"),
    ]

    @staticmethod
    def _failing_once(backend, failure):
        """Make the next read-back raise `failure`, and the ones after it work."""
        real_resolve = type(backend)._resolve
        failed = threading.Event()

        def fail_once(self, pairs):
            if not failed.is_set():
                failed.set()
                raise failure
            return real_resolve(self, pairs)

        backend._resolve = fail_once.__get__(backend)

    @pytest.mark.parametrize(
        "failure", READ_BACK_FAILURES, ids=lambda e: type(e).__name__
    )
    def test_a_read_back_that_fails_keeps_the_listener_connected(
        self, failure, schema, backends
    ):
        """The connection the announcements arrive on is not the one the
        content is read on, and a failure on the second must not close the
        first - nor stop the announcements after it."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        collector = _ReadingCollector()
        self._failing_once(backend, failure)

        backend.start_change_notification(collector)
        try:
            before = backend._listen_conn.info.backend_pid
            writer.upsert_node(node_payload("a"))
            collector.wait_for(1)
            assert [type(exc) for exc in collector.failures] == [type(failure)], (
                "the failed read must reach the application, which is the only "
                "party that can turn it into a reload"
            )
            assert backend._listen_conn is not None
            assert backend._listen_conn.info.backend_pid == before, (
                "the failed read-back took the listening connection with it"
            )

            writer.upsert_node(node_payload("b"))
            changes = collector.wait_for(2)
        finally:
            backend.stop_change_notification()

        assert [op.entity_id for op in changes[1].operations] == ["b"]

    @pytest.mark.parametrize(
        "failure", READ_BACK_FAILURES, ids=lambda e: type(e).__name__
    )
    def test_a_read_back_that_fails_reloads_the_graph(
        self, failure, schema, backends, tmp_path, monkeypatch
    ):
        """The change was real, so losing it is not an option. A content read
        this instance could not complete says nothing about what the store
        holds, and the only honest answer left is to read all of it."""
        monkeypatch.chdir(tmp_path)
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        backend.save_graph_data(snapshot())
        storage = GraphStorage(persistence_backend=backend)
        self._failing_once(backend, failure)

        try:
            writer.upsert_node(node_payload("a", name="Alpha"))
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and "a" not in storage.nodes:
                time.sleep(0.05)
        finally:
            storage.shutdown_events()

        assert "a" in storage.nodes, (
            "a read-back that failed dropped the change instead of reloading"
        )
        assert storage.nodes["a"].name == "Alpha"


class TestPostgresReportsAContractViolationDistinctly:
    """`ExternalChangeRefused` means this backend reported on a thread that
    was running a write - its own obligation, stated on ChangeNotifyingBackend
    and impossible to see from the application's side.

    It subclasses RuntimeError, so a generic handler would swallow it into the
    same line as an application bug. The refusal says the refresh did not
    happen and this instance is now behind, which is a different thing to
    report and a different thing to fix.
    """

    def test_a_refusal_is_reported_and_reporting_continues(
        self, schema, backends, capsys
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        seen = _Collector()

        def refusing(change):
            seen(change)
            if len(seen.changes) == 1:
                raise ExternalChangeRefused("reported from a writing thread")

        backend.start_change_notification(refusing)
        try:
            writer.upsert_node(node_payload("first"))
            seen.wait_for(1)
            writer.upsert_node(node_payload("second"))
            changes = seen.wait_for(2)
        finally:
            backend.stop_change_notification()

        assert [op.entity_id for op in changes[1].operations] == ["second"]
        assert "refused" in capsys.readouterr().out


class TestPostgresStopFromInsideTheListener:
    """A listener that closes its own backend cannot be joined by the stop it
    triggered - and that is not the same thing as nothing running.

    Reading it as such cleared the thread handle on the one thread guaranteed
    to still be alive, which is precisely what the handle is kept for: the
    next start would then find nothing running, clear the stop flag, and the
    old thread would fall back into its reconnect branch beside the new one.
    Two listeners, every change delivered twice - the round-1 failure reached
    by another road.
    """

    def test_stopping_from_the_listener_does_not_free_the_handle(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        stopped = threading.Event()
        handle_after = []

        def stop_myself(change):
            backend.stop_change_notification()
            handle_after.append(backend._listen_thread)
            stopped.set()

        backend.start_change_notification(stop_myself)
        try:
            writer.upsert_node(node_payload("a"))
            assert stopped.wait(30), "the listener was never called"
        finally:
            backend.stop_change_notification()

        assert handle_after and handle_after[0] is not None, (
            "stop called from the listener cleared the handle to the thread "
            "it was running on, which the next start would read as nothing "
            "running"
        )


class TestPostgresPacesAServerThatRefusesConnections:
    """The other branch of the reconnect loop, and the one the flapping test
    never enters.

    That test kills connections the server has already accepted. A server that
    is *down* refuses them, which is a different path with its own wait - and
    it is the path the constant's own comment justifies: "so a server that is
    down does not get hammered by every instance at once". Every instance in
    the deployment is in this loop at the same moment, which is exactly when
    a tight retry is worst.
    """

    def _refused_connect_gaps(self, backend, monkeypatch, ceiling, attempts_wanted):
        """Drive the connect-failure branch and time the attempts.

        The escalation and the ceiling need different windows and are asserted
        in different tests: a ceiling low enough to bind saturates the
        doubling after two steps, so a growth assertion under it has nothing
        left to see. Measured - one test asserting both passed on good code
        only by rounding.
        """
        import backend.core.postgres_backend as module

        attempts = []
        refusing = threading.Event()
        real_connect = module.psycopg.connect

        def refuse_while_asked(*args, **kwargs):
            if refusing.is_set():
                attempts.append(time.monotonic())
                raise psycopg.OperationalError("injected: connection refused")
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(module, "NOTIFY_RECONNECT_MAX_SECONDS", ceiling)
        monkeypatch.setattr(module.psycopg, "connect", refuse_while_asked)

        backend.start_change_notification(_Collector())
        # Opened BEFORE the refusal is armed: the patch is on the module
        # `psycopg` object, which this test uses too, so a connection taken
        # afterwards would be refused along with the backend's.
        killer = psycopg.connect(DSN, autocommit=True)
        try:
            victim = backend._listen_conn.info.backend_pid
            refusing.set()
            # Drop the established connection so the loop goes round and meets
            # the refusing connect.
            killer.execute("SELECT pg_terminate_backend(%s)", (victim,))
            deadline = time.monotonic() + 40
            while len(attempts) < attempts_wanted and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            refusing.clear()
            killer.close()
            backend.stop_change_notification()

        assert len(attempts) >= attempts_wanted, (
            f"only {len(attempts)} connect attempts of {attempts_wanted} in "
            f"40s - the loop is not retrying a refused connection at all"
        )
        return [b - a for a, b in zip(attempts, attempts[1:])]

    def test_a_refused_connection_is_retried_with_a_growing_wait(
        self, schema, backends, monkeypatch
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)

        # High enough that the ceiling never binds: this is about the shape of
        # the escalation, not about where it stops.
        gaps = self._refused_connect_gaps(backend, monkeypatch, 30.0, 4)

        assert gaps[-1] >= 2 * gaps[0], (
            f"connect attempts {[round(g, 3) for g in gaps]} are evenly "
            f"spaced: a server that is refusing connections is being retried "
            f"at a fixed rate by every instance at once"
        )

    def test_the_wait_between_refused_connections_has_a_ceiling(
        self, schema, backends, monkeypatch
    ):
        """The other end of the same schedule.

        Growth alone does not need a bound, and unbounded doubling satisfies
        the test above perfectly - while an instance that flapped for a few
        minutes then waits hours, staying deaf long after the server came
        back. Which is the same silent staleness this capability exists to
        remove, reached from the recovery side.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        import backend.core.postgres_backend as module

        # Read before the helper patches it, so the message can tell the two
        # apart. The shipped ceiling is thirty seconds; the one in force here
        # is a stand-in, and reporting the stand-in as "the documented
        # ceiling" would send a reader looking for a constant that does not
        # exist.
        shipped = module.NOTIFY_RECONNECT_MAX_SECONDS
        # Low enough to bind, and enough attempts that an unbounded schedule
        # has had room to pass it: bounded this gives 0.5, 1, 1, 1, 1;
        # unbounded, 0.5, 1, 2, 4, 8.
        in_force = 1.0
        gaps = self._refused_connect_gaps(backend, monkeypatch, in_force, 6)

        assert max(gaps) <= in_force + 0.6, (
            f"connect attempts {[round(g, 3) for g in gaps]} passed the "
            f"ceiling of {in_force}s in force for this test (the shipped one "
            f"is {shipped}s): the backoff has no upper bound"
        )

    def test_the_listening_connection_carries_a_connect_timeout(
        self, schema, backends, monkeypatch
    ):
        """Without it, start's own bound is the only thing that expires and it
        expires against a thread still inside connect - which is the state
        that cannot be joined and cannot be started over. A server that
        accepts TCP and never answers is the ordinary way to reach it: a
        firewall dropping packets, a failing-over primary.

        Asserted on the call rather than through behaviour, because the
        behaviour it prevents is an unbounded hang.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        import backend.core.postgres_backend as module

        seen = []
        real_connect = module.psycopg.connect

        def record(*args, **kwargs):
            seen.append(kwargs)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(module.psycopg, "connect", record)
        backend.start_change_notification(_Collector())
        backend.stop_change_notification()

        assert seen, "the listening connection was never opened"
        assert "connect_timeout" in seen[0], (
            "the listening connection is opened without a connect timeout, so "
            "a server that accepts TCP and never answers holds the thread for "
            "as long as the kernel allows"
        )
        assert seen[0]["connect_timeout"] < module._LISTEN_START_TIMEOUT, (
            "a connect timeout at or above start's own bound cannot keep "
            "start from expiring against a live thread"
        )


class TestPostgresKeepsItsPromiseWhenTheJoinFails:
    """A stop whose join times out still keeps its promise, and its handle.

    The suite's other stop cases all join successfully, so the failed-join
    path - the one stop's docstring makes its promise about - was reached by
    nothing.

    What this pins is the outcome, not any one mechanism. THREE things stop a
    change arriving after such a stop: `_deliver` re-reads the flag, the
    reading loop checks it before going back for another notification, and
    `stop` clears the listener. Any one of them alone is enough on this path,
    so removing any one does not fail this - measured. What the second case
    below adds is the outcome with all three gone, which is reachable more
    cheaply than it looked: two notifications committed in ONE transaction
    arrive in one read, so the loop is already holding the second when the
    listener blocks on the first.
    """

    def test_nothing_reaches_the_listener_after_a_stop_whose_join_timed_out(
        self, schema, backends, monkeypatch
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        import backend.core.postgres_backend as module

        collector = _Collector()
        inside = threading.Event()
        release = threading.Event()

        def slow_then_recording(change):
            if not inside.is_set():
                inside.set()
                release.wait(60)
            collector(change)

        backend.start_change_notification(slow_then_recording)
        try:
            writer.upsert_node(node_payload("first"))
            assert inside.wait(30), "the listener was never called"

            # The join cannot succeed: the thread is inside the listener,
            # which is waiting on us.
            monkeypatch.setattr(module, "_LISTEN_STOP_TIMEOUT", 0.2)
            backend.stop_change_notification()
            assert backend._listen_thread is not None, (
                "a join that timed out still cleared the handle"
            )

            release.set()
            writer.upsert_node(node_payload("second"))
            # The first change was already in flight and may still land; what
            # must never arrive is anything announced after stop returned.
            time.sleep(2.0)
            assert not any(
                op.entity_id == "second"
                for change in collector.changes
                if change.operations
                for op in change.operations
            ), (
                "a change announced after stop returned reached the listener: "
                "the promise rests on the flag, not on the join"
            )
        finally:
            release.set()


class TestPostgresReconnectsWithoutLeakingConnections:
    """Five reconnects, and the server's connection count back where it was.

    The server is the shared resource this backend is careful with, and
    nothing counted it across a reconnect. What this does NOT establish is
    that the explicit close is what keeps it there: measured, CPython's
    refcounting closes a dropped connection promptly, so removing the close
    leaves this passing. The close is still right - depending on the
    interpreter's collection strategy for a server resource is not a thing to
    do deliberately - but its reason is determinism rather than a leak, and
    the production comment was corrected to say so.
    """

    def test_repeated_drops_leave_no_connections_behind(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        collector = _Collector()

        def total():
            with psycopg.connect(DSN, autocommit=True) as conn:
                return conn.execute(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                ).fetchone()[0]

        backend.start_change_notification(collector)
        try:
            baseline = total()
            for _ in range(5):
                conn = backend._listen_conn
                if conn is not None:
                    with psycopg.connect(DSN, autocommit=True) as killer:
                        killer.execute(
                            "SELECT pg_terminate_backend(%s)",
                            (conn.info.backend_pid,),
                        )
                time.sleep(0.4)
            collector.wait_for(1, timeout=60)
            time.sleep(1.0)
            after = total()
        finally:
            backend.stop_change_notification()

        assert after <= baseline + 1, (
            f"{after - baseline} connections above baseline after five "
            f"reconnects: the loop is leaking one per attempt"
        )


class TestPostgresCanListenAgainAfterStopping:
    """Nothing in this module ever started notification twice on one backend.

    `start` resets three pieces of per-run state - the thread handle, the stop
    flag and the recorded start error - and only the handle was pinned. Both
    of the others hide a backend that looks healthy and hears nothing:

    - leave the stop flag set and `start` blocks for its whole bound and then
      raises, although the connection is fine. `close()` calls `stop`, so any
      backend that has ever been stopped could never listen again.
    - leave the error set and a start that SUCCEEDS raises the previous
      attempt's failure, and tears down the working listener on its way out.

    Neither is reachable from GraphStorage, which starts once. Both are
    reachable from a script, a test, or any future caller that reconnects a
    backend rather than building a new one.
    """

    def test_a_backend_that_was_stopped_can_start_again(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])

        backend.start_change_notification(_Collector())
        backend.stop_change_notification()

        second = _Collector()
        backend.start_change_notification(second)
        try:
            writer.upsert_node(node_payload("after_restart"))
            (change,) = second.wait_for(1)
        finally:
            backend.stop_change_notification()
        assert [op.entity_id for op in change.operations] == ["after_restart"]

    def test_a_start_that_failed_does_not_poison_the_next_one(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        good = backend.conninfo
        backend.conninfo = psycopg.conninfo.make_conninfo(
            **{
                **psycopg.conninfo.conninfo_to_dict(DSN),
                "dbname": f"co_absent_{uuid.uuid4().hex[:12]}",
            }
        )
        with pytest.raises(psycopg.OperationalError):
            backend.start_change_notification(_Collector())

        backend.conninfo = good
        collector = _Collector()
        # The failure above must not be raised at this one, and - the quieter
        # half - must not have this successful start tear itself down on the
        # way out.
        backend.start_change_notification(collector)
        try:
            writer.upsert_node(node_payload("after_a_failed_start"))
            (change,) = collector.wait_for(1)
        finally:
            backend.stop_change_notification()
        assert [op.entity_id for op in change.operations] == ["after_a_failed_start"]


class TestPostgresReadsEachKindEvenAlone:
    """A write that names only one kind is the ordinary case, and it was the
    one no notification test made.

    Every case in this module that names an edge names a node in the same
    batch, so `_resolve`'s per-table loop always ran its node arm first with
    something to look up. Change its skip-the-empty-kind `continue` to a
    `break` - one character - and a write of edges alone stops before the edge
    table is read: every other instance is told the edge was DELETED while the
    store holds it, and deletes a live edge. The suite passes.
    """

    def test_an_edge_written_alone_is_reported_as_an_upsert(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        writer.apply_batch([EntityOperation.upsert_node(node_payload("a"))])
        collector.wait_for(1)

        writer.upsert_edge(edge_payload("aa", "a", "a", label="Alone"))

        changes = collector.wait_for(2)
        (op,) = changes[1].operations
        assert (op.kind, op.action, op.entity_id) == ("edge", "upsert", "aa")
        assert op.payload["label"] == "Alone"

    def test_a_node_written_alone_is_reported_as_an_upsert(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)

        writer.upsert_node(node_payload("solo", name="Solo"))

        (change,) = collector.wait_for(1)
        (op,) = change.operations
        assert (op.kind, op.action, op.entity_id) == ("node", "upsert", "solo")
        assert op.payload["name"] == "Solo"

    def test_a_batch_of_edges_alone_is_reported_whole(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        writer.apply_batch([EntityOperation.upsert_node(node_payload("a"))])
        collector.wait_for(1)

        writer.apply_batch(
            [
                EntityOperation.upsert_edge(edge_payload(f"e{i}", "a", "a"))
                for i in range(5)
            ]
        )

        changes = collector.wait_for(2)
        assert [(op.kind, op.action) for op in changes[1].operations] == [
            ("edge", "upsert")
        ] * 5


class TestPostgresNamesEveryEntityOrNone:
    """An announcement names all of what it names, or it names none of them.

    "Name as many as fit" is the plausible alternative to degrading, and it is
    silently wrong: the entities past the cut are never reported by name and
    never covered by an `unknown()` either, so every other instance diverges
    permanently with nothing left to notice it. Nothing pinned it - the
    largest DESCRIBED batch anywhere else in this module is four operations,
    and every case that crosses the cap crosses it with 240-byte ids, which
    degrade whole.
    """

    def test_a_large_batch_of_short_ids_is_named_in_full(
        self, listening, schema, backends
    ):
        _, collector = listening()
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(writer)
        # Short enough that 250 of them still fit under the cap, so this batch
        # must be described rather than degraded - and long enough past any
        # round number that a truncation would show.
        ids = [f"n{i:03d}" for i in range(250)]
        batch = [EntityOperation.upsert_node(node_payload(i)) for i in ids]
        assert json.loads(writer._encode(batch)).get("ops") is not None, (
            "this batch no longer fits the cap, so it cannot say anything "
            "about a described announcement naming all of its entities"
        )

        writer.apply_batch(batch)

        (change,) = collector.wait_for(1)
        assert [op.entity_id for op in change.operations] == ids


class TestPostgresOriginIsWideEnoughNotToCollide:
    """Two instances that share an origin are deaf to each other.

    Each skips the other's announcements as its own, so both go on serving
    what they last loaded, both look healthy, and nothing ever reports it -
    the failure this capability exists to remove, produced by the mechanism
    that prevents a different one. The channel digest has a collision test;
    the origin does the same job on the same payload and had none, and
    shortening it is exactly what the 8000-byte cap invites, because every
    origin byte is a byte not spent on entity names.
    """

    def test_the_origin_keeps_a_full_uuid_of_width(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert len(backend._origin) == 32
        int(backend._origin, 16)  # hex, so the width is the whole entropy

    def test_backends_on_one_store_do_not_share_an_origin(self, schema, backends):
        made = [PostgresGraphPersistenceBackend(DSN, schema=schema) for _ in range(200)]
        backends.extend(made)
        assert len({b._origin for b in made}) == len(made)


class TestPostgresSubscribesBeforeItReloads:
    """The reconnect's reload must read a store it is already subscribed to.

    Deliver the `unknown()` before `LISTEN` is re-registered and the reload
    reads a snapshot taken before the channel exists again - and a refresh
    drains the application's write queue, so it can take seconds. Anything
    announced in that window is in neither the reload nor the subscription:
    permanently missed, with no further report coming. Which is the staleness
    the reconnect exists to repair, reintroduced by the repair.

    Asserted on the order of the two events rather than by racing a write into
    a window measured in microseconds - a test that tried the race would pass
    on a slow machine for the wrong reason.
    """

    def test_listen_is_registered_before_the_reconnect_reports(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        events = []
        real_execute = psycopg.Connection.execute

        def note_listen(conn, query, *args, **kwargs):
            if "LISTEN" in " ".join(str(query).split()).upper():
                events.append("listen")
            return real_execute(conn, query, *args, **kwargs)

        collector = _Collector()

        def note_report(change):
            events.append("report")
            collector(change)

        psycopg.Connection.execute = note_listen
        try:
            backend.start_change_notification(note_report)
            with psycopg.connect(DSN, autocommit=True) as killer:
                killer.execute(
                    "SELECT pg_terminate_backend(%s)",
                    (backend._listen_conn.info.backend_pid,),
                )
            collector.wait_for(1, timeout=60)
        finally:
            psycopg.Connection.execute = real_execute
            backend.stop_change_notification()

        assert events[:1] == ["listen"], f"unexpected first event: {events[:3]}"
        assert "report" in events, "the reconnect never reported"
        # The report belongs to the SECOND listen, not before it.
        assert events.index("report") > 1, (
            f"the reconnect reported before re-registering LISTEN: {events}"
        )


class TestPostgresStopHoldsWithABatchAlreadyRead:
    """The promise with a notification already in hand.

    Two `pg_notify` calls in one transaction are delivered together and read
    into one batch, so when the listener blocks on the first, the second is
    already inside this process - past the server, past the socket, past the
    read. That is the case where "stop returns and nothing else is delivered"
    stops being obvious, and it is the one the suite could not reach before:
    every other stop case has the loop go back to the server for the next
    notification, where the loop's own flag check catches it.
    """

    def test_a_notification_already_read_is_not_delivered_after_stop(
        self, schema, backends, monkeypatch
    ):
        import backend.core.postgres_backend as module

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        writer = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([backend, writer])
        writer.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("first")),
                EntityOperation.upsert_node(node_payload("second")),
            ]
        )
        seen = _Collector()
        inside = threading.Event()
        release = threading.Event()

        def blocking(change):
            seen(change)
            if len(seen.changes) == 1:
                inside.set()
                release.wait(60)

        backend.start_change_notification(blocking)
        try:
            # Two announcements, one transaction: the server releases them
            # together and psycopg hands them over in one batch.
            with psycopg.connect(DSN) as sender:
                sender.execute(
                    "SELECT pg_notify(%s, %s)",
                    (backend._channel, '{"o": "elsewhere", "ops": [["n", "first"]]}'),
                )
                sender.execute(
                    "SELECT pg_notify(%s, %s)",
                    (backend._channel, '{"o": "elsewhere", "ops": [["n", "second"]]}'),
                )
                sender.commit()

            assert inside.wait(30), "the listener was never called"
            monkeypatch.setattr(module, "_LISTEN_STOP_TIMEOUT", 0.2)
            backend.stop_change_notification()
            release.set()
            time.sleep(2.0)
        finally:
            release.set()
            backend.stop_change_notification()

        delivered = [
            op.entity_id
            for change in seen.changes
            if change.operations
            for op in change.operations
        ]
        assert "second" not in delivered, (
            "a notification the loop had already read was delivered after "
            f"stop returned: {delivered}"
        )
