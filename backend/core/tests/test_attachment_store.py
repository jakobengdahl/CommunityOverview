"""The file-backed node attachment store (ADR 0008 sections 1-3, 11)."""

import asyncio
import hashlib
import json
import os
import time

import pytest

from backend.core import attachment_store
from backend.core.attachment_store import (
    AttachmentIndexCorrupt,
    AttachmentLimitExceeded,
    AttachmentLimits,
    AttachmentNodeArchived,
    AttachmentNodeGone,
    AttachmentNotFound,
    AttachmentTooLarge,
    FileAttachmentStore,
    NewAttachment,
    hashed_key,
)

NS = "graph"


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def _new(filename: str = "a.txt") -> NewAttachment:
    return NewAttachment(
        filename=filename,
        declared_content_type="text/plain",
        content_type="text/plain",
        uploaded_by=None,
    )


def _upload(store, node_id, data=b"hello", limits=None, precondition=None):
    async def run():
        staged = await store.stage(NS, node_id, _chunks(data), 10**9)
        return await store.commit(
            staged,
            _new(),
            limits or AttachmentLimits(),
            precondition or (lambda: None),
        )

    return asyncio.run(run())


@pytest.fixture
def store(tmp_path):
    return FileAttachmentStore(tmp_path / "attachments")


def _tmp_entries(store):
    tmp = store.root / ".tmp"
    return list(tmp.iterdir()) if tmp.exists() else []


def test_upload_is_listed_and_read_back_byte_for_byte(store):
    record = _upload(store, "n1", b"payload")

    assert store.list(NS, "n1") == [record]
    assert record.size == 7
    assert record.sha256 == hashlib.sha256(b"payload").hexdigest()
    with store.open_read(NS, "n1", record.id) as handle:
        assert handle.read() == b"payload"
    assert _tmp_entries(store) == []


def test_layout_uses_hashed_segments_and_never_the_filename(store):
    node_id = "federated::g1::../../etc"
    record = _upload(store, node_id)

    node_dir = store.root / hashed_key(NS) / hashed_key(node_id)
    assert (node_dir / "blobs" / record.id).is_file()
    index = json.loads((node_dir / "index.json").read_text())
    assert index["node_id"] == node_id
    assert index["schema_version"] == 1
    all_paths = [str(p) for p in store.root.rglob("*")]
    assert not any("a.txt" in p or ".." in p for p in all_paths)


def test_namespaces_keep_the_same_node_id_apart(store):
    _upload(store, "n1")
    assert store.list("other", "n1") == []


def test_unknown_or_malformed_attachment_id_is_not_found(store):
    record = _upload(store, "n1")
    for bad in ["0" * 32, "../index.json", "index.json", record.id.upper(), ""]:
        assert store.get_record(NS, "n1", bad) is None
        with pytest.raises(AttachmentNotFound):
            store.open_read(NS, "n1", bad)
        assert store.delete(NS, "n1", bad) is False


def test_blob_on_disk_without_index_record_is_not_found(store):
    _upload(store, "n1")
    stray = "f" * 32
    blobs = store.root / hashed_key(NS) / hashed_key("n1") / "blobs"
    (blobs / stray).write_bytes(b"x")
    assert store.get_record(NS, "n1", stray) is None
    with pytest.raises(AttachmentNotFound):
        store.open_read(NS, "n1", stray)


def test_stage_over_cap_raises_and_leaves_nothing_behind(store):
    async def run():
        await store.stage(NS, "n1", _chunks(b"12345", b"6"), 5)

    with pytest.raises(AttachmentTooLarge):
        asyncio.run(run())
    assert _tmp_entries(store) == []
    assert store.list(NS, "n1") == []


def test_stage_at_exactly_the_cap_succeeds(store):
    async def run():
        return await store.stage(NS, "n1", _chunks(b"123", b"45"), 5)

    staged = asyncio.run(run())
    assert staged.size == 5
    store.discard(staged)
    assert _tmp_entries(store) == []


def test_commit_rechecks_count_under_the_lock(store):
    limits = AttachmentLimits(max_files_per_node=2)
    _upload(store, "n1", limits=limits)
    _upload(store, "n1", limits=limits)
    with pytest.raises(AttachmentLimitExceeded):
        _upload(store, "n1", limits=limits)
    assert len(store.list(NS, "n1")) == 2
    assert _tmp_entries(store) == []


