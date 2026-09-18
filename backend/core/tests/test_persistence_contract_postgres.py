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
import pathlib
import random
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
    SCOPE_COLUMN,
    SCOPE_POLICY_SUFFIX,
    SCOPE_SETTING,
    SCOPED_TABLES,
    CrossScopeWriteRefused,
    PostgresGraphPersistenceBackend,
    ScopeIsolationUnavailable,
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


# One shared length source for every batch-length case in this module.
#
# This module used to pin three fixed lengths (2, 7, 41) across the batch
# and lock-mode tests below. A fixed set leaves every gap between its values
# untested: a mutation gated on `len(operations) >= N`, or on the operation
# at index N, for any N the fixed set happens to skip, is invisible to the
# whole suite - and review repeatedly found exactly that, in the 6..39 gap
# and above 41. Redrawing the non-structural anchors from a seeded RNG each
# session does not close any one run's gap either, but it stops the gap from
# being the *same* gap every run: a threshold anywhere in range eventually
# falls on a session that draws past it, and CI runs this module on every
# PR. `CO_TEST_PG_LENGTH_SEED` pins the seed for an exact rerun, and every
# case that draws from it prints the seed as its first line, so a failure's
# own captured output already carries what reproduces it.
def _pg_length_seed() -> int:
    override = os.environ.get("CO_TEST_PG_LENGTH_SEED")
    if override is not None:
        return int(override)
    return random.SystemRandom().randrange(2**31)


PG_LENGTH_SEED = _pg_length_seed()
_LENGTHS_RNG = random.Random(PG_LENGTH_SEED)


def _pg_length_seed_banner() -> str:
    """Printed by every case that draws a length, so a failure's own
    captured output names the seed that reproduces it - see the module-level
    comment above `_pg_length_seed`."""
    return (
        f"postgres persistence-contract length seed: {PG_LENGTH_SEED} "
        f"(rerun this exact draw with CO_TEST_PG_LENGTH_SEED={PG_LENGTH_SEED})"
    )


# Kept fixed rather than drawn: `TestPostgresEntityWritesTouchOneRow._long_batch`
# cycles through 4 slots in runs of `_RUN`, and this length is what truncates
# that cycle down to only its first slot (a delete_edge run) - a structural
# case this family exists to cover, not a stand-in for "some short length".
SHORT_BATCH_LENGTH = 2

# The two non-structural anchors every length-sensitive case below shares -
# one length in the gap the old fixed values left untested (6..39), and one
# clearly past the old fixed ceiling (41) so "a long batch" stays
# unambiguously long whatever it draws. Drawn once per session from the seed
# above and reused everywhere a case needs "a middling batch" or "a long
# batch" rather than its own private magic number.
MID_BATCH_LENGTH = _LENGTHS_RNG.randint(6, 39)
LONG_BATCH_LENGTH = _LENGTHS_RNG.randint(42, 90)
_POSTGRES_SOURCE_FILES = (
    pathlib.Path(__file__).resolve().parents[1] / "postgres_backend.py",
    pathlib.Path(__file__).resolve(),
)
_BOUNDARY_SENSITIVE_TERMS = tuple(
    "".join(parts)
    for parts in (
        ("co", "-", "ten", "ant"),
        ("co", "_", "ten", "ant"),
        ("ten", "ant"),
        ("ten", "ants"),
    )
)


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


class TestPostgresStatementSpyHandlesAnEmptyExecutemany:
    """`_EXECUTED_NEVER`, reached rather than left as dead test infrastructure.

    Every other test that calls `_statements_issued` around a save hands it
    at least one node or edge, so the `executemany` calls for `graph_nodes`
    and `graph_edges` always carry at least one row and the `rows[0] if
    rows else _EXECUTED_NEVER` branch above never takes its `else`. An
    empty save still issues both `executemany` calls - with zero rows - so
    this is what reaches it, and what proves `_sequential_scans` treats "no
    example row to plan with" as "skip it", not as "cannot plan it" (which
    is the *other* branch just above, for a statement that DID run but
    whose parameters this spy failed to capture).
    """

    def test_an_empty_saves_executemany_calls_are_read_as_never_executed(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.exists()  # migrate first, so only the save itself is captured

        issued = _statements_issued(lambda: backend.save_graph_data(snapshot()))

        sentinelled = [q for q, p in issued if p is _EXECUTED_NEVER]
        assert sentinelled, (
            "an empty save's executemany calls were not recorded with the "
            "'executed never' sentinel - _EXECUTED_NEVER is dead code"
        )
        # Not an error and not a scan: `_sequential_scans` must get past
        # these silently rather than raising the "cannot plan" error meant
        # for a statement it failed to capture parameters for.
        _touched, scanning = _sequential_scans(issued)
        assert not scanning, f"an empty save's statements read as scans: {scanning}"


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

    def test_a_save_on_a_different_schema_does_not_wait_for_this_ones_lock(
        self, schema, backends, monkeypatch
    ):
        """SV_LOCKGLOBAL, the timing assertion the test above says it lacks.

        `test_overlapping_saves_leave_one_writers_graph` says explicitly that
        it does not prove the lock is keyed on the schema - a lock keyed on
        `SAVE_LOCK_KEY` alone, with the `hashtext(schema)` half dropped,
        passes it too, since that test only ever uses one schema. This is
        the second schema that comment asks for: a save on it must land
        while this schema's save is still held open, or every schema in the
        database serialises on one save at a time - the opposite of what
        the module docstring promises.
        """
        other_schema = f"{schema}_other"
        first = PostgresGraphPersistenceBackend(DSN, schema=schema)
        second = PostgresGraphPersistenceBackend(DSN, schema=other_schema)
        backends.extend([first, second])
        try:
            first.save_graph_data(snapshot([node_payload("seed")]))
            second.save_graph_data(snapshot([node_payload("seed")]))

            released = threading.Event()
            # The pid `_stalled_save` reports is for a caller that asks the
            # server whether a second write is BLOCKED on it
            # (`_wait_until_blocking`); this test asks the opposite question
            # - that the second schema's save is NOT blocked - which a
            # bounded wait answers directly, with nothing to attribute.
            stalled, _held_pid = self._stalled_save(
                first, [node_payload("held")], released, monkeypatch
            )

            done = threading.Event()

            def save_elsewhere():
                second.save_graph_data(snapshot([node_payload("free")]))
                done.set()

            other = threading.Thread(target=save_elsewhere, daemon=True)
            other.start()
            landed = done.wait(10)
            # Release before asserting anything, same reason as the lock-mode
            # class above: failing here with the first save still parked
            # would leave it holding a pooled connection, an open
            # transaction and the advisory lock for the rest of its wait.
            still_holding = stalled.is_alive()
            released.set()
            stalled.join(30)
            other.join(30)

            assert still_holding, "the first save let go before the probe could run"
            assert not other.is_alive(), "the second schema's save never finished"
            assert landed, (
                "a save on a different schema waited for this schema's save "
                "lock: the lock is keyed on a constant rather than "
                "hashtext(schema), so every schema in the database "
                "serialises on one save at a time"
            )
            assert self.stall_errors == [], f"the held save failed: {self.stall_errors}"
            assert {n["id"] for n in second.load_graph_data()["nodes"]} == {"free"}
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{other_schema}" CASCADE')


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

    def test_a_role_that_owns_nothing_is_not_warned_about_indexes_it_has(
        self, lowpriv, backends, capsys
    ):
        """The operator provisioned the store exactly as the docs prescribe -
        tables AND the two traversal indexes - and the role owns none of it.
        `CREATE INDEX IF NOT EXISTS` checks ownership before existence, so
        without a catalog check first this boots with two warnings saying the
        traversal will scan, while it seeks. A warning that fires when nothing
        is wrong teaches an operator to ignore warnings.
        """
        name, schema, password = lowpriv
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
            for index, expression in (
                ("graph_edges_source_idx", "((doc->>'source'))"),
                ("graph_edges_target_idx", "((doc->>'target'))"),
            ):
                conn.execute(
                    psycopg.sql.SQL("CREATE INDEX {} ON {}.graph_edges {}").format(
                        psycopg.sql.Identifier(index),
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.SQL(expression),
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
        capsys.readouterr()
        backend.save_graph_data(snapshot([node_payload("a")]))
        printed = capsys.readouterr().out
        assert "scan instead of seek" not in printed, (
            "warned about indexes the store already has: " + printed
        )
        # The other half of the same boot: ANALYZE does NOT raise for this
        # role - PostgreSQL emits a warning and skips the table - so without
        # the notice handler the save reports success while the statistics it
        # exists to refresh were never touched. The docs promise the operator
        # this line; nothing else asserts it.
        # Matched on the backend's own prefix, not on the server's wording:
        # PostgreSQL 16 says "permission denied to analyze", 15 and earlier say
        # "only table or database owner can analyze it", and both are
        # lc_messages-dependent. What this test is about is that the notice
        # reaches the operator at all.
        # With the colon: "Warning: ANALYZE after save:" is the notice-handler
        # line, which is what this test is about. "could not ANALYZE after
        # save;" is the exception line, and matching both would let a mutation
        # that makes ANALYZE raise outright satisfy an assertion about the
        # handler.
        # Exactly once per table per save, and no handler left on the
        # connection afterwards. A handler that is installed and not removed
        # attaches to whatever runs on that pooled connection next: four saves
        # leave four handlers, and an unrelated statement's notices then print
        # four times, each labelled as coming from ANALYZE - which a
        # substring assertion is perfectly happy with.
        assert printed.count("Warning: ANALYZE after save:") == 2, (
            "one line per table, once: " + printed
        )
        # A second save must cost the same two lines, not four. A handler
        # installed and never removed stays on the pooled connection and
        # attaches to whatever runs on it next, so they accumulate: four saves
        # leave four handlers, and an unrelated statement's notices then print
        # four times, each labelled as coming from ANALYZE. A substring
        # assertion is perfectly happy with that; a count is not.
        backend.save_graph_data(snapshot([node_payload("a")]))
        again = capsys.readouterr().out
        assert again.count("Warning: ANALYZE after save:") == 2, (
            "the notice handler from the first save is still attached: " + again
        )
        assert "Warning: ANALYZE after save:" in printed, (
            "a role that cannot ANALYZE was told nothing about it: " + printed
        )
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]


class TestPostgresIndexCreationLosesRacesQuietly:
    """The index loop runs outside the migrating transaction, and the advisory
    lock is transaction-scoped - so it holds no lock, and N instances booting
    together all pass the catalog check and all issue the statement. Measured
    with 8 concurrent boots against a 5 000-row table, the create raised 5
    times; against 200 000 rows, 14 times and 14 false warnings saying the
    traversal would scan, on a store whose index was there. The window is the
    index build, so it widens with the table: it is the upgrade of a large
    existing store that hits this.

    Raced deterministically rather than by threads and hope - a race a fast
    machine wins is a test that passes having exercised nothing. The create is
    made to do what the losing instance sees: the index exists, and the
    statement raises anyway.
    """

    def _boot_with_a_losing_create(self, schema, backends, capsys, really_create):
        import psycopg as _psycopg

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        original = _psycopg.Connection.execute

        def _lose(self, query, *args, **kwargs):
            if "CREATE INDEX" in repr(query):
                if really_create:
                    # Whoever won the race already made it.
                    with _psycopg.connect(DSN, autocommit=True) as winner:
                        original(winner, query, *args, **kwargs)
                raise _psycopg.errors.UniqueViolation(
                    "duplicate key value violates unique constraint"
                    ' "pg_class_relname_nsp_index"'
                )
            return original(self, query, *args, **kwargs)

        _psycopg.Connection.execute = _lose
        try:
            capsys.readouterr()
            backend._ensure_schema()
        finally:
            _psycopg.Connection.execute = original
        return capsys.readouterr().out

    def test_a_lost_race_is_not_reported(self, schema, backends, capsys):
        printed = self._boot_with_a_losing_create(
            schema, backends, capsys, really_create=True
        )
        assert "scan instead of seek" not in printed, (
            "reported a failure on a store whose index is there: " + printed
        )

    def test_a_create_that_really_failed_is_still_reported(
        self, schema, backends, capsys
    ):
        # The same index name exists in ANOTHER schema first. The re-check asks
        # the catalog by name and schema; by name alone it finds the neighbour's
        # and stays silent about this store's missing one, forever. Two schemas
        # in one database is a supported shape here - `self.schema` exists for
        # exactly that.
        neighbour = schema + "_neighbour"
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(neighbour)
                )
            )
            conn.execute(
                psycopg.sql.SQL(
                    "CREATE TABLE {}.graph_edges"
                    " (id text PRIMARY KEY, doc jsonb NOT NULL)"
                ).format(psycopg.sql.Identifier(neighbour))
            )
            # BOTH names, and the assertion below names one of them. With
            # only one planted here, the other index's warning satisfies a
            # name-agnostic assertion while the planted one is silently
            # skipped - which is how this test passed against a re-check whose
            # schema predicate had been removed.
            for index in ("graph_edges_source_idx", "graph_edges_target_idx"):
                conn.execute(
                    psycopg.sql.SQL(
                        "CREATE INDEX {} ON {}.graph_edges ((doc->>'source'))"
                    ).format(
                        psycopg.sql.Identifier(index),
                        psycopg.sql.Identifier(neighbour),
                    )
                )
        try:
            printed = self._boot_with_a_losing_create(
                schema, backends, capsys, really_create=False
            )
            for index in ("graph_edges_source_idx", "graph_edges_target_idx"):
                assert index in printed and "scan instead of seek" in printed, (
                    f"{index} is genuinely missing from this store and was not "
                    f"reported: {printed}"
                )
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(neighbour)
                    )
                )


