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

from .models import (
    ABANDONED_REASON,
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
)
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
        # A plain cancel carries no result; only a caller that passes one
        # (e.g. a superseded import) gets one stored.
        assert store.get(j.id).result is None

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

    def test_cancel_can_carry_a_result(self, store):
        """A caller cancelling a job it can explain (e.g. "superseded by a
        later import") attaches a result the same way `complete` does."""
        j = store.enqueue(_job())
        store.cancel(j.id, result={"reason": "superseded"}, now=T0)
        assert store.get(j.id).result == {"reason": "superseded"}

    # -- restart recovery ---------------------------------------------------

    def test_recover_stale_resets_expired_leases(self, store):
        j = store.enqueue(_job())
        store.claim_next("w1", now=T0, lease_seconds=60)
        # Before lease expiry: nothing to recover.
        assert store.recover_stale(now=T0 + timedelta(seconds=30)) == []
        recovered = store.recover_stale(now=T0 + timedelta(seconds=90))
        assert [r.id for r in recovered] == [j.id]
        assert store.get(j.id).state == ExecutionState.PENDING

    def test_recover_stale_resets_every_expired_lease(self, store):
        # More than one expired job, so the recovery must cover the whole set,
        # not just the first id it finds.
        jobs = [store.enqueue(_job(idempotency_key=f"k{i}")) for i in range(3)]
        for _ in jobs:
            store.claim_next("w1", now=T0, lease_seconds=60)
        recovered = store.recover_stale(now=T0 + timedelta(seconds=90))
        assert sorted(r.id for r in recovered) == sorted(j.id for j in jobs)
        for j in jobs:
            got = store.get(j.id)
            assert got.state == ExecutionState.PENDING
            assert got.lease_owner is None

    def test_recover_stale_leaves_live_leases_running(self, store):
        # Expired and live leases side by side: recovery must touch only the
        # expired set, not every RUNNING row.
        expired = [store.enqueue(_job(idempotency_key=f"e{i}")) for i in range(2)]
        for _ in expired:
            store.claim_next("w1", now=T0, lease_seconds=60)
        live = store.enqueue(_job(idempotency_key="live"))
        claimed = store.claim_next(
            "w2", now=T0 + timedelta(seconds=30), lease_seconds=120
        )
        assert claimed.id == live.id
        recovered = store.recover_stale(now=T0 + timedelta(seconds=90))
        assert sorted(r.id for r in recovered) == sorted(j.id for j in expired)
        got = store.get(live.id)
        assert got.state == ExecutionState.RUNNING
        assert got.lease_owner == "w2"

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

    def test_list_jobs_matches_quote_bearing_agent_id_literally(self, store):
        # A filter value carrying SQL syntax must be compared as a literal: it
        # matches only the job whose agent_id is exactly that string.
        hostile = "a1' OR '1'='1"
        target = store.enqueue(_job(idempotency_key="h", agent_id=hostile))
        store.enqueue(_job(idempotency_key="o", agent_id="a1"))
        store.enqueue(_job(idempotency_key="p", agent_id="a2"))
        assert [j.id for j in store.list_jobs(agent_id=hostile)] == [target.id]
        assert store.list_jobs(agent_id="x' OR '1'='1") == []

    def test_list_jobs_newest_first(self, store):
        older = store.enqueue(
            _job(idempotency_key="old", run_at=T0 - timedelta(seconds=60))
        )
        newer = store.enqueue(_job(idempotency_key="new", run_at=T0))
        ordered = store.list_jobs()
        assert [j.id for j in ordered] == [newer.id, older.id]
        # limit keeps the newest.
        assert [j.id for j in store.list_jobs(limit=1)] == [newer.id]

    # -- retention: list / redact / purge -------------------------------------

    def _finished(self, store, key, finished_at, **kw) -> ExecutionJob:
        """Enqueue a job and drive it to SUCCEEDED at ``finished_at``."""
        job = store.enqueue(
            _job(
                idempotency_key=key,
                run_at=finished_at - timedelta(seconds=5),
                payload={
                    "agent_name": "Agent One",
                    "event": {
                        "event_type": "node.update",
                        "origin": {"actor": {"actor_id": "user-7"}},
                        "entity": {
                            "kind": "node",
                            "id": "n1",
                            "type": "Actor",
                            "data": {"after": {"name": "secret content"}},
                        },
                    },
                    "other": "dropped",
                },
                correlation_id="corr-1",
                session_id="sess-1",
                origin="ui",
                last_error="boom with content",
                **kw,
            )
        )
        store.claim_next("w", now=finished_at - timedelta(seconds=1))
        return store.complete(
            job.id,
            result={"handled": True, "summary": "content summary", "turns": 2},
            now=finished_at,
        )

    def test_list_terminal_older_than_skips_non_terminal_and_younger(self, store):
        old = self._finished(store, "old", T0 - timedelta(days=10))
        self._finished(store, "young", T0 - timedelta(days=1))
        store.enqueue(_job(idempotency_key="pending", run_at=T0 - timedelta(days=30)))
        store.enqueue(_job(idempotency_key="running", run_at=T0 - timedelta(days=30)))
        # Claims one of the two old non-terminal jobs; both stay non-terminal.
        store.claim_next("w", now=T0 - timedelta(days=30))
        found = store.list_terminal_older_than(T0 - timedelta(days=5))
        assert [j.id for j in found] == [old.id]

    def test_redact_strips_content_and_keeps_accounting_fields(self, store):
        job = self._finished(store, "r", T0)
        assert store.redact([job.id], now=T0 + timedelta(days=1)) == 1
        red = store.get(job.id)
        assert red.payload == {
            "agent_name": "Agent One",
            "event": {
                "event_type": "node.update",
                "entity": {"kind": "node", "id": "n1", "type": "Actor"},
            },
        }
        assert red.result == {"handled": True, "summary": None, "turns": 2}
        assert red.last_error is None
        assert red.session_id is None
        assert red.origin is None
        assert red.redacted_at == T0 + timedelta(days=1)
        # Accounting fields survive.
        assert red.state == ExecutionState.SUCCEEDED
        assert red.agent_id == job.agent_id
        assert red.correlation_id == "corr-1"
        assert red.attempts == job.attempts
        assert red.created_at == job.created_at
        assert red.started_at == job.started_at
        assert red.finished_at == job.finished_at
        assert red.updated_at == job.updated_at

    def test_redact_is_idempotent(self, store):
        job = self._finished(store, "r", T0)
        store.redact([job.id], now=T0 + timedelta(days=1))
        assert store.redact([job.id], now=T0 + timedelta(days=2)) == 0
        assert store.get(job.id).redacted_at == T0 + timedelta(days=1)
        assert (
            store.list_terminal_older_than(T0 + timedelta(days=3), redacted=False) == []
        )
        assert [
            j.id
            for j in store.list_terminal_older_than(
                T0 + timedelta(days=3), redacted=True
            )
        ] == [job.id]

    def test_redact_and_purge_never_touch_non_terminal_jobs(self, store):
        pending = store.enqueue(_job(idempotency_key="p", payload={"x": "keep"}))
        running = store.enqueue(_job(idempotency_key="r", payload={"x": "keep"}))
        store.claim_next("w", now=T0)
        ids = [pending.id, running.id]
        assert store.redact(ids, now=T0) == 0
        assert store.purge(ids) == 0
        for job_id in ids:
            kept = store.get(job_id)
            assert kept is not None
            assert kept.payload == {"x": "keep"}
            assert kept.redacted_at is None

    def test_redact_and_purge_ignore_unknown_ids(self, store):
        assert store.redact(["missing"], now=T0) == 0
        assert store.purge(["missing"]) == 0

    def test_purge_deletes_and_forgets_idempotency_key(self, store):
        job = self._finished(store, "once", T0)
        keep = self._finished(store, "keep", T0)
        assert store.purge([job.id]) == 1
        assert store.get(job.id) is None
        assert store.get(keep.id) is not None
        assert store.purge([job.id]) == 0
        again = store.enqueue(_job(idempotency_key="once"))
        assert again.id != job.id
        assert again.state == ExecutionState.PENDING

    def test_list_jobs_created_after_is_exclusive(self, store):
        old = store.enqueue(_job(idempotency_key="old", run_at=T0 - timedelta(days=2)))
        edge = store.enqueue(_job(idempotency_key="edge", run_at=T0))
        new = store.enqueue(_job(idempotency_key="new", run_at=T0 + timedelta(days=1)))
        ids = [j.id for j in store.list_jobs(created_after=T0)]
        assert ids == [new.id]
        assert edge.id not in ids and old.id not in ids

    def test_list_terminal_older_than_is_strict_and_uses_finished_at(self, store):
        cutoff = T0 - timedelta(days=30)
        at_cutoff = self._finished(store, "edge", cutoff)
        older = self._finished(store, "older", cutoff - timedelta(seconds=1))
        oldest = self._finished(store, "oldest", cutoff - timedelta(days=1))
        # Redaction stamps redacted_at but leaves updated_at/finished_at alone,
        # so age keeps being measured from finished_at.
        store.redact([older.id], now=T0)
        ids = [j.id for j in store.list_terminal_older_than(cutoff)]
        assert ids == [oldest.id, older.id]
        assert at_cutoff.id not in ids

    # -- retention: abandoning orphaned RUNNING jobs ----------------------------

    def _orphan(self, store, key, last_seen, **kw) -> ExecutionJob:
        """A job recorded RUNNING at ``last_seen`` that nothing will finalize."""
        return store.enqueue(
            _job(
                idempotency_key=key,
                run_at=last_seen,
                state=ExecutionState.RUNNING,
                attempts=1,
                lease_owner="agent-1",
                updated_at=last_seen,
                started_at=last_seen,
                payload={"event": {"entity": {"data": "secret content"}}},
                session_id="sess-1",
                **kw,
            )
        )

    def test_list_running_older_than_is_strict_and_skips_other_states(self, store):
        cutoff = T0 - timedelta(days=30)
        oldest = self._orphan(store, "oldest", cutoff - timedelta(days=2))
        older = self._orphan(store, "older", cutoff - timedelta(seconds=1))
        self._orphan(store, "edge", cutoff)
        self._orphan(store, "young", cutoff + timedelta(days=1))
        store.enqueue(
            _job(idempotency_key="pending", run_at=cutoff - timedelta(days=9))
        )
        self._finished(store, "done", cutoff - timedelta(days=9))
        ids = [j.id for j in store.list_running_older_than(cutoff)]
        assert ids == [oldest.id, older.id]

    def test_list_running_older_than_measures_a_claimed_job_from_its_last_update(
        self, store
    ):
        job = store.enqueue(_job(idempotency_key="c", run_at=T0 - timedelta(days=60)))
        store.claim_next("w", now=T0 - timedelta(days=60))
        assert store.renew_lease(job.id, "w", now=T0 - timedelta(days=1))
        assert store.list_running_older_than(T0 - timedelta(days=30)) == []
        assert [j.id for j in store.list_running_older_than(T0)] == [job.id]

    def test_abandon_dead_letters_with_fixed_reason_and_last_update_as_finish(
        self, store
    ):
        last_seen = T0 - timedelta(days=40)
        job = self._orphan(store, "o", last_seen)
        assert store.abandon([job.id], cutoff=T0 - timedelta(days=30), now=T0) == 1
        out = store.get(job.id)
        assert out.state == ExecutionState.DEAD_LETTER
        assert out.dead_letter_reason == ABANDONED_REASON
        assert out.finished_at == last_seen
        assert out.updated_at == T0
        assert out.lease_owner is None and out.lease_expiry is None
        # Abandoning only finalizes; content goes with redaction/purge.
        assert out.payload == job.payload
        assert out.session_id == "sess-1"
        assert out.created_at == job.created_at
        assert out.started_at == job.started_at
        assert out.attempts == job.attempts
        # It now ages out like any terminal job, from its last update.
        assert [
            j.id for j in store.list_terminal_older_than(T0 - timedelta(days=30))
        ] == [job.id]

    def test_abandon_never_touches_young_pending_or_terminal_jobs(self, store):
        cutoff = T0 - timedelta(days=30)
        edge = self._orphan(store, "edge", cutoff)
        young = self._orphan(store, "young", cutoff + timedelta(days=1))
        pending = store.enqueue(
            _job(idempotency_key="pending", run_at=cutoff - timedelta(days=9))
        )
        done = self._finished(store, "done", cutoff - timedelta(days=9))
        ids = [edge.id, young.id, pending.id, done.id, "missing"]
        assert store.abandon(ids, cutoff=cutoff, now=T0) == 0
        for job in (edge, young):
            assert store.get(job.id).state == ExecutionState.RUNNING
        assert store.get(pending.id).state == ExecutionState.PENDING
        kept = store.get(done.id)
        assert kept.state == ExecutionState.SUCCEEDED
        assert kept.dead_letter_reason is None
        assert kept.finished_at == done.finished_at

    def test_abandon_rechecks_age_at_write_time(self, store):
        job = store.enqueue(_job(idempotency_key="c", run_at=T0 - timedelta(days=60)))
        store.claim_next("w", now=T0 - timedelta(days=60))
        cutoff = T0 - timedelta(days=30)
        listed = [j.id for j in store.list_running_older_than(cutoff)]
        assert listed == [job.id]
        # The run shows life between the listing and the write.
        assert store.renew_lease(job.id, "w", now=T0 - timedelta(days=1))
        assert store.abandon(listed, cutoff=cutoff, now=T0) == 0
        assert store.get(job.id).state == ExecutionState.RUNNING

    def test_abandon_is_idempotent_and_sticky_over_late_finalization(self, store):
        job = self._orphan(store, "o", T0 - timedelta(days=40))
        cutoff = T0 - timedelta(days=30)
        assert store.abandon([job.id, job.id], cutoff=cutoff, now=T0) == 1
        once = store.get(job.id).to_dict()
        assert store.abandon([job.id], cutoff=T0 + timedelta(days=1), now=T0) == 0
        store.complete(job.id, result={"late": True}, now=T0)
        store.fail(job.id, error="late", now=T0)
        assert store.get(job.id).to_dict() == once