def test_commit_rechecks_total_bytes_under_the_lock(store):
    limits = AttachmentLimits(max_bytes_per_node=10)
    _upload(store, "n1", b"123456", limits=limits)
    with pytest.raises(AttachmentLimitExceeded):
        _upload(store, "n1", b"12345", limits=limits)
    _upload(store, "n1", b"1234", limits=limits)
    assert sum(r.size for r in store.list(NS, "n1")) == 10


def test_concurrent_commits_cannot_exceed_the_count_together(store):
    limits = AttachmentLimits(max_files_per_node=1)

    async def run():
        first = await store.stage(NS, "n1", _chunks(b"a"), 100)
        second = await store.stage(NS, "n1", _chunks(b"b"), 100)
        return await asyncio.gather(
            store.commit(first, _new(), limits, lambda: None),
            store.commit(second, _new(), limits, lambda: None),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    assert sum(isinstance(r, AttachmentLimitExceeded) for r in results) == 1
    assert len(store.list(NS, "n1")) == 1
    assert _tmp_entries(store) == []


@pytest.mark.parametrize("error", [AttachmentNodeGone, AttachmentNodeArchived])
def test_failed_precondition_discards_the_staged_file(store, error):
    def precondition():
        raise error("n1")

    with pytest.raises(error):
        _upload(store, "n1", precondition=precondition)
    assert store.list(NS, "n1") == []
    assert _tmp_entries(store) == []


def test_delete_removes_record_then_blob(store):
    keep = _upload(store, "n1", b"keep")
    drop = _upload(store, "n1", b"drop")
    assert store.delete(NS, "n1", drop.id) is True
    assert store.list(NS, "n1") == [keep]
    blobs = store.root / hashed_key(NS) / hashed_key("n1") / "blobs"
    assert not (blobs / drop.id).exists()
    assert store.delete(NS, "n1", drop.id) is False


def test_delete_node_removes_every_file_of_that_node_only(store):
    _upload(store, "n1")
    _upload(store, "n1")
    other = _upload(store, "n2")
    assert store.delete_node(NS, "n1") == 2
    assert store.list(NS, "n1") == []
    assert store.list(NS, "n2") == [other]
    assert store.delete_node(NS, "n1") == 0


def test_quarantine_moves_only_the_named_records_and_keeps_their_bytes(store):
    old = _upload(store, "n1", b"old")
    new = _upload(store, "n1", b"new")
    assert store.quarantine(NS, "n1", {old.id}) == 1
    assert store.list(NS, "n1") == [new]
    assert store.get_record(NS, "n1", old.id) is None
    (target,) = (store.root / hashed_key(NS) / ".quarantine").iterdir()
    index = json.loads((target / "index.json").read_text(encoding="utf-8"))
    assert index["node_id"] == "n1"
    assert [r["id"] for r in index["attachments"]] == [old.id]
    assert (target / "blobs" / old.id).read_bytes() == b"old"
    assert not (
        store.root / hashed_key(NS) / hashed_key("n1") / "blobs" / old.id
    ).exists()


def test_quarantined_files_are_not_reported_as_a_node(store):
    old = _upload(store, "n1")
    store.quarantine(NS, "n1", {old.id})
    assert store.quarantine(NS, "n1", {old.id}) == 0
    assert list(store.iter_nodes(NS)) == ["n1"]
    assert len(list((store.root / hashed_key(NS) / ".quarantine").iterdir())) == 1


def test_two_quarantines_of_one_node_keep_both_sets(store):
    first = _upload(store, "n1", b"first")
    store.quarantine(NS, "n1", {first.id})
    second = _upload(store, "n1", b"second")
    store.quarantine(NS, "n1", {second.id})
    targets = list((store.root / hashed_key(NS) / ".quarantine").iterdir())
    kept = {
        blob.name: blob.read_bytes()
        for target in targets
        for blob in (target / "blobs").iterdir()
    }
    assert len(targets) == 2
    assert kept == {first.id: b"first", second.id: b"second"}


def test_quarantine_moves_blobs_where_hard_links_are_unsupported(store, monkeypatch):
    old = _upload(store, "n1", b"old")

    def no_links(*args, **kwargs):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    assert store.quarantine(NS, "n1", {old.id}) == 1
    assert store.list(NS, "n1") == []
    (target,) = (store.root / hashed_key(NS) / ".quarantine").iterdir()
    assert (target / "blobs" / old.id).read_bytes() == b"old"


def test_quarantine_failing_midway_leaves_every_blob_named_by_an_index(
    store, monkeypatch
):
    old = _upload(store, "n1", b"old")
    real_write = store._write_index

    def fail_live_index_rewrite(*args, **kwargs):
        # The quarantine copy passes node_dir; the live index rewrite does not.
        if kwargs.get("node_dir") is None:
            raise OSError("disk full")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store, "_write_index", fail_live_index_rewrite)
    with pytest.raises(OSError):
        store.quarantine(NS, "n1", {old.id})
    monkeypatch.undo()
    # The live index still names the blob, and it is still where it says.
    assert store.list(NS, "n1") == [old]
    with store.open_read(NS, "n1", old.id) as handle:
        assert handle.read() == b"old"
    # The quarantine copy is complete too, so nothing is lost either way.
    (target,) = (store.root / hashed_key(NS) / ".quarantine").iterdir()
    assert (target / "blobs" / old.id).read_bytes() == b"old"


