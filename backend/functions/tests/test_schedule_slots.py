# covers: backend/functions/schedule_slots.py
"""Persistent schedule slot identity and bounded execution-store lifecycle."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.agents.execution import (
    ExecutionState,
    RetryPolicy,
    SqliteExecutionStore,
)
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
    assert (
        slots.complete(job.id, attempt=1, now=TIMEPOINT).state
        == ExecutionState.SUCCEEDED
    )
    slots.close()
    slots = ScheduleSlots(path)
    try:
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.SUCCEEDED
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
    finally:
        slots.close()


@pytest.mark.parametrize("stale_report", ["fail", "complete"])
def test_failed_slot_obeys_store_backoff_and_attempt_limit(tmp_path, stale_report):
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        job = slots.enqueue(
            "example",
            TIMEPOINT,
            retry_policy=RetryPolicy(max_attempts=2, base_seconds=5),
        )
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10).attempts == 1
        assert (
            slots.fail(job.id, attempt=1, now=TIMEPOINT).state == ExecutionState.PENDING
        )
        assert slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10) is None
        later = TIMEPOINT + timedelta(seconds=5)
        assert slots.claim_next("worker", now=later, lease_seconds=10).attempts == 2
        assert (
            getattr(slots, stale_report)(job.id, attempt=1, now=later).state
            == ExecutionState.RUNNING
        )
        assert slots.claim_next("other", now=later, lease_seconds=10) is None
        assert (
            slots.fail(job.id, attempt=2, now=later).state == ExecutionState.DEAD_LETTER
        )
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
            slots.complete(
                job.id, attempt=1, now=TIMEPOINT + timedelta(seconds=10)
            ).state
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


@pytest.mark.parametrize(
    "field", ["max_attempts", "base_seconds", "factor", "max_seconds"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_rejects_invalid_inputs(tmp_path, field, value):
    for path in ("", ":memory:"):
        with pytest.raises(ValueError):
            ScheduleSlots(path)
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        with pytest.raises(ValueError):
            slots.enqueue("example", datetime(2026, 1, 1))
        policy = RetryPolicy(**{field: value})
        with pytest.raises(ValueError, match="Invalid retry policy"):
            slots.enqueue("example", TIMEPOINT, retry_policy=policy)
        job = slots.enqueue("example", TIMEPOINT)
        slots.claim_next("worker", now=TIMEPOINT, lease_seconds=10)
        # Rows written before validation must not remain running on failure.
        slots._store._conn.execute(
            "UPDATE schedule_jobs SET payload = ? WHERE id = ?",
            (json.dumps({"retry_policy": policy.to_dict()}), job.id),
        )
        slots._store._conn.commit()
        failed = slots.fail(job.id, attempt=1, now=TIMEPOINT)
        assert failed.state == ExecutionState.DEAD_LETTER
        assert failed.dead_letter_reason == "invalid retry policy"
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
    assert slots.fail(other.id, attempt=1, now=now).state == ExecutionState.DEAD_LETTER
    assert slots.claim_next("w", now=now, lease_seconds=1).id == latest.id
    assert slots.fail(latest.id, attempt=1, now=now).state == ExecutionState.PENDING
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
        assert (
            slots.fail(latest.id, attempt=2, now=later).state
            == ExecutionState.DEAD_LETTER
        )
        assert slots.enqueue("example", TIMEPOINT - timedelta(seconds=1)).id != old.id
        assert slots.claim_next("w", now=later, lease_seconds=1) is None
    finally:
        slots.close()


def test_late_enqueued_missed_slots_are_skipped_after_restart(tmp_path):
    path = tmp_path / "slots.db"
    now = TIMEPOINT + timedelta(minutes=5)
    slots = ScheduleSlots(path)
    first = slots.enqueue("example", TIMEPOINT)
    assert slots.claim_next("w", now=now, lease_seconds=1).id == first.id
    slots.close()
    slots = ScheduleSlots(path)
    try:
        for minute in (1, 2, 3, 4, 5):
            slots.enqueue("example", TIMEPOINT + timedelta(minutes=minute))
            assert slots.claim_next("w", now=now, lease_seconds=1) is None
            assert (
                slots.enqueue("example", TIMEPOINT + timedelta(minutes=minute)).state
                == ExecutionState.CANCELLED
            )
        future = slots.enqueue("example", now + timedelta(minutes=1))
        assert (
            slots.claim_next("w", now=now + timedelta(minutes=1), lease_seconds=1).id
            == future.id
        )
    finally:
        slots.close()


def test_rejects_existing_execution_store_without_changing_jobs(tmp_path):
    path = tmp_path / "shared.db"
    store = SqliteExecutionStore(path)
    try:
        with pytest.raises(ValueError, match="execution store database"):
            ScheduleSlots(path)
        assert store.list_jobs() == []
    finally:
        store.close()


def test_general_consumer_cannot_access_ledger_jobs(tmp_path):
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    job = slots.enqueue("example", TIMEPOINT)
    store = SqliteExecutionStore(path)
    try:
        assert store.get(job.id) is None
        assert store.claim_next("other", now=TIMEPOINT, lease_seconds=10) is None
        assert not store.cancel(job.id, now=TIMEPOINT)
        assert (
            slots.claim_next("schedule-worker", now=TIMEPOINT, lease_seconds=10).id
            == job.id
        )
        with pytest.raises(ValueError, match="execution store database"):
            ScheduleSlots(path)
    finally:
        store.close()
        slots.close()


def test_due_retry_is_not_cancelled_by_newer_due_slot(tmp_path):
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        retry = slots.enqueue(
            "example",
            TIMEPOINT,
            retry_policy=RetryPolicy(max_attempts=2, base_seconds=5),
        )
        assert slots.claim_next("w", now=TIMEPOINT, lease_seconds=10).id == retry.id
        assert (
            slots.fail(retry.id, attempt=1, now=TIMEPOINT).state
            == ExecutionState.PENDING
        )
        due = TIMEPOINT + timedelta(seconds=6)
        newer = slots.enqueue("example", due)
        claimed = slots.claim_next("w", now=due, lease_seconds=10)
        assert claimed.id == retry.id
        assert claimed.attempts == 2
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.RUNNING
        assert slots.claim_next("w", now=due, lease_seconds=10).id == newer.id
    finally:
        slots.close()
