"""Persistent schedule-slot identities, separate from purgable run history.

The ledger stores graph event snapshots needed for delivery, but no function output. An uncertain running attempt is
never automatically reclaimed: the caller must establish that it has stopped
and failed before reporting failure. This favors at-most-once execution over
recovery after a crash with an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
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
from backend.agents.execution.models import ABANDONED_REASON


def _valid_retry_policy(policy: RetryPolicy) -> bool:
    return (
        type(policy.max_attempts) is int
        and 1 <= policy.max_attempts <= 10
        and 0 <= policy.base_seconds <= 86400
        and 1 <= policy.factor <= 10
        and 0 <= policy.max_seconds <= 86400
        and all(
            math.isfinite(value)
            for value in (policy.base_seconds, policy.factor, policy.max_seconds)
        )
    )


def _retry_at(policy: RetryPolicy, attempts: int, ts: float) -> float:
    delay = min(policy.base_seconds, policy.max_seconds)
    for _ in range(attempts - 1):
        delay = min(delay * policy.factor, policy.max_seconds)
    run_at = ts + delay
    # The store converts this timestamp back to a datetime when reading it.
    datetime.fromtimestamp(run_at, timezone.utc)
    return run_at


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
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schedule_rate "
                "(function_id TEXT NOT NULL, admitted_at REAL NOT NULL)"
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
            try:
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
            except Exception:
                self._conn.rollback()
                raise

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

    def __init__(self, path: str | Path, *, rate_limit: int = 60):
        if str(path) in ("", ":memory:"):
            raise ValueError("Schedule slots require a file-backed database")
        if type(rate_limit) is not int or rate_limit < 1:
            raise ValueError("Invalid rate limit")
        self.rate_limit = rate_limit
        self._store = _LedgerStore(path)

    def close(self):
        self._store.close()

    def enqueue_manifest(self, root, function_id: str, fire_time: datetime):
        """Validate the package and persist its declared schedule retry policy."""
        from backend.functions.invocation import load_manifest

        manifest, _ = load_manifest(root, function_id)
        if not any(t["kind"] == "schedule" for t in manifest["triggers"]):
            raise ValueError("Schedule trigger not declared")
        retry = manifest.get("retry")
        return self.enqueue(
            function_id, fire_time, retry_policy=RetryPolicy(**retry) if retry else None
        )

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
        if retry_policy is not None and (
            not isinstance(retry_policy, RetryPolicy)
            or not _valid_retry_policy(retry_policy)
        ):
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

    def enqueue_event(
        self,
        function_id,
        event,
        entity,
        retry_policy=None,
        *,
        graph_id=None,
        node_types=None,
        matched_triggers=None,
    ):
        payload = event.to_webhook_payload()
        identity = json.dumps([function_id, payload["event_id"]], separators=(",", ":"))
        key = "function-graph:" + hashlib.sha256(identity.encode()).hexdigest()
        origin = payload["origin"]
        caused = (origin.get("event_origin") or "").startswith("function:")
        depth = origin.get("event_causation_depth", 0) + 1 if caused else 0
        policy = retry_policy or RetryPolicy(max_attempts=1)
        return self._store.enqueue(
            ExecutionJob(
                agent_id=function_id,
                kind=ExecutionKind.SCHEDULED,
                idempotency_key=key,
                correlation_id=origin.get("event_correlation_id")
                or payload["event_id"],
                run_at=datetime.now(timezone.utc),
                payload={
                    "retry_policy": policy.to_dict(),
                    "entity": entity,
                    "chain_depth": depth,
                    "graph_id": graph_id,
                    "node_types": node_types,
                    "matched_triggers": matched_triggers or [],
                },
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
                conn.execute(
                    "DELETE FROM schedule_rate WHERE admitted_at <= ?", (ts - 60,)
                )
                row = conn.execute(
                    """
                    SELECT * FROM schedule_jobs WHERE kind = ?
                      AND (idempotency_key GLOB 'function-schedule:*' OR idempotency_key GLOB 'function-graph:*')
                      AND state = ? AND run_at <= ?
                      AND (attempts = 0 OR (SELECT COUNT(*) FROM schedule_rate AS r
                           WHERE r.function_id = schedule_jobs.agent_id
                             AND r.admitted_at > ?) < ?)
                    ORDER BY run_at, created_at LIMIT 1
                """,
                    (
                        ExecutionKind.SCHEDULED.value,
                        ExecutionState.PENDING.value,
                        ts,
                        ts - 60,
                        self.rate_limit,
                    ),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                if row["attempts"] == 0 and row["idempotency_key"].startswith(
                    "function-schedule:"
                ):
                    conn.execute(
                        """
                        INSERT INTO schedule_watermarks VALUES (?, ?)
                        ON CONFLICT(function_id) DO UPDATE SET fire_time =
                            MAX(fire_time, excluded.fire_time)
                    """,
                        (row["agent_id"], ts),
                    )
                if row["attempts"] == 0:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM schedule_rate WHERE function_id = ? AND admitted_at > ?",
                        (row["agent_id"], ts - 60),
                    ).fetchone()[0]
                    if count >= self.rate_limit:
                        conn.execute(
                            "UPDATE schedule_jobs SET state = ?, dead_letter_reason = ?, "
                            "finished_at = ?, updated_at = ? WHERE id = ?",
                            (
                                ExecutionState.CANCELLED.value,
                                "rate limited",
                                ts,
                                ts,
                                row["id"],
                            ),
                        )
                        conn.commit()
                        return None
                conn.execute(
                    "INSERT INTO schedule_rate VALUES (?, ?)", (row["agent_id"], ts)
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

    def reconcile(self, service):
        """Finish running slots whose exact invocation has a durable outcome."""
        with self._store._lock:
            rows = self._store._conn.execute(
                "SELECT * FROM schedule_jobs WHERE state = ?",
                (ExecutionState.RUNNING.value,),
            ).fetchall()
            jobs = [SqliteExecutionStore._row_to_job(row) for row in rows]
        for job in jobs:
            suffix = f"{job.agent_id}:{job.idempotency_key}:attempt:{job.attempts}"
            key = f"schedule-fn:{suffix}"
            legacy_key = f"fn:{suffix}"
            runs = service.store.list_jobs(
                agent_id=job.agent_id, kind=ExecutionKind.FUNCTION
            )
            run = next(
                (
                    run
                    for run in runs
                    if run.idempotency_key == key
                    or (
                        run.idempotency_key == legacy_key
                        and (run.payload or {}).get("trigger") == "schedule"
                    )
                ),
                None,
            )
            if run is None:
                lookup = getattr(service.store, "schedule_attempt_outcome", None)
                outcome = lookup(key) if lookup else None
                if outcome is not None:
                    from types import SimpleNamespace

                    state, uncertain, finished = outcome
                    run = SimpleNamespace(
                        state=state,
                        finished_at=finished,
                        updated_at=finished,
                        dead_letter_reason=(ABANDONED_REASON if uncertain else None),
                    )
            if run is not None and run.state.is_terminal:
                self._finish_result(
                    service, job, run, run.finished_at or run.updated_at
                )

    def _finish_result(self, service, job, result, finished):
        if getattr(result, "dead_letter_reason", None) == ABANDONED_REASON:
            # Retention cannot prove the worker stopped; never retry uncertainty.
            return
        if result.state == ExecutionState.SUCCEEDED:
            self.complete(job.id, attempt=job.attempts, now=finished)
        elif result.state == ExecutionState.DEAD_LETTER:
            retry = self._retry_declared(service, job)
            self._finish(
                job.id, attempt=job.attempts, now=finished, success=False, retry=retry
            )
        else:
            self._finish(
                job.id, attempt=job.attempts, now=finished, success=False, retry=False
            )

    def dispatch(
        self,
        service,
        worker_id: str,
        *,
        actor_binding,
        now: datetime,
        lease_seconds: float,
        graph_event_filter=None,
    ):
        """Claim one slot and invoke it through the shared admission service.

        An exception leaves the attempt running: its outcome may be uncertain.
        """
        job = self.claim_next(worker_id, now=now, lease_seconds=lease_seconds)
        if job is None:
            return None
        graph_event = job.idempotency_key.startswith("function-graph:")
        if callable(actor_binding):
            actor_binding = actor_binding(job.agent_id)
        if graph_event and (
            actor_binding is None
            or not job.payload.get("graph_id")
            or job.payload["graph_id"] != actor_binding.get("graph_id")
        ):
            self._skip(job, now, "binding_changed")
            return None
        if job.attempts > 1 and not self._retry_declared(service, job):
            self._finish(
                job.id, attempt=job.attempts, now=now, success=False, retry=False
            )
            return None
        if actor_binding is None:
            self._finish(
                job.id, attempt=job.attempts, now=now, success=False, retry=False
            )
            return None
        if graph_event:
            entity = graph_event_filter(job) if graph_event_filter else None
            if entity is None:
                self._skip(job, now, "trigger_changed")
                return None
            extra = {"entity": entity}
        else:
            extra = {}
        result = service.invoke(
            job.agent_id,
            {},
            trigger="graph_change" if graph_event else "schedule",
            actor_binding=actor_binding,
            idempotency_key=f"{job.idempotency_key}:attempt:{job.attempts}",
            correlation_id=job.correlation_id,
            chain_depth=job.payload.get("chain_depth", 0),
            **extra,
        )
        self._finish_result(service, job, result, datetime.now(timezone.utc))
        return result

    def _skip(self, job, now, reason):
        with self._store._lock:
            self._store._conn.execute(
                "UPDATE schedule_jobs SET state = ?, dead_letter_reason = ?, "
                "lease_owner = NULL, lease_expiry = NULL, updated_at = ?, finished_at = ? "
                "WHERE id = ? AND state = ? AND attempts = ?",
                (
                    ExecutionState.CANCELLED.value,
                    reason,
                    now.timestamp(),
                    now.timestamp(),
                    job.id,
                    ExecutionState.RUNNING.value,
                    job.attempts,
                ),
            )
            self._store._conn.commit()

    @staticmethod
    def _retry_declared(service, job):
        from backend.functions.invocation import load_manifest

        try:
            manifest, _ = load_manifest(service.root, job.agent_id)
            declared = manifest.get("retry")
            return bool(declared) and RetryPolicy(
                **declared
            ).to_dict() == job.payload.get("retry_policy")
        except (OSError, ValueError, TypeError):
            return False

    def _finish(
        self,
        job_id: str,
        *,
        attempt: int,
        now: datetime,
        success: bool,
        retry: bool = True,
    ):
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
                    or not job.idempotency_key.startswith(
                        ("function-schedule:", "function-graph:")
                    )
                ):
                    raise JobNotFoundError(job_id)
                if job.state != ExecutionState.RUNNING or job.attempts != attempt:
                    conn.commit()
                    return job
                # Old ledger rows have no declared retry and therefore fail closed.
                try:
                    policy = (
                        RetryPolicy.from_dict(job.payload["retry_policy"])
                        if "retry_policy" in job.payload
                        else RetryPolicy(max_attempts=1)
                    )
                    if not _valid_retry_policy(policy):
                        raise ValueError("Invalid retry policy")
                except (ValueError, TypeError, OverflowError):
                    policy = None
                if success:
                    state, run_at, reason = (
                        ExecutionState.SUCCEEDED,
                        job.run_at.timestamp(),
                        None,
                    )
                elif policy is None:
                    state, run_at, reason = (
                        ExecutionState.DEAD_LETTER,
                        job.run_at.timestamp(),
                        "invalid retry policy",
                    )
                elif not retry or job.attempts >= policy.max_attempts:
                    state, run_at, reason = (
                        ExecutionState.DEAD_LETTER,
                        job.run_at.timestamp(),
                        "attempts exhausted",
                    )
                else:
                    try:
                        run_at = _retry_at(policy, job.attempts, ts)
                        state, reason = ExecutionState.PENDING, None
                    except (OverflowError, ValueError, OSError):
                        state, run_at, reason = (
                            ExecutionState.DEAD_LETTER,
                            job.run_at.timestamp(),
                            "invalid retry policy",
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
