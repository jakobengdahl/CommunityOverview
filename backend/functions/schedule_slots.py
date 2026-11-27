"""Persistent schedule-slot identities, separate from purgable run history.

The ledger stores no function input or output. An uncertain running attempt is
never automatically reclaimed: the caller must establish that it has stopped
and failed before reporting failure. This favors at-most-once execution over
recovery after a crash with an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
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


class ScheduleSlots:
    """Durable slot queue. Latest due initial slot per function wins catch-up.

    Pending retries are independent of catch-up selection. A permanent watermark
    also skips older slots enqueued after a newer slot has already been claimed.
    """

    def __init__(self, path: str | Path):
        if str(path) in ("", ":memory:"):
            raise ValueError("Schedule slots require a file-backed database")
        self._store = SqliteExecutionStore(path)
        with self._store._lock:
            self._store._conn.execute(
                "CREATE TABLE IF NOT EXISTS schedule_watermarks "
                "(function_id TEXT PRIMARY KEY, fire_time REAL NOT NULL)"
            )
            self._store._conn.commit()

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
                    UPDATE execution_jobs AS j SET state = ?,
                        dead_letter_reason = 'superseded schedule slot',
                        finished_at = ?, updated_at = ?
                    WHERE j.state = ? AND j.attempts = 0 AND j.run_at <= ?
                      AND (EXISTS (SELECT 1 FROM execution_jobs AS newer
                            WHERE newer.agent_id = j.agent_id AND newer.attempts = 0
                              AND newer.run_at <= ? AND newer.run_at > j.run_at)
                           OR EXISTS (SELECT 1 FROM schedule_watermarks AS w
                            WHERE w.function_id = j.agent_id AND w.fire_time >= j.run_at))
                """,
                    (
                        ExecutionState.CANCELLED.value,
                        ts,
                        ts,
                        ExecutionState.PENDING.value,
                        ts,
                        ts,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT * FROM execution_jobs WHERE state = ? AND run_at <= ?
                    ORDER BY run_at, created_at LIMIT 1
                """,
                    (ExecutionState.PENDING.value, ts),
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
                        (row["agent_id"], row["run_at"]),
                    )
                conn.execute(
                    """
                    UPDATE execution_jobs SET state = ?, attempts = attempts + 1,
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

    def complete(self, job_id: str, *, now: datetime):
        return self._finish(job_id, now=now, success=True)

    def fail(self, job_id: str, *, now: datetime):
        """Report a confirmed stopped failure, never an uncertain lease expiry."""
        return self._finish(job_id, now=now, success=False)

    def _finish(self, job_id: str, *, now: datetime, success: bool):
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Completion time must be timezone-aware")
        store = self._store
        ts = now.timestamp()
        with store._lock:
            conn = store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                job = store._get_by_id(job_id)
                if job is None:
                    raise JobNotFoundError(job_id)
                if job.state != ExecutionState.RUNNING:
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
                    UPDATE execution_jobs SET state = ?, run_at = ?,
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
