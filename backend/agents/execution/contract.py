"""
Executable specification for the durable execution-store contract.

``ExecutionStoreContractTests`` is a reusable test mixin: any adapter proves it
honours the contract by subclassing it and implementing ``make_store``. The
reference in-memory adapter is bound in
``backend/agents/tests/test_execution_contract.py``; durable local and hosted
adapters reuse this same class so every adapter is held to identical semantics.

The mixin is deliberately *not* named with a ``Test`` prefix so pytest does not
try to collect it on its own (it has no store to run against).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .models import ExecutionJob, ExecutionKind, ExecutionState, RetryPolicy
from .store import ExecutionStore, JobNotFoundError

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _job(
    *,
    agent_id: str = "agent-1",
    kind: ExecutionKind = ExecutionKind.EVENT,
    idempotency_key: str = "key-1",
    run_at: datetime = T0,
    **kw,
) -> ExecutionJob:
    return ExecutionJob(
        agent_id=agent_id,
        kind=kind,
        idempotency_key=idempotency_key,
        run_at=run_at,
        created_at=run_at,
        **kw,
    )


class ExecutionStoreContractTests:
    """Adapter-agnostic behavioural contract. Subclass and define ``make_store``."""

    # Backoff is large and deterministic so scheduled retries land well in the
    # future relative to the test clock.
    retry_policy = RetryPolicy(
        max_attempts=3, base_seconds=10.0, factor=2.0, max_seconds=1000.0
    )

    def make_store(self, retry_policy: RetryPolicy) -> ExecutionStore:
        raise NotImplementedError

    @pytest.fixture
    def store(self) -> ExecutionStore:
        return self.make_store(self.retry_policy)

    # -- enqueue / idempotency ----------------------------------------------

    def test_enqueue_persists_and_is_retrievable(self, store):
        job = store.enqueue(_job())
        assert job.state == ExecutionState.PENDING
        fetched = store.get(job.id)
        assert fetched is not None
        assert fetched.id == job.id
        assert fetched.idempotency_key == "key-1"

    def test_enqueue_is_idempotent_on_key(self, store):
        first = store.enqueue(_job(idempotency_key="dup"))
        second = store.enqueue(_job(idempotency_key="dup"))
        assert second.id == first.id
        assert len(store.list_jobs()) == 1

    def test_enqueue_dedups_against_terminal_job(self, store):
        # The restart-safety property (§5): a key that already reached a terminal
        # state must not re-enqueue new work — a schedule re-fired after it
        # already succeeded stays deduplicated.
        first = store.enqueue(_job(idempotency_key="once"))
        store.claim_next("w1", now=T0)
        store.complete(first.id, now=T0)
        again = store.enqueue(_job(idempotency_key="once"))
        assert again.id == first.id
        assert again.state == ExecutionState.SUCCEEDED
        assert len(store.list_jobs()) == 1

    def test_enqueue_returns_independent_copy(self, store):
        job = store.enqueue(_job())
        job.payload["mutated"] = True
        assert store.get(job.id).payload == {}

    def test_enqueue_dedup_return_is_independent_copy(self, store):
        store.enqueue(_job(idempotency_key="dup"))
        dup = store.enqueue(_job(idempotency_key="dup"))
        dup.payload["mutated"] = True
        assert store.get(dup.id).payload == {}

    # -- claim / lease ------------------------------------------------------

    def test_claim_next_empty_returns_none(self, store):
        assert store.claim_next("w1", now=T0) is None

    def test_claim_next_marks_running_and_leased(self, store):
        store.enqueue(_job())
        claimed = store.claim_next("w1", now=T0, lease_seconds=60)
        assert claimed is not None
        assert claimed.state == ExecutionState.RUNNING
        assert claimed.attempts == 1
        assert claimed.lease_owner == "w1"
        assert claimed.lease_expiry == T0 + timedelta(seconds=60)

    def test_claim_next_respects_run_at(self, store):
        store.enqueue(_job(run_at=T0 + timedelta(seconds=30)))
        assert store.claim_next("w1", now=T0) is None
        later = store.claim_next("w1", now=T0 + timedelta(seconds=30))
        assert later is not None

    def test_claim_next_hands_job_to_one_worker(self, store):
        store.enqueue(_job())
        assert store.claim_next("w1", now=T0) is not None
        # Second claim within the lease window finds nothing runnable.
        assert store.claim_next("w2", now=T0) is None

    def test_claim_next_oldest_first(self, store):
        store.enqueue(_job(idempotency_key="a", run_at=T0))
        store.enqueue(_job(idempotency_key="b", run_at=T0 - timedelta(seconds=5)))
        claimed = store.claim_next("w1", now=T0)
        assert claimed.idempotency_key == "b"

    # -- completion ---------------------------------------------------------

    def test_complete_marks_succeeded(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0)
        done = store.complete(j.id, result={"ok": True}, now=T0)
        assert done.state == ExecutionState.SUCCEEDED
        assert done.result == {"ok": True}
        assert done.finished_at == T0

    def test_complete_unknown_raises(self, store):
        with pytest.raises(JobNotFoundError):
            store.complete("nope")

    # -- retry / backoff / dead-letter --------------------------------------

    def test_fail_with_budget_reschedules_with_backoff(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0)  # attempts -> 1
        failed = store.fail(j.id, error="boom", now=T0)
        assert failed.state == ExecutionState.PENDING
        assert failed.last_error == "boom"
        # base_seconds=10 after the first failed attempt.
        assert failed.run_at == T0 + timedelta(seconds=10)
        # Not runnable before the backoff elapses...
        assert store.claim_next("w1", now=T0) is None
        # ...runnable once it does.
        assert store.claim_next("w1", now=T0 + timedelta(seconds=10)) is not None

    def test_fail_exhausts_budget_dead_letters(self, store):
        j = store.enqueue(_job())
        now = T0
        for _ in range(self.retry_policy.max_attempts):
            claimed = store.claim_next("w1", now=now)
            assert claimed is not None
            failed = store.fail(j.id, error="boom", now=now)
            now = failed.run_at or now
        final = store.get(j.id)
        assert final.state == ExecutionState.DEAD_LETTER
        assert final.attempts == self.retry_policy.max_attempts
        assert final.dead_letter_reason

    # -- cancellation -------------------------------------------------------

    def test_cancel_non_terminal(self, store):
        j = store.enqueue(_job())
        assert store.cancel(j.id, now=T0) is True
        assert store.get(j.id).state == ExecutionState.CANCELLED

    def test_cancel_terminal_returns_false(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0)
        store.complete(j.id, now=T0)
        assert store.cancel(j.id, now=T0) is False

    def test_cancellation_is_sticky_over_completion(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0)
        store.cancel(j.id, now=T0)
        # A late completion must not resurrect a cancelled job.
        after = store.complete(j.id, now=T0)
        assert after.state == ExecutionState.CANCELLED

    def test_cancellation_is_sticky_over_failure(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0)
        store.cancel(j.id, now=T0)
        after = store.fail(j.id, error="boom", now=T0)
        assert after.state == ExecutionState.CANCELLED

    # -- restart recovery ---------------------------------------------------

    def test_recover_stale_resets_expired_leases(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0, lease_seconds=60)
        # Before lease expiry: nothing to recover.
        assert store.recover_stale(now=T0 + timedelta(seconds=30)) == []
        recovered = store.recover_stale(now=T0 + timedelta(seconds=90))
        assert [r.id for r in recovered] == [j.id]
        assert store.get(j.id).state == ExecutionState.PENDING

    def test_claim_next_reclaims_expired_lease(self, store):
        store.enqueue(_job())
        first = store.claim_next("w1", now=T0, lease_seconds=60)
        assert first.attempts == 1
        # Worker crashed; a later claim past the lease reclaims the job.
        reclaimed = store.claim_next("w2", now=T0 + timedelta(seconds=90))
        assert reclaimed is not None
        assert reclaimed.attempts == 2
        assert reclaimed.lease_owner == "w2"

    def test_crash_loop_dead_letters_via_claim(self, store):
        j = store.enqueue(_job())
        now = T0
        # Each claim leases; the worker "crashes" (never completes/fails), the
        # lease expires, and the next claim reclaims it. Past the budget the
        # store dead-letters instead of running again.
        for _ in range(self.retry_policy.max_attempts):
            claimed = store.claim_next("w1", now=now, lease_seconds=60)
            assert claimed is not None
            now = now + timedelta(seconds=90)
        assert store.claim_next("w1", now=now) is None
        assert store.get(j.id).state == ExecutionState.DEAD_LETTER

    def test_renew_lease_extends_and_guards_owner(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0, lease_seconds=60)
        assert store.renew_lease(j.id, "w1", now=T0 + timedelta(seconds=30)) is True
        # Wrong worker cannot renew.
        assert store.renew_lease(j.id, "w2", now=T0 + timedelta(seconds=30)) is False
        # Lease now extends past the original expiry, so no recovery yet.
        assert store.recover_stale(now=T0 + timedelta(seconds=80)) == []

    def test_renew_lease_rejects_non_running_and_unknown(self, store):
        assert store.renew_lease("nope", "w1", now=T0) is False
        j = store.enqueue(_job())
        # PENDING (never claimed) cannot be renewed.
        assert store.renew_lease(j.id, "w1", now=T0) is False
        store.claim_next("w1", now=T0)
        store.complete(j.id, now=T0)
        # Terminal cannot be renewed.
        assert store.renew_lease(j.id, "w1", now=T0) is False

    # -- inspection ---------------------------------------------------------

    def test_list_jobs_filters_and_orders(self, store):
        store.enqueue(
            _job(idempotency_key="a", agent_id="a1", kind=ExecutionKind.EVENT)
        )
        store.enqueue(
            _job(idempotency_key="b", agent_id="a2", kind=ExecutionKind.SCHEDULED)
        )
        assert len(store.list_jobs(agent_id="a1")) == 1
        assert len(store.list_jobs(kind=ExecutionKind.SCHEDULED)) == 1
        assert len(store.list_jobs(states=[ExecutionState.PENDING])) == 2
        assert len(store.list_jobs(states=[ExecutionState.SUCCEEDED])) == 0
        assert len(store.list_jobs(limit=1)) == 1

    def test_list_jobs_newest_first(self, store):
        older = store.enqueue(
            _job(idempotency_key="old", run_at=T0 - timedelta(seconds=60))
        )
        newer = store.enqueue(_job(idempotency_key="new", run_at=T0))
        ordered = store.list_jobs()
        assert [j.id for j in ordered] == [newer.id, older.id]
        # limit keeps the newest.
        assert [j.id for j in store.list_jobs(limit=1)] == [newer.id]