class TestPostgresIndexWorkIsDoneOnce:
    """Two properties of the boot path that the answer cannot show.

    The catalog pre-check's own purpose is not to be correct - the re-check
    after a failure covers that - but to stop issuing DDL that will fail on
    every boot of a store that already has its indexes. Deleting it leaves
    every test green while a least-privilege instance sends two doomed
    statements per start, forever.

    And the ANALYZE after a save is outside the save's transaction so that it
    cannot fail the save. That is the whole reason it is where it is, and
    nothing exercised the path where it raises.
    """

    def test_a_store_that_has_its_indexes_issues_no_ddl(self, schema, backends, capsys):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend._ensure_schema()

        # Second boot, same store: the indexes are there now.
        again = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(again)
        # The module's own helper, which patches the CURSOR: `Connection.execute`
        # delegates to one, so the cursor sees both, and a refactor that issued
        # this DDL through `conn.cursor()` would leave a connection-level spy
        # recording nothing and this assertion passing over two doomed
        # statements.
        issued = _statements_issued(again._ensure_schema)

        creates = [q for q, _ in issued if "CREATE INDEX" in repr(q)]
        assert not creates, (
            "re-issued DDL for indexes that are already there; on a role that "
            f"may not create them, that is a failure every boot: {creates}"
        )

    def test_a_catalog_it_cannot_read_is_reported_not_assumed_away(
        self, schema, backends, capsys
    ):
        """`_index_state` answers "missing" when it cannot ask - a pool blip
        during boot, a server that refuses a connection - so the caller
        reports rather than hides. Answering "valid" there brings the store up
        with neither index and nothing printed, which is the silent
        degradation this whole path exists to prevent.
        """
        from psycopg_pool import ConnectionPool

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        real = ConnectionPool.connection
        # After the migration's own checkout, so the tables still get made and
        # only the index work meets the blip. Refusing every checkout would
        # fail `_ensure_schema` before it ever reaches the index loop, and the
        # test would assert nothing.
        seen = {"n": 0}

        def _blip(self, *args, **kwargs):
            seen["n"] += 1
            if seen["n"] > 1:
                raise RuntimeError("pool exhausted")
            return real(self, *args, **kwargs)

        backend.save_graph_data(snapshot([node_payload("a")]))
        again = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(again)
        capsys.readouterr()
        ConnectionPool.connection = _blip
        try:
            again._ensure_schema()
        except Exception:
            pass
        finally:
            ConnectionPool.connection = real
        printed = capsys.readouterr().out
        assert "scan instead of seek" in printed, (
            "a catalog that could not be read was taken as an answer: " + printed
        )

    def test_an_analyze_that_raises_does_not_fail_the_save(
        self, schema, backends, capsys
    ):
        import psycopg as _psycopg

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend._ensure_schema()

        original = _psycopg.Connection.execute

        def _refuse_analyze(self, query, *args, **kwargs):
            if "ANALYZE" in repr(query):
                raise _psycopg.errors.InsufficientPrivilege("no ANALYZE for you")
            return original(self, query, *args, **kwargs)

        _psycopg.Connection.execute = _refuse_analyze
        try:
            backend.save_graph_data(snapshot([node_payload("a")]))
        finally:
            _psycopg.Connection.execute = original

        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"], (
            "the save did not land"
        )
        assert "could not ANALYZE after save" in capsys.readouterr().out


class TestPostgresReportsAnIndexItCannotUse:
    """An index left invalid by a failed `CREATE INDEX CONCURRENTLY` is
    present in the catalog, unusable by the planner, and matched by
    `IF NOT EXISTS` - so a presence check by name alone accepts it, re-issuing
    the statement is a no-op, and the store scans every edge at every level
    with nothing printed. The docs tell an operator to use CONCURRENTLY on a
    live store, which is exactly where a build can fail.
    """

    def test_an_invalid_index_is_reported_rather_than_taken_as_done(
        self, schema, backends, capsys
    ):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(schema)
                )
            )
            conn.execute(
                psycopg.sql.SQL(
                    "CREATE TABLE {}.graph_edges"
                    " (id text PRIMARY KEY, doc jsonb NOT NULL)"
                ).format(psycopg.sql.Identifier(schema))
            )
            conn.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {}.graph_edges VALUES"
                    " ('a', '{{\"source\": \"x\"}}'),"
                    " ('b', '{{\"source\": \"x\"}}')"
                ).format(psycopg.sql.Identifier(schema))
            )
            # A concurrent build that cannot succeed: the values collide, so
            # the unique index is left behind with indisvalid = false. That is
            # the shape a failed CONCURRENTLY build leaves in any case.
            try:
                conn.execute(
                    psycopg.sql.SQL(
                        "CREATE UNIQUE INDEX CONCURRENTLY graph_edges_source_idx"
                        " ON {}.graph_edges ((doc->>'source'))"
                    ).format(psycopg.sql.Identifier(schema))
                )
            except psycopg.errors.UniqueViolation:
                pass
            valid = conn.execute(
                "SELECT i.indisvalid FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_index i ON i.indexrelid = c.oid"
                " WHERE n.nspname = %s AND c.relname = %s",
                (schema, "graph_edges_source_idx"),
            ).fetchone()
        assert valid == (False,), (
            f"the fixture did not leave an invalid index behind: {valid}"
        )

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        capsys.readouterr()
        backend._ensure_schema()
        printed = capsys.readouterr().out
        assert "graph_edges_source_idx" in printed and "not valid" in printed, (
            "an index the planner cannot use was taken as done: " + printed
        )
        # And it must not name a cause. The same indisvalid = false is what a
        # HEALTHY concurrent build reads while it is still running - verified
        # against this server - and the docs tell an operator to use exactly
        # that on a live store. "Your build failed, drop it" is how they abort
        # their own build and take the lock they were avoiding.
        assert "still running" in printed, (
            "named a failed build as the cause when the catalog cannot tell "
            "that from a healthy one still in progress: " + printed
        )
        # The whole command, quoted. The operator is meant to paste it, and
        # the least-privilege fixture above parametrises over a schema that
        # only survives quoted for exactly this class of bug: unquoted, the
        # remedy fails with `schema "..." does not exist`.
        assert (
            f'REINDEX INDEX CONCURRENTLY "{schema}"."graph_edges_source_idx"' in printed
        ), "the remedy is not a command the operator can run: " + printed
        assert "REINDEX INDEX CONCURRENTLY" in printed, (
            "an invalid index is repairable in place; DROP is not the remedy "
            "and takes a stronger lock: " + printed
        )
        assert "scan instead of seek" in printed, (
            "said the index was unusable without saying what it costs: " + printed
        )
        # And the boot carries on. Reporting must not stop the OTHER index
        # being created - one invalid index would then keep the second from
        # ever existing, on this boot and every later one.
        with psycopg.connect(DSN, autocommit=True) as conn:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                    (schema,),
                ).fetchall()
            }
        assert "graph_edges_target_idx" in present, (
            "an invalid source index stopped the target index being created: "
            f"{sorted(present)}"
        )


