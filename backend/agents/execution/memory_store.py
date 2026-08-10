"""
Reference in-memory implementation of the execution-store contract.

This adapter is the *null* / volatile reference: it proves the contract is
implementable and gives the scheduler/worker a drop-in store for tests and
standalone runs, exactly as ``FileGraphPersistenceBackend`` is the default for
the graph persistence seam. It is **not** the durable adapter — its state does
not survive a process restart. Durable local and hosted adapters implement the
same ``ExecutionStore`` methods with real persistence and must pass
``ExecutionStoreContractTests``.

The store is thread-safe: a single lock guards all mutations so concurrent
workers calling ``claim_next`` never claim the same job twice.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Dict, List, Optional, Sequence

from .models import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    utcnow,
)
from .store import JobNotFoundError


class InMemoryExecutionStore:
    """Volatile, thread-safe reference adapter for ``ExecutionStore``."""

    def __init__(self, retry_policy: Optional[RetryPolicy] = None) -> None:
        self._retry = retry_policy or RetryPolicy()
        self._jobs: Dict[str, ExecutionJob] = {}
        # idempotency_key -> job id, so enqueue can dedup in O(1).
        self._by_key: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Producer side
    # ------------------------------------------------------------------

    def enqueue(self, job: ExecutionJob) -> ExecutionJob:
        with self._lock:
            existing_id = self._by_key.get(job.idempotency_key)
            if existing_id is not None:
                return self._jobs[existing_id].copy()

            stored = job.copy()
            self._jobs[stored.id] = stored
            self._by_key[stored.idempotency_key] = stored.id
            return stored.copy()

    # ------------------------------------------------------------------
    # Consumer side
    # ------------------------------------------------------------------

    def claim_next(
        self,
        worker_id: str,
        *,
        now=None,
        lease_seconds: float = 60.0,
    ) -> Optional[ExecutionJob]:
        now = now or utcnow()
        with self._lock:
            for job in self._runnable(now):
                job.attempts += 1
                job.started_at = job.started_at or now
                job.updated_at = now

                if job.attempts > self._retry.max_attempts:
                    # A crash-loop reclaimed past its budget: dead-letter rather
                    # than run again.
                    job.state = ExecutionState.DEAD_LETTER
                    job.dead_letter_reason = "max attempts exceeded"
                    job.lease_owner = None
                    job.lease_expiry = None
                    job.finished_at = now
                    continue

                job.state = ExecutionState.RUNNING
                job.lease_owner = worker_id
                job.lease_expiry = now + timedelta(seconds=lease_seconds)
                return job.copy()
            return None

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        now=None,
        lease_seconds: float = 60.0,
    ) -> bool:
        now = now or utcnow()
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.state != ExecutionState.RUNNING
                or job.lease_owner != worker_id
            ):
                return False
            job.lease_expiry = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

    def complete(self, job_id: str, *, result=None, now=None) -> ExecutionJob:
        now = now or utcnow()
        with self._lock:
            job = self._require(job_id)
            if job.is_terminal:
                return job.copy()
            job.state = ExecutionState.SUCCEEDED
            job.result = dict(result) if result is not None else None
            job.lease_owner = None
            job.lease_expiry = None
            job.finished_at = now
            job.updated_at = now
            return job.copy()

    def fail(self, job_id: str, *, error: str, now=None) -> ExecutionJob:
        now = now or utcnow()
        with self._lock:
            job = self._require(job_id)
            if job.is_terminal:
                return job.copy()

            job.last_error = error
            job.lease_owner = None
            job.lease_expiry = None
            job.updated_at = now

            if job.attempts >= self._retry.max_attempts:
                job.state = ExecutionState.DEAD_LETTER
                job.dead_letter_reason = f"failed after {job.attempts} attempt(s)"
                job.finished_at = now
            else:
                job.state = ExecutionState.PENDING
                job.run_at = now + timedelta(
                    seconds=self._retry.backoff_seconds(job.attempts)
                )
            return job.copy()

    def cancel(self, job_id: str, *, now=None) -> bool:
        now = now or utcnow()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return False
            job.state = ExecutionState.CANCELLED
            job.lease_owner = None
            job.lease_expiry = None
            job.finished_at = now
            job.updated_at = now
            return True

    def recover_stale(self, *, now=None) -> List[ExecutionJob]:
        now = now or utcnow()
        recovered: List[ExecutionJob] = []
        with self._lock:
            for job in self._jobs.values():
                if job.state == ExecutionState.RUNNING and self._lease_expired(
                    job, now
                ):
                    job.state = ExecutionState.PENDING
                    job.lease_owner = None
                    job.lease_expiry = None
                    job.run_at = now
                    job.updated_at = now
                    recovered.append(job.copy())
        return recovered

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def get(self, job_id: str) -> Optional[ExecutionJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.copy() if job else None

    def list_jobs(
        self,
        *,
        states: Optional[Sequence[ExecutionState]] = None,
        agent_id: Optional[str] = None,
        kind: Optional[ExecutionKind] = None,
        limit: Optional[int] = None,
    ) -> List[ExecutionJob]:
        state_set = set(states) if states else None
        with self._lock:
            jobs = [
                job.copy()
                for job in self._jobs.values()
                if (state_set is None or job.state in state_set)
                and (agent_id is None or job.agent_id == agent_id)
                and (kind is None or job.kind == kind)
            ]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        if limit is not None:
            jobs = jobs[:limit]
        return jobs

    # ------------------------------------------------------------------
    # Internal helpers (call under self._lock)
    # ------------------------------------------------------------------

    def _require(self, job_id: str) -> ExecutionJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    @staticmethod
    def _lease_expired(job: ExecutionJob, now) -> bool:
        return job.lease_expiry is not None and job.lease_expiry <= now

    def _runnable(self, now) -> List[ExecutionJob]:
        """Runnable jobs, oldest-first: due PENDING or reclaimable RUNNING."""
        candidates = [
            job
            for job in self._jobs.values()
            if (job.state == ExecutionState.PENDING and job.run_at <= now)
            or (job.state == ExecutionState.RUNNING and self._lease_expired(job, now))
        ]
        candidates.sort(key=lambda j: (j.run_at, j.created_at))
        return candidates
