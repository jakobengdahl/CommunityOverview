"""
Durable SQLite execution-store adapter tests.

The SQLite adapter is held to the identical behavioural contract as the
reference in-memory adapter via ``ExecutionStoreContractTests`` — same
semantics, real persistence. Beyond the shared contract, this module proves the
one property the in-memory adapter cannot: state survives a process restart
(reopening the same database file recovers every job and its state).
"""

import sqlite3
from datetime import timedelta

import pytest

from backend.agents.execution.contract import ExecutionStoreContractTests, T0, _job
from backend.agents.execution.models import (
    ABANDONED_REASON,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
)
from backend.agents.execution import sqlite_store
from backend.agents.execution.sqlite_store import SqliteExecutionStore, _to_epoch
from backend.agents.execution.store import ExecutionStore
from backend.agents.tests._sql_recording import RecordingConn, only_statement


class TestSqliteExecutionStore(ExecutionStoreContractTests):
    """Bind the adapter-agnostic contract to the durable SQLite adapter."""

    @pytest.fixture
    def store(self, tmp_path):
        s = SqliteExecutionStore(
            tmp_path / "execution.db", retry_policy=self.retry_policy
        )
        try:
            yield s
        finally:
            s.close()


def test_sqlite_store_satisfies_protocol(tmp_path):
    # runtime_checkable Protocol: structural conformance of the durable adapter.
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        assert isinstance(store, ExecutionStore)
    finally:
        store.close()


def test_state_survives_restart(tmp_path):
    """The durability property: reopening the same file recovers job state."""
    db = tmp_path / "execution.db"
    policy = RetryPolicy(max_attempts=3, base_seconds=10.0)

    store = SqliteExecutionStore(db, retry_policy=policy)
    # Distinct run_at values make the claim order deterministic: the pending job
    # is scheduled far in the future so it is never claimed here.
    pending = store.enqueue(
        _job(idempotency_key="pending", run_at=T0 + timedelta(seconds=3600))
    )
    running = store.enqueue(
        _job(idempotency_key="running", run_at=T0 - timedelta(seconds=10))
    )
    done = store.enqueue(_job(idempotency_key="done", run_at=T0 - timedelta(seconds=5)))
    store.claim_next("w1", now=T0, lease_seconds=60)  # oldest run_at -> running
    store.claim_next("w1", now=T0, lease_seconds=60)  # next -> done
    # Complete one explicitly by id so we assert a known terminal outcome.
    store.complete(done.id, result={"ok": True}, now=T0)
    store.close()

    # "Restart": a brand-new store object over the same file, nothing in memory.
    reopened = SqliteExecutionStore(db, retry_policy=policy)
    try:
        assert reopened.get(pending.id).state == ExecutionState.PENDING
        recovered_running = reopened.get(running.id)
        assert recovered_running.state == ExecutionState.RUNNING
        assert recovered_running.lease_owner == "w1"
        finished = reopened.get(done.id)
        assert finished.state == ExecutionState.SUCCEEDED
        assert finished.result == {"ok": True}
        # All three jobs are still present and inspectable.
        assert len(reopened.list_jobs()) == 3
    finally:
        reopened.close()


def test_running_job_recovered_after_restart_and_lease_expiry(tmp_path):
    """A job left RUNNING by a crash is reclaimable once its lease expires."""
    db = tmp_path / "execution.db"
    policy = RetryPolicy(max_attempts=3, base_seconds=10.0)

    store = SqliteExecutionStore(db, retry_policy=policy)
    job = store.enqueue(_job())
    claimed = store.claim_next("w1", now=T0, lease_seconds=60)
    assert claimed.state == ExecutionState.RUNNING
    store.close()  # process "crashes" mid-run, leaving the job RUNNING

    reopened = SqliteExecutionStore(db, retry_policy=policy)
    try:
        # Before the lease expires nothing is runnable...
        assert reopened.claim_next("w2", now=T0 + timedelta(seconds=30)) is None
        # ...after it expires a new worker reclaims it, counting a fresh attempt.
        reclaimed = reopened.claim_next("w2", now=T0 + timedelta(seconds=90))
        assert reclaimed is not None
        assert reclaimed.id == job.id
        assert reclaimed.attempts == 2
        assert reclaimed.lease_owner == "w2"
    finally:
        reopened.close()


