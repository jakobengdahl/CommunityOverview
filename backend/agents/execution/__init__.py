"""
Durable agent execution seam.

Defines the open-core contract for restart-safe scheduled and event-triggered
agent execution: the job data model, the replaceable ``ExecutionStore``
persistence seam, a reference in-memory adapter, the durable local SQLite
adapter, and the executable contract tests every adapter must pass. See
``docs/DURABLE_EXECUTION_CONTRACT.md`` and
``docs/adr/0001-local-durable-execution-store.md``.

Wiring this seam into the scheduler/worker (so live agent runs flow through a
store) is a separate, later slice; this package defines the contract, its
reference in-memory adapter and the default durable adapter.
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
from .sqlite_store import SqliteExecutionStore

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
    "SqliteExecutionStore",
]
