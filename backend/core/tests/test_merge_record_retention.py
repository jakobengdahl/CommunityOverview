"""The merge-record retention runner: bounded batches, and a failure that
never escapes (docs/SESSION_OVERLAY_CONTRACT.md §11.5)."""

from datetime import timedelta

from backend.core import layer_store as ls
from backend.core.merge_record_retention import (
    MergeRecordRetention,
    purge_expired_merge_records,
)

A = "1111-2222-3333-4444"
RETENTION = timedelta(days=90)


class _Store:
    def __init__(self, backlog):
        self.backlog = backlog
        self.calls = []

    def purge_merge_records(self, retention, *, limit, now=None):
        self.calls.append((retention, limit))
        taken = min(limit, self.backlog)
        self.backlog -= taken
        return taken


def test_a_run_purges_batch_after_batch_until_one_comes_back_short():
    store = _Store(backlog=25)
    assert purge_expired_merge_records(store, RETENTION, batch_size=10) == 25
    assert store.calls == [(RETENTION, 10)] * 3


def test_a_run_stops_at_its_bound_and_leaves_the_rest_to_the_next():
    store = _Store(backlog=100)
    assert (
        purge_expired_merge_records(store, RETENTION, batch_size=10, max_batches=3)
        == 30
    )
    assert store.backlog == 70


def test_a_failing_purge_is_logged_and_never_raised():
    class Broken:
        def purge_merge_records(self, retention, *, limit, now=None):
            raise OSError("store gone")

    assert MergeRecordRetention(Broken(), RETENTION).run_once() is None


def test_the_runner_purges_once_at_start_and_stops():
    store = _Store(backlog=3)
    runner = MergeRecordRetention(store, RETENTION, interval_seconds=3600)
    runner.start()
    runner.stop()
    assert store.backlog == 0


def test_a_record_not_marked_applied_is_never_a_candidate():
    retired = {
        A: {"status": ls.STATUS_RETIRED, "retired_at": "2020-01-01T00:00:00+00:00"}
    }
    merges = {
        "pending": {"session_id": A, "status": ls.MERGE_PENDING},
        "applied": {"session_id": A, "status": ls.MERGE_APPLIED},
    }
    assert ls.merge_records_to_purge(merges, retired, ls.utc_now(), 10) == ["applied"]


def test_a_limited_purge_takes_the_lowest_merge_ids_first():
    retired = {
        A: {"status": ls.STATUS_RETIRED, "retired_at": "2020-01-01T00:00:00+00:00"}
    }
    merges = {
        merge_id: {"session_id": A, "status": ls.MERGE_APPLIED}
        for merge_id in ("m-c", "m-a", "m-b")
    }
    assert ls.merge_records_to_purge(merges, retired, ls.utc_now(), 2) == ["m-a", "m-b"]
