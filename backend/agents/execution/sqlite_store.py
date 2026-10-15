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

from .models import (
    ABANDONED_REASON,
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    redact_job,
    utcnow,
)
from .store import JobNotFoundError

_SCHEMA_VERSION = 2

_TERMINAL_VALUES = (
    ExecutionState.SUCCEEDED.value,
    ExecutionState.DEAD_LETTER.value,
    ExecutionState.CANCELLED.value,
)


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

# v2 (retention): when the job's content was stripped. Additive, forward-only.
_MIGRATE_V2 = "ALTER TABLE execution_jobs ADD COLUMN redacted_at REAL"


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
        # busy_timeout first so every later statement, including the migration's
        # BEGIN IMMEDIATE, waits for a concurrent opener instead of failing.
        # (The one-time WAL switch of a brand-new file is not covered: SQLite
        # does not run the busy handler for it.)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Overwrite freed pages so purged or redacted content does not linger
        # in the file.
        self._conn.execute("PRAGMA secure_delete=ON")
        self._migrate()

    # ------------------------------------------------------------------
    # Schema / lifecycle
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        # One write transaction, re-reading the version inside it, so processes
        # opening the same file concurrently migrate it exactly once.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if version < 1:
                    for statement in _CREATE_SCHEMA.split(";"):
                        if statement.strip():
                            self._conn.execute(statement)
                if version < 2 and "redacted_at" not in self._columns():
                    self._conn.execute(_MIGRATE_V2)
                if version < _SCHEMA_VERSION:
                    # PRAGMA does not accept bound parameters.
                    self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _columns(self) -> set:
        rows = self._conn.execute("PRAGMA table_info(execution_jobs)").fetchall()
        return {row["name"] for row in rows}

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
                    created_at, updated_at, started_at, finished_at, redacted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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

            if job.attempts >= self._retry.max_attempts:
                self._conn.execute(
                    """
                    UPDATE execution_jobs SET
                        state = ?, last_error = ?, dead_letter_reason = ?,
                        result = COALESCE(?, result),
                        lease_owner = NULL, lease_expiry = NULL,
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ExecutionState.DEAD_LETTER.value,
                        error,
                        f"failed after {job.attempts} attempt(s)",
                        json.dumps(result) if result is not None else None,
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

    def cancel(
        self,
        job_id: str,
        *,
        result: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        now = now or utcnow()
        now_ts = _to_epoch(now)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE execution_jobs SET
                    state = ?, result = ?, lease_owner = NULL, lease_expiry = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND state NOT IN (?,?,?)
                """,
                (
                    ExecutionState.CANCELLED.value,
                    json.dumps(result) if result is not None else None,
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
            # Only the "?" placeholder count is interpolated; the ids are bound.
            self._conn.execute(
                f"""
                UPDATE execution_jobs SET
                    state = ?, lease_owner = NULL, lease_expiry = NULL,
                    run_at = ?, updated_at = ?
                WHERE id IN ({",".join("?" for _ in ids)})
                """,  # nosec B608
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
        created_after: Optional[datetime] = None,
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
        if created_after is not None:
            clauses.append("created_at > ?")
            params.append(_to_epoch(created_after))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # WHERE holds only fixed column clauses and "?" placeholders; values are bound.
        sql = f"SELECT * FROM execution_jobs {where} ORDER BY created_at DESC"  # nosec B608
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_job(row) for row in rows]

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def list_terminal_older_than(
        self,
        cutoff: datetime,
        *,
        redacted: Optional[bool] = None,
    ) -> List[ExecutionJob]:
        want = None if redacted is None else int(redacted)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM execution_jobs
                WHERE state IN (?,?,?) AND COALESCE(finished_at, updated_at) < ?
                  AND (? IS NULL OR (redacted_at IS NOT NULL) = ?)
                ORDER BY COALESCE(finished_at, updated_at) ASC
                """,
                (*_TERMINAL_VALUES, _to_epoch(cutoff), want, want),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def list_running_older_than(self, cutoff: datetime) -> List[ExecutionJob]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM execution_jobs
                WHERE state = ? AND updated_at < ?
                ORDER BY updated_at ASC
                """,
                (ExecutionState.RUNNING.value, _to_epoch(cutoff)),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def abandon(
        self,
        job_ids: Sequence[str],
        *,
        cutoff: datetime,
        now: Optional[datetime] = None,
    ) -> int:
        now = now or utcnow()
        # The RUNNING/age guard lives in the SQL, so a run finalized or touched
        # since it was listed (by this or another process) is left alone.
        params = [
            (
                ExecutionState.DEAD_LETTER.value,
                ABANDONED_REASON,
                _to_epoch(now),
                job_id,
                ExecutionState.RUNNING.value,
                _to_epoch(cutoff),
            )
            for job_id in dict.fromkeys(job_ids)
        ]
        if not params:
            return 0
        with self._lock:
            try:
                cur = self._conn.executemany(
                    """
                    UPDATE execution_jobs SET
                        state = ?, dead_letter_reason = ?,
                        lease_owner = NULL, lease_expiry = NULL,
                        finished_at = updated_at, updated_at = ?
                    WHERE id = ? AND state = ? AND updated_at < ?
                    """,
                    params,
                )
                count = cur.rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return count

    def redact(
        self,
        job_ids: Sequence[str],
        *,
        now: Optional[datetime] = None,
    ) -> int:
        now = now or utcnow()
        count = 0
        with self._lock:
            try:
                for job_id in dict.fromkeys(job_ids):
                    job = self._get_by_id(job_id)
                    if (
                        job is None
                        or not job.is_terminal
                        or job.redacted_at is not None
                    ):
                        continue
                    redact_job(job, now)
                    # The terminal/unredacted guard is repeated in SQL so a row
                    # changed by another process since the read is left alone.
                    cur = self._conn.execute(
                        """
                        UPDATE execution_jobs SET
                            payload = ?, result = ?, last_error = NULL,
                            session_id = NULL, origin = NULL, redacted_at = ?
                        WHERE id = ? AND redacted_at IS NULL
                          AND state IN (?,?,?)
                        """,
                        (
                            json.dumps(job.payload),
                            json.dumps(job.result) if job.result is not None else None,
                            _to_epoch(now),
                            job_id,
                            *_TERMINAL_VALUES,
                        ),
                    )
                    count += cur.rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if count:
                self._checkpoint()
        return count

    def purge(self, job_ids: Sequence[str]) -> int:
        params = [(job_id, *_TERMINAL_VALUES) for job_id in dict.fromkeys(job_ids)]
        if not params:
            return 0
        with self._lock:
            try:
                cur = self._conn.executemany(
                    "DELETE FROM execution_jobs WHERE id = ? AND state IN (?,?,?)",
                    params,
                )
                count = cur.rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if count:
                self._checkpoint()
        return count

    def _checkpoint(self) -> None:
        # Fold the WAL back into the file so pre-redaction/purge page images do
        # not linger in the -wal file. Best effort: a busy reader only delays it.
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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
            _to_epoch(job.redacted_at),
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
        job.redacted_at = _from_epoch(row["redacted_at"])
        return job