def test_quarantine_fallback_move_leaves_no_live_blob(store, monkeypatch):
    old = _upload(store, "n1", b"old")
    live_blob = store.root / hashed_key(NS) / hashed_key("n1") / "blobs" / old.id

    def no_links(*args, **kwargs):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    store.quarantine(NS, "n1", {old.id})
    assert not live_blob.exists()


def test_quarantine_survives_a_blob_vanishing_between_failed_link_and_move(
    store, monkeypatch
):
    old = _upload(store, "n1", b"old")
    live_blob = store.root / hashed_key(NS) / hashed_key("n1") / "blobs" / old.id

    def link_fails_and_blob_vanishes(*args, **kwargs):
        live_blob.unlink()
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "link", link_fails_and_blob_vanishes)
    assert store.quarantine(NS, "n1", {old.id}) == 1
    assert store.list(NS, "n1") == []
    (target,) = (store.root / hashed_key(NS) / ".quarantine").iterdir()
    assert [
        r["id"] for r in json.loads((target / "index.json").read_text())["attachments"]
    ] == [old.id]


def test_quarantine_moves_a_record_whose_blob_is_already_gone(store):
    old = _upload(store, "n1")
    (store.root / hashed_key(NS) / hashed_key("n1") / "blobs" / old.id).unlink()
    assert store.quarantine(NS, "n1", {old.id}) == 1
    assert store.list(NS, "n1") == []


