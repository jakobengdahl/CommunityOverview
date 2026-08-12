"""Unit tests for the execution-store data model."""

from datetime import datetime, timezone

import pytest

from backend.agents.execution.models import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
)


def test_terminal_state_classification():
    assert ExecutionState.SUCCEEDED.is_terminal
    assert ExecutionState.DEAD_LETTER.is_terminal
    assert ExecutionState.CANCELLED.is_terminal
    assert not ExecutionState.PENDING.is_terminal
    assert not ExecutionState.RUNNING.is_terminal


def test_retry_policy_capped_exponential_backoff():
    policy = RetryPolicy(max_attempts=5, base_seconds=1.0, factor=2.0, max_seconds=10.0)
    assert policy.backoff_seconds(0) == 0.0
    assert policy.backoff_seconds(1) == 1.0
    assert policy.backoff_seconds(2) == 2.0
    assert policy.backoff_seconds(3) == 4.0
    assert policy.backoff_seconds(4) == 8.0
    # Capped at max_seconds.
    assert policy.backoff_seconds(5) == 10.0
    assert policy.backoff_seconds(50) == 10.0


def test_retry_policy_validates_bounds():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(factor=0.5)
    with pytest.raises(ValueError):
        RetryPolicy(base_seconds=-1)


def test_job_accepts_string_enums():
    job = ExecutionJob(agent_id="a", kind="event", idempotency_key="k")
    assert job.kind is ExecutionKind.EVENT
    assert job.state is ExecutionState.PENDING


def test_job_roundtrips_through_dict():
    job = ExecutionJob(
        agent_id="agent-9",
        kind=ExecutionKind.SCHEDULED,
        idempotency_key="sched:agent-9:2026-01-01T12:00",
        payload={"event_type": "scheduled_trigger"},
        correlation_id="corr-1",
        session_id="sess-1",
        origin="scheduler",
        run_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    job.attempts = 2
    job.state = ExecutionState.RUNNING
    job.lease_owner = "worker-1"
    job.lease_expiry = datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)

    restored = ExecutionJob.from_dict(job.to_dict())

    assert restored.to_dict() == job.to_dict()
    assert restored.kind is ExecutionKind.SCHEDULED
    assert restored.state is ExecutionState.RUNNING
    assert restored.run_at == job.run_at
    assert restored.lease_expiry == job.lease_expiry
    assert restored.payload == {"event_type": "scheduled_trigger"}


def test_from_dict_parses_z_suffix_timestamps():
    job = ExecutionJob.from_dict(
        {
            "agent_id": "a",
            "kind": "event",
            "idempotency_key": "k",
            "run_at": "2026-01-01T12:00:00Z",
        }
    )
    assert job.run_at == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_copy_is_independent():
    job = ExecutionJob(agent_id="a", kind=ExecutionKind.EVENT, idempotency_key="k")
    job.payload["x"] = 1
    clone = job.copy()
    clone.payload["x"] = 2
    assert job.payload["x"] == 1
