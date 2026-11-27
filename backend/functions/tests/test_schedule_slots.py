# covers: backend/functions/schedule_slots.py
"""Persistent schedule slot identity and bounded execution-store lifecycle."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3

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


@pytest.mark.parametrize(
    "policy",
    [
        RetryPolicy(max_attempts=3, factor=1e200),
        RetryPolicy(max_attempts=3, base_seconds=1e20, max_seconds=1e20),
    ],
    ids=["exponent-overflow", "timestamp-overflow"],
)
def test_rejects_overflowing_retry_policies(tmp_path, policy):
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        with pytest.raises(ValueError, match="Invalid retry policy"):
            slots.enqueue("example", TIMEPOINT, retry_policy=policy)
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


def test_rate_limit_skips_initial_slot_but_not_retry(tmp_path):
    slots = ScheduleSlots(tmp_path / "slots.db", rate_limit=1)
    try:
        first = slots.enqueue(
            "example",
            TIMEPOINT,
            retry_policy=RetryPolicy(max_attempts=2, base_seconds=1),
        )
        slots.claim_next("w", now=TIMEPOINT, lease_seconds=10)
        slots.fail(first.id, attempt=1, now=TIMEPOINT)
        later = TIMEPOINT + timedelta(seconds=2)
        skipped = slots.enqueue("example", later)
        assert slots.claim_next("w", now=later, lease_seconds=10) is None
        assert slots.enqueue("example", later).dead_letter_reason == "rate limited"
        assert slots.enqueue("example", later).id == skipped.id
        pending = slots.enqueue("example", TIMEPOINT)
        assert pending.state == ExecutionState.PENDING
        assert pending.attempts == 1
        other = slots.enqueue("another", later)
        assert slots.claim_next("w", now=later, lease_seconds=10).id == other.id
        slots.close()
        slots = ScheduleSlots(tmp_path / "slots.db", rate_limit=1)
        assert slots.claim_next("w", now=later, lease_seconds=10) is None
        available = TIMEPOINT + timedelta(seconds=60)
        retry = slots.claim_next("w", now=available, lease_seconds=10)
        assert retry.id == first.id and retry.attempts == 2
        assert (
            slots.complete(first.id, attempt=2, now=available).state
            == ExecutionState.SUCCEEDED
        )
    finally:
        slots.close()


def test_enqueue_lock_failure_rolls_back(tmp_path):
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    other = sqlite3.connect(path)
    try:
        slots._store._conn.execute("PRAGMA busy_timeout = 1")
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            slots.enqueue("example", TIMEPOINT)
        other.rollback()
        job = slots.enqueue("example", TIMEPOINT)
        assert slots.claim_next("w", now=TIMEPOINT, lease_seconds=1).id == job.id
        assert (
            slots.complete(job.id, attempt=1, now=TIMEPOINT).state
            == ExecutionState.SUCCEEDED
        )
    finally:
        other.close()
        slots.close()


def test_retry_revoked_before_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.functions import invocation

    slots = ScheduleSlots(tmp_path / "slots.db")
    policy = RetryPolicy(max_attempts=2, base_seconds=0)
    job = slots.enqueue("example", TIMEPOINT, retry_policy=policy)
    slots.claim_next("w", now=TIMEPOINT, lease_seconds=10)
    slots.fail(job.id, attempt=1, now=TIMEPOINT)
    monkeypatch.setattr(invocation, "load_manifest", lambda *args: ({}, None))
    service = SimpleNamespace(root=tmp_path, invoke=Mock())
    try:
        slots.dispatch(service, "w", actor_binding={}, now=TIMEPOINT, lease_seconds=10)
        service.invoke.assert_not_called()
        assert slots.enqueue("example", TIMEPOINT).state == ExecutionState.DEAD_LETTER
    finally:
        slots.close()


def test_invalid_manifest_schema_only_dead_letters_its_delivery(tmp_path):
    from types import SimpleNamespace
    from jsonschema.exceptions import SchemaError
    from backend.functions.invocation import load_manifest

    old = RetryPolicy(max_attempts=3, base_seconds=1)
    changed = RetryPolicy(max_attempts=3, base_seconds=9)
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        jobs = []
        for name in ("first", "broken", "last"):
            directory = tmp_path / name
            directory.mkdir()
            manifest = {
                "contract_version": "5",
                "id": name,
                "version": "1.0.0",
                "name": name,
                "runtime": "python",
                "entry_point": "main.py",
                "triggers": [{"kind": "schedule", "interval_seconds": 60}],
                "grants": [],
                "retry": changed.to_dict(),
                "input_schema": {"type": "invalid" if name == "broken" else "object"},
            }
            (directory / "function.json").write_text(json.dumps(manifest))
            (directory / "main.py").write_text("pass")
            job = slots.enqueue(name, TIMEPOINT, retry_policy=old)
            slots.claim_next("w", now=TIMEPOINT, lease_seconds=10)
            slots.fail(job.id, attempt=1, now=TIMEPOINT)
            jobs.append(job)

        with pytest.raises(SchemaError):
            load_manifest(tmp_path, "broken")
        slots._refresh_pending_retries(SimpleNamespace(root=tmp_path), TIMEPOINT)
        first, broken, last = (slots._store._get_by_id(job.id) for job in jobs)
        assert broken.state == ExecutionState.DEAD_LETTER
        for job in (first, last):
            assert job.state == ExecutionState.PENDING
            assert job.run_at == TIMEPOINT + timedelta(seconds=9)
            assert job.payload["retry_policy"] == changed.to_dict()
    finally:
        slots.close()


def test_pending_retry_refresh_caps_backoff_at_one_day(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.functions import invocation

    old = RetryPolicy(max_attempts=3, base_seconds=0)
    changed = RetryPolicy(
        max_attempts=3, base_seconds=50000, factor=2, max_seconds=86400
    )
    monkeypatch.setattr(
        invocation, "load_manifest", lambda *args: ({"retry": changed.to_dict()}, None)
    )
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        job = slots.enqueue("example", TIMEPOINT, retry_policy=old)
        for attempt in (1, 2):
            assert slots.claim_next("w", now=TIMEPOINT, lease_seconds=10).id == job.id
            slots.fail(job.id, attempt=attempt, now=TIMEPOINT)
        slots._refresh_pending_retries(SimpleNamespace(root=tmp_path), TIMEPOINT)
        current = slots._store._get_by_id(job.id)
        assert current.run_at == TIMEPOINT + timedelta(seconds=86400)
        assert (
            slots.claim_next(
                "w", now=current.run_at - timedelta(seconds=1), lease_seconds=10
            )
            is None
        )
        assert slots.claim_next("w", now=current.run_at, lease_seconds=10).id == job.id
    finally:
        slots.close()


@pytest.mark.parametrize("delivery", ["schedule", "graph"])
@pytest.mark.parametrize("max_attempts", [1, 3, 11])
@pytest.mark.parametrize("dispatch_limit", [1, 2])
@pytest.mark.parametrize("change_after_claim", [False, True])
def test_changed_retry_policy_governs_failed_and_pending_attempts(
    tmp_path, monkeypatch, delivery, max_attempts, dispatch_limit, change_after_claim
):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.functions import invocation

    old = RetryPolicy(max_attempts=2, base_seconds=5)
    changed = RetryPolicy(max_attempts=max_attempts, base_seconds=7, factor=2)
    declared = [changed.to_dict()]
    monkeypatch.setattr(
        invocation, "load_manifest", lambda *args: ({"retry": declared[0]}, None)
    )
    slots = ScheduleSlots(tmp_path / "slots.db")
    service = SimpleNamespace(
        root=tmp_path,
        invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.SUCCEEDED)),
    )
    try:
        if delivery == "schedule":
            job = slots.enqueue("example", TIMEPOINT, retry_policy=old)
        else:
            event = SimpleNamespace(
                to_webhook_payload=lambda: {"event_id": "e", "origin": {}}
            )
            job = slots.enqueue_event("example", event, {"id": "n"}, old, graph_id="g")
        now = max(TIMEPOINT, job.run_at)
        running = slots.claim_next("w", now=now, lease_seconds=10)
        slots._finish_result(
            service, running, SimpleNamespace(state=ExecutionState.DEAD_LETTER), now
        )
        current = slots._store._get_by_id(job.id)
        if max_attempts != 3:
            assert current.state == ExecutionState.DEAD_LETTER
            assert service.invoke.call_count == 0
            return
        assert current.state == ExecutionState.PENDING
        assert current.run_at == now + timedelta(seconds=7)
        assert current.payload["retry_policy"] == changed.to_dict()
        declared[0] = RetryPolicy(max_attempts=dispatch_limit, base_seconds=9).to_dict()
        if change_after_claim and dispatch_limit == 2:
            read_retry = slots._retry_declared

            def change_policy(service, job):
                declared[0] = RetryPolicy(max_attempts=2, base_seconds=11).to_dict()
                return read_retry(service, job)

            monkeypatch.setattr(slots, "_retry_declared", change_policy)
        slots.dispatch(
            service,
            "w",
            actor_binding={"graph_id": "g"},
            now=current.run_at,
            lease_seconds=10,
            graph_event_filter=lambda job: job.payload["entity"],
        )
        current = slots._store._get_by_id(job.id)
        if dispatch_limit == 2:
            assert current.state == ExecutionState.PENDING
            assert current.run_at == now + timedelta(seconds=9)
            if change_after_claim:
                slots.dispatch(
                    service,
                    "w",
                    actor_binding={"graph_id": "g"},
                    now=current.run_at,
                    lease_seconds=10,
                    graph_event_filter=lambda job: job.payload["entity"],
                )
                current = slots._store._get_by_id(job.id)
                assert current.state == ExecutionState.PENDING
                assert current.attempts == 1
                assert current.run_at == now + timedelta(seconds=11)
                assert service.invoke.call_count == 0
                assert (
                    slots._store._conn.execute(
                        "SELECT COUNT(*) FROM schedule_rate"
                    ).fetchone()[0]
                    == 1
                )
            slots.dispatch(
                service,
                "w",
                actor_binding={"graph_id": "g"},
                now=current.run_at,
                lease_seconds=10,
                graph_event_filter=lambda job: job.payload["entity"],
            )
            current = slots._store._get_by_id(job.id)
        assert service.invoke.call_count == (dispatch_limit == 2)
        assert current.state == (
            ExecutionState.SUCCEEDED
            if dispatch_limit == 2
            else ExecutionState.DEAD_LETTER
        )
        assert current.payload["retry_policy"] == declared[0]
        assert (
            slots.claim_next("w", now=now + timedelta(days=1), lease_seconds=10) is None
        )
    finally:
        slots.close()


@pytest.mark.parametrize(
    "outcome,purge,expected",
    [
        ("failed", True, ExecutionState.PENDING),
        ("failed", False, ExecutionState.PENDING),
        ("abandoned", False, ExecutionState.RUNNING),
        ("abandoned", True, ExecutionState.RUNNING),
        ("succeeded", True, ExecutionState.SUCCEEDED),
        ("no_retry", True, ExecutionState.DEAD_LETTER),
        ("manual", False, ExecutionState.RUNNING),
        ("manual", True, ExecutionState.RUNNING),
        ("legacy_schedule", False, ExecutionState.SUCCEEDED),
    ],
)
def test_reconcile_retained_and_purged_outcomes(
    tmp_path, monkeypatch, outcome, purge, expected
):
    from types import SimpleNamespace
    from backend.agents.execution import ExecutionJob, ExecutionKind
    from backend.functions import invocation

    policy = RetryPolicy(max_attempts=2, base_seconds=5)
    monkeypatch.setattr(
        invocation,
        "load_manifest",
        lambda *args: (
            {"retry": policy.to_dict()} if outcome != "no_retry" else {},
            None,
        ),
    )
    slots = ScheduleSlots(tmp_path / "slots.db")
    store = SqliteExecutionStore(tmp_path / "runs.db")
    try:
        slot = slots.enqueue("example", TIMEPOINT, retry_policy=policy)
        slots.claim_next("w", now=TIMEPOINT, lease_seconds=10)
        run = ExecutionJob(
            agent_id="example",
            kind=ExecutionKind.FUNCTION,
            idempotency_key=f"{'fn' if outcome in ('manual', 'legacy_schedule') else 'schedule-fn'}:example:{slot.idempotency_key}:attempt:1",
            payload={"trigger": "manual" if outcome == "manual" else "schedule"},
            state=(
                ExecutionState.RUNNING
                if outcome == "abandoned"
                else ExecutionState.SUCCEEDED
                if outcome in ("succeeded", "manual", "legacy_schedule")
                else ExecutionState.DEAD_LETTER
            ),
            updated_at=TIMEPOINT,
            finished_at=None if outcome == "abandoned" else TIMEPOINT,
        )
        store.enqueue(run)
        if outcome == "abandoned":
            assert store.abandon([run.id], cutoff=TIMEPOINT + timedelta(days=1)) == 1
            assert store.redact([run.id]) == 1
        if purge:
            assert store.purge([run.id]) == 1
            store.close()
            store = SqliteExecutionStore(tmp_path / "runs.db")
        slots.close()
        slots = ScheduleSlots(tmp_path / "slots.db")
        slots.reconcile(SimpleNamespace(store=store, root=tmp_path))
        current = slots.enqueue("example", TIMEPOINT)
        assert current.state == expected
        assert current.attempts == 1
        if expected == ExecutionState.PENDING:
            assert current.run_at == TIMEPOINT + timedelta(seconds=5)
    finally:
        store.close()
        slots.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_graph_attempt_purge_preserves_recovery_outcome(
    tmp_path, monkeypatch, store_kind
):
    from types import SimpleNamespace
    from backend.agents.execution import (
        ExecutionJob,
        ExecutionKind,
        InMemoryExecutionStore,
    )
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )
    from backend.functions import invocation

    policy = RetryPolicy(max_attempts=2, base_seconds=5)
    monkeypatch.setattr(
        invocation, "load_manifest", lambda *args: ({"retry": policy.to_dict()}, None)
    )
    slots = ScheduleSlots(tmp_path / "graph.db")
    store = (
        InMemoryExecutionStore()
        if store_kind == "memory"
        else SqliteExecutionStore(tmp_path / "runs.db")
    )
    event = Event(
        event_type=EventType.NODE_UPDATE,
        origin=EventContext(),
        entity=EntityData(kind=EntityKind.NODE, id="n", type="Task"),
    )
    slot = slots.enqueue_event("example", event, {}, policy)
    slots.claim_next(
        "w", now=datetime.now(timezone.utc) + timedelta(seconds=1), lease_seconds=10
    )
    run = ExecutionJob(
        agent_id="example",
        kind=ExecutionKind.FUNCTION,
        idempotency_key=f"schedule-fn:example:{slot.idempotency_key}:attempt:1",
        state=ExecutionState.DEAD_LETTER,
        updated_at=TIMEPOINT,
        finished_at=TIMEPOINT,
    )
    store.enqueue(run)
    assert store.purge([run.id]) == 1
    slots.reconcile(SimpleNamespace(store=store, root=tmp_path))
    recovered = slots._store._get_by_id(slot.id)
    assert recovered.state == ExecutionState.PENDING
    assert recovered.run_at == TIMEPOINT + timedelta(seconds=5)
    if store_kind == "sqlite":
        store.close()
    slots.close()


@pytest.mark.parametrize("close_by", ["size", "window"])
def test_graph_batch_selection_uses_time_after_lock(tmp_path, monkeypatch, close_by):
    import threading
    from types import SimpleNamespace
    from backend.functions import schedule_slots

    current = [TIMEPOINT]
    waiting = threading.Event()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if threading.current_thread().name == "enqueue-worker":
                waiting.set()
            return current[0]

    monkeypatch.setattr(schedule_slots, "datetime", Clock)
    slots = ScheduleSlots(tmp_path / "slots.db")
    batching = {"debounce_seconds": 10, "batch_size": 2}

    def enqueue(event_id):
        event = SimpleNamespace(
            to_webhook_payload=lambda: {"event_id": event_id, "origin": {}}
        )
        return slots.enqueue_event("example", event, {}, batching=batching)

    try:
        first = enqueue("first")
        result = []
        with slots._store._lock:
            thread = threading.Thread(
                target=lambda: result.append(enqueue("waiting")), name="enqueue-worker"
            )
            thread.start()
            assert waiting.wait(5)
            current[0] += timedelta(seconds=11 if close_by == "window" else 1)
            if close_by == "size":
                assert enqueue("filler").id == first.id
        thread.join(5)
        assert not thread.is_alive()
        assert result[0].id != first.id
        first = slots._store._get_by_id(first.id)
        assert len(first.payload["entries"]) == (2 if close_by == "size" else 1)
        assert len(result[0].payload["entries"]) == 1
    finally:
        slots.close()


def test_graph_batch_records_member_refusal_across_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    slots = ScheduleSlots(tmp_path / "slots.db")
    policy = RetryPolicy(max_attempts=2, base_seconds=0)
    batching = {"debounce_seconds": 10, "batch_size": 2}

    def enqueue(event_id):
        event = SimpleNamespace(
            to_webhook_payload=lambda: {"event_id": event_id, "origin": {}}
        )
        return slots.enqueue_event(
            "example", event, {"id": event_id}, policy, graph_id="g", batching=batching
        )

    try:
        job = enqueue("accepted")
        assert enqueue("rejected").id == job.id
        service = SimpleNamespace(
            invoke=Mock(return_value=SimpleNamespace(state=ExecutionState.DEAD_LETTER))
        )
        monkeypatch.setattr(slots, "_retry_declared", lambda *args: policy)
        monkeypatch.setattr(slots, "_read_retry_policy", lambda *args: policy)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        slots.dispatch(
            service,
            "w",
            actor_binding={"graph_id": "g"},
            now=now,
            lease_seconds=10,
            graph_event_filter=lambda member: (
                member.payload["entity"]
                if member.payload["entity"]["id"] == "accepted"
                else None
            ),
        )
        rows = slots._store._conn.execute(
            "SELECT event_id, refusal_reason FROM graph_batch_members ORDER BY event_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("accepted", None),
            ("rejected", "trigger_changed"),
        ]
        assert [
            entry["event_id"]
            for entry in service.invoke.call_args.kwargs["entity"]["events"]
        ] == ["accepted"]
        slots.close()
        slots = ScheduleSlots(tmp_path / "slots.db")
        monkeypatch.setattr(slots, "_retry_declared", lambda *args: policy)
        monkeypatch.setattr(slots, "_read_retry_policy", lambda *args: policy)
        slots.dispatch(
            service,
            "w",
            actor_binding={"graph_id": "g"},
            now=now + timedelta(seconds=1),
            lease_seconds=10,
            graph_event_filter=lambda member: member.payload["entity"],
        )
        assert [
            entry["event_id"]
            for entry in service.invoke.call_args.kwargs["entity"]["events"]
        ] == ["accepted"]
    finally:
        slots.close()


@pytest.mark.parametrize("batching", [None, {"debounce_seconds": 1, "batch_size": 1}])
def test_graph_retention_purges_only_finished_deliveries_and_their_identity(
    tmp_path, batching
):
    """Finished graph deliveries and batches, with their event identities, age out; pending ones stay."""
    from backend.core.events.models import (
        Event,
        EventContext,
        EntityData,
        EntityKind,
        EventType,
    )

    slots = ScheduleSlots(tmp_path / "graph.db", graph_retention_seconds=60)
    events = [
        Event(
            event_type=EventType.NODE_UPDATE,
            origin=EventContext(),
            entity=EntityData(kind=EntityKind.NODE, id=node_id, type="Task"),
        )
        for node_id in ("done", "retry")
    ]
    policy = RetryPolicy(max_attempts=2, base_seconds=3600)
    done, retry = (
        slots.enqueue_event("example", e, {}, policy, batching=batching) for e in events
    )
    assert done.id != retry.id
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    for _ in range(2):
        job = slots.claim_next("w", now=now, lease_seconds=10)
        finish = slots.complete if job.id == done.id else slots.fail
        finish(job.id, attempt=1, now=now)
    later = now + timedelta(seconds=61)
    assert slots.purge_graph_deliveries(later) == 1
    assert slots._store._get_by_id(done.id) is None
    assert slots._store._get_by_id(retry.id).state == ExecutionState.PENDING
    members = slots._store._conn.execute("SELECT job_id FROM graph_batch_members")
    assert [row[0] for row in members] == [retry.id]
    # The purge is throttled, and an aged-out identity no longer deduplicates.
    again = slots.enqueue_event("example", events[0], {}, batching=batching)
    assert again.id not in (done.id, retry.id)
    assert slots.purge_graph_deliveries(later + timedelta(days=1)) == 0
    slots.close()