def test_quarantines_in_the_same_instant_do_not_collide(store, monkeypatch):
    import backend.core.attachment_store as module

    fixed = module.datetime(2026, 1, 1, tzinfo=module.timezone.utc)

    class FrozenDatetime(module.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    first = _upload(store, "n1", b"first")
    second = _upload(store, "n1", b"second")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    store.quarantine(NS, "n1", {first.id})
    store.quarantine(NS, "n1", {second.id})
    assert len(list((store.root / hashed_key(NS) / ".quarantine").iterdir())) == 2


def test_iter_nodes_reports_node_ids_from_the_index(store):
    _upload(store, "n1")
    _upload(store, "federated::g::x")
    assert sorted(store.iter_nodes(NS)) == ["federated::g::x", "n1"]


def test_corrupt_index_raises_rather_than_reading_as_empty(store):
    _upload(store, "n1")
    index = store.root / hashed_key(NS) / hashed_key("n1") / "index.json"
    index.write_text("{not json")
    with pytest.raises(AttachmentIndexCorrupt):
        store.list(NS, "n1")


def test_index_for_another_node_id_is_corrupt(store):
    _upload(store, "n1")
    index = store.root / hashed_key(NS) / hashed_key("n1") / "index.json"
    data = json.loads(index.read_text())
    data["node_id"] = "n2"
    index.write_text(json.dumps(data))
    with pytest.raises(AttachmentIndexCorrupt):
        store.list(NS, "n1")


def test_sweep_removes_only_stale_staged_uploads(store):
    tmp = store.root / ".tmp"
    tmp.mkdir(parents=True)
    stale = tmp / "upload_old"
    fresh = tmp / "upload_new"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    assert store.sweep_stale_staged() == 1
    assert not stale.exists()
    assert fresh.exists()


def test_limits_refuse_zero_or_negative_values():
    for field in ("max_file_bytes", "max_files_per_node", "max_bytes_per_node"):
        for value in (0, -1, True):
            with pytest.raises(ValueError):
                AttachmentLimits(**{field: value})


def test_failed_index_write_on_delete_keeps_record_and_blob(store, monkeypatch):
    record = _upload(store, "n1", b"keep me")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_index", broken)
    with pytest.raises(OSError):
        store.delete(NS, "n1", record.id)
    # Index first, then blob: the record is still there and still readable.
    with store.open_read(NS, "n1", record.id) as handle:
        assert handle.read() == b"keep me"


def test_failed_index_write_on_commit_leaves_no_record_and_no_blob(store, monkeypatch):
    existing = _upload(store, "n1", b"first")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_index", broken)
    with pytest.raises(OSError):
        _upload(store, "n1", b"second")
    assert store.list(NS, "n1") == [existing]
    blobs = store.root / hashed_key(NS) / hashed_key("n1") / "blobs"
    assert [p.name for p in blobs.iterdir()] == [existing.id]
    assert _tmp_entries(store) == []


def test_commit_moves_the_blob_before_writing_the_index(store, monkeypatch):
    seen = []
    original = store._write_index

    def spying(namespace, node_id, records):
        blobs = store.root / hashed_key(namespace) / hashed_key(node_id) / "blobs"
        seen.append(all((blobs / r.id).is_file() for r in records))
        return original(namespace, node_id, records)

    monkeypatch.setattr(store, "_write_index", spying)
    _upload(store, "n1")
    assert seen == [True]


def test_lock_file_lives_outside_the_node_dir_and_survives_delete_node(store):
    _upload(store, "n1")
    lock = store.root / hashed_key(NS) / ".locks" / f"{hashed_key('n1')}.lock"
    assert lock.is_file()
    store.delete_node(NS, "n1")
    assert lock.is_file()


def test_delete_node_removes_a_directory_whose_index_is_corrupt(store):
    _upload(store, "n1")
    node_dir = store.root / hashed_key(NS) / hashed_key("n1")
    (node_dir / "index.json").write_text("{broken")
    store.delete_node(NS, "n1")
    assert not node_dir.exists()


def test_commit_refuses_a_staged_file_over_the_file_limit(store):
    with pytest.raises(AttachmentTooLarge):
        _upload(store, "n1", b"x" * 11, limits=AttachmentLimits(max_file_bytes=10))
    assert store.list(NS, "n1") == []
    assert _tmp_entries(store) == []


@pytest.mark.parametrize(
    "bad", ["../x", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", ""]
)
def test_malformed_ids_never_reach_the_index(store, bad, monkeypatch):
    from backend.core.attachment_store import is_valid_attachment_id

    assert is_valid_attachment_id(bad) is False
    calls = []
    monkeypatch.setattr(store, "_read_index", lambda *a: calls.append(a) or [])
    assert store.get_record(NS, "n1", bad) is None
    assert store.delete(NS, "n1", bad) is False
    assert calls == []


def test_blob_path_refuses_an_id_that_is_not_a_minted_one(store):
    with pytest.raises(AttachmentNotFound):
        store._blob_path(NS, "n1", "../x")
    with pytest.raises(AttachmentNotFound):
        store._blob_path(NS, "n1", "A" * 32)


def test_os_lock_alone_serializes_two_store_instances(tmp_path, monkeypatch):
    # Two instances share no thread locks, so only the OS file lock (what
    # separates two processes on one root) can make the second commit wait.
    import threading

    root = tmp_path / "attachments"
    holder, contender = FileAttachmentStore(root), FileAttachmentStore(root)
    entered, release = threading.Event(), threading.Event()
    blocking = threading.Event()
    calls = []
    real_lock = attachment_store._lock_file

    def announcing_lock(handle, exclusive):
        # The first call is the holder's; the second is the contender's commit.
        calls.append(handle)
        if len(calls) == 2:
            blocking.set()
        return real_lock(handle, exclusive=exclusive)

    monkeypatch.setattr(attachment_store, "_lock_file", announcing_lock)

    def hold():
        with holder._node_lock(NS, "n1"):
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(10)
    committed = threading.Event()

    def contend():
        _upload(contender, "n1")
        committed.set()

    contending = threading.Thread(target=contend)
    contending.start()
    try:
        # Only once the contender is at the lock does waiting prove it blocks.
        assert blocking.wait(10)
        assert not committed.wait(0.3)
    finally:
        release.set()
        thread.join(10)
    contending.join(10)
    assert committed.is_set()
    assert len(holder.list(NS, "n1")) == 1
