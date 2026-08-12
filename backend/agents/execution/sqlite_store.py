"""
Durable local implementation of the execution-store contract, backed by SQLite.

This is the default *durable* adapter chosen in
``docs/adr/0001-local-durable-execution-store.md``: SQLite via the Python
standard-library ``sqlite3`` module. Unlike ``InMemoryExecutionStore``, its
state survives a process restart — reopening the same database file recovers
every queued, running, dead-lettered and completed job. It implements the same
``ExecutionStore`` methods and is held to the same
``ExecutionStoreContractTests`` as every other adapter.

Design (per ADR 0001):

* One SQLite database file dedicated to execution state, owned by this store and
  separate from the graph snapshot.
* WAL journal mode with a bounded ``busy_timeout``; ``claim_next`` runs inside a
  ``BEGIN IMMEDIATE`` write transaction so the select-and-transition is atomic
  under concurrent workers.
* ``idempotency_key`` carries a ``UNIQUE`` constraint; ``enqueue`` inserts and,
  on conflict, returns the stored row unchanged (it never updates it).
* Schema version is tracked with ``PRAGMA user_version``; migrations are
  forward-only and additive.

Concurrency posture: the default target is one host / one process with worker
threads (ADR 0001). A single connection guarded by a re-entrant lock serialises
Python-side access; SQLite's own locking plus ``busy_timeout`` covers occasional
multi-process access on the shared file. Horizontal scale-out is a
hosted-adapter concern, not a promise of this local default.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Union

from .models import ExecutionJob, ExecutionKind, ExecutionState, RetryPolicy, utcnow
from .store import JobNotFoundError

_SCHEMA_VERSION = 1

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_jobs (
    id                 TEXT PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    kind               TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    payload            TEXT NOT NULL DEFAULT '{}',
    state              TEXT NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    run_at             REAL NOT NULL,
    lease_owner        TEXT,
    lease_expiry       REAL,
    correlation_id     TEXT,
    session_id         TEXT,
    origin             TEXT,
    last_error         TEXT,
    dead_letter_reason TEXT,
    result             TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL
);
-- Backs claim_next (oldest runnable first) and list_jobs (newest first).
CREATE INDEX IF NOT EXISTS idx_execution_jobs_runnable
    ON execution_jobs (state, run_at);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_created
    ON execution_jobs (created_at);
"""


