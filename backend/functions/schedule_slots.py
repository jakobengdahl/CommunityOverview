"""Persistent schedule-slot identities, separate from purgable run history.

This ledger owns its database: it must not be passed to run-history retention.
It deliberately stores no function input, actor attribution or run output.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    RetryPolicy,
    SqliteExecutionStore,
)


class ScheduleSlots:
    """A durable queue of uniquely identified function and fire-time pairs."""

    def __init__(self, path: str | Path, *, retry_policy: RetryPolicy):
        if str(path) in ("", ":memory:"):
            raise ValueError("Schedule slots require a file-backed database")
        self._store = SqliteExecutionStore(path, retry_policy=retry_policy)

    def close(self):
        self._store.close()

    def enqueue(self, function_id: str, fire_time: datetime) -> ExecutionJob:
        if not isinstance(function_id, str) or not function_id:
            raise ValueError("Invalid function id")
        if not isinstance(fire_time, datetime) or fire_time.utcoffset() is None:
            raise ValueError("Fire time must be timezone-aware")
        fire_time = fire_time.astimezone(timezone.utc)
        identity = json.dumps(
            [function_id, fire_time.isoformat()], separators=(",", ":")
        )
        key = "function-schedule:" + hashlib.sha256(identity.encode()).hexdigest()
        return self._store.enqueue(
            ExecutionJob(
                agent_id=function_id,
                kind=ExecutionKind.SCHEDULED,
                idempotency_key=key,
                correlation_id=key,
                run_at=fire_time,
            )
        )

    def claim_next(self, worker_id: str, *, now: datetime, lease_seconds: float):
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("Worker id is required")
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Claim time must be timezone-aware")
        if not 0 < lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        return self._store.claim_next(worker_id, now=now, lease_seconds=lease_seconds)

    def complete(self, job_id: str, *, now: datetime):
        return self._store.complete(job_id, now=now)

    def fail(self, job_id: str, *, now: datetime):
        # No potentially sensitive exception text belongs in the permanent ledger.
        return self._store.fail(job_id, error="attempt failed", now=now)
