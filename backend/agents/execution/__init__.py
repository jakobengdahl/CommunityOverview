"""
Durable agent execution seam.

Defines the open-core contract for restart-safe scheduled and event-triggered
agent execution: the job data model, the replaceable ``ExecutionStore``
persistence seam, a reference in-memory adapter, and the executable contract
tests every adapter must pass. See ``docs/DURABLE_EXECUTION_CONTRACT.md``.

Wiring this seam into the scheduler/worker and shipping a durable adapter are
separate, later slices; this package defines the contract and its reference
implementation only.
"""

from .models import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    utcnow,
)
from .store import ExecutionStore, ExecutionStoreError, JobNotFoundError
from .memory_store import InMemoryExecutionStore

__all__ = [
    "ExecutionJob",
    "ExecutionKind",
    "ExecutionState",
    "RetryPolicy",
    "utcnow",
    "ExecutionStore",
    "ExecutionStoreError",
    "JobNotFoundError",
    "InMemoryExecutionStore",
]
