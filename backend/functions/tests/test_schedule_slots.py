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
    slots = ScheduleSlots(path, retry_policy=policy)
    first = slots.enqueue("example", TIMEPOINT)
    slots.close()
    slots = ScheduleSlots(path, retry_policy=policy)
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
    slots = ScheduleSlots(path, retry_policy=policy)
    job = slots.enqueue("example", TIMEPOINT)
    assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10).id == job.id
    assert slots.complete(job.id, now=TIMEPOINT).state == ExecutionState.SUCCEEDED
    slots.close()
    slots = ScheduleSlots(path, retry_policy=policy)
    try:
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.SUCCEEDED
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
    finally:
        slots.close()


def test_failed_slot_obeys_store_backoff_and_attempt_limit(tmp_path):
    slots = ScheduleSlots(
        tmp_path / "slots.db", retry_policy=RetryPolicy(max_attempts=2, base_seconds=5)
    )
    try:
        job = slots.enqueue("example", TIMEPOINT)
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10).attempts == 1
        assert slots.fail(job.id, now=TIMEPOINT).state == ExecutionState.PENDING
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
        later = TIMEPOINT + timedelta(seconds=5)
        assert slots.claim_next("worker", now=later, lease_seconds=10).attempts == 2
        assert slots.fail(job.id, now=later).state == ExecutionState.DEAD_LETTER
        assert slots.claim_next("worker", now=later, lease_seconds=10) is None
    finally:
        slots.close()


def test_expired_lease_can_be_reclaimed_after_restart(tmp_path):
    path = tmp_path / "slots.db"
    policy = RetryPolicy(max_attempts=2)
    slots = ScheduleSlots(path, retry_policy=policy)
    job = slots.enqueue("example", TIMEPOINT)
    slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10)
    slots.close()
    slots = ScheduleSlots(path, retry_policy=policy)
    try:
        recovered = slots.claim_next(
            "other", now=TIMEPOINT + timedelta(seconds=10), lease_seconds=10
        )
        assert recovered.id == job.id
        assert recovered.attempts == 2
    finally:
        slots.close()


def test_rejects_invalid_inputs(tmp_path):
    for path in ("", ":memory:"):
        with pytest.raises(ValueError):
            ScheduleSlots(path, retry_policy=RetryPolicy())
    slots = ScheduleSlots(tmp_path / "slots.db", retry_policy=RetryPolicy())
    try:
        with pytest.raises(ValueError):
            slots.enqueue("example", datetime(2026, 1, 1))
    finally:
        slots.close()
