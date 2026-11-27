"""Persistent schedule-slot identities, separate from purgable run history.

The ledger stores no function input or output. An uncertain running attempt is
never automatically reclaimed: the caller must establish that it has stopped
and failed before reporting failure. This favors at-most-once execution over
recovery after a crash with an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.execution.store import JobNotFoundError


class _LedgerStore:
    """Private persistence for schedule jobs, never the execution queue."""

    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'execution_jobs' "
                "AND type IN ('table', 'view')"
            ).fetchone():
                raise ValueError(
                    "Schedule ledger cannot use an execution store database"
                )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schedule_jobs (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL, run_at REAL NOT NULL,
                    lease_owner TEXT, lease_expiry REAL, correlation_id TEXT,
                    session_id TEXT, origin TEXT, last_error TEXT, dead_letter_reason TEXT,
                    result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, redacted_at REAL
                )
            """)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schedule_watermarks "
                "(function_id TEXT PRIMARY KEY, fire_time REAL NOT NULL)"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            self._conn.close()
            raise

    def close(self):
        with self._lock:
            self._conn.close()

    def enqueue(self, job: ExecutionJob) -> ExecutionJob:
        with self._lock:
            self._conn.execute(
                "INSERT INTO schedule_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                SqliteExecutionStore._to_row(job),
            )
            row = self._conn.execute(
                "SELECT * FROM schedule_jobs WHERE idempotency_key = ?",
                (job.idempotency_key,),
            ).fetchone()
            self._conn.commit()
            return SqliteExecutionStore._row_to_job(row)

    def _get_by_id(self, job_id: str):
        row = self._conn.execute(
            "SELECT * FROM schedule_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return SqliteExecutionStore._row_to_job(row) if row is not None else None


class ScheduleSlots:
    """Durable slot queue with at most one initial claim per recovery window.

    Of the slots already queued, the latest due slot wins. A claim also closes
    the window through its claim time, skipping slots enqueued later for that
    window. Pending retries are independent of catch-up selection.
    """

    def __init__(self, path: str | Path):
        if str(path) in ("", ":memory:"):
            raise ValueError("Schedule slots require a file-backed database")
        self._store = _LedgerStore(path)

    def close(self):
        self._store.close()

    def enqueue(
        self,
        function_id: str,
        fire_time: datetime,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> ExecutionJob:
        if not isinstance(function_id, str) or not function_id:
            raise ValueError("Invalid function id")
        if not isinstance(fire_time, datetime) or fire_time.utcoffset() is None:
            raise ValueError("Fire time must be timezone-aware")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise ValueError("Invalid retry policy")
        fire_time = fire_time.astimezone(timezone.utc)
        identity = json.dumps(
            [function_id, fire_time.isoformat()], separators=(",", ":")
        )
        key = "function-schedule:" + hashlib.sha256(identity.encode()).hexdigest()
        policy = retry_policy or RetryPolicy(max_attempts=1)
        return self._store.enqueue(
            ExecutionJob(
                agent_id=function_id,
                kind=ExecutionKind.SCHEDULED,
                idempotency_key=key,
                correlation_id=key,
                run_at=fire_time,
                payload={"retry_policy": policy.to_dict()},
            )
        )

    def claim_next(self, worker_id: str, *, now: datetime, lease_seconds: float):
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("Worker id is required")
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Claim time must be timezone-aware")
        if not 0 < lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        ts = now.timestamp()
        store = self._store
        with store._lock:
            conn = store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Initial slots superseded by another due slot or the durable
                # watermark are recorded as skipped; retries are never skipped.
                conn.execute(
                    """
                    UPDATE schedule_jobs AS j SET state = ?,
                        dead_letter_reason = 'superseded schedule slot',
                        finished_at = ?, updated_at = ?
                    WHERE j.kind = ? AND j.idempotency_key GLOB 'function-schedule:*'
                      AND j.state = ? AND j.attempts = 0 AND j.run_at <= ?
                      AND (EXISTS (SELECT 1 FROM schedule_jobs AS newer
                            WHERE newer.kind = ?
                              AND newer.idempotency_key GLOB 'function-schedule:*'
                              AND newer.agent_id = j.agent_id AND newer.attempts = 0
                              AND newer.run_at <= ? AND newer.run_at > j.run_at)
                           OR EXISTS (SELECT 1 FROM schedule_watermarks AS w
                            WHERE w.function_id = j.agent_id AND w.fire_time >= j.run_at))
                """,
                    (
                        ExecutionState.CANCELLED.value,
                        ts,
                        ts,
                        ExecutionKind.SCHEDULED.value,
                        ExecutionState.PENDING.value,
                        ts,
                        ExecutionKind.SCHEDULED.value,
                        ts,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT * FROM schedule_jobs WHERE kind = ?
                      AND idempotency_key GLOB 'function-schedule:*'
                      AND state = ? AND run_at <= ?
                    ORDER BY run_at, created_at LIMIT 1
                """,
                    (ExecutionKind.SCHEDULED.value, ExecutionState.PENDING.value, ts),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                if row["attempts"] == 0:
                    conn.execute(
                        """
                        INSERT INTO schedule_watermarks VALUES (?, ?)
                        ON CONFLICT(function_id) DO UPDATE SET fire_time =
                            MAX(fire_time, excluded.fire_time)
                    """,
                        (row["agent_id"], ts),
                    )
                conn.execute(
                    """
                    UPDATE schedule_jobs SET state = ?, attempts = attempts + 1,
                        lease_owner = ?, lease_expiry = ?,
                        started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE id = ?
                """,
                    (
                        ExecutionState.RUNNING.value,
                        worker_id,
                        ts + lease_seconds,
                        ts,
                        ts,
                        row["id"],
                    ),
                )
                job = store._get_by_id(row["id"])
                conn.commit()
                return job
            except Exception:
                conn.rollback()
                raise

    def complete(self, job_id: str, *, attempt: int, now: datetime):
        return self._finish(job_id, attempt=attempt, now=now, success=True)

    def fail(self, job_id: str, *, attempt: int, now: datetime):
        """Report a confirmed stopped failure, never an uncertain lease expiry."""
        return self._finish(job_id, attempt=attempt, now=now, success=False)

    def _finish(self, job_id: str, *, attempt: int, now: datetime, success: bool):
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Attempt must be a positive integer")
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Completion time must be timezone-aware")
        store = self._store
        ts = now.timestamp()
        with store._lock:
            conn = store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                job = store._get_by_id(job_id)
                if (
                    job is None
                    or job.kind != ExecutionKind.SCHEDULED
                    or not job.idempotency_key.startswith("function-schedule:")
                ):
                    raise JobNotFoundError(job_id)
                if job.state != ExecutionState.RUNNING or job.attempts != attempt:
                    conn.commit()
                    return job
                # Old ledger rows have no declared retry and therefore fail closed.
                policy = (
                    RetryPolicy.from_dict(job.payload["retry_policy"])
                    if "retry_policy" in job.payload
                    else RetryPolicy(max_attempts=1)
                )
                if success:
                    state, run_at, reason = (
                        ExecutionState.SUCCEEDED,
                        job.run_at.timestamp(),
                        None,
                    )
                elif job.attempts >= policy.max_attempts:
                    state, run_at, reason = (
                        ExecutionState.DEAD_LETTER,
                        job.run_at.timestamp(),
                        "attempts exhausted",
                    )
                else:
                    state, run_at, reason = (
                        ExecutionState.PENDING,
                        ts + policy.backoff_seconds(job.attempts),
                        None,
                    )
                conn.execute(
                    """
                    UPDATE schedule_jobs SET state = ?, run_at = ?,
                        last_error = ?, dead_letter_reason = ?, lease_owner = NULL,
                        lease_expiry = NULL, updated_at = ?, finished_at = ?
                    WHERE id = ?
                """,
                    (
                        state.value,
                        run_at,
                        None if success else "attempt failed",
                        reason,
                        ts,
                        ts if state.is_terminal else None,
                        job_id,
                    ),
                )
                result = store._get_by_id(job_id)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
