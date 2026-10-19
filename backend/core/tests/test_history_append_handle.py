"""
append_record keeps one append handle open instead of mkdir + open per record.

Guarantees:
G1 every record is on disk, in order, once, as soon as append_record returns
G2 a new store on the same path (restart) appends after what is there
G3 compaction (own or another process's rename) never strands later appends
   on an unlinked file
G4 a deleted file or changed history_path is recreated/followed, not lost
G5 concurrent appenders neither lose nor duplicate nor tear records
G6 the file is opened once for N appends (the cost this change removes)
G7 a torn tail (partial write, crash) is never glued onto by the next record:
   the next record and get_recent stay readable
G8 close() waits for an in-flight append; a failing handle.close() and a failing
   compaction never fail the caller
"""

import builtins
import json
import os
import threading
from pathlib import Path

import pytest

from backend.core import history_store
from backend.core.history_store import GraphHistoryStore


def _rec(i: int) -> dict:
    return {
        "event_id": f"evt-{i}",
        "entity_id": f"n{i}",
        "entity_kind": "node",
        "occurred_at": "2026-01-01T00:00:00Z",
    }


def _ids(path: Path) -> list:
    return [json.loads(x)["event_id"] for x in path.read_text().splitlines() if x]


@pytest.fixture
def path(tmp_path):
    return tmp_path / "nested" / "graph.history.ndjson"


def test_each_record_is_readable_immediately_and_in_order(path):
    store = GraphHistoryStore(path)
    for i in range(20):
        store.append_record(_rec(i))
        assert _ids(path) == [f"evt-{j}" for j in range(i + 1)]
    store.close()


def test_file_is_opened_once_for_many_appends(path, monkeypatch):
    opened = []
    real_open = builtins.open

    def counting_open(file, *a, **kw):
        if Path(file) == path:
            opened.append(file)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting_open)
    store = GraphHistoryStore(path)
    for i in range(50):
        store.append_record(_rec(i))
    store.close()
    assert len(opened) == 1
    assert len(_ids(path)) == 50


def test_new_store_after_restart_appends_after_existing_records(path):
    first = GraphHistoryStore(path)
    first.append_record(_rec(0))
    first.append_record(_rec(1))
    first.close()
    second = GraphHistoryStore(path)
    second.append_record(_rec(2))
    second.close()
    assert _ids(path) == ["evt-0", "evt-1", "evt-2"]


def test_appends_after_own_compaction_land_in_the_live_file(path):
    store = GraphHistoryStore(path, max_events=3, compaction_interval=1000)
    for i in range(10):
        store.append_record(_rec(i))
    store.compact()
    # Released before the rename (a held handle blocks it on Windows).
    assert store._append_handle is None
    assert _ids(path) == ["evt-7", "evt-8", "evt-9"]
    store.append_record(_rec(10))
    assert _ids(path) == ["evt-7", "evt-8", "evt-9", "evt-10"]
    store.close()


def test_appends_after_interval_compaction_are_not_lost(path):
    store = GraphHistoryStore(path, max_events=5, compaction_interval=2)
    for i in range(30):
        store.append_record(_rec(i))
    ids = _ids(path)
    assert ids[-1] == "evt-29"
    assert ids == sorted(ids, key=lambda s: int(s.split("-")[1]))
    assert len(ids) == len(set(ids))
    store.close()