class TestPostgresProvisionsWhatTheTraversalNeeds:
    def test_the_traversal_indexes_and_statistics_are_actually_there(
        self, schema, backends
    ):
        """Two mechanisms that exist only for speed, and speed is what a test
        suite of small graphs cannot see: deleting either leaves every other
        test green. Measured on 2000 nodes / 20 000 edges, the index takes the
        level query from a sequential scan to a bitmap scan, and the ANALYZE
        takes the planner's row estimate from a 21x overshoot to the truth.
        Asserted as facts in the catalog rather than as timings, so this says
        the same thing on a loaded runner.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

        with psycopg.connect(DSN, autocommit=True) as conn:
            # Definitions, not names. An index of the right name on the wrong
            # column, or on graph_nodes, or over `id`, satisfies a name check
            # and puts the sequential scan straight back: measured on 20 000
            # edges, a BitmapOr over both indexes at cost 138 becomes a Seq
            # Scan at 901.
            defined = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT indexname, tablename, indexdef"
                    " FROM pg_indexes WHERE schemaname = %s",
                    (schema,),
                ).fetchall()
            }
            for index, column in (
                ("graph_edges_source_idx", "source"),
                ("graph_edges_target_idx", "target"),
            ):
                assert index in defined, f"{index} is not there: {sorted(defined)}"
                table, definition = defined[index]
                assert table == "graph_edges", (
                    f"{index} is on {table}, so the traversal's filter on "
                    f"graph_edges cannot use it"
                )
                assert f"'{column}'" in definition, (
                    f"{index} does not index doc->>'{column}', which is what "
                    f"the traversal filters on: {definition}"
                )
            analysed = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT relname, last_analyze IS NOT NULL"
                    " FROM pg_stat_user_tables WHERE schemaname = %s",
                    (schema,),
                ).fetchall()
            }
        assert analysed.get("graph_nodes") and analysed.get("graph_edges"), (
            "a whole-graph save left the planner's statistics describing the "
            f"table it replaced: {analysed}"
        )
        # And they describe the table as SAVED, not as it was before. Running
        # the ANALYZE first satisfies last_analyze while leaving reltuples at
        # the pre-save count - 0 for a first save - which is exactly the stale
        # statistics the block exists to prevent.
        with psycopg.connect(DSN, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT c.relname, c.reltuples FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = %s AND c.relname = 'graph_nodes'",
                (schema,),
            ).fetchone()
        assert rows is not None and rows[1] >= 1, (
            "the statistics describe the table before the save rather than "
            f"after it: reltuples = {rows}"
        )


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

    def test_a_save_that_actually_inherits_hostile_isolation_raises_rather_than_hiding_it(
        self, schema, backends, monkeypatch
    ):
        """ER_SERIAL: were the SET above ever lost, its failure must still
        reach the caller.

        The test above proves the SET statement runs. This is the other
        half the docstring names: an inheriting save that waited on the
        advisory lock takes its snapshot before the lock, so once it
        unblocks and tries to update the row the other writer already
        committed, the server raises a genuine SerializationFailure (SQLSTATE
        40001) rather than letting it proceed. Simulated here by discarding
        the SET statement specifically - the regression this class exists to
        catch - so the rest of the save runs for real, under the hostile
        connection's actual REPEATABLE READ default, against a real
        contended row. Nothing between the server and `apply_batch`'s caller
        may turn that into a silent no-op: the caller must see the error, or
        it believes a write landed that the store never took.
        """
        dsn = self._dsn_defaulting_to_repeatable_read()
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, pool_size=1)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("contested")]))

        real_execute = psycopg.Connection.execute

        def skip_the_override(conn, query, *args, **kwargs):
            if "SET TRANSACTION ISOLATION LEVEL READ COMMITTED" in str(query):
                # The regression under test: the override never reaches the
                # server, so this transaction runs at the connection's own
                # (hostile) default instead.
                return None
            return real_execute(conn, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", skip_the_override)

        blocker = psycopg.connect(dsn, autocommit=False)
        errors = []
        contended = False
        try:
            blocker.execute(
                psycopg.sql.SQL(
                    "UPDATE {}.graph_nodes SET doc = doc WHERE id = %s"
                ).format(psycopg.sql.Identifier(schema)),
                ("contested",),
            )

            def contend():
                try:
                    backend.save_graph_data(
                        snapshot([node_payload("contested", name="Second")])
                    )
                except Exception as exc:
                    errors.append(exc)

            writer = threading.Thread(target=contend, daemon=True)
            writer.start()
            contended = _wait_until_blocking(blocker.info.backend_pid)
            contended = contended and writer.is_alive()
            blocker.commit()
            writer.join(30)
        finally:
            blocker.close()

        assert contended, "the writer never contended for the row"
        assert not writer.is_alive(), "the contended save never finished"
        assert len(errors) == 1 and isinstance(
            errors[0], psycopg.errors.SerializationFailure
        ), (
            "a save that inherited REPEATABLE READ and lost the row race "
            f"did not raise SerializationFailure to its caller: {errors!r}"
        )


class TestPostgresDeclaresWhatItImplements:
    """Under-declaring is invisible to the contract, by construction.

    Every clause reads the backend's own declaration and skips itself when
    the flag is absent, so dropping a flag turns tests into skips and the
    suite stays green - while GraphStorage quietly reverts to whole-graph
    writes, which is the regression this slice exists to prevent. Only an
    assertion outside the contract can see it.
    """

    def test_the_declaration_names_every_capability(self, schema, backends):
        """Equality, not a flag read each: a capability added to the dataclass
        and left undeclared here would pass every `is True` in the file.

        It did its job once already - `store_traversal` was added and this
        assertion is what asked for it to be declared on purpose rather than
        picked up by accident."""
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert backend.capabilities() == BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
            store_traversal=True,
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

    # Three lengths, none of them a fresh magic number: they are the module's
    # shared anchors (see `SHORT_BATCH_LENGTH` / `MID_BATCH_LENGTH` /
    # `LONG_BATCH_LENGTH` at module scope), so a threshold gate that survives
    # this family's draw this run is the same gate the lock-mode and
    # no-lock-behind families below are also probing with. Still no claim
    # that any one session pins the property completely - a gate can sit
    # between whatever this session drew - only that the gap moves every
    # run instead of sitting still at 2/7/41 forever.
    #
    # `GraphStorage.delete_nodes` builds one edge delete per edge plus one
    # node delete per node, so the length is whatever the caller deleted.
    # What each of the three exercises:
    #
    # - SHORT_BATCH_LENGTH truncates the cycle below, which is the point of
    #   running it - see the module-level comment on that constant;
    # - MID_BATCH_LENGTH is a length under the cost and blast-radius
    #   assertions that is neither the truncating case nor the long one -
    #   the module's other seven-operation batch is a contract clause that
    #   checks the resulting graph and not the statements;
    # - LONG_BATCH_LENGTH is also the lock probe's `batch_many` holder
    #   length (`TestPostgresEntityWritesDoNotSerialiseAgainstEachOther`),
    #   so that length is reached by a test asking a different question:
    #   about the lock's mode rather than about what a write costs.
    BATCH_LENGTHS = (SHORT_BATCH_LENGTH, MID_BATCH_LENGTH, LONG_BATCH_LENGTH)

    # Runs of two, cycling edge-delete, node-delete, edge-upsert,
    # node-upsert. Runs rather than strict alternation because
    # `GraphStorage.delete_nodes` emits every edge delete and then every
    # node delete, so consecutive same-table operations are the ordinary
    # shape - and a backend that merged adjacent ones into a single
    # statement would be invisible to a batch that never has two in a row.
    # Both actions appear from length 5; a shorter length truncates the
    # cycle, which is what makes SHORT_BATCH_LENGTH worth running as well as
    # the longer anchors.
    _RUN = 2

    @classmethod
    def _long_batch(cls, length):
        """A cycle of both kinds and both actions, in runs of two, cut off
        at a caller-chosen length - so a short length gets only the start
        of it, which is what the comment above says SHORT_BATCH_LENGTH is
        for.
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
        print(_pg_length_seed_banner())
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
        print(_pg_length_seed_banner())
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
                        # serialization failure instead of waiting. The
                        # filler count is the module's shared mid-length
                        # anchor (MID_BATCH_LENGTH) rather than a fixed 5,
                        # so "short" cannot be defined narrowly enough to
                        # dodge this case run after run.
                        second.apply_batch(
                            [
                                EntityOperation.upsert_node(node_payload(f"filler{i}"))
                                for i in range(MID_BATCH_LENGTH)
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


class TestPostgresConnectionPoolIsConfiguredAsDocumented:
    """CN_POOLSIZE / CN_MINSIZE / CN_NOVALIDATE.

    Three properties of the pool `__init__` builds, none of them exercised
    by any functional test: the size a caller asks for is the size
    enforced, a backend that is constructed and never used holds no server
    connection at all (the class docstring's own `min_size=0` claim), and a
    pool size below the documented minimum is refused outright rather than
    silently accepted as "however many connections psycopg feels like
    opening".
    """

    def test_the_pool_enforces_the_requested_max_size(self, schema, backends):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=2)
        backends.append(backend)
        assert backend._pool.max_size == 2, (
            f"pool_size=2 was requested; the pool enforces max_size="
            f"{backend._pool.max_size}"
        )

    def test_an_unused_backend_holds_no_server_connection(self, schema, backends):
        """`min_size=0`, read off the pool's own stats rather than assumed.

        `pg_stat_activity` would answer the same question but shares the
        database with whatever else is connected to it - another suite run,
        a developer's own session - so a count taken there is attributable
        to nobody. The pool's own stats are this backend's alone.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert backend._pool.min_size == 0, (
            f"min_size is {backend._pool.min_size}, not the documented 0"
        )
        stats = backend._pool.get_stats()
        assert stats.get("pool_size", 0) == 0, (
            f"a backend that has done nothing yet already holds "
            f"{stats.get('pool_size')} connection(s): min_size=0 means an "
            "unused backend should hold none"
        )

    def test_a_pool_size_below_the_minimum_is_refused(self, schema):
        with pytest.raises(ValueError):
            PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=0)


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

    @pytest.mark.parametrize(
        "length", [SHORT_BATCH_LENGTH, MID_BATCH_LENGTH, LONG_BATCH_LENGTH]
    )
    def test_a_long_batch_interrupted_part_way_lands_nothing(
        self, schema, backends, length
    ):
        """One transaction, whatever the length.

        The contract's atomicity clause drives a two-operation batch
        through the `interrupt_next_append` hook. A backend that opened a
        fresh transaction every few operations - committing the first
        chunk and failing on a later one - would satisfy that clause and
        still leave a partly-applied batch behind, which is G1 broken on
        exactly the length production issues. The module's shared length
        anchors (see `SHORT_BATCH_LENGTH` and friends at module scope) stand
        in for the fixed [5, 12, 40] this case used to run, so the same
        moving gap the cost/blast-radius family closes applies here too -
        and the failure lands on the LAST operation, so a chunk boundary
        anywhere earlier has already committed something by the time it
        happens.
        """
        print(_pg_length_seed_banner())
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

    def test_an_exhausted_deadlock_retry_gives_its_connections_back(
        self, schema, backends
    ):
        """CN_LEAK_DEADLOCK: the deadlock path, not the payload-error path.

        `TestPostgresEntityWritesSpendTheConnectionBudget
        .test_a_failed_batch_gives_its_connection_back` proves a connection
        comes back after a payload error - one `apply_batch`'s retry loop
        never even sees, because it is not a `DeadlockDetected`. It says
        nothing about the loop's own retried attempts, each of which opens
        its own connection via a fresh `with self._pool.connection()` inside
        `_apply_batch_once`. Only `_apply_one` is stubbed here, so every
        attempt still runs the real connection-acquire/release path; a pool
        of one turns a leak on any of them into the very next write hanging.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        real_apply_one = backend._apply_one

        def always_deadlocks(conn, operation):
            raise psycopg.errors.DeadlockDetected("injected")

        backend._apply_one = always_deadlocks
        with pytest.raises(psycopg.errors.DeadlockDetected):
            backend.apply_batch([EntityOperation.upsert_node(node_payload("a"))])
        backend._apply_one = real_apply_one

        # The write after exhaustion is the whole test: with a connection
        # leaked on any of the DEADLOCK_RETRIES + 1 attempts, the pool of
        # one has nothing left to hand out and this blocks until the pool
        # times out.
        backend.upsert_node(node_payload("after"))
        assert "after" in by_id(backend.load_graph_data(), "nodes")

    def test_a_non_deadlock_error_is_not_retried(self, schema, backends):
        """RT_WIDE: the retry names DeadlockDetected, not psycopg.Error at large.

        `apply_batch` catches `psycopg.errors.DeadlockDetected` specifically.
        A version broadened to `except psycopg.Error` would also retry a
        payload error, a serialization failure, or any other server error up
        to `DEADLOCK_RETRIES` extra times - each one re-issuing whatever of
        the batch had already been re-applied before the fresh error, for an
        error that was never transient to begin with.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        attempts = []

        def always_a_different_error(operations):
            attempts.append(1)
            raise psycopg.errors.UniqueViolation("injected, not a deadlock")

        backend._apply_batch_once = always_a_different_error
        with pytest.raises(psycopg.errors.UniqueViolation):
            backend.apply_batch([EntityOperation.upsert_node(node_payload("a"))])

        assert len(attempts) == 1, (
            f"a non-deadlock psycopg.Error was retried {len(attempts) - 1} "
            "extra time(s) rather than raised on the first attempt"
        )

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
        # Long - LONG_BATCH_LENGTH operations, the module's shared "long
        # batch" anchor - because the short holders above cannot see a lock
        # made exclusive only for longer batches. It is still one length: a
        # gate that opens between it and the next-longest holder walks past
        # this too, which is why it is drawn from the shared, re-randomised
        # source rather than pinned as a hand-picked value.
        "batch_many": lambda b: b.apply_batch(
            [EntityOperation.upsert_node(node_payload("held", name="Held"))]
            + [
                EntityOperation.upsert_edge(edge_payload(f"e{i}", "held", "free"))
                for i in range(LONG_BATCH_LENGTH)
            ]
        ),
        # Delete-leading, and of middling length (the shared MID_BATCH_LENGTH
        # anchor). Every holder above begins with an upsert, so a lock made
        # exclusive for batches that START with a delete - which is exactly
        # the shape `GraphStorage.delete_nodes` builds, edges first - held
        # open here would have been invisible.
        "batch_deletes_first": lambda b: b.apply_batch(
            [EntityOperation.delete_edge(f"gone{i}") for i in range(MID_BATCH_LENGTH)]
            + [EntityOperation.upsert_node(node_payload("held", name="Held"))]
        ),
        # Deletes and nothing else, which is what `delete_nodes` emits:
        # a lock keyed on "no operation in this batch is an upsert" is
        # invisible to every other holder here, the delete-leading one
        # included, because they all end with an upsert. Split from the
        # same shared mid-length anchor rather than a fresh pair of
        # constants.
        "batch_all_deletes": lambda b: b.apply_batch(
            [
                EntityOperation.delete_edge(f"gone{i}")
                for i in range(MID_BATCH_LENGTH // 2)
            ]
            + [
                EntityOperation.delete_node(f"absent{i}")
                for i in range(MID_BATCH_LENGTH // 2)
            ]
        ),
    }

    @pytest.mark.parametrize("holds", sorted(HOLDS))
    def test_a_second_instance_writes_while_another_batch_is_open(
        self, schema, backends, holds
    ):
        print(_pg_length_seed_banner())
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

    def _seed_then_delete(b):
        b.upsert_node(node_payload("gone"))
        b.delete_node("gone")

    WRITES = {
        "node": lambda b: b.upsert_node(node_payload("a")),
        "edge": lambda b: b.upsert_edge(edge_payload("e", "a", "a")),
        # A delete-only case: every other write above upserts, and the
        # substitution this class is about could as easily be made only for
        # the delete path - `_apply_one`'s DELETE branch is a different
        # statement from its INSERT branch, and either could be the one that
        # took the wrong lock form.
        "delete": _seed_then_delete,
        "batch": lambda b: b.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("a")),
                EntityOperation.upsert_edge(edge_payload("e", "a", "a")),
            ]
        ),
        "batch_many": lambda b: b.apply_batch(
            [
                EntityOperation.upsert_node(node_payload(f"n{i}"))
                for i in range(LONG_BATCH_LENGTH)
            ]
        ),
    }

    @pytest.mark.parametrize("write", sorted(WRITES))
    def test_no_advisory_lock_survives_an_entity_write(self, schema, backends, write):
        print(_pg_length_seed_banner())
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot())

        self.WRITES[write](backend)

        # Attributed to the backend under test, not counted database-wide:
        # `pg_locks` sees every session's advisory locks, so an unrelated
        # suite run against the same server - a second CI job, a developer's
        # own session - would false-red this one. Asking the connection the
        # write actually used whether *it* still holds anything narrows the
        # question to this backend, the same way `_wait_until_blocking`
        # narrows "is anything blocked" to "is this pid blocked".
        with backend._pool.connection() as conn:
            held = conn.execute(
                "SELECT count(*) FROM pg_locks"
                " WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchone()[0]

        assert held == 0, (
            f"{held} advisory lock(s) outlived the {write} write on this "
            "backend's own connection: a session-scoped lock on a pooled "
            "connection blocks the next whole-graph save for the life of "
            "the process"
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
        print(_pg_length_seed_banner())
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
                    # The module's shared "long batch" anchor
                    # (LONG_BATCH_LENGTH), not a fixed 40. It is still ONE
                    # length per run, not a spread, so it says nothing about
                    # thresholds either side of whatever it draws - a save
                    # lock skipped below it survives this and the whole
                    # suite - measured, at the old fixed 40. An earlier
                    # version of this comment claimed the opposite, in the
                    # same words the length comment on
                    # TestPostgresEntityWritesTouchOneRow had to withdraw;
                    # drawing this length from the shared, re-randomised
                    # source is what keeps that from happening again. The
                    # filler ids are absent, which is not an error and keeps
                    # the assertion about the three that matter.
                    writer.apply_batch(
                        [
                            EntityOperation.delete_edge("e"),
                            EntityOperation.delete_node("a"),
                            EntityOperation.delete_node("b"),
                        ]
                        + [
                            EntityOperation.delete_node(f"absent{i}")
                            for i in range(LONG_BATCH_LENGTH)
                        ]
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

    def test_the_traversal_runs_repeatable_read_and_leaves_nothing_behind(
        self, schema, backends, monkeypatch
    ):
        """The same two halves as the load, and for the same reason: the
        tearing test infers the level from an answer, which is true but weak.
        It kills a drop to READ COMMITTED because that answer tears - but an
        unnoticed change to SERIALIZABLE gives the right answer and the wrong
        failure mode, and nothing would notice a level left behind on the
        pooled connection either.
        """
        with psycopg.connect(DSN) as check:
            baseline = check.execute("SHOW transaction_isolation").fetchone()[0]

        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

        seen = []
        real_execute = psycopg.Connection.execute

        def spy(conn, query, *args, **kwargs):
            result = real_execute(conn, query, *args, **kwargs)
            if "ISOLATION LEVEL" in str(query).upper() and not seen:
                seen.append(
                    real_execute(conn, "SHOW transaction_isolation").fetchone()[0]
                )
            return result

        monkeypatch.setattr(psycopg.Connection, "execute", spy)
        backend.traverse("a", 2)

        assert seen == ["repeatable read"], (
            f"the traversal did not run at REPEATABLE READ: {seen}"
        )

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


class TestPostgresLoadOrdersByEntityId:
    def test_nodes_and_edges_load_in_id_order_regardless_of_insertion_order(
        self, schema, backends
    ):
        """LD_NOORDER: the documented order, not whatever a heap scan gives.

        `load_graph_data` states `ORDER BY id` for both tables, because a
        plain scan makes no promise about row order at all - correct by
        accident today, wrong the day autovacuum rewrites the table. Every
        other save in this module hands ids to `save_graph_data` already in
        ascending order (`n0`, `n1`, `n2`, ...), so a mutation dropping
        `ORDER BY id` would pass every one of them by getting the right
        answer for the wrong reason. Inserting deliberately out of order
        closes that.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        node_ids = ["n5", "n1", "n9", "n3", "n7"]
        edge_ids = ["e5", "e1", "e9", "e3", "e7"]
        backend.save_graph_data(
            snapshot(
                [node_payload(i) for i in node_ids],
                [edge_payload(i, node_ids[0], node_ids[1]) for i in edge_ids],
            )
        )

        loaded = backend.load_graph_data()
        got_nodes = [n["id"] for n in loaded["nodes"]]
        got_edges = [e["id"] for e in loaded["edges"]]
        assert got_nodes == sorted(node_ids), (
            f"nodes did not load in id order: inserted {node_ids}, got {got_nodes}"
        )
        assert got_edges == sorted(edge_ids), (
            f"edges did not load in id order: inserted {edge_ids}, got {got_edges}"
        )


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
        # ANALYZE excluded on the same ground as the announcement, and not on
        # a weaker one: it writes no graph row, and it runs after the
        # transaction has committed rather than inside it. What the hooks
        # depend on is unchanged - every row the save writes is written before
        # the metadata upsert.
        row_writes = [
            q for q in writes if _tables_named(q) and "ANALYZE" not in q.upper()
        ]
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
        # `scope_id` is nullable, and that is the seam's whole optionality:
        # a row that carries none is the row every store held before the
        # column existed, and the policy admits it to every session. NOT NULL
        # here would break every install that keeps no scopes apart.
        assert columns == {
            ("graph_nodes", "id", "text", "NO"),
            ("graph_nodes", "doc", "jsonb", "NO"),
            ("graph_nodes", SCOPE_COLUMN, "text", "YES"),
            ("graph_edges", "id", "text", "NO"),
            ("graph_edges", "doc", "jsonb", "NO"),
            ("graph_edges", SCOPE_COLUMN, "text", "YES"),
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

    def test_exists_after_a_save_whose_metadata_is_empty(self, schema, backends):
        """SV_SKIPEMPTYMETA: an empty metadata dict is still a save.

        `exists()` reads `graph_metadata`, which `save_graph_data` writes
        unconditionally. Every other save in this module carries
        `snapshot()`'s non-empty metadata (`{"version": "1.0", ...}`), so a
        mutation that skips the metadata upsert when the caller's metadata
        is falsy - `if metadata:` guarding the write, in place of an
        unconditional one - passes every one of them while making a real,
        saved, empty-metadata graph permanently report as never having
        existed.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        assert not backend.exists()
        backend.save_graph_data({"nodes": [], "edges": [], "metadata": {}})
        assert backend.exists(), (
            "a save with empty metadata left graph_metadata empty, so "
            "exists() cannot tell it from a store that was never saved"
        )

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
        survives it. What does not survive is two stores in one database:
        they hear each other's writes and each reads the other's ids out of
        its own schema, reporting deletes for entities that exist elsewhere.
        Measured at four hex characters: a collision inside 211 names.
        """
        names = [f"co_scope_{i}" for i in range(20_000)]
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
        application_name = f"co_reconnect_{uuid.uuid4().hex[:12]}"
        dsn = psycopg.conninfo.make_conninfo(DSN, application_name=application_name)
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema)
        backends.append(backend)
        collector = _Collector()

        def total():
            with psycopg.connect(DSN, autocommit=True) as conn:
                return conn.execute(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    " AND application_name = %s",
                    (application_name,),
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


class TestTheLevelQueryIsNeverPrepared:
    """The traversal's level query must plan against the frontier it was given.

    Its selectivity depends entirely on a parameter: `= ANY(%(frontier)s)` is
    one id on the first level and thousands by the third. PostgreSQL switches
    a PREPARED statement to a generic plan after a few executions, and a
    generic plan cannot see how large that array is - so it plans for the
    small case and then meets the large one.

    Measured at 50,000 nodes, depth 3 from the most connected node, the same
    call repeated: 329, 209, 201, 4358, 4365, 4437, 4402, 4285 ms. The cliff
    is between the third call and the fourth and never recovers, because the
    plan is cached for the life of the connection. Roughly 17x, on the read
    path an interactive canvas uses.

    Asserting the LATENCY would be flaky and would need a large fixture. This
    asserts the server-side fact that causes it instead: no prepared statement
    for the level query exists, however many traversals have run.
    """

    def _pinned_backend(self, schema, backends):
        # One connection, so every traversal below reuses it and the prepare
        # threshold is reached on the connection this test then inspects.
        # pg_prepared_statements is per-session: a pool free to hand out a
        # different connection would let the test look at the wrong one.
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, pool_size=1)
        backends.append(backend)
        return backend

    def test_no_prepared_statement_for_the_level_query_survives_many_traversals(
        self, schema, backends
    ):
        backend = self._pinned_backend(schema, backends)
        backend.save_graph_data(
            {
                "nodes": [
                    {"id": "a", "type": "Actor", "name": "A"},
                    {"id": "b", "type": "Actor", "name": "B"},
                    {"id": "c", "type": "Actor", "name": "C"},
                ],
                "edges": [
                    {"id": "ab", "source": "a", "target": "b", "type": "RELATES_TO"},
                    {"id": "bc", "source": "b", "target": "c", "type": "RELATES_TO"},
                ],
                "metadata": {"version": "1.0", "graph_name": "g"},
            }
        )

        # Well past psycopg's prepare threshold: each depth-2 traversal issues
        # one level execution per level, so this is ~40 executions.
        for _ in range(20):
            backend.traverse("a", 2)

        with backend._pool.connection() as conn:
            prepared = [
                row[0]
                for row in conn.execute(
                    "SELECT statement FROM pg_prepared_statements"
                ).fetchall()
            ]

        # The opt-out is ONE query, not the pool. Without this half, replacing
        # `prepare=False` with a pool-wide `prepare_threshold=None` passes the
        # whole of backend/core/tests - 1648 tests - while the property the
        # change is about is destroyed. The anchor probe is the natural
        # witness: `traverse` issues it on this same connection, inside the
        # same call, so if it is missing the backend stopped preparing
        # everything.
        assert any("graph_nodes" in s and "WHERE id = $1" in s for s in prepared), (
            "the backend stopped preparing its other statements, so the "
            "opt-out is no longer one query but the whole pool: "
            f"{prepared}"
        )

        # CROSS JOIN LATERAL is the level query's fingerprint - no other
        # statement in this backend uses it - so this identifies it without
        # pinning the whole SQL text, which would break on any edit to it.
        # The guard is what keeps that trade honest: rewriting _LEVEL to an
        # equivalent derived-table form drops the phrase, and without this the
        # filter below would match nothing and the test would pass forever
        # while the query prepared again. `test_traversal_equivalence.py`
        # guards its own fingerprint the same way, for the same reason.
        assert "CROSS JOIN LATERAL" in PostgresGraphPersistenceBackend._LEVEL, (
            "the level query no longer contains the fingerprint this test "
            "filters on, so the assertion below matches nothing and passes "
            "vacuously - update both together"
        )
        offenders = [s for s in prepared if "CROSS JOIN LATERAL" in s]
        assert not offenders, (
            "the traversal's level query was prepared, so PostgreSQL will "
            "plan it generically once the plan cache warms and it will stop "
            "seeing how large the frontier is: "
            f"{offenders}"
        )

    def test_the_traversal_still_answers_correctly_without_preparation(
        self, schema, backends
    ):
        """The opt-out must not be bought with a wrong answer: the same walk,
        repeated past the threshold, keeps returning the same thing."""
        backend = self._pinned_backend(schema, backends)
        backend.save_graph_data(
            {
                "nodes": [
                    {"id": "a", "type": "Actor", "name": "A"},
                    {"id": "b", "type": "Actor", "name": "B"},
                    {"id": "c", "type": "Actor", "name": "C"},
                ],
                "edges": [
                    {"id": "ab", "source": "a", "target": "b", "type": "RELATES_TO"},
                    {"id": "bc", "source": "b", "target": "c", "type": "RELATES_TO"},
                ],
                "metadata": {"version": "1.0", "graph_name": "g"},
            }
        )

        answers = set()
        for _ in range(20):
            found = backend.traverse("a", 2)
            answers.add(
                (tuple(sorted(found["node_ids"])), tuple(sorted(found["edge_ids"])))
            )

        assert answers == {(("a", "b", "c"), ("ab", "bc"))}, (
            f"the traversal's answer changed across repeated calls: {answers}"
        )


# --- the optional scope seam ------------------------------------------------


def _stored(schema, table, column=SCOPE_COLUMN):
    """`{id: <column>}` for a table, read past any policy.

    Through the suite's own DSN, whose role created this database and is not
    subject to row-level security: a test about what was STORED must not ask
    through the predicate it is testing, or a row written to the wrong scope
    and a row not written at all read alike.
    """
    with psycopg.connect(DSN, autocommit=True) as conn:
        return {
            row[0]: row[1]
            for row in conn.execute(
                psycopg.sql.SQL("SELECT id, {} FROM {}.{}").format(
                    psycopg.sql.Identifier(column),
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(table),
                )
            )
        }


def _ids_seen_by(dsn, schema, table, scope):
    """Every id a session with `scope` set can read, asked WITHOUT a predicate.

    That is the whole point of the query's shape: the statement names no
    scope, so anything it fails to return was refused by the server rather
    than by the application. `scope=None` leaves the setting unset, which is
    the case the policy has to fail closed on.
    """
    with psycopg.connect(dsn) as conn:
        if scope is not None:
            conn.execute("SELECT set_config(%s, %s, false)", (SCOPE_SETTING, scope))
        return sorted(
            row[0]
            for row in conn.execute(
                psycopg.sql.SQL("SELECT id FROM {}.{}").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table)
                )
            )
        )


