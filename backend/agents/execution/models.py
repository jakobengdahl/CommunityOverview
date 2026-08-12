"""
Data model for the durable agent execution-store contract.

These types describe *what* a durable execution store persists, independent of
*how* any particular adapter persists it. They are deliberately storage-free:
no I/O, no locks, no threads — pure data plus small helpers so both the
reference in-memory adapter and future durable adapters (local file/SQL,
hosted) agree on the same shape and semantics.

See ``docs/DURABLE_EXECUTION_CONTRACT.md`` for the normative contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class ExecutionKind(str, Enum):
    """What triggered a unit of agent work."""

    SCHEDULED = "scheduled"  # time-based trigger (AgentScheduler)
    EVENT = "event"  # graph-mutation / subscription trigger


class ExecutionState(str, Enum):
    """
    Lifecycle state of a job in the execution store.

    Non-terminal:
        PENDING  – queued, eligible to run once ``run_at`` has passed.
        RUNNING  – claimed by a worker and holding a lease until ``lease_expiry``.

    Terminal:
        SUCCEEDED   – completed successfully.
        DEAD_LETTER – gave up after exhausting the retry budget.
        CANCELLED   – cancelled before reaching another terminal state.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.DEAD_LETTER,
        ExecutionState.CANCELLED,
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """
    Bounded, capped-exponential retry policy.

    A job is attempted at most ``max_attempts`` times. The delay applied before
    the attempt that follows ``attempts`` failures is::

        min(base_seconds * (factor ** (attempts - 1)), max_seconds)

    The policy is intentionally deterministic (no jitter) so that recovery and
    backoff behaviour is reproducible in tests and across adapters. Jitter, if
    ever needed, belongs in a hosted adapter, not in this core contract.
    """

    max_attempts: int = 3
    base_seconds: float = 0.5
    factor: float = 2.0
    max_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_seconds < 0 or self.max_seconds < 0:
            raise ValueError("backoff seconds must be non-negative")
        if self.factor < 1:
            raise ValueError("factor must be >= 1")

    def backoff_seconds(self, attempts: int) -> float:
        """Delay before the next attempt, given how many attempts have failed."""
        if attempts <= 0:
            return 0.0
        delay = self.base_seconds * (self.factor ** (attempts - 1))
        return min(delay, self.max_seconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_seconds": self.base_seconds,
            "factor": self.factor,
            "max_seconds": self.max_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetryPolicy":
        return cls(
            max_attempts=int(data.get("max_attempts", 3)),
            base_seconds=float(data.get("base_seconds", 0.5)),
            factor=float(data.get("factor", 2.0)),
            max_seconds=float(data.get("max_seconds", 300.0)),
        )


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (or passthrough datetime) into aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ExecutionJob:
    """
    One durable unit of agent work.

    A job is created by a producer (scheduler or event router), claimed by a
    worker, and driven to a terminal state. Every field here must survive a
    process restart when persisted by a durable adapter; the reference
    in-memory adapter keeps the same shape without the durability.

    Correlation fields (``correlation_id``, ``session_id``, ``origin``) mirror
    the event-context vocabulary already used across the graph so a job can be
    traced back to the mutation or schedule that produced it.
    """

    agent_id: str
    kind: ExecutionKind
    idempotency_key: str
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"job-{uuid.uuid4()}")
    state: ExecutionState = ExecutionState.PENDING
    attempts: int = 0

    # Scheduling / backoff: the job is eligible to run once run_at has passed.
    run_at: datetime = field(default_factory=utcnow)

    # Lease held while RUNNING; used for restart recovery.
    lease_owner: Optional[str] = None
    lease_expiry: Optional[datetime] = None

    # Correlation / provenance.
    correlation_id: Optional[str] = None
    session_id: Optional[str] = None
    origin: Optional[str] = None

    # Outcome.
    last_error: Optional[str] = None
    dead_letter_reason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    # Timestamps.
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        # Accept plain strings for enum fields so from_dict / callers stay simple.
        if not isinstance(self.kind, ExecutionKind):
            self.kind = ExecutionKind(self.kind)
        if not isinstance(self.state, ExecutionState):
            self.state = ExecutionState(self.state)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def copy(self) -> "ExecutionJob":
        """Return an independent copy (payload/result dicts are shallow-copied)."""
        clone = replace(self)
        clone.payload = dict(self.payload)
        clone.result = dict(self.result) if self.result is not None else None
        return clone

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "kind": self.kind.value,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "state": self.state.value,
            "attempts": self.attempts,
            "run_at": _fmt_dt(self.run_at),
            "lease_owner": self.lease_owner,
            "lease_expiry": _fmt_dt(self.lease_expiry),
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "origin": self.origin,
            "last_error": self.last_error,
            "dead_letter_reason": self.dead_letter_reason,
            "result": self.result,
            "created_at": _fmt_dt(self.created_at),
            "updated_at": _fmt_dt(self.updated_at),
            "started_at": _fmt_dt(self.started_at),
            "finished_at": _fmt_dt(self.finished_at),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionJob":
        job = cls(
            agent_id=data["agent_id"],
            kind=ExecutionKind(data["kind"]),
            idempotency_key=data["idempotency_key"],
            payload=dict(data.get("payload") or {}),
        )
        job.id = data.get("id", job.id)
        job.state = ExecutionState(data.get("state", ExecutionState.PENDING.value))
        job.attempts = int(data.get("attempts", 0))
        job.run_at = _parse_dt(data.get("run_at")) or utcnow()
        job.lease_owner = data.get("lease_owner")
        job.lease_expiry = _parse_dt(data.get("lease_expiry"))
        job.correlation_id = data.get("correlation_id")
        job.session_id = data.get("session_id")
        job.origin = data.get("origin")
        job.last_error = data.get("last_error")
        job.dead_letter_reason = data.get("dead_letter_reason")
        job.result = data.get("result")
        job.created_at = _parse_dt(data.get("created_at")) or job.created_at
        job.updated_at = _parse_dt(data.get("updated_at")) or job.updated_at
        job.started_at = _parse_dt(data.get("started_at"))
        job.finished_at = _parse_dt(data.get("finished_at"))
        return job