def test_migration_is_idempotent_across_reopens(tmp_path):
    """Reopening an existing database re-runs no migration and keeps data."""
    db = tmp_path / "execution.db"
    first = SqliteExecutionStore(db)
    first.enqueue(_job(idempotency_key="keep"))
    first.close()

    second = SqliteExecutionStore(db)
    try:
        version = second._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
        assert len(second.list_jobs()) == 1
    finally:
        second.close()


def test_v1_database_migrates_forward_keeping_rows(tmp_path):
    """A schema-v1 file gains redacted_at (unset) and keeps every job."""
    import sqlite3

    from backend.agents.execution.sqlite_store import _CREATE_SCHEMA

    db = tmp_path / "execution.db"
    conn = sqlite3.connect(db)
    conn.executescript(_CREATE_SCHEMA)
    conn.execute(
        "INSERT INTO execution_jobs (id, agent_id, kind, idempotency_key, state,"
        " run_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("job-v1", "a1", "event", "k1", "succeeded", 1.0, 1.0, 1.0),
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    store = SqliteExecutionStore(db)
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        job = store.get("job-v1")
        assert job is not None and job.redacted_at is None
        assert store.redact(["job-v1"], now=T0) == 1
        assert store.get("job-v1").redacted_at == T0
    finally:
        store.close()


def test_purge_matches_quote_bearing_id_literally(tmp_path):
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        target = store.enqueue(_job(idempotency_key="t"))
        store.complete(target.id, now=T0)
        hostile = f"{target.id}' OR '1'='1"
        assert store.purge([hostile]) == 0
        assert store.get(target.id) is not None
        assert store.purge([target.id]) == 1
    finally:
        store.close()


def test_list_jobs_binds_every_filter_value(tmp_path):
    # The f-string SQL carries "nosec B608": this pins that only fixed column
    # clauses and "?" reach the text, and every caller value is a bound param.
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        hostile = "a1' OR '1'='1"
        recorder = RecordingConn(store._conn)
        store._conn = recorder
        store.list_jobs(
            states=[ExecutionState.PENDING, ExecutionState.RUNNING],
            agent_id=hostile,
            kind=ExecutionKind.SCHEDULED,
            limit=7331,
        )
        sql, params = only_statement(recorder, "SELECT * FROM execution_jobs")
        assert params == ["pending", "running", hostile, "scheduled", 7331]
        assert sql.count("?") == len(params)
        assert "'" not in sql
        assert "7331" not in sql
        for value in ("pending", "running", "scheduled"):
            assert value not in sql
    finally:
        store.close()


def test_recover_stale_binds_every_job_id(tmp_path):
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        jobs = [store.enqueue(_job(idempotency_key=f"k{i}")) for i in range(3)]
        for _ in jobs:
            store.claim_next("w1", now=T0, lease_seconds=60)
        recorder = RecordingConn(store._conn)
        store._conn = recorder
        now = T0 + timedelta(seconds=90)
        recovered = store.recover_stale(now=now)
        assert len(recovered) == 3
        sql, params = only_statement(recorder, "UPDATE execution_jobs")
        now_ts = _to_epoch(now)
        assert params[:3] == ["pending", now_ts, now_ts]
        assert sorted(params[3:]) == sorted(j.id for j in jobs)
        assert sql.count("?") == len(params)
        assert "'" not in sql
        assert "pending" not in sql
        assert str(int(now_ts)) not in sql
        for j in jobs:
            assert j.id not in sql
    finally:
        store.close()


def _v1_file(db):
    import sqlite3

    from backend.agents.execution.sqlite_store import _CREATE_SCHEMA

    conn = sqlite3.connect(db)
    # A v1 file written by this store is already in (persistent) WAL mode.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CREATE_SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()


def test_migration_survives_crash_between_alter_and_version_bump(tmp_path):
    """A v1 file that already has redacted_at (crash mid-migration) still opens."""
    import sqlite3

    db = tmp_path / "execution.db"
    _v1_file(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE execution_jobs ADD COLUMN redacted_at REAL")
    conn.commit()
    conn.close()

    store = SqliteExecutionStore(db)
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()


def test_concurrent_opens_of_a_v1_file_migrate_once(tmp_path):
    """Several stores opening one v1 file at once all succeed."""
    import threading

    db = tmp_path / "execution.db"
    _v1_file(db)
    barrier = threading.Barrier(8)
    errors, stores = [], []

    def open_store():
        barrier.wait()
        try:
            stores.append(SqliteExecutionStore(db))
        except Exception as exc:  # pragma: no cover - the failure being tested
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    assert errors == []
    assert len(stores) == 8


@pytest.mark.parametrize("operation", ["redact", "purge"])
def test_removed_content_leaves_no_trace_in_db_or_wal(tmp_path, operation):
    """secure_delete + WAL checkpoint: the marker is gone from every file."""
    import os

    marker = "MARKER-7f3a9c-unique-content"
    db = tmp_path / "execution.db"
    store = SqliteExecutionStore(db)
    try:
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        job = store.enqueue(
            _job(idempotency_key="m", payload={"event": {"summary": marker * 50}})
        )
        store.complete(job.id, result={"summary": marker}, now=T0)
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if operation == "redact":
            assert store.redact([job.id], now=T0) == 1
        else:
            assert store.purge([job.id]) == 1
        wal = str(db) + "-wal"
        assert not os.path.exists(wal) or os.path.getsize(wal) == 0
        for path in (db, wal):
            if os.path.exists(path):
                assert marker.encode() not in open(path, "rb").read()
    finally:
        store.close()


def test_abandon_survives_restart(tmp_path):
    """An abandoned run is committed: a fresh store over the file sees it."""
    db = tmp_path / "execution.db"
    store = SqliteExecutionStore(db)
    job = store.enqueue(
        _job(
            idempotency_key="orphan",
            run_at=T0 - timedelta(days=40),
            state=ExecutionState.RUNNING,
            updated_at=T0 - timedelta(days=40),
        )
    )
    assert store.abandon([job.id], cutoff=T0 - timedelta(days=30), now=T0) == 1
    store.close()

    reopened = SqliteExecutionStore(db)
    try:
        out = reopened.get(job.id)
        assert out.state == ExecutionState.DEAD_LETTER
        assert out.dead_letter_reason == ABANDONED_REASON
    finally:
        reopened.close()


class _LockedOnWalSwitch:
    """Connection proxy whose first WAL switches fail as a contended one does."""

    def __init__(self, conn, failures):
        self._conn = conn
        self.failures = failures
        self.attempts = 0

    def execute(self, sql, *args):
        if sql == "PRAGMA journal_mode=WAL":
            self.attempts += 1
            if self.attempts <= self.failures:
                raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ("_conn", "failures", "attempts"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


def _patch_connect(monkeypatch, failures):
    proxies = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        proxy = _LockedOnWalSwitch(real_connect(*args, **kwargs), failures)
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(sqlite3, "connect", connect)
    return proxies


def test_wal_switch_retries_when_the_file_is_locked(tmp_path, monkeypatch):
    """A locked WAL switch on a fresh file is retried, not raised."""
    proxies = _patch_connect(monkeypatch, failures=3)
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        assert proxies[0].attempts == 4
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        store.close()


class _VirtualClock:
    """Stand-in for ``time`` in the store: sleeping advances a virtual clock.

    Every ``monotonic`` read also ticks the clock, so a loop that never sleeps
    still reaches its deadline instead of hanging the test.
    """

    TICK = 0.0001

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        self.now += self.TICK
        assert self.now < 60, "retry loop did not terminate"
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_wal_switch_gives_up_after_the_busy_timeout(tmp_path, monkeypatch):
    """A switch that stays locked surfaces once the busy timeout has passed."""
    _patch_connect(monkeypatch, failures=10**9)
    clock = _VirtualClock()
    monkeypatch.setattr(sqlite_store, "time", clock)
    # Neither the old 50 ms value nor the 5 s default: only the constant can
    # explain a give-up time near 0.3 s.
    monkeypatch.setattr(sqlite_store, "_BUSY_TIMEOUT_MS", 300)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        SqliteExecutionStore(tmp_path / "execution.db")
    slack = max(clock.sleeps) + 10 * _VirtualClock.TICK
    assert 0.3 <= clock.now <= 0.3 + slack


def test_wal_switch_backs_off_between_retries(tmp_path, monkeypatch):
    """Retries sleep briefly: no busy spin, no sleep that eats the budget."""
    _patch_connect(monkeypatch, failures=10**9)
    clock = _VirtualClock()
    monkeypatch.setattr(sqlite_store, "time", clock)
    monkeypatch.setattr(sqlite_store, "_BUSY_TIMEOUT_MS", 300)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        SqliteExecutionStore(tmp_path / "execution.db")
    # A busy spin would sleep never; a long sleep would leave only a few tries.
    assert len(clock.sleeps) >= 10
    assert all(0 < s <= 0.05 for s in clock.sleeps)


def test_wal_switch_does_not_retry_other_errors(tmp_path, monkeypatch):
    """Only lock contention is retried; any other failure surfaces at once."""
    attempts = []
    real_connect = sqlite3.connect

    class Failing(_LockedOnWalSwitch):
        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: Failing(real_connect(*a, **k), 0)
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        SqliteExecutionStore(tmp_path / "execution.db")
    assert len(attempts) == 1


def test_concurrent_first_opens_of_a_fresh_file_all_succeed(tmp_path):
    """Several stores opening one brand-new (non-WAL) file at once all succeed."""
    import threading

    errors, stores = [], []
    for round_no in range(10):
        db = tmp_path / f"fresh-{round_no}.db"
        barrier = threading.Barrier(8)

        def open_store():
            barrier.wait()
            try:
                stores.append(SqliteExecutionStore(db))
            except Exception as exc:  # pragma: no cover - the failure being tested
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    for s in stores:
        s.close()
    assert errors == []
    assert len(stores) == 80


def test_busy_timeout_is_set_before_the_wal_switch_and_the_migration(
    tmp_path, monkeypatch
):
    """The wait budget must apply to every statement a concurrent opener can block."""
    statements = []
    real_connect = sqlite3.connect

    class Recording(RecordingConn):
        def __init__(self, conn):
            super().__init__(conn)
            self.statements = statements

        def __setattr__(self, name, value):
            if name in ("_conn", "statements"):
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: Recording(real_connect(*a, **k))
    )
    SqliteExecutionStore(tmp_path / "execution.db").close()
    sql = [s.strip() for s, _ in statements]
    busy = sql.index(f"PRAGMA busy_timeout={sqlite_store._BUSY_TIMEOUT_MS}")
    assert busy < sql.index("PRAGMA journal_mode=WAL")
    assert busy < sql.index("BEGIN IMMEDIATE")


def test_failed_migration_rolls_back_and_the_next_open_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "execution.db"
    _v1_file(db)
    monkeypatch.setattr(sqlite_store, "_MIGRATE_V2", "ALTER TABLE no_such ADD x REAL")
    with pytest.raises(sqlite3.OperationalError, match="no_such"):
        SqliteExecutionStore(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        cols = [r[1] for r in conn.execute("PRAGMA table_info(execution_jobs)")]
        assert "redacted_at" not in cols
    finally:
        conn.close()

    monkeypatch.undo()
    store = SqliteExecutionStore(db)
    try:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "redacted_at" in store._columns()
    finally:
        store.close()


@pytest.mark.parametrize("concurrent_change", ["running", "redacted"])
def test_redact_leaves_a_row_changed_after_the_read_alone(
    tmp_path, monkeypatch, concurrent_change
):
    """The SQL guard, not the Python one, protects against another process."""
    store = SqliteExecutionStore(tmp_path / "execution.db")
    try:
        job = store.enqueue(_job(idempotency_key="r", payload={"secret": "keep"}))
        store.complete(job.id, result={"r": 1}, now=T0)
        real_get = store._get_by_id

        def get_then_change(job_id):
            row = real_get(job_id)  # read as terminal and unredacted
            if concurrent_change == "running":
                store._conn.execute(
                    "UPDATE execution_jobs SET state = ? WHERE id = ?",
                    (ExecutionState.RUNNING.value, job_id),
                )
            else:
                store._conn.execute(
                    "UPDATE execution_jobs SET redacted_at = 1.0 WHERE id = ?",
                    (job_id,),
                )
            store._conn.commit()
            return row

        monkeypatch.setattr(store, "_get_by_id", get_then_change)
        assert store.redact([job.id], now=T0) == 0
        monkeypatch.undo()
        row = store._conn.execute(
            "SELECT payload, redacted_at FROM execution_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()
        assert "keep" in row["payload"]
        assert row["redacted_at"] == (1.0 if concurrent_change == "redacted" else None)
    finally:
        store.close()