def _plans(issued, dsn=DSN, scope=None):
    """Every plan for a statement of `issued` that names a graph table.

    Planned as `dsn`'s role sees it, with the scope setting bound the way the
    backend binds it, so the plan is the one the deployment actually gets -
    policy qual included, where the server applies one.
    """
    plans = []
    with psycopg.connect(dsn) as conn:
        if scope is not None:
            conn.execute("SELECT set_config(%s, %s, false)", (SCOPE_SETTING, scope))
        for query, params in issued:
            text = _rendered(query)
            if "graph_nodes" not in text and "graph_edges" not in text:
                continue
            if params is _EXECUTED_NEVER:
                continue
            rows = conn.execute(psycopg.sql.SQL("EXPLAIN ") + query, params).fetchall()
            plans.append((text, "\n".join(r[0] for r in rows)))
    return plans


def _owning_role(mixed_case=False):
    """A non-superuser role owning a schema of its own, as a deployment has.

    The migration run by this role creates the tables, so the role owns them -
    the ordinary self-provisioning install, and the one where
    `ENABLE ROW LEVEL SECURITY` alone would buy nothing: a table's owner is
    exempt from its own policies until they are FORCEd. A superuser is exempt
    whatever is declared, which is why the suite's own DSN cannot be used to
    ask whether the server refuses anything.
    """
    suffix = uuid.uuid4().hex[:12]
    name = f"co_own_{suffix}"
    schema = f"CoOwn_{suffix}" if mixed_case else f"co_own_{suffix}_sch"
    password = secrets.token_hex(16)
    created = False
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
            created = True
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
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
            if created:
                conn.execute(
                    psycopg.sql.SQL("DROP OWNED BY {}").format(
                        psycopg.sql.Identifier(name)
                    )
                )
                conn.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                        psycopg.sql.Identifier(name)
                    )
                )


