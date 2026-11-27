# covers: backend/functions/schedule_slots.py
"""Persistent schedule slot identity and bounded execution-store lifecycle."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.execution import ExecutionState, RetryPolicy
from backend.functions.schedule_slots import ScheduleSlots


TIMEPOINT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_slot_identity_survives_restart_and_normalizes_timezones(tmp_path):
    path = tmp_path / "slots.db"
    policy = RetryPolicy(max_attempts=1)
    slots = ScheduleSlots(path)
    first = slots.enqueue("example", TIMEPOINT, retry_policy=policy)
    slots.close()
    slots = ScheduleSlots(path)
    try:
        same = slots.enqueue(
            "example", TIMEPOINT.astimezone(timezone(timedelta(hours=2)))
        )
        other_function = slots.enqueue("another", TIMEPOINT)
        other_time = slots.enqueue("example", TIMEPOINT + timedelta(minutes=1))
        assert same.id == first.id
        assert len({first.id, other_function.id, other_time.id}) == 3
        assert same.correlation_id == first.idempotency_key
    finally:
        slots.close()


def test_completed_slot_is_not_reclaimed_or_recreated(tmp_path):
    path = tmp_path / "slots.db"
    policy = RetryPolicy(max_attempts=3)
    slots = ScheduleSlots(path)
    job = slots.enqueue("example", TIMEPOINT, retry_policy=policy)
    assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10).id == job.id
    assert slots.complete(job.id, now=TIMEPOINT).state == ExecutionState.SUCCEEDED
    slots.close()
    slots = ScheduleSlots(path)
    try:
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.SUCCEEDED
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
    finally:
        slots.close()


def test_failed_slot_obeys_store_backoff_and_attempt_limit(tmp_path):
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        job = slots.enqueue(
            "example",
            TIMEPOINT,
            retry_policy=RetryPolicy(max_attempts=2, base_seconds=5),
        )
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10).attempts == 1
        assert slots.fail(job.id, now=TIMEPOINT).state == ExecutionState.PENDING
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
        later = TIMEPOINT + timedelta(seconds=5)
        assert slots.claim_next("worker", now=later, lease_seconds=10).attempts == 2
        assert slots.fail(job.id, now=later).state == ExecutionState.DEAD_LETTER
        assert slots.claim_next("worker", now=later, lease_seconds=10) is None
    finally:
        slots.close()


def test_expired_lease_is_not_reclaimed_after_restart(tmp_path):
    path = tmp_path / "slots.db"
    policy = RetryPolicy(max_attempts=2)
    slots = ScheduleSlots(path)
    job = slots.enqueue("example", TIMEPOINT, retry_policy=policy)
    slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10)
    slots.close()
    slots = ScheduleSlots(path)
    try:
        recovered = slots.claim_next(
            "other", now=TIMEPOINT + timedelta(seconds=10), lease_seconds=10
        )
        assert recovered is None
        assert (
            slots.complete(job.id, now=TIMEPOINT + timedelta(seconds=10)).state
            == ExecutionState.SUCCEEDED
        )
        assert (
            slots.claim_next(
                "other", now=TIMEPOINT + timedelta(seconds=20), lease_seconds=10
            )
            is None
        )
    finally:
        slots.close()


def test_rejects_invalid_inputs(tmp_path):
    for path in ("", ":memory:"):
        with pytest.raises(ValueError):
            ScheduleSlots(path)
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        with pytest.raises(ValueError):
            slots.enqueue("example", datetime(2026, 1, 1))
    finally:
        slots.close()


def test_persisted_per_slot_retry_and_catchup(tmp_path):
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    old = slots.enqueue("example", TIMEPOINT)
    latest = slots.enqueue(
        "example",
        TIMEPOINT + timedelta(seconds=1),
        retry_policy=RetryPolicy(max_attempts=2, base_seconds=5),
    )
    other = slots.enqueue("another", TIMEPOINT)
    now = TIMEPOINT + timedelta(seconds=2)
    assert slots.claim_next("w", now=now, lease_seconds=1).id == other.id
    assert slots.fail(other.id, now=now).state == ExecutionState.DEAD_LETTER
    assert slots.claim_next("w", now=now, lease_seconds=1).id == latest.id
    assert slots.fail(latest.id, now=now).state == ExecutionState.PENDING
    slots.close()
    slots = ScheduleSlots(path)
    try:
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.CANCELLED
        assert (
            slots.enqueue("example", TIMEPOINT + timedelta(seconds=1)).id == latest.id
        )
        assert slots.claim_next("w", now=now, lease_seconds=1) is None
        later = now + timedelta(seconds=5)
        assert slots.claim_next("w", now=later, lease_seconds=1).attempts == 2
        assert slots.fail(latest.id, now=later).state == ExecutionState.DEAD_LETTER
        assert slots.enqueue("example", TIMEPOINT - timedelta(seconds=1)).id != old.id
        assert slots.claim_next("w", now=later, lease_seconds=1) is None
    finally:
        slots.close()