def _to_epoch(dt: Optional[datetime]) -> Optional[float]:
    """Serialize an aware datetime to epoch seconds; numeric ordering is exact."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).timestamp()


def _from_epoch(value: Optional[float]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


class SqliteExecutionStore:
    """Durable, file-backed ``ExecutionStore`` adapter (SQLite)."""

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self._retry = retry_policy or RetryPolicy()
        self.db_path = str(db_path)
        # A single shared connection guarded by a re-entrant lock: the local
        # target is one process with worker threads (ADR 0001). check_same_thread
        # is disabled because worker threads share this connection under the lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ------------------------------------------------------------------
    # Schema / lifecycle
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                self._conn.executescript(_CREATE_SCHEMA)
                # PRAGMA does not accept bound parameters.
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection. The database file persists."""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    def enqueue(self, job: ExecutionJob) -> ExecutionJob:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO execution_jobs (
                    id, agent_id, kind, idempotency_key, payload, state, attempts,
                    run_at, lease_owner, lease_expiry, correlation_id, session_id,
                    origin, last_error, dead_letter_reason, result,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                self._to_row(job),
            )
            self._conn.commit()
            stored = self._get_by_key(job.idempotency_key)
            # The row always exists here: either we inserted it, or a prior job
            # with this key did. Never None.
            assert stored is not None
            return stored

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    def claim_next(
        self,
        worker_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: float = 60.0,
    ) -> Optional[ExecutionJob]:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                while True:
                    row = self._conn.execute(
                        """
                        SELECT * FROM execution_jobs
                        WHERE (state = ? AND run_at <= ?)
                           OR (state = ? AND lease_expiry IS NOT NULL
                               AND lease_expiry <= ?)
                        ORDER BY run_at ASC, created_at ASC
                        LIMIT 1
                        """,
                        (
                            ExecutionState.PENDING.value,
                            now_ts,
                            ExecutionState.RUNNING.value,
                            now_ts,
                        ),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None

                    attempts = row["attempts"] + 1
                    if attempts > self._retry.max_attempts:
                        # Reclaimed past its budget (a crash loop): dead-letter
                        # rather than run again, then look for the next job.
                        self._conn.execute(
                            """
                            UPDATE execution_jobs SET
                                state = ?, attempts = ?, dead_letter_reason = ?,
                                lease_owner = NULL, lease_expiry = NULL,
                                started_at = COALESCE(started_at, ?),
                                finished_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                ExecutionState.DEAD_LETTER.value,
                                attempts,
                                "max attempts exceeded",
                                now_ts,
                                now_ts,
                                now_ts,
                                row["id"],
                            ),
                        )
                        continue

                    self._conn.execute(
                        """
                        UPDATE execution_jobs SET
                            state = ?, attempts = ?, lease_owner = ?,
                            lease_expiry = ?, started_at = COALESCE(started_at, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            ExecutionState.RUNNING.value,
                            attempts,
                            worker_id,
                            _to_epoch(now) + lease_seconds,
                            now_ts,
                            now_ts,
                            row["id"],
                        ),
                    )
                    self._conn.commit()
                    return self._get_by_id(row["id"])
            except Exception:
                self._conn.rollback()
                raise

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE execution_jobs SET lease_expiry = ?, updated_at = ?
                WHERE id = ? AND state = ? AND lease_owner = ?
                """,
                (
                    now_ts + lease_seconds,
                    now_ts,
                    job_id,
                    ExecutionState.RUNNING.value,
                    worker_id,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def complete(
        self,
        job_id: str,
        *,
        result: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> ExecutionJob:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            job = self._get_by_id(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.is_terminal:
                return job
            self._conn.execute(
                """
                UPDATE execution_jobs SET
                    state = ?, result = ?, lease_owner = NULL, lease_expiry = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ExecutionState.SUCCEEDED.value,
                    json.dumps(result) if result is not None else None,
                    now_ts,
                    now_ts,
                    job_id,
                ),
            )
            self._conn.commit()
            return self._get_by_id(job_id)

    def fail(
        self,
        job_id: str,
        *,
        error: str,
        now: Optional[datetime] = None,
    ) -> ExecutionJob:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            job = self._get_by_id(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.is_terminal:
                return job

            if job.attempts >= self._retry.max_attempts:
                self._conn.execute(
                    """
                    UPDATE execution_jobs SET
                        state = ?, last_error = ?, dead_letter_reason = ?,
                        lease_owner = NULL, lease_expiry = NULL,
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ExecutionState.DEAD_LETTER.value,
                        error,
                        f"failed after {job.attempts} attempt(s)",
                        now_ts,
                        now_ts,
                        job_id,
                    ),
                )
            else:
                run_at = now_ts + self._retry.backoff_seconds(job.attempts)
                self._conn.execute(
                    """
                    UPDATE execution_jobs SET
                        state = ?, last_error = ?, run_at = ?,
                        lease_owner = NULL, lease_expiry = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ExecutionState.PENDING.value,
                        error,
                        run_at,
                        now_ts,
                        job_id,
                    ),
                )
            self._conn.commit()
            return self._get_by_id(job_id)

    def cancel(self, job_id: str, *, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE execution_jobs SET
                    state = ?, lease_owner = NULL, lease_expiry = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND state NOT IN (?,?,?)
                """,
                (
                    ExecutionState.CANCELLED.value,
                    now_ts,
                    now_ts,
                    job_id,
                    ExecutionState.SUCCEEDED.value,
                    ExecutionState.DEAD_LETTER.value,
                    ExecutionState.CANCELLED.value,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def recover_stale(self, *, now: Optional[datetime] = None) -> List[ExecutionJob]:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id FROM execution_jobs
                WHERE state = ? AND lease_expiry IS NOT NULL AND lease_expiry <= ?
                """,
                (ExecutionState.RUNNING.value, now_ts),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            self._conn.execute(
                f"""
                UPDATE execution_jobs SET
                    state = ?, lease_owner = NULL, lease_expiry = NULL,
                    run_at = ?, updated_at = ?
                WHERE id IN ({",".join("?" for _ in ids)})
                """,
                [ExecutionState.PENDING.value, now_ts, now_ts, *ids],
            )
            self._conn.commit()
            return [self._get_by_id(job_id) for job_id in ids]

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> Optional[ExecutionJob]:
        with self._lock:
            return self._get_by_id(job_id)

    def list_jobs(
        self,
        *,
        states: Optional[Sequence[ExecutionState]] = None,
        agent_id: Optional[str] = None,
        kind: Optional[ExecutionKind] = None,
        limit: Optional[int] = None,
    ) -> List[ExecutionJob]:
        clauses = []
        params: list = []
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(s.value for s in states)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM execution_jobs {where} ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_job(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers (call under self._lock)
    # ------------------------------------------------------------------

    def _get_by_id(self, job_id: str) -> Optional[ExecutionJob]:
        row = self._conn.execute(
            "SELECT * FROM execution_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def _get_by_key(self, idempotency_key: str) -> Optional[ExecutionJob]:
        row = self._conn.execute(
            "SELECT * FROM execution_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_job(row) if row is not None else None

    @staticmethod
    def _to_row(job: ExecutionJob) -> tuple:
        return (
            job.id,
            job.agent_id,
            job.kind.value,
            job.idempotency_key,
            json.dumps(job.payload or {}),
            job.state.value,
            job.attempts,
            _to_epoch(job.run_at),
            job.lease_owner,
            _to_epoch(job.lease_expiry),
            job.correlation_id,
            job.session_id,
            job.origin,
            job.last_error,
            job.dead_letter_reason,
            json.dumps(job.result) if job.result is not None else None,
            _to_epoch(job.created_at),
            _to_epoch(job.updated_at),
            _to_epoch(job.started_at),
            _to_epoch(job.finished_at),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ExecutionJob:
        job = ExecutionJob(
            agent_id=row["agent_id"],
            kind=ExecutionKind(row["kind"]),
            idempotency_key=row["idempotency_key"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
        )
        job.id = row["id"]
        job.state = ExecutionState(row["state"])
        job.attempts = row["attempts"]
        job.run_at = _from_epoch(row["run_at"])
        job.lease_owner = row["lease_owner"]
        job.lease_expiry = _from_epoch(row["lease_expiry"])
        job.correlation_id = row["correlation_id"]
        job.session_id = row["session_id"]
        job.origin = row["origin"]
        job.last_error = row["last_error"]
        job.dead_letter_reason = row["dead_letter_reason"]
        job.result = json.loads(row["result"]) if row["result"] else None
        job.created_at = _from_epoch(row["created_at"])
        job.updated_at = _from_epoch(row["updated_at"])
        job.started_at = _from_epoch(row["started_at"])
        job.finished_at = _from_epoch(row["finished_at"])
        return job