class TestTheOptionalScopeSeam:
    """An opaque scope id on the entity tables, and the policy binding on it.

    Every test here is about one of two halves, and they are deliberately
    tested apart. The APPLICATION half is the predicate the backend puts in
    its own statements; it is exercised through the suite's own superuser
    DSN, where row-level security never applies, so what it asserts cannot be
    the server's doing. The SERVER half is the policy, and it is exercised as
    a role that is neither superuser nor exempt - which is why the `owner`
    fixture exists at all, since a policy is not applied to a table's owner
    unless it is forced, and not to a superuser however it is declared.
    """

    def test_scope_wording_stays_generic_in_source(self):
        """The scope value is opaque, so source prose may not give it a domain."""
        offenders = []
        for path in _POSTGRES_SOURCE_FILES:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                folded = line.lower()
                if any(term in folded for term in _BOUNDARY_SENSITIVE_TERMS):
                    offenders.append(
                        f"{path.relative_to(path.parents[3])}:{line_number}: {line}"
                    )

        assert offenders == [], (
            "scope wording must stay generic; replace domain-specific terms: "
            + "\n".join(offenders)
        )

    @pytest.fixture(params=["plain", "MixedCase"], ids=["plain", "needs-quoting"])
    def owner(self, request):
        """Parametrised over the schema's NAME, like the least-privilege
        fixture above and for the same reason.

        This seam adds three quoting surfaces: the catalog lookups keyed on
        `nspname`, the `ALTER TABLE` / `CREATE POLICY` DDL, and the BARE,
        unqualified table reference in the upsert's `ON CONFLICT … WHERE` -
        which is the one place in this change where an identifier reaches SQL
        without its schema. A name that only survives quoted is what tells a
        correct one from a lookup that parses and case-folds.
        """
        yield from _owning_role(mixed_case=request.param == "MixedCase")

    @pytest.fixture
    def other_owner(self):
        """A second store of the first one's kind, for a test that needs two.

        Two schemas rather than two graphs in one: the entity tables are keyed
        on `id` alone, so co-locating two stores that both hold `n0` is not
        something this seam offers - a schema each is, and it is what the
        backend has offered since before the column existed.
        """
        yield from _owning_role()

    @pytest.fixture
    def columnless(self):
        """A store provisioned exactly as the document prescribed BEFORE this
        column existed, and a role that owns none of it.

        Which is the deployment the seam has to stay optional for: the role
        holds DML and no DDL, so `ALTER TABLE` is refused and the column will
        never be there. The indexes are pre-created for the same reason the
        least-privilege tests create them - an index this role cannot build is
        a warning of its own, and it would drown out the question here, which
        is whether the scope seam says anything.
        """
        suffix = uuid.uuid4().hex[:12]
        name = f"co_nocol_{suffix}"
        schema = f"co_nocol_{suffix}_sch"
        password = secrets.token_hex(16)
        created = False
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
                created = True
                conn.execute(
                    psycopg.sql.SQL("CREATE SCHEMA {}").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                for table, columns in (
                    ("graph_nodes", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                    ("graph_edges", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                    (
                        "graph_metadata",
                        "only_row boolean PRIMARY KEY DEFAULT true"
                        " CHECK (only_row), doc jsonb NOT NULL",
                    ),
                ):
                    conn.execute(
                        psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                            psycopg.sql.Identifier(schema),
                            psycopg.sql.Identifier(table),
                            psycopg.sql.SQL(columns),
                        )
                    )
                for index, expression in (
                    ("graph_edges_source_idx", "((doc->>'source'))"),
                    ("graph_edges_target_idx", "((doc->>'target'))"),
                ):
                    conn.execute(
                        psycopg.sql.SQL("CREATE INDEX {} ON {}.graph_edges {}").format(
                            psycopg.sql.Identifier(index),
                            psycopg.sql.Identifier(schema),
                            psycopg.sql.SQL(expression),
                        )
                    )
                conn.execute(
                    psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name)
                    )
                )
                conn.execute(
                    psycopg.sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE"
                        " ON ALL TABLES IN SCHEMA {} TO {}"
                    ).format(
                        psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name)
                    )
                )
            yield _dsn_as_role(name, password), schema
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                if created:
                    conn.execute(
                        psycopg.sql.SQL("DROP OWNED BY {}").format(
                            psycopg.sql.Identifier(name)
                        )
                    )
                    conn.execute(
                        psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                            psycopg.sql.Identifier(name)
                        )
                    )

    @staticmethod
    def _as_owner(owner, backends, scope=None):
        name, schema, password = owner
        backend = PostgresGraphPersistenceBackend(
            _dsn_as_role(name, password), schema=schema, scope=scope
        )
        backends.append(backend)
        return backend

    # -- the server half -----------------------------------------------------

    def test_the_migration_adds_the_column_and_forces_its_policy(self, owner, backends):
        name, schema, password = owner
        backend = self._as_owner(owner, backends, scope="scope-a")
        backend.exists()

        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in SCOPED_TABLES:
                enabled, forced, policy = conn.execute(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity,"
                    " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relname = %s",
                    (schema, table),
                ).fetchone()
                assert enabled, f"{table} has no row-level security"
                # FORCE is the half that matters here: this role OWNS the
                # table, and an owner is exempt from an unforced policy - so
                # without it every isolation assertion below would pass for a
                # store that isolates nothing.
                assert forced, f"{table} does not force row-level security"
                assert policy, f"{table} has row-level security and no policy"

    def test_the_server_refuses_a_row_belonging_to_another_scope(self, owner, backends):
        name, schema, password = owner
        role_dsn = _dsn_as_role(name, password)
        self._as_owner(owner, backends, scope="scope-a").upsert_node(node_payload("a"))
        self._as_owner(owner, backends, scope="scope-b").upsert_node(node_payload("b"))

        assert _ids_seen_by(role_dsn, schema, "graph_nodes", "scope-a") == ["a"]
        assert _ids_seen_by(role_dsn, schema, "graph_nodes", "scope-b") == ["b"]

    def test_the_server_refuses_a_scoped_row_to_a_session_that_named_no_scope(
        self, owner, backends
    ):
        """The direction that has to fail closed.

        An unset setting is the case a mistake arrives as - a host that forgot
        to bind it, a connection that came from somewhere else - and the
        expression is written so that `= NULL` is never true rather than so
        that an absent value matches everything.
        """
        name, schema, password = owner
        self._as_owner(owner, backends, scope="scope-a").upsert_node(node_payload("a"))

        assert (
            _ids_seen_by(_dsn_as_role(name, password), schema, "graph_nodes", None)
            == []
        )

    def test_a_row_carrying_no_scope_is_refused_to_nobody(self, owner, backends):
        """What an already-populated table does when this lands.

        Every row written before the column existed carries NULL, and the
        policy admits NULL to every session: an install that never asked to
        keep scopes apart does not discover one day that its graph is empty.
        """
        name, schema, password = owner
        role_dsn = _dsn_as_role(name, password)
        self._as_owner(owner, backends).save_graph_data(snapshot([node_payload("a")]))

        assert _stored(schema, "graph_nodes") == {"a": None}
        assert _ids_seen_by(role_dsn, schema, "graph_nodes", None) == ["a"]
        assert _ids_seen_by(role_dsn, schema, "graph_nodes", "scope-a") == ["a"]

    def test_a_scope_round_trips_every_path_under_its_own_policy(self, owner, backends):
        """Every path, as the one role a policy is actually enforced against.

        The suite's own DSN is a superuser, so row-level security is inert
        there and the application's predicate is the only thing under test on a
        load, a walk or a read-back. Under a forced policy a path that does not
        bind the scope reads an EMPTY store rather than a wrong one, which is
        the direction that hides: the load returns nothing, and the whole-graph
        save - which does bind - then deletes everything the load did not
        return. Measured on a copy with the load's binding removed: the store
        ended empty and the instance had destroyed its own graph.

        So this walks the whole contract as that role rather than asserting one
        statement: save, load, traverse, upsert, delete, exists, and the
        listener's read-back.
        """
        name, schema, password = owner
        backend = self._as_owner(owner, backends, scope="scope-a")

        backend.save_graph_data(
            snapshot(
                [node_payload("a1"), node_payload("a2")],
                [edge_payload("e1", "a1", "a2")],
            )
        )

        assert backend.exists()
        loaded = backend.load_graph_data()
        assert [n["id"] for n in loaded["nodes"]] == ["a1", "a2"], (
            "the instance cannot read the rows it just wrote through its own policy"
        )
        assert [e["id"] for e in loaded["edges"]] == ["e1"]

        found = backend.traverse("a1", 2)
        assert sorted(found["node_ids"]) == ["a1", "a2"]
        assert found["edge_ids"] == ["e1"]

        backend.upsert_node(node_payload("a3"))
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == [
            "a1",
            "a2",
            "a3",
        ]

        # The read-back the listener uses: the store decides what happened, and
        # a row this instance owns has to read as present.
        resolved = backend._resolve([("node", "a3")])
        assert [(op.kind, op.action, op.entity_id) for op in resolved] == [
            ("node", "upsert", "a3")
        ], f"the read-back lost a row the instance owns: {resolved}"

        backend.delete_node("a3")
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a1", "a2"]

        # And a save of what the load returned is a no-op rather than a loss -
        # the whole round trip, which is where a path that reads nothing and a
        # save that deletes everything meet.
        backend.save_graph_data(backend.load_graph_data())
        assert _stored(schema, "graph_nodes") == {"a1": "scope-a", "a2": "scope-a"}

    def test_the_server_refuses_a_row_written_into_another_scope(self, owner, backends):
        """The policy's WRITE direction, which a SELECT probe never asks about.

        `FOR ALL` with no `WITH CHECK` of its own means the `USING` expression
        governs what a session may write as well as what it may read. Without
        that, a session could be refused a row and accept one written in its
        place - so this issues the INSERT the backend never would, with the
        setting bound to one scope and the row stamped another.
        """
        name, schema, password = owner
        self._as_owner(owner, backends, scope="scope-a").upsert_node(node_payload("a"))

        with psycopg.connect(_dsn_as_role(name, password)) as conn:
            conn.execute("SELECT set_config(%s, %s, false)", (SCOPE_SETTING, "scope-b"))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    psycopg.sql.SQL(
                        "INSERT INTO {}.graph_nodes (id, doc, {}) VALUES (%s, %s, %s)"
                    ).format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(SCOPE_COLUMN),
                    ),
                    (
                        "smuggled",
                        psycopg.types.json.Jsonb(node_payload("smuggled")),
                        "scope-a",
                    ),
                )
            conn.rollback()

        assert "smuggled" not in _stored(schema, "graph_nodes")

        # And the two statements another scope would actually issue. `FOR ALL`
        # means the USING expression governs these as well, so narrowing the
        # policy to FOR SELECT would leave the reads refused and the writes
        # through - and a test that asked only about INSERT would not notice.
        for scope in ("scope-b", None):
            with psycopg.connect(_dsn_as_role(name, password)) as conn:
                if scope is not None:
                    conn.execute(
                        "SELECT set_config(%s, %s, false)", (SCOPE_SETTING, scope)
                    )
                updated = conn.execute(
                    psycopg.sql.SQL(
                        "UPDATE {}.graph_nodes SET doc = %s WHERE id = %s"
                    ).format(psycopg.sql.Identifier(schema)),
                    (psycopg.types.json.Jsonb(node_payload("a", name="taken")), "a"),
                ).rowcount
                deleted = conn.execute(
                    psycopg.sql.SQL("DELETE FROM {}.graph_nodes WHERE id = %s").format(
                        psycopg.sql.Identifier(schema)
                    ),
                    ("a",),
                ).rowcount
                conn.rollback()
            assert (updated, deleted) == (0, 0), (
                f"a session scoped {scope!r} reached another scope's row with a "
                f"bare UPDATE/DELETE"
            )
        assert _stored(schema, "graph_nodes") == {"a": "scope-a"}

    def test_the_two_layers_agree_on_a_cross_scope_upsert(self, owner, backends):
        """The refusal, asked where the server is enforcing as well.

        The application half is tested through the superuser DSN, where no
        policy applies - so nothing there pins the claim this change makes in
        both its code and its document: that where the policy IS in force the
        server refuses the same write. That is a genuine branch in the server
        rather than a consequence of this SQL (how PostgreSQL treats an
        `ON CONFLICT DO UPDATE` whose conflicting row is invisible under
        `USING`), so if it ever changed, a host would start seeing a different
        exception from the same misconfiguration and nothing would notice.
        """
        name, schema, password = owner
        a = self._as_owner(owner, backends, scope="scope-a")
        b = self._as_owner(owner, backends, scope="scope-b")
        a.upsert_node(node_payload("n0", name="A's own"))

        with pytest.raises(CrossScopeWriteRefused):
            b.upsert_node(node_payload("n0", name="B's version"))

        assert _stored(schema, "graph_nodes") == {"n0": "scope-a"}
        assert by_id(a.load_graph_data(), "nodes")["n0"]["name"] == "A's own"

    def test_the_policy_admits_a_scopeless_row_to_every_session(self, owner, backends):
        """The `IS NULL` arm, asked of a table that actually has the policy.

        The sibling test writes its scopeless row through an unscoped instance,
        which by design creates no policy at all - so it cannot tell this arm
        from a table with no row-level security. Here the policy is provisioned
        by a scoped instance and the scopeless row is planted underneath it,
        which is the state an upgraded store is in: rows from before the column
        existed, under a policy added after them.
        """
        name, schema, password = owner
        role_dsn = _dsn_as_role(name, password)
        self._as_owner(owner, backends, scope="scope-a").upsert_node(
            node_payload("mine")
        )
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {}.graph_nodes (id, doc) VALUES (%s, %s)"
                ).format(psycopg.sql.Identifier(schema)),
                ("legacy", psycopg.types.json.Jsonb(node_payload("legacy"))),
            )

        assert _ids_seen_by(role_dsn, schema, "graph_nodes", "scope-a") == [
            "legacy",
            "mine",
        ]
        assert _ids_seen_by(role_dsn, schema, "graph_nodes", None) == ["legacy"], (
            "a row carrying no scope was refused to a session that named none"
        )

    # -- the application half ------------------------------------------------

    def test_a_scoped_instance_does_not_load_another_scopes_rows(
        self, schema, backends
    ):
        """Through the suite's own DSN, where the policy never applies.

        So this is the backend's own predicate and nothing else: a deployment
        whose role cannot take a policy - an operator's least-privilege setup -
        still keeps the scopes apart, and a defect in the SQL cannot hide
        behind the server.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("a"))
        b.upsert_node(node_payload("b"))

        assert [n["id"] for n in a.load_graph_data()["nodes"]] == ["a"]
        assert [n["id"] for n in b.load_graph_data()["nodes"]] == ["b"]

    def test_a_scoped_instance_traverses_through_another_scopes_node(
        self, schema, backends
    ):
        """A node it may not see reads as an endpoint that is not a node.

        Which is the dangling-endpoint rule the walk already has, reached by a
        second road: the edge is returned, the far id is traversed THROUGH,
        and it is not in `node_ids`. The alternative - an inner join - would
        have dropped the edge as well and told the caller a different graph.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("a"))
        b.upsert_node(node_payload("b"))
        b.upsert_edge(edge_payload("ab", "a", "b"))

        found = b.traverse("b", 3)
        assert found["edge_ids"] == ["ab"]
        assert found["node_ids"] == ["b"], (
            "a node outside this scope was returned as a node of the walk"
        )

    def test_a_scoped_instance_traverses_from_nothing_it_does_not_own(
        self, schema, backends
    ):
        """An anchor outside the scope is no traversal, not an empty one.

        The walk's own rule: an anchor that is not there returns nothing at all
        rather than a lone anchor, and a node this instance may not see is not
        there as far as it is concerned. Without the anchor probe's predicate
        the other scope's id comes back as a node of the walk - the one place
        the traversal reports an id it never read a row for.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("na"))

        assert b.traverse("na", 2) == {"node_ids": [], "edge_ids": []}

    def test_the_read_back_reports_another_scopes_row_as_absent(self, schema, backends):
        """The listener's read-back, which shares a channel with every scope.

        The notification channel is derived from the SCHEMA, so two scopes
        behind one set of tables hear each other's announcements. What the
        read-back must not do is answer one with the foreign row's content: the
        store decides what happened, and a row this instance may not read did
        not happen to it.

        `b` is migrated first, and that is the state the read-back runs in
        rather than a convenience: `_resolve` is reached only from the
        listening thread, which exists only once `start_change_notification`
        has run - and that migrates. Calling it on a freshly constructed
        backend instead asks the question in a state no caller can produce,
        where the predicate is absent because the store has not yet been
        examined, and reads as a defect that is not one.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("na", name="A's own"))
        b.exists()  # migrates b, the way starting its listener would

        resolved = b._resolve([("node", "na")])

        assert [(op.kind, op.action, op.entity_id) for op in resolved] == [
            ("node", "delete", "na")
        ], f"the read-back handed over another scope's row: {resolved}"
        assert all(op.payload is None for op in resolved)

    def test_a_scoped_instance_cannot_delete_another_scopes_row(self, schema, backends):
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("a"))

        b.delete_node("a")

        assert _stored(schema, "graph_nodes") == {"a": "scope-a"}

    def test_every_row_a_scoped_instance_writes_carries_its_scope(
        self, schema, backends
    ):
        """Both write paths, because they are separate statements.

        A whole-graph save inserts, an entity write upserts, and stamping one
        and not the other leaves rows the instance can write and never read
        again.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)
        backend.save_graph_data(
            snapshot([node_payload("a")], [edge_payload("e", "a", "a")])
        )
        backend.upsert_node(node_payload("b"))

        assert _stored(schema, "graph_nodes") == {"a": "scope-a", "b": "scope-a"}
        assert _stored(schema, "graph_edges") == {"e": "scope-a"}

    def test_an_unscoped_instance_writes_rows_that_carry_no_scope(
        self, schema, backends
    ):
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))
        backend.upsert_node(node_payload("b"))

        assert _stored(schema, "graph_nodes") == {"a": None, "b": None}

    def test_a_whole_graph_save_replaces_exactly_what_the_load_returned(
        self, schema, backends
    ):
        """The interesting failure is the save that deletes LESS than it loads.

        A store written before a scope was configured holds rows carrying
        none, and a scoped instance loads them - the policy and the predicate
        both admit them. If its save then deleted only rows carrying its own
        scope, GraphStorage would hand back a row the delete left behind and
        the insert would die on the primary key. Nothing else in the suite
        reaches this: it needs a store holding both kinds of row at once.
        """
        PostgresGraphPersistenceBackend(DSN, schema=schema).save_graph_data(
            snapshot([node_payload("old")])
        )
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)

        loaded = backend.load_graph_data()
        assert [n["id"] for n in loaded["nodes"]] == ["old"]
        backend.save_graph_data(loaded)

        assert _stored(schema, "graph_nodes") == {"old": "scope-a"}

    def test_an_unscoped_instance_does_not_reach_a_scoped_row(self, schema, backends):
        """The unscoped side of the same predicate, which is not symmetric
        with the scoped side by accident.

        An instance that named no scope sees rows carrying none - the set the
        policy would show it - and NOT rows carrying one. The whole-graph save
        is the half that matters: it deletes what the load returned, so an
        instance reading every row would delete every row, and a scoped store
        sharing the schema would lose its graph to a maintenance instance that
        only meant to re-save its own.
        """
        scoped = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        plain = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.extend([scoped, plain])
        scoped.upsert_node(node_payload("a"))

        assert [n["id"] for n in plain.load_graph_data()["nodes"]] == []
        plain.save_graph_data(snapshot([node_payload("b")]))

        assert _stored(schema, "graph_nodes") == {"a": "scope-a", "b": None}
        assert [n["id"] for n in scoped.load_graph_data()["nodes"]] == ["a", "b"]

    def test_a_scoped_instance_does_not_traverse_another_scopes_edge(
        self, schema, backends
    ):
        """The edge half of the traversal's predicate, on its own.

        Both endpoints belong to the traversing scope, so only the EDGE is out
        of scope - which is what makes this fail for a backend that filters
        nodes and forgets edges. Without it the walk returns an edge the
        instance may not see, and with it the far node is unreachable by that
        road.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("a1"))
        a.upsert_node(node_payload("a2"))
        b.upsert_edge(edge_payload("cross", "a1", "a2"))

        found = a.traverse("a1", 3)
        assert found["edge_ids"] == [], (
            "the walk returned an edge belonging to another scope"
        )
        assert found["node_ids"] == ["a1"]

    def test_a_scoped_upsert_refuses_an_id_another_scope_holds(self, schema, backends):
        """Through the suite's own DSN, where no policy applies - so this is
        the application layer refusing, not the server.

        `id` is unique across the table, so the conflicting row may be another
        scope's, and an upsert is the one write path where the predicate cannot
        simply hide it: before the conflict carried one, the statement replaced
        that row's content and restamped its scope, and the other scope's next
        load returned nothing. A no-op would be no better - the caller would be
        told a write happened that did not - so it raises.
        """
        a = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        b = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
        backends.extend([a, b])
        a.upsert_node(node_payload("n0", name="A's own"))

        with pytest.raises(CrossScopeWriteRefused) as raised:
            b.upsert_node(node_payload("n0", name="B's version"))
        assert "n0" in str(raised.value)

        assert _stored(schema, "graph_nodes") == {"n0": "scope-a"}
        kept = by_id(a.load_graph_data(), "nodes")["n0"]
        assert kept["name"] == "A's own", (
            "another scope's upsert changed the row it may not write"
        )

    def test_a_scoped_upsert_updates_a_row_this_instance_already_owns(
        self, schema, backends
    ):
        """The arm of the refusal that must NOT refuse.

        The statement has four reachable outcomes - a fresh insert, the
        adoption of a scopeless row, an update of this instance's own row, and
        a conflict with another scope's - and only the last is a refusal. With
        the first three tested and this one missing, the suite cannot tell
        "refuses a cross-scope write" from "refuses every update": passing the
        wrong parameter to the conflict predicate collapses it to
        `scope_id IS NULL`, and every second write to an id raises. Measured on
        a copy with that one parameter changed - the suite stayed green and a
        scoped instance could not update a row it had written itself.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)

        backend.upsert_node(node_payload("n0", name="first"))
        backend.upsert_node(node_payload("n0", name="second"))

        assert _stored(schema, "graph_nodes") == {"n0": "scope-a"}
        assert by_id(backend.load_graph_data(), "nodes")["n0"]["name"] == "second"

    def test_an_upsert_still_adopts_a_row_that_carries_no_scope(self, schema, backends):
        """The refusal above must not catch the case it shares a statement with.

        A row written before a scope was configured carries none, and the
        predicate admits it - so an upsert of it is this instance's to make,
        and it takes the scope with it.
        """
        PostgresGraphPersistenceBackend(DSN, schema=schema).upsert_node(
            node_payload("old", name="from before")
        )
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)

        backend.upsert_node(node_payload("old", name="Renamed"))

        assert _stored(schema, "graph_nodes") == {"old": "scope-a"}
        assert by_id(backend.load_graph_data(), "nodes")["old"]["name"] == "Renamed"

    def test_a_column_without_its_policy_is_reported_once_asked_for(
        self, columnless, backends, capsys
    ):
        """The only thing that tells an operator the server enforces nothing.

        The operator added the column and not the policy - the role owns
        nothing, so the backend cannot add either - and a scope was configured.
        The instance then runs on its own predicate alone, which is a fact
        about that deployment that nothing else would state.
        """
        dsn, schema = columnless
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in SCOPED_TABLES:
                conn.execute(
                    psycopg.sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} text").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.Identifier(SCOPE_COLUMN),
                    )
                )
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, scope="scope-a")
        backends.append(backend)
        capsys.readouterr()

        backend.save_graph_data(snapshot([node_payload("a")]))

        printed = capsys.readouterr().out
        assert "not its row-level security policy" in printed, (
            f"nothing said the server enforces nothing: {printed}"
        )
        for table in SCOPED_TABLES:
            assert table in printed
        # And it runs: the application's predicate is what holds here.
        assert _stored(schema, "graph_nodes") == {"a": "scope-a"}
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]

    # -- optional, and what happens when it cannot be provisioned ------------

    def test_a_store_without_the_column_is_read_and_written_as_it_was(
        self, columnless, backends, capsys
    ):
        """Nothing about such a store may change.

        Not the statements, which must not name a column that is not there,
        and not the output: a store that keeps no scopes apart is not missing
        anything by having no policy, and a warning that fires when nothing is
        wrong teaches an operator to ignore warnings.
        """
        dsn, schema = columnless
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema)
        backends.append(backend)
        # Migrated first, so what is captured below is the data path rather
        # than the boot. The migration DOES name the column, once, in the
        # `ALTER TABLE` this role refuses - that attempt is how the backend
        # finds out the column is not there, and it is not a statement any
        # read or write issues.
        #
        # The boot's own output is read rather than discarded, though. Clearing
        # it unread is how a seam that announces itself on every columnless
        # boot would go unnoticed - and the only thing this store is entitled
        # to hear about is the ANALYZE its role cannot run, which it heard
        # about before this seam existed.
        backend.exists()
        self._assert_silent_about_the_seam(capsys.readouterr().out)

        issued = _statements_issued(
            lambda: (
                backend.save_graph_data(
                    snapshot([node_payload("a")], [edge_payload("e", "a", "a")])
                ),
                backend.upsert_node(node_payload("b")),
                backend.delete_node("b"),
                backend.traverse("a", 2),
                backend.load_graph_data(),
            )
        )

        rendered = [_rendered(query) for query, _ in issued]
        named = [text for text in rendered if SCOPE_COLUMN in text]
        assert not named, f"a statement named a column this store has not: {named}"
        # The PARAMETERS too, not only the SQL. The setting's name reaches the
        # server as a bind parameter of `set_config`, so it can never appear in
        # a statement's text - an assertion that looked only there would be one
        # that cannot fail, and a binding added to the unscoped path would
        # satisfy it.
        assert not [
            (text, params)
            for text, (_, params) in zip(rendered, issued)
            if "set_config" in text
            or (params and SCOPE_SETTING in [str(value) for value in params])
        ], "an unscoped instance bound a scope setting"
        self._assert_silent_about_the_seam(capsys.readouterr().out)
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]

    @staticmethod
    def _assert_silent_about_the_seam(printed):
        """No line of `printed` is about the scope seam.

        Not silence outright: this role cannot ANALYZE, and the backend has
        said so since before the seam existed. Silence about THE SEAM, because
        a store that keeps no scopes apart is not missing anything by having no
        column and no policy, and a warning that fires when nothing is wrong
        teaches an operator to ignore warnings.
        """
        spoke = [
            line
            for line in printed.splitlines()
            if SCOPE_COLUMN in line
            or SCOPE_SETTING in line
            or "row-level" in line.lower()
            or "scope" in line.lower()
        ]
        assert not spoke, (
            f"the scope seam spoke up on a store that asked for none: {spoke}"
        )

    def test_a_scope_against_a_store_without_the_column_refuses_to_start(
        self, columnless, backends
    ):
        """The one case that does not degrade quietly.

        Writing rows that carry no scope would hand them to every other
        session on the store, and a store that cannot be provisioned is an
        operator's problem with a remedy - so this raises rather than
        proceeding, and the message names the column to add.
        """
        dsn, schema = columnless
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, scope="scope-a")
        backends.append(backend)

        with pytest.raises(ScopeIsolationUnavailable) as raised:
            backend.save_graph_data(snapshot([node_payload("a")]))
        assert SCOPE_COLUMN in str(raised.value)
        assert _stored(schema, "graph_nodes", "id") == {}, (
            "a row was written by a backend that could not scope it"
        )

    @pytest.mark.parametrize(
        "provision",
        ["column only", "policy, not enabled", "enabled, not forced"],
    )
    def test_a_scope_the_server_does_not_enforce_is_reported(
        self, columnless, backends, capsys, provision
    ):
        """Three ways for the server to be enforcing nothing, and the warning
        has to fire for each.

        A policy that exists but was never ENABLEd is inert, and one enabled
        without FORCE does not apply to the table's owner - so checking only
        that a policy is PRESENT would report a store as protected when its
        rows are readable by every session on it. Parametrised because with
        the column-only case alone, the `enabled` and `forced` conjuncts are
        never the thing that decides.
        """
        dsn, schema = columnless
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in SCOPED_TABLES:
                conn.execute(
                    psycopg.sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} text").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.Identifier(SCOPE_COLUMN),
                    )
                )
                if provision == "column only":
                    continue
                conn.execute(
                    psycopg.sql.SQL(
                        "CREATE POLICY {} ON {}.{} FOR ALL USING ({col} IS NULL"
                        " OR {col} = current_setting({setting}, true))"
                    ).format(
                        psycopg.sql.Identifier(f"{table}{SCOPE_POLICY_SUFFIX}"),
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        col=psycopg.sql.Identifier(SCOPE_COLUMN),
                        setting=psycopg.sql.Literal(SCOPE_SETTING),
                    )
                )
                if provision == "policy, not enabled":
                    continue
                conn.execute(
                    psycopg.sql.SQL(
                        "ALTER TABLE {}.{} ENABLE ROW LEVEL SECURITY"
                    ).format(
                        psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table)
                    )
                )
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, scope="scope-a")
        backends.append(backend)
        capsys.readouterr()

        backend.save_graph_data(snapshot([node_payload("a")]))

        printed = capsys.readouterr().out
        assert "not by the server" in printed, (
            f"a store the server does not enforce was not reported ({provision}): "
            f"{printed}"
        )
        # And it still runs on the application's own predicate.
        assert _stored(schema, "graph_nodes") == {"a": "scope-a"}

    def test_an_unscoped_instance_on_a_half_provisioned_store_says_nothing(
        self, columnless, backends, capsys
    ):
        """The column on one scoped table and not the other, with no scope.

        The scoped instance refuses such a store. The unscoped one must run on
        it, and run as it did before the column existed - naming the column
        nowhere, because `graph_edges` has not got it. What decides that is
        that the catalog re-read finds `graph_edges` missing and leaves the
        flag False for BOTH tables; a version that took the flag from whether
        the ALTER was attempted would read `graph_nodes` with the predicate
        and then fail on every edge query with `column scope_id does not
        exist`.
        """
        dsn, schema = columnless
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("ALTER TABLE {}.graph_nodes ADD COLUMN {} text").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(SCOPE_COLUMN),
                )
            )
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema)
        backends.append(backend)
        backend.exists()
        self._assert_silent_about_the_seam(capsys.readouterr().out)

        issued = _statements_issued(
            lambda: (
                backend.save_graph_data(
                    snapshot([node_payload("a")], [edge_payload("e", "a", "a")])
                ),
                backend.traverse("a", 2),
                backend.load_graph_data(),
            )
        )

        named = [text for q, _ in issued if SCOPE_COLUMN in (text := _rendered(q))]
        assert not named, f"a statement named a column graph_edges has not: {named}"
        assert [n["id"] for n in backend.load_graph_data()["nodes"]] == ["a"]
        assert _stored(schema, "graph_nodes") == {"a": None}

    def test_a_scope_refuses_a_store_with_the_column_on_only_one_table(
        self, columnless, backends
    ):
        """Half-provisioned is not half-isolated, it is not isolated.

        The provisioning loop is one transaction PER TABLE, so a store really
        can end with the column on one and not the other - and this repo
        already treats half-provisioned stores as a live case. A backend that
        refused only when EVERY scoped table lacked the column would boot here
        and write edges carrying no scope, readable by every other session, on
        behalf of an instance that asked to be separated.
        """
        dsn, schema = columnless
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("ALTER TABLE {}.graph_nodes ADD COLUMN {} text").format(
                    psycopg.sql.Identifier(schema),
                    psycopg.sql.Identifier(SCOPE_COLUMN),
                )
            )
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, scope="scope-a")
        backends.append(backend)

        with pytest.raises(ScopeIsolationUnavailable) as raised:
            backend.save_graph_data(snapshot([node_payload("a")]))
        assert "graph_edges" in str(raised.value)
        assert _stored(schema, "graph_nodes", "id") == {}

    def test_an_empty_scope_is_refused_rather_than_read_as_no_scope(self):
        """An empty value is what a variable templated out of a deploy command
        arrives as, and reading it as "no isolation wanted" is how a deployment
        that asked to be separated silently is not."""
        for value in ("", "   "):
            with pytest.raises(ValueError):
                PostgresGraphPersistenceBackend(DSN, scope=value)

    def test_the_seam_leaves_the_metadata_table_alone(self, schema, backends):
        """Stated as a test because it is a boundary, not an oversight.

        `graph_metadata`'s primary key is a column that can only hold true, so
        the table holds one row for the whole store and has nowhere to put a
        second scope's. Adding a column there would suggest an isolation the
        shape cannot deliver.
        """
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

        with psycopg.connect(DSN, autocommit=True) as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'graph_metadata'",
                    (schema,),
                )
            }
        assert columns == {"only_row", "doc"}

    def test_the_scope_leaves_no_trace_on_a_pooled_connection(self, schema, backends):
        """`set_config(..., is_local => true)`, not a session-wide SET.

        One connection in the pool, so the connection taken below is the one
        the write used. A scope left on it would reach whatever ran next -
        including, on a host that pools across scopes, a read that then
        answered with someone else's rows.
        """
        backend = PostgresGraphPersistenceBackend(
            DSN, schema=schema, scope="scope-a", pool_size=1
        )
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

        with backend._pool.connection() as conn:
            left = conn.execute(
                "SELECT current_setting(%s, true)", (SCOPE_SETTING,)
            ).fetchone()[0]
        # Falsy rather than None: measured on PostgreSQL 16, a placeholder GUC
        # that has been set locally and reverted reads back as the empty string
        # rather than as unset, so asserting None would fail for correct code.
        # What matters is that no scope survives, and the empty string cannot
        # BE a scope - the constructor refuses one, and so does the
        # configuration layer - so it cannot match a row this instance wrote.
        assert not left, f"the scope outlived its transaction: {left!r}"

    # Small enough to seed in a test and large enough that the planner
    # prefers the expression indexes over reading the table: measured on this
    # server, a one-id frontier plans a Bitmap Index Scan on both of them at
    # 1000 edges and a Seq Scan at 200, where the assertions below would hold
    # for a backend with no indexes at all.
    PLAN_SEED = 1000

    def _plan_seeded(self, owner, backends, scope, bind=True):
        """The traversal's plans on a seeded store, as its own role sees them.

        `bind=False` plans the same statements for a session that is subject to
        no policy, which is how the two costs below are told apart: what the
        application's own predicate costs, and what a policy costs on top of
        it.
        """
        name, schema, password = owner
        backend = self._as_owner(owner, backends, scope=scope)
        backend.save_graph_data(
            snapshot(
                [node_payload(f"n{i}") for i in range(self.PLAN_SEED)],
                [edge_payload(f"e{i}", f"n{i}", "n0") for i in range(self.PLAN_SEED)],
            )
        )
        with psycopg.connect(DSN, autocommit=True) as conn:
            # So the plan asserted is the one a live store gets rather than
            # the one a never-analysed table happens to get.
            conn.execute(
                psycopg.sql.SQL("ANALYZE {}.graph_nodes, {}.graph_edges").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(schema)
                )
            )
        # Depth 1, so there is one level and its frontier is one id. That is
        # the selective case the indexes were measured on; a later level whose
        # frontier is most of the graph may legitimately read the table.
        issued = _statements_issued(lambda: backend.traverse("n5", 1))
        if bind:
            return _plans(issued, _dsn_as_role(name, password), scope)
        return _plans(issued, DSN)

    @staticmethod
    def _edge_indexes(plans):
        return {
            index
            for text, plan in plans
            if "graph_edges" in text
            for index in ("graph_edges_source_idx", "graph_edges_target_idx")
            if index in plan
        }

    def test_the_scope_predicate_costs_the_traversal_no_index(
        self, owner, other_owner, backends
    ):
        """The predicate this change adds composes with the expression indexes.

        Both stores planned for a session no policy applies to, so what is
        compared is the SQL and only the SQL: one store's statements carry the
        scope predicate and the other's do not, and the planner reaches the
        same two indexes either way. Those indexes are the difference between
        a walk that seeks and one that reads every edge at every level, so a
        predicate that cost them would be paid at every step of every walk.
        """
        scoped = self._plan_seeded(owner, backends, "scope-a", bind=False)
        unscoped = self._plan_seeded(other_owner, backends, None, bind=False)

        assert self._edge_indexes(unscoped) == {
            "graph_edges_source_idx",
            "graph_edges_target_idx",
        }, f"the seed is too small to make the indexes worth using: {unscoped}"
        assert self._edge_indexes(scoped) == self._edge_indexes(unscoped), (
            f"the scope predicate changed which indexes the traversal reaches: "
            f"{self._edge_indexes(scoped)} against {self._edge_indexes(unscoped)}"
        )

    def test_a_policy_in_force_costs_the_traversal_those_indexes(self, owner, backends):
        """The price of the second layer, pinned so it cannot move silently.

        A policy reaches a query as a security qual, and PostgreSQL will not
        evaluate a qual that is not leakproof before one. The traversal's index
        condition is `doc->>'source' = ...`, and `jsonb_object_field_text` is
        not leakproof - so a store the policy applies to reads the edge table
        at every level instead of seeking, whatever this backend writes. That
        is why the policy is created only for a store that configured a scope,
        why the document says so in the section an operator reads before
        turning it on, and why this test asserts the cost rather than its
        absence: the day PostgreSQL marks that function leakproof, or the day
        this backend stops filtering on a JSONB expression, this test fails and
        the document it guards is the thing to fix.
        """
        plans = self._plan_seeded(owner, backends, "scope-a")

        assert plans, "the traversal planned nothing against a graph table"
        assert self._edge_indexes(plans) == set(), (
            f"the expression indexes are reachable under a policy after all - "
            f"the document says they are not: {plans}"
        )
        assert any("Seq Scan on graph_edges" in plan for _, plan in plans)
        with psycopg.connect(DSN) as conn:
            assert conn.execute(
                "SELECT proleakproof FROM pg_proc WHERE proname = 'texteq'"
            ).fetchone()[0], (
                "texteq is not leakproof, so the reason given above is not the reason"
            )
            assert not conn.execute(
                "SELECT proleakproof FROM pg_proc"
                " WHERE proname = 'jsonb_object_field_text'"
            ).fetchone()[0], (
                "jsonb_object_field_text is leakproof on this server, so the "
                "cost above has some other cause than the one documented"
            )

    def test_a_store_that_configured_no_scope_gets_no_policy(
        self, owner, backends, capsys
    ):
        """The column, and nothing that would cost it its traversal plan.

        Which is the whole reason the policy is not created unconditionally: a
        store that keeps no scopes apart would be paying the cost above for a
        guarantee it did not ask for and does not get.
        """
        name, schema, password = owner
        backend = self._as_owner(owner, backends)
        capsys.readouterr()
        backend.save_graph_data(snapshot([node_payload("a")]))

        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in SCOPED_TABLES:
                enabled, policy = conn.execute(
                    "SELECT c.relrowsecurity,"
                    " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relname = %s",
                    (schema, table),
                ).fetchone()
                assert not enabled, f"{table} took row-level security unasked"
                assert not policy, f"{table} took a policy unasked"
                assert _stored(schema, table, "id") is not None
        assert capsys.readouterr().out == ""
        # The column is there all the same, which is what lets a host turn the
        # seam on later without rewriting a table full of rows.
        assert _stored(schema, "graph_nodes") == {"a": None}

    @pytest.mark.parametrize(
        "refuse_on", ["ROW LEVEL SECURITY", "CREATE POLICY"], ids=["enable", "policy"]
    )
    def test_a_provisioning_step_that_fails_leaves_nothing_half_done(
        self, schema, backends, monkeypatch, refuse_on
    ):
        """Row-level security with no policy admits NOTHING.

        So the step that enables it and the step that creates the policy have
        to be one transaction: a store that ended up with the first and not
        the second would answer every query with an empty graph, and look
        exactly like a store whose data was gone.

        WITH a scope configured, which is the only case that issues those
        statements at all - the unscoped path returns before them, so the same
        test without a scope refuses nothing, asserts that a store which never
        tried is not half-way, and would pass with the two steps in either
        order. The refusal count below is asserted for exactly that reason.

        Both steps are refused in turn, because one of them is not enough
        either. Refusing only the ENABLE leaves the ordering itself untested:
        a version that enabled row-level security BEFORE creating the policy,
        and failed in between, would end with the table admitting nothing -
        and would pass a test that only ever refuses the statement it puts
        second.
        """
        real = psycopg.Cursor.execute
        refused = []

        def refuse(cur, query, params=None, *args, **kwargs):
            text = query if isinstance(query, str) else _rendered(query)
            if refuse_on in text.upper():
                refused.append(text)
                raise psycopg.errors.InsufficientPrivilege("refused for the test")
            return real(cur, query, params, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", refuse)
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
        backends.append(backend)
        # The rollback takes the ADD COLUMN with it, so the store ends with no
        # column - and a scope with nowhere to go is the refusal, not a warning.
        with pytest.raises(ScopeIsolationUnavailable):
            backend.save_graph_data(snapshot([node_payload("a")]))
        monkeypatch.undo()

        assert refused, "no row-level-security statement was issued to refuse"
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in SCOPED_TABLES:
                enabled, policy = conn.execute(
                    "SELECT c.relrowsecurity,"
                    " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relname = %s",
                    (schema, table),
                ).fetchone()
                assert not enabled, (
                    f"{table} was left with row-level security enabled by a "
                    f"step that did not finish"
                )
                assert not policy
        # And an unscoped instance still reads and writes it, because the
        # rollback left the table exactly as it found it.
        plain = PostgresGraphPersistenceBackend(DSN, schema=schema)
        backends.append(plain)
        plain.save_graph_data(snapshot([node_payload("a")]))
        assert [n["id"] for n in plain.load_graph_data()["nodes"]] == ["a"]


class TestTheDocumentPublishesTheScopeSeam:
    """The names an operator provisions from, pinned from both sides.

    A host binds the setting and provisions the column by NAME, from
    docs/PERSISTENCE_BACKENDS.md. Renaming either in the code and not in the
    document leaves every test above green and every operator's store
    unreadable by the instance that is meant to read it.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[3]
    DOC = ROOT / "docs" / "PERSISTENCE_BACKENDS.md"
    # The repo's canonical environment-variable table, which lists every
    # sibling of this setting. It reads as exhaustive, so a variable missing
    # from it reads as one that does not exist.
    ENV_TABLE = ROOT / "backend" / "DEVELOPMENT.md"

    def test_the_document_names_what_the_code_uses(self):
        published = self.DOC.read_text(encoding="utf-8")
        for name in (SCOPE_COLUMN, SCOPE_SETTING, "GRAPH_POSTGRES_SCOPE"):
            assert name in published, (
                f"{name} is what the code uses and the document does not name it"
            )
        for table in SCOPED_TABLES:
            assert f"{table}{SCOPE_POLICY_SUFFIX}" in published, (
                f"the policy on {table} is not in the document an operator "
                f"provisions from"
            )

    def test_the_environment_table_lists_the_setting_beside_its_siblings(self):
        """Pinned because the omission is invisible: the table listed the other
        four `GRAPH_POSTGRES_*` variables and not this one, and nothing else
        reads that file."""
        listed = self.ENV_TABLE.read_text(encoding="utf-8")
        assert "GRAPH_POSTGRES_POOL_SIZE" in listed, (
            "this test is pinned to the wrong table"
        )
        assert "GRAPH_POSTGRES_SCOPE" in listed, (
            "the setting is missing from the environment table that lists "
            "every one of its siblings"
        )