def test_file_replaced_by_another_writer_is_followed(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    # What another process's compaction does: write a new file, rename over.
    replacement = path.with_name("replacement.tmp")
    replacement.write_text(json.dumps(_rec(0)) + "\n")
    os.rename(replacement, path)
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-0", "evt-1"]
    store.close()


def test_deleted_file_and_directory_are_recreated(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    path.unlink()
    path.parent.rmdir()
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-1"]
    store.close()


def test_changed_history_path_is_followed(tmp_path):
    a, b = tmp_path / "a" / "h.ndjson", tmp_path / "b" / "h.ndjson"
    store = GraphHistoryStore(a)
    store.append_record(_rec(0))
    store.history_path = b
    store.append_record(_rec(1))
    assert _ids(a) == ["evt-0"]
    assert _ids(b) == ["evt-1"]
    store.close()


def test_close_is_idempotent_and_append_reopens(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    store.close()
    store.close()
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-0", "evt-1"]
    store.close()


def test_failed_write_drops_the_handle_and_next_append_succeeds(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    store._append_handle = _FlakyHandle(store._append_handle)
    with pytest.raises(OSError):
        store.append_record(_rec(1))
    assert store._append_handle is None
    store.append_record(_rec(2))
    assert _ids(path) == ["evt-0", "evt-2"]
    store.close()


class _FlakyHandle:
    def __init__(self, inner):
        self._inner = inner

    def write(self, data):
        raise OSError("disk full")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_concurrent_appenders_lose_and_duplicate_nothing(path):
    store = GraphHistoryStore(path, max_events=10_000, compaction_interval=7)
    threads, per = 8, 100

    def work(t):
        for i in range(per):
            store.append_record(_rec(t * per + i))

    ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ids = _ids(path)
    assert sorted(ids) == sorted(f"evt-{i}" for i in range(threads * per))
    assert len(ids) == len(set(ids))
    # per-thread order preserved
    for t in range(threads):
        mine = [
            int(x.split("-")[1])
            for x in ids
            if t * per <= int(x.split("-")[1]) < (t + 1) * per
        ]
        assert mine == sorted(mine)
    store.close()


def test_handle_is_released_after_each_append_where_rename_over_open_file_fails(
    path, monkeypatch
):
    monkeypatch.setattr(history_store, "_RELEASE_HANDLE_AFTER_APPEND", True)
    store = GraphHistoryStore(path)
    for i in range(3):
        store.append_record(_rec(i))
        assert store._append_handle is None
    assert _ids(path) == ["evt-0", "evt-1", "evt-2"]


def test_close_actually_closes_the_file_and_append_reopens(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    handle = store._append_handle
    store.close()
    assert handle.closed
    assert store._append_handle is None
    store.append_record(_rec(1))
    assert store._append_handle is not handle


def test_handle_is_closed_by_compaction_and_by_path_change(tmp_path):
    a = tmp_path / "a.ndjson"
    store = GraphHistoryStore(a, max_events=2, compaction_interval=1000)
    store.append_record(_rec(0))
    first = store._append_handle
    store.compact()
    assert first.closed
    store.append_record(_rec(1))
    second = store._append_handle
    store.history_path = tmp_path / "b.ndjson"
    store.append_record(_rec(2))
    assert second.closed
    store.close()


@pytest.mark.parametrize("exc", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_write_closes_the_dropped_handle(path, exc):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    inner = store._append_handle

    class Failing(_FlakyHandle):
        def write(self, data):
            raise exc

    store._append_handle = Failing(inner)
    with pytest.raises(type(exc)):
        store.append_record(_rec(1))
    assert store._append_handle is None
    assert inner.closed


def test_externally_closed_handle_is_reopened(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    store._append_handle.close()
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-0", "evt-1"]
    store.close()


def test_write_happens_between_lock_and_unlock_even_on_failure(path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        history_store, "_lock_file", lambda f, exclusive=True: calls.append("lock")
    )
    monkeypatch.setattr(history_store, "_unlock_file", lambda f: calls.append("unlock"))
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    assert calls == ["lock", "unlock"]
    assert _ids(path) == ["evt-0"]

    calls.clear()

    class Failing(_FlakyHandle):
        def write(self, data):
            calls.append("write")
            raise OSError("x")

    store._append_handle = Failing(store._append_handle)
    with pytest.raises(OSError):
        store.append_record(_rec(1))
    assert calls == ["lock", "write", "unlock"]


def test_several_missing_parent_directories_are_created(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "graph.history.ndjson"
    store = GraphHistoryStore(deep)
    store.append_record(_rec(0))
    store.close()
    assert _ids(deep) == ["evt-0"]


def test_append_takes_an_exclusive_lock(path, monkeypatch):
    modes = []
    monkeypatch.setattr(
        history_store, "_lock_file", lambda f, exclusive=True: modes.append(exclusive)
    )
    monkeypatch.setattr(history_store, "_unlock_file", lambda f: None)
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    store.close()
    assert modes == [True]


def test_non_ascii_is_stored_as_raw_utf8(path):
    store = GraphHistoryStore(path)
    store.append_record({**_rec(0), "name": "é"})
    store.close()
    assert "é".encode("utf-8") in path.read_bytes()


def test_partial_write_does_not_glue_onto_the_next_record(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))

    class Torn(_FlakyHandle):
        def write(self, data):
            # What ENOSPC/EFBIG leaves: part of the line reaches the file.
            self._inner.write(data[: len(data) // 2])
            self._inner.flush()
            raise OSError("disk full")

    store._append_handle = Torn(store._append_handle)
    with pytest.raises(OSError):
        store.append_record(_rec(1))
    assert not path.read_bytes().endswith(b"\n")

    store.append_record(_rec(2))
    assert [r["event_id"] for r in store.get_recent()] == ["evt-2", "evt-0"]
    store.close()


def test_torn_tail_from_a_previous_process_is_not_glued_onto(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps(_rec(0)).encode() + b'\n{"event_id": "evt-')
    store = GraphHistoryStore(path)
    store.append_record(_rec(1))
    store.append_record(_rec(2))
    assert [r["event_id"] for r in store.get_recent()] == ["evt-2", "evt-1", "evt-0"]
    store.close()


def test_clean_file_gets_no_spurious_blank_line(path):
    store = GraphHistoryStore(path)
    for i in range(3):
        store.append_record(_rec(i))
    store.close()
    GraphHistoryStore(path).append_record(_rec(3))
    assert path.read_bytes() == b"".join(
        json.dumps(_rec(i)).encode() + b"\n" for i in range(4)
    )


def test_tail_is_checked_once_per_handle_not_per_append(path, monkeypatch):
    calls = []
    real = GraphHistoryStore._tail_is_torn
    monkeypatch.setattr(
        GraphHistoryStore,
        "_tail_is_torn",
        lambda self: calls.append(1) or real(self),
    )
    store = GraphHistoryStore(path)
    for i in range(5):
        store.append_record(_rec(i))
    assert len(calls) == 1
    store.close()


def test_tail_check_runs_under_the_exclusive_lock(path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        history_store, "_lock_file", lambda f, exclusive=True: calls.append("lock")
    )
    monkeypatch.setattr(history_store, "_unlock_file", lambda f: calls.append("unlock"))
    monkeypatch.setattr(
        GraphHistoryStore, "_tail_is_torn", lambda self: calls.append("tail") or False
    )
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    assert calls == ["lock", "tail", "unlock"]
    store.close()


def test_fragment_left_by_another_process_while_we_hold_a_handle(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    # Another process fails partway through its append: same inode, no newline.
    with open(path, "ab") as other:
        other.write(b'{"event_id": "evt-x')
    store.append_record(_rec(1))
    store.append_record(_rec(2))
    assert [r["event_id"] for r in store.get_recent()] == ["evt-2", "evt-1", "evt-0"]
    store.close()


def test_unreadable_tail_counts_as_torn(path, monkeypatch):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    store.close()
    real_open = os.open

    def deny(p, *a, **kw):
        if Path(p) == path:
            raise PermissionError("no read")
        return real_open(p, *a, **kw)

    monkeypatch.setattr(os, "open", deny)
    store.append_record(_rec(1))
    monkeypatch.undo()
    assert [r["event_id"] for r in store.get_recent()] == ["evt-1", "evt-0"]
    store.close()


def test_close_waits_for_an_in_flight_append(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    inner = store._append_handle
    in_write, release = threading.Event(), threading.Event()
    closed_while_writing = []

    class Slow(_FlakyHandle):
        def write(self, data):
            in_write.set()
            assert release.wait(5)
            closed_while_writing.append(self._inner.closed)
            return self._inner.write(data)

    store._append_handle = Slow(inner)
    appender = threading.Thread(target=store.append_record, args=(_rec(1),))
    appender.start()
    assert in_write.wait(5)
    closer = threading.Thread(target=store.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive(), "close() must wait for the append's lock"
    release.set()
    appender.join(5)
    closer.join(5)
    assert closed_while_writing == [False]
    assert inner.closed
    assert _ids(path) == ["evt-0", "evt-1"]


def test_close_swallows_an_oserror_from_handle_close(path):
    store = GraphHistoryStore(path)
    store.append_record(_rec(0))
    inner = store._append_handle

    class BadClose(_FlakyHandle):
        def close(self):
            self._inner.close()
            raise OSError("flush on close failed")

    store._append_handle = BadClose(inner)
    store.close()
    assert store._append_handle is None
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-0", "evt-1"]
    store.close()


def test_compaction_failure_does_not_fail_the_append(path, monkeypatch):
    store = GraphHistoryStore(path, max_events=5, compaction_interval=1)

    def boom():
        raise OSError("rename failed")

    monkeypatch.setattr(store, "_compact_locked", boom)
    store.append_record(_rec(0))
    store.append_record(_rec(1))
    assert _ids(path) == ["evt-0", "evt-1"]
    store.close()
