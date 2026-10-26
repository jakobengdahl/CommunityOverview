"""The merge-record retention runner: bounded batches, and a failure that
never escapes (docs/SESSION_OVERLAY_CONTRACT.md §11.5)."""

import threading
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


def test_the_runner_purges_before_its_first_wait():
    purged = threading.Event()

    class Store:
        def purge_merge_records(self, retention, *, limit, now=None):
            purged.set()
            return 0

    runner = MergeRecordRetention(Store(), RETENTION, interval_seconds=3600)
    runner.start()
    try:
        # The interval is an hour, so a purge seen now ran before any wait.
        assert purged.wait(5)
    finally:
        runner.stop()


def test_stop_ends_the_thread_without_waiting_out_the_interval():
    runner = MergeRecordRetention(_Store(backlog=0), RETENTION, interval_seconds=3600)
    runner.start()
    thread = runner._thread
    runner.stop(timeout=5)
    assert not thread.is_alive()
    assert thread.daemon


def test_starting_twice_runs_one_thread():
    runner = MergeRecordRetention(_Store(backlog=0), RETENTION, interval_seconds=3600)
    # Apps other tests built may have runners of their own still alive.
    before = set(threading.enumerate())
    runner.start()
    try:
        first = runner._thread
        runner.start()
        assert runner._thread is first
        started = [t for t in threading.enumerate() if t not in before]
        assert started == [first]
    finally:
        runner.stop()


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


def test_a_record_whose_session_has_no_record_is_never_a_candidate():
    merges = {"orphan": {"session_id": A, "status": ls.MERGE_APPLIED}}
    assert ls.merge_records_to_purge(merges, {}, ls.utc_now(), 10) == []


def test_an_active_session_with_a_stale_retirement_time_is_never_purged():
    active = {
        A: {"status": ls.STATUS_ACTIVE, "retired_at": "2020-01-01T00:00:00+00:00"}
    }
    merges = {"m": {"session_id": A, "status": ls.MERGE_APPLIED}}
    assert ls.merge_records_to_purge(merges, active, ls.utc_now(), 10) == []


def test_a_retired_session_without_a_retirement_time_is_never_purged():
    unstamped = {A: {"status": ls.STATUS_RETIRED}}
    merges = {"m": {"session_id": A, "status": ls.MERGE_APPLIED}}
    assert ls.merge_records_to_purge(merges, unstamped, ls.utc_now(), 10) == []
