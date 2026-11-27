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
from datetime import datetime, timedelta, timezone
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
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS graph_batch_members "
                "(function_id TEXT NOT NULL, event_id TEXT NOT NULL, job_id TEXT NOT NULL, "
                "refusal_reason TEXT, PRIMARY KEY (function_id, event_id))"
            )
            if "refusal_reason" not in {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(graph_batch_members)")
            }:
                self._conn.execute(
                    "ALTER TABLE graph_batch_members ADD COLUMN refusal_reason TEXT"
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
        batching=None,
    ):
        payload = event.to_webhook_payload()
        identity = json.dumps([function_id, payload["event_id"]], separators=(",", ":"))
        key = "function-graph:" + hashlib.sha256(identity.encode()).hexdigest()
        origin = payload["origin"]
        caused = (origin.get("event_origin") or "").startswith(("function:", "agent:"))
        depth = origin.get("event_causation_depth", 0) + 1 if caused else 0
        policy = retry_policy or RetryPolicy(max_attempts=1)
        correlation = origin.get("event_correlation_id") or payload["event_id"]
        now = datetime.now(timezone.utc)
        job = ExecutionJob(
            agent_id=function_id,
            kind=ExecutionKind.SCHEDULED,
            idempotency_key=key,
            correlation_id=correlation,
            run_at=now,
            payload={
                "retry_policy": policy.to_dict(),
                "entity": entity,
                "chain_depth": depth,
                "graph_id": graph_id,
                "node_types": node_types,
                "matched_triggers": matched_triggers or [],
            },
        )
        return self._enqueue_graph(job, payload["event_id"], batching)

    def _enqueue_graph(self, job, event_id, batching):
        store = self._store
        with store._lock:
            conn = store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = datetime.now(timezone.utc)
                member = conn.execute(
                    "SELECT job_id FROM graph_batch_members WHERE function_id = ? AND event_id = ?",
                    (job.agent_id, event_id),
                ).fetchone()
                if member:
                    result = store._get_by_id(member[0])
                    conn.commit()
                    return result
                old = conn.execute(
                    "SELECT * FROM schedule_jobs WHERE idempotency_key = ?",
                    (job.idempotency_key,),
                ).fetchone()
                if old:
                    existing = SqliteExecutionStore._row_to_job(old)
                    conn.execute(
                        "INSERT INTO graph_batch_members (function_id, event_id, job_id) VALUES (?, ?, ?)",
                        (job.agent_id, event_id, existing.id),
                    )
                    conn.commit()
                    return existing
                if batching:
                    fingerprint = job.payload["matched_triggers"]
                    rows = conn.execute(
                        "SELECT * FROM schedule_jobs WHERE agent_id = ? AND state = ? "
                        "AND attempts = 0 AND run_at > ? AND idempotency_key GLOB 'function-graph:*' "
                        "ORDER BY created_at",
                        (job.agent_id, ExecutionState.PENDING.value, now.timestamp()),
                    ).fetchall()
                    existing = next(
                        (
                            SqliteExecutionStore._row_to_job(row)
                            for row in rows
                            if json.loads(row["payload"]).get("batch_fingerprint")
                            == fingerprint
                            and json.loads(row["payload"]).get("graph_id")
                            == job.payload["graph_id"]
                            and json.loads(row["payload"]).get("retry_policy")
                            == job.payload["retry_policy"]
                            and len(json.loads(row["payload"]).get("entries", []))
                            < batching["batch_size"]
                        ),
                        None,
                    )
                    entry = {
                        "entity": job.payload["entity"],
                        "correlation_id": job.correlation_id,
                        "event_id": event_id,
                        "chain_depth": job.payload["chain_depth"],
                        "node_types": job.payload["node_types"],
                    }
                    if existing:
                        job = existing
                        job.payload["entries"].append(entry)
                        job.payload["chain_depth"] = max(
                            job.payload["chain_depth"], entry["chain_depth"]
                        )
                        run_at = (
                            now.timestamp()
                            if len(job.payload["entries"]) >= batching["batch_size"]
                            else job.run_at.timestamp()
                        )
                        conn.execute(
                            "UPDATE schedule_jobs SET payload = ?, run_at = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(job.payload), run_at, now.timestamp(), job.id),
                        )
                    else:
                        job.payload["entries"] = [entry]
                        job.payload["batch_fingerprint"] = fingerprint
                        job.run_at = (
                            now
                            if batching["batch_size"] == 1
                            else now + timedelta(seconds=batching["debounce_seconds"])
                        )
                        conn.execute(
                            "INSERT INTO schedule_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            SqliteExecutionStore._to_row(job),
                        )
                else:
                    conn.execute(
                        "INSERT INTO schedule_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
                        SqliteExecutionStore._to_row(job),
                    )
                    job = SqliteExecutionStore._row_to_job(
                        conn.execute(
                            "SELECT * FROM schedule_jobs WHERE idempotency_key = ?",
                            (job.idempotency_key,),
                        ).fetchone()
                    )
                conn.execute(
                    "INSERT INTO graph_batch_members (function_id, event_id, job_id) VALUES (?, ?, ?)",
                    (job.agent_id, event_id, job.id),
                )
                conn.commit()
                return job
            except Exception:
                conn.rollback()
                raise

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
                admission = conn.execute(
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
                job._previous_update = row["updated_at"]
                job._admission_id = admission.lastrowid
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
        self._refresh_pending_retries(service, now)
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
        if job.attempts > 1:
            policy = self._retry_declared(service, job)
            if not policy or job.attempts > policy.max_attempts:
                self._finish(
                    job.id, attempt=job.attempts, now=now, success=False, retry=False
                )
                return None
            try:
                run_at = _retry_at(policy, job.attempts - 1, job._previous_update)
            except (OverflowError, ValueError, OSError):
                self._finish(
                    job.id, attempt=job.attempts, now=now, success=False, retry=False
                )
                return None
            if run_at > now.timestamp():
                self._defer_retry(job, run_at, now)
                return None
        if actor_binding is None:
            self._finish(
                job.id, attempt=job.attempts, now=now, success=False, retry=False
            )
            return None
        if graph_event:
            if "entries" in job.payload:
                from types import SimpleNamespace

                entries = []
                with self._store._lock:
                    refused = {
                        row[0]
                        for row in self._store._conn.execute(
                            "SELECT event_id FROM graph_batch_members WHERE job_id = ? AND refusal_reason IS NOT NULL",
                            (job.id,),
                        )
                    }
                newly_refused = []
                for entry in job.payload["entries"]:
                    if entry["event_id"] in refused:
                        continue
                    member_payload = {
                        **job.payload,
                        "entity": entry["entity"],
                        "node_types": entry["node_types"],
                    }
                    filtered = (
                        graph_event_filter(
                            SimpleNamespace(
                                agent_id=job.agent_id, payload=member_payload
                            )
                        )
                        if graph_event_filter
                        else None
                    )
                    if filtered is not None:
                        entries.append(
                            {
                                "event_id": entry["event_id"],
                                "correlation_id": entry["correlation_id"],
                                "entity": filtered,
                            }
                        )
                    else:
                        newly_refused.append(entry["event_id"])
                if newly_refused:
                    with self._store._lock:
                        self._store._conn.executemany(
                            "UPDATE graph_batch_members SET refusal_reason = 'trigger_changed' WHERE job_id = ? AND event_id = ?",
                            [(job.id, event_id) for event_id in newly_refused],
                        )
                        self._store._conn.commit()
                entity = {"events": entries} if entries else None
            else:
                entity = graph_event_filter(job) if graph_event_filter else None
            if entity is None:
                self._skip(job, now, "trigger_changed")
                return None
            extra = {"entity": entity}
            if "entries" in job.payload:
                extra["correlation_ids"] = [
                    entry["correlation_id"] for entry in job.payload["entries"]
                ]
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

    def _defer_retry(self, job, run_at, now):
        with self._store._lock:
            conn = self._store._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    "UPDATE schedule_jobs SET state = ?, attempts = attempts - 1, "
                    "run_at = ?, updated_at = ?, lease_owner = NULL, lease_expiry = NULL "
                    "WHERE id = ? AND state = ? AND attempts = ? AND lease_owner = ?",
                    (
                        ExecutionState.PENDING.value,
                        run_at,
                        job._previous_update,
                        job.id,
                        ExecutionState.RUNNING.value,
                        job.attempts,
                        job.lease_owner,
                    ),
                )
                if updated.rowcount:
                    conn.execute(
                        "DELETE FROM schedule_rate WHERE rowid = ? AND function_id = ? AND admitted_at = ?",
                        (job._admission_id, job.agent_id, now.timestamp()),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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
    def _read_retry_policy(service, job):
        from backend.functions.invocation import load_manifest

        try:
            manifest, _ = load_manifest(service.root, job.agent_id)
        except Exception:
            return None
        try:
            declared = manifest.get("retry")
            if (
                not isinstance(declared, dict)
                or set(declared)
                != {"max_attempts", "base_seconds", "factor", "max_seconds"}
                or any(type(value) not in (int, float) for value in declared.values())
            ):
                return None
            policy = RetryPolicy(**declared)
            if not _valid_retry_policy(policy):
                return None
            return policy
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def _retry_declared(self, service, job):
        policy = self._read_retry_policy(service, job)
        if policy is not None:
            with self._store._lock:
                current = self._store._get_by_id(job.id)
                if (
                    current is None
                    or current.state != ExecutionState.RUNNING
                    or current.attempts != job.attempts
                ):
                    return None
                current.payload["retry_policy"] = policy.to_dict()
                updated = self._store._conn.execute(
                    "UPDATE schedule_jobs SET payload = ? WHERE id = ? AND state = ? AND attempts = ?",
                    (
                        json.dumps(current.payload),
                        job.id,
                        ExecutionState.RUNNING.value,
                        job.attempts,
                    ),
                )
                self._store._conn.commit()
                if not updated.rowcount:
                    return None
        return policy

    def _refresh_pending_retries(self, service, now):
        store = self._store
        with store._lock:
            conn = store._conn
            ids = conn.execute(
                "SELECT id FROM schedule_jobs WHERE state = ? AND attempts > 0",
                (ExecutionState.PENDING.value,),
            ).fetchall()
            for item in ids:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM schedule_jobs WHERE id = ? AND state = ? AND attempts > 0",
                        (item["id"], ExecutionState.PENDING.value),
                    ).fetchone()
                    if row is None:
                        conn.commit()
                        continue
                    job = SqliteExecutionStore._row_to_job(row)
                    policy = self._read_retry_policy(service, job)
                    try:
                        run_at = (
                            _retry_at(policy, job.attempts, job.updated_at.timestamp())
                            if policy
                            else None
                        )
                    except (OverflowError, ValueError, OSError):
                        run_at = None
                    if policy is not None:
                        job.payload["retry_policy"] = policy.to_dict()
                    if (
                        policy is None
                        or run_at is None
                        or job.attempts >= policy.max_attempts
                    ):
                        conn.execute(
                            "UPDATE schedule_jobs SET state = ?, payload = ?, dead_letter_reason = ?, finished_at = ? WHERE id = ?",
                            (
                                ExecutionState.DEAD_LETTER.value,
                                json.dumps(job.payload),
                                "attempts exhausted",
                                now.timestamp(),
                                job.id,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE schedule_jobs SET payload = ?, run_at = ? WHERE id = ?",
                            (json.dumps(job.payload), run_at, job.id),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

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
