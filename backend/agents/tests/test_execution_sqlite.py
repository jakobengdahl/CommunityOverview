"""
Durable SQLite execution-store adapter tests.

The SQLite adapter is held to the identical behavioural contract as the
reference in-memory adapter via ``ExecutionStoreContractTests`` — same
semantics, real persistence. Beyond the shared contract, this module proves the
one property the in-memory adapter cannot: state survives a process restart
(reopening the same database file recovers every job and its state).
"""

from datetime import timedelta

import pytest

from backend.agents.execution.contract import ExecutionStoreContractTests, T0, _job
from backend.agents.execution.models import (
    ExecutionState,
    RetryPolicy,
)
from backend.agents.execution.sqlite_store import SqliteExecutionStore
from backend.agents.execution.store import ExecutionStore


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
        assert version == 1
        assert len(second.list_jobs()) == 1
    finally:
        second.close()
