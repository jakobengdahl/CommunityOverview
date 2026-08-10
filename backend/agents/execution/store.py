"""
The durable execution-store seam.

``ExecutionStore`` is the replaceable persistence contract for durable agent
execution. The reference ``InMemoryExecutionStore`` implements it for tests and
standalone use; durable adapters (local file/SQL, hosted) implement the same
methods so the scheduler, event router and worker can be wired to any of them
without change.

The contract this interface must satisfy is specified normatively in
``docs/DURABLE_EXECUTION_CONTRACT.md`` and exercised by
``ExecutionStoreContractTests`` in ``contract.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Protocol, Sequence, runtime_checkable

from .models import ExecutionJob, ExecutionKind, ExecutionState


class ExecutionStoreError(Exception):
    """Base class for execution-store errors."""


class JobNotFoundError(ExecutionStoreError):
    """Raised when an operation targets a job id the store does not hold."""


@runtime_checkable
class ExecutionStore(Protocol):
    """
    Durable queue + state store for agent jobs.

    Implementations MUST honour the invariants in
    ``docs/DURABLE_EXECUTION_CONTRACT.md``. In particular:

    * ``enqueue`` is idempotent on ``idempotency_key``.
    * ``claim_next`` hands out at most one worker at a time per job and treats a
      job whose lease has expired as reclaimable (restart recovery).
    * A job is attempted at most ``RetryPolicy.max_attempts`` times before it is
      dead-lettered.
    * Terminal states (SUCCEEDED, DEAD_LETTER, CANCELLED) are sticky: no
      operation moves a job out of a terminal state.
    * All ``now`` parameters default to the current UTC time; passing an
      explicit value keeps behaviour deterministic in tests.
    """

    def enqueue(self, job: ExecutionJob) -> ExecutionJob:
        """
        Persist ``job`` unless a job with the same ``idempotency_key`` already
        exists, in which case return the existing job unchanged. Returns the
        stored job (a copy owned by the caller).
        """
        ...

    def claim_next(
        self,
        worker_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: float = 60.0,
    ) -> Optional[ExecutionJob]:
        """
        Atomically claim the oldest runnable job for ``worker_id`` and return it
        in RUNNING state with a lease valid until ``now + lease_seconds``.

        A job is runnable when it is PENDING with ``run_at <= now``, or RUNNING
        with an expired lease (reclaimed after a crash/restart). Claiming counts
        as one attempt; a claim that would exceed the retry budget dead-letters
        the job instead of running it. Returns None when nothing is runnable.
        """
        ...

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: float = 60.0,
    ) -> bool:
        """
        Extend the lease on a RUNNING job held by ``worker_id``. Returns False if
        the job is not RUNNING, is held by a different worker, or does not exist.
        """
        ...

    def complete(
        self,
        job_id: str,
        *,
        result: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> ExecutionJob:
        """
        Mark a job SUCCEEDED. No-op if the job is already terminal (cancellation
        wins over a late completion). Raises ``JobNotFoundError`` if unknown.
        """
        ...

    def fail(
        self,
        job_id: str,
        *,
        error: str,
        now: Optional[datetime] = None,
    ) -> ExecutionJob:
        """
        Record a failed attempt. If the retry budget remains, reschedule the job
        as PENDING with a backoff delay; otherwise dead-letter it. No-op if the
        job is already terminal. Raises ``JobNotFoundError`` if unknown.
        """
        ...

    def cancel(self, job_id: str, *, now: Optional[datetime] = None) -> bool:
        """
        Cancel a non-terminal job. Returns True if it was cancelled, False if it
        was already terminal or unknown. Cancellation is sticky.
        """
        ...

    def recover_stale(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> List[ExecutionJob]:
        """
        Reset RUNNING jobs whose lease has expired back to PENDING so they can be
        reclaimed. Returns the jobs that were reset. Intended as an explicit
        startup sweep; ``claim_next`` also reclaims expired leases on demand.
        """
        ...

    def get(self, job_id: str) -> Optional[ExecutionJob]:
        """Return the job by id, or None."""
        ...

    def list_jobs(
        self,
        *,
        states: Optional[Sequence[ExecutionState]] = None,
        agent_id: Optional[str] = None,
        kind: Optional[ExecutionKind] = None,
        limit: Optional[int] = None,
    ) -> List[ExecutionJob]:
        """
        Return jobs matching the filters, newest-first by ``created_at``. This is
        the inspection surface durable AgentRun history builds on.
        """
        ...
