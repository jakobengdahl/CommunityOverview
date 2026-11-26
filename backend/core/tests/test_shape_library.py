# covers: backend/core/shape_library.py, backend/core/session_store.py, backend/core/session_manager.py, backend/service/rest_api.py
import asyncio
import copy
import errno
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.config.node_shapes import resolve_shape
from backend.core import GraphStorage
from backend.core import session_manager
from backend.core.session_manager import SessionManager
from backend.core.session_store import (
    FileSessionPersistenceBackend,
    SessionStore,
    _fsync_directory,
)
from backend.core.shape_library import (
    FileShapeLibraryPersistenceBackend,
    LibraryConflict,
    LibraryForbidden,
    ShapeLibrary,
)
from backend.service import GraphService
from backend.service.rest_api import create_rest_router


DEFINITION = {
    "version": 1,
    "base": "rectangle",
    "scale": 1.0,
    "placement": "middle-centre",
    "areas": {"first": "type", "second": "name", "third": None},
}


def test_refused_writes_leave_the_library_unchanged(tmp_path):
    """Disabled, anonymous and invalid writes persist nothing."""
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        FileSessionPersistenceBackend(tmp_path / "sessions"),
    )
    assert library.writes_enabled is False
    with pytest.raises(LibraryForbidden):
        library.put("card", "Card", DEFINITION, identity="actor")
    with pytest.raises(LibraryForbidden):
        library.delete("card", identity="actor", expected_revision=1, expected_usage=0)
    library.writes_enabled = True
    for identity in (None, ""):
        with pytest.raises(LibraryForbidden):
            library.put("card", "Card", DEFINITION, identity=identity)
    for key, name, definition in (
        ("Bad Key!", "Card", DEFINITION),
        ("card", "  ", DEFINITION),
        ("card", "x" * 129, DEFINITION),
        ("card", "Card", {**DEFINITION, "base": "hexagon-ish"}),
    ):
        with pytest.raises(ValueError):
            library.put(key, name, definition, identity="actor")
    with pytest.raises(LibraryConflict):
        library.delete("card", identity="actor", expected_revision=1, expected_usage=0)
    assert library.snapshot() == {"revision": 0, "entries": {}}
    assert not (tmp_path / "library" / "library.json").exists()


def test_revision_usage_tombstones_and_restart(tmp_path):
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    active = store.create()
    closed = store.create()
    active.state["shape_overrides"] = {"a": {"ref": "saved:card"}}
    active.state["type_shapes"] = {"X": {"ref": "saved:card"}, "Y": {"builtin": True}}
    closed.state["type_shapes"] = {"X": {"ref": "saved:card"}, "Z": "saved:card"}
    closed.state["shape_overrides"] = {"b": {"ref": "saved:other"}}
    store.persist(active)
    store.persist(closed)
    backend = FileShapeLibraryPersistenceBackend(tmp_path / "library")
    library = ShapeLibrary(backend, sessions, writes_enabled=True)
    entry = library.put("card", "Card", DEFINITION, identity="owner")
    assert entry["revision"] == 1 and entry["owner"] == "owner"
    assert entry["deleted"] is False
    assert library.usage("card") == 2
    assert library.usage("other") == 1
    assert library.usage("missing") == 0
    for revision, usage in (
        (None, 2),
        (entry["revision"], 1),
        (entry["revision"], None),
        (entry["revision"] + 1, 2),
    ):
        with pytest.raises(LibraryConflict):
            library.put(
                "card",
                "New",
                DEFINITION,
                identity="editor",
                expected_revision=revision,
                expected_usage=usage,
            )
    assert library.snapshot() == {"revision": 1, "entries": {"card": entry}}
    flat = {**DEFINITION, "base": "rectangle-flat"}
    edited = library.put(
        "card",
        "New",
        flat,
        identity="editor",
        expected_revision=entry["revision"],
        expected_usage=2,
    )
    assert edited["owner"] == "owner"
    assert edited["created_at"] == entry["created_at"]
    assert (edited["name"], edited["revision"]) == ("New", 2)
    assert edited["definition"]["base"] == "rectangle-flat"
    library.snapshot()["entries"]["card"]["name"] = "mutated"
    assert library.snapshot()["entries"]["card"] == edited
    with pytest.raises(LibraryConflict):
        library.delete(
            "card",
            identity="editor",
            expected_revision=entry["revision"],
            expected_usage=2,
        )
    library.put("keep", "Keep", DEFINITION, identity="owner")
    tombstone = library.delete(
        "card",
        identity="editor",
        expected_revision=edited["revision"],
        expected_usage=2,
    )
    assert tombstone["deleted"] is True and tombstone["revision"] == 4
    assert tombstone["definition"] == edited["definition"]
    restarted = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        FileSessionPersistenceBackend(tmp_path / "sessions"),
        writes_enabled=True,
    )
    assert restarted.usage("card") == 2
    snapshot = restarted.snapshot()
    assert snapshot["revision"] == 4 and list(snapshot["entries"]) == ["keep"]
    assert backend.read()["entries"]["card"] == tombstone
    for revision in (None, tombstone["revision"]):
        with pytest.raises(LibraryConflict):
            restarted.put(
                "card",
                "Again",
                DEFINITION,
                identity="owner",
                expected_revision=revision,
                expected_usage=2,
            )
    assert restarted.snapshot() == snapshot
    result = resolve_shape(
        "a",
        "X",
        shape_overrides=active.state["shape_overrides"],
        type_shapes={"X": {"inline": DEFINITION}},
        saved_shapes={
            key: entry["definition"] for key, entry in snapshot["entries"].items()
        },
    )
    assert result["definition"] is None
    assert result["diagnostic"]["code"] == "missing_shape"


def _committing_library(tmp_path):
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    session = SessionStore(sessions).create()
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        writes_enabled=True,
    )
    return library, sessions, session


def _reference(state, entry):
    state["type_shapes"]["X"] = {"ref": "saved:" + entry["key"]}


def test_commit_writes_library_then_session_and_adopts_staged_state(
    tmp_path, monkeypatch
):
    """The library, including its new directory entry, is durable before the
    session is saved, both directories are fsynced, and the live session adopts
    the written state without re-applying."""
    library, sessions, session = _committing_library(tmp_path)
    live_state = session.state
    events = []
    monkeypatch.setattr(
        "backend.core.session_store._fsync_directory",
        lambda directory: events.append(("fsync", Path(directory).name)),
    )
    monkeypatch.setattr(
        "backend.core.shape_library._fsync_directory",
        lambda directory: events.append(("fsync", Path(directory).name)),
    )
    save = sessions.save

    def recording_save(snapshot):
        on_disk = json.loads((tmp_path / "library/library.json").read_text())
        events.append(("save", "card" in on_disk["entries"]))
        save(snapshot)

    monkeypatch.setattr(sessions, "save", recording_save)
    calls = []

    def apply(state, entry):
        calls.append(entry["revision"])
        _reference(state, entry)

    entry = library.put(
        "card", "Card", DEFINITION, identity="owner", session=session, apply=apply
    )
    assert events == [
        ("fsync", tmp_path.name),
        ("fsync", "library"),
        ("save", True),
        ("fsync", "sessions"),
    ]
    assert calls == [entry["revision"]]
    persisted = sessions.load(session.id)
    assert session.state is live_state
    assert session.state == persisted["state"]
    assert session.updated_at == persisted["updated_at"] == entry["updated_at"]
    assert library.usage("card") == 1


@pytest.mark.parametrize("failing", ["library", "session", "session-directory"])
def test_failed_commit_adopts_nothing_and_releases_the_lock(
    tmp_path, monkeypatch, failing
):
    """A failed library write, session write or session directory fsync leaves
    the live and persisted session as before, never persists a reference ahead
    of the library, and unlocks."""
    library, sessions, session = _committing_library(tmp_path)
    before = copy.deepcopy(session.to_dict())

    def fail(*args):
        raise OSError(errno.EIO, "disk")

    if failing == "library":
        monkeypatch.setattr("backend.core.shape_library.os.replace", fail)
    elif failing == "session":
        monkeypatch.setattr(sessions, "save", fail)
    else:
        library.backend.read()  # the library directory exists before the patch
        monkeypatch.setattr("backend.core.session_store._fsync_directory", fail)
    with pytest.raises(OSError):
        library.put(
            "card",
            "Card",
            DEFINITION,
            identity="owner",
            session=session,
            apply=_reference,
        )
    assert session.to_dict() == before
    assert sessions.load(session.id) == before
    assert ("card" in library.backend.read()["entries"]) is (failing != "library")
    monkeypatch.undo()
    acquired = []

    def lock_both():
        with sessions.transaction():
            acquired.append(library.backend.read()["revision"])

    other = threading.Thread(target=lock_both, daemon=True)
    other.start()
    other.join(timeout=5)
    assert acquired


@pytest.mark.parametrize(
    "code, fails",
    [(errno.EINVAL, False), (errno.ENOTSUP, False), (errno.EIO, True)],
)
def test_directory_fsync_tolerates_only_unsupported(tmp_path, monkeypatch, code, fails):
    """A filesystem that cannot fsync a directory keeps the completed rename; a
    real I/O error fails the write."""

    def refuse(fd):
        raise OSError(code, "fsync")

    monkeypatch.setattr("backend.core.session_store.os.fsync", refuse)
    if fails:
        with pytest.raises(OSError):
            _fsync_directory(tmp_path)
    else:
        _fsync_directory(tmp_path)


def _live_writer(writer, manager, session):
    if writer == "apply_op_sync":
        manager.rename_session_sync(session.id, "Renamed", "c1")
    else:
        manager.undo_last_action(session.id, "c1")


@pytest.mark.parametrize("writer", ["apply_op_sync", "undo"])
def test_live_writers_are_marked_through_persist_and_refuse_a_commit(
    tmp_path, monkeypatch, writer
):
    """A synchronous live writer holds the writing marker across its persist, a
    library commit for that session is refused meanwhile, and the marker clears."""
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    manager = SessionManager(store)
    session = manager.create_session()
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        writes_enabled=True,
        writers=store,
    )
    seen = []

    def commit_during_persist(*args):
        seen.append(store.is_writing(session.id))
        with pytest.raises(LibraryConflict):
            library.put(
                "card",
                "Card",
                DEFINITION,
                identity="owner",
                session=session,
                apply=_reference,
            )

    if writer == "undo":
        manager.upsert_annotation(session.id, "c1", {"id": "a", "type": "note"})
        replay = manager._apply_op_sync

        def commit_before_replay(*args, **kwargs):
            commit_during_persist()
            return replay(*args, **kwargs)

        # Undo flips the record's flags before the replay's own section.
        monkeypatch.setattr(manager, "_apply_op_sync", commit_before_replay)
    monkeypatch.setattr(store, "persist", commit_during_persist)
    monkeypatch.setattr(store, "persist_snapshot", commit_during_persist)
    _live_writer(writer, manager, session)
    assert seen == [True] * (2 if writer == "undo" else 1)
    assert not store.is_writing(session.id)
    assert library.snapshot()["entries"] == {}
    assert "X" not in session.state["type_shapes"]


@pytest.mark.parametrize("writer", ["apply_op_sync", "undo"])
def test_live_writers_hold_the_marker_from_snapshot_through_rollback(
    tmp_path, monkeypatch, writer
):
    """The marker is held when the writer snapshots, applies and, after a
    failed persist, rolls back, and clears once the rollback is done."""
    store = SessionStore(FileSessionPersistenceBackend(tmp_path))
    manager = SessionManager(store)
    session = manager.create_session()
    if writer == "undo":
        manager.upsert_annotation(session.id, "c1", {"id": "a", "type": "note"})
    seen = []

    def observe(name, original):
        def wrapper(*args, **kwargs):
            seen.append((name, store.is_writing(session.id)))
            return original(*args, **kwargs)

        return wrapper

    def fail_persist(*args):
        seen.append(("persist", store.is_writing(session.id)))
        raise OSError(errno.EIO, "disk")

    for name in ("ring", "find_latest_undoable", "apply_state_op", "restore_ring"):
        monkeypatch.setattr(store, name, observe(name, getattr(store, name)))
    monkeypatch.setattr(store, "persist", fail_persist)
    monkeypatch.setattr(
        session_manager,
        "_restore_deleted_ids",
        observe("restore_deleted_ids", session_manager._restore_deleted_ids),
    )
    with pytest.raises(OSError):
        _live_writer(writer, manager, session)
    names = [name for name, _ in seen]
    rollback = ["persist", "restore_deleted_ids", "restore_ring"]
    if writer == "undo":
        # The replay's own restore of record flags and deleted ids runs last.
        assert names[0] == "find_latest_undoable"
        assert names[-4:] == rollback + ["restore_deleted_ids"]
    else:
        assert names[-3:] == rollback
    assert {"ring", "apply_state_op"} <= set(names)
    assert all(marked for _, marked in seen)
    assert not store.is_writing(session.id)
    if writer == "undo":
        assert not session.activity_log[-1].get("undone")
        assert [a["id"] for a in session.state["annotations"]] == ["a"]
    else:
        assert session.name != "Renamed"


def test_writing_nests_waits_for_a_transaction_and_clears_on_error(tmp_path):
    """A writer inside another writer's section, on any thread, neither
    deadlocks nor ends the marker (the transaction is not held across a
    section), entry waits for a running transaction, and the outermost exit
    clears the marker, on error too."""
    sessions = FileSessionPersistenceBackend(tmp_path)
    store = SessionStore(sessions)
    manager = SessionManager(store)
    session = manager.create_session()
    with pytest.raises(RuntimeError):
        with store.writing(session.id):
            manager.rename_session_sync(session.id, "Inner", "c1")
            assert store.is_writing(session.id) and session.name == "Inner"
            inner_done = []

            def nested_elsewhere():
                with store.writing(session.id):
                    pass
                inner_done.append(True)

            other = threading.Thread(target=nested_elsewhere, daemon=True)
            other.start()
            other.join(5)
            assert inner_done == [True] and store.is_writing(session.id)
            raise RuntimeError
    assert not store.is_writing(session.id)
    held, release, marked = threading.Event(), threading.Event(), []

    def hold():
        with sessions.transaction():
            held.set()
            release.wait(5)

    def write():
        with store.writing(session.id):
            marked.append(True)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    held.wait(5)
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(0.2)
    assert writer.is_alive() and not marked and not store.is_writing(session.id)
    release.set()
    writer.join(5)
    assert marked == [True] and not store.is_writing(session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancelled_apply_ops_keeps_the_marker_until_the_save_finishes(
    tmp_path, monkeypatch, save_fails
):
    """A batch applies and persists under the marker; if its task (or every
    task, as at loop teardown) is cancelled mid-save, marker and lock stay held
    until the save thread finishes, then the batch commits (or rolls back on a
    failed save) and the marker clears."""
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    manager = SessionManager(store)
    session = manager.create_session()
    name_before = session.name
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        writes_enabled=True,
        writers=store,
    )
    marked, published = [], []
    started, release = threading.Event(), threading.Event()
    save, apply_state_op = store.persist_snapshot, store.apply_state_op

    def observe_apply(*args, **kwargs):
        marked.append(store.is_writing(session.id))
        return apply_state_op(*args, **kwargs)

    def blocking_save(snapshot):
        marked.append(store.is_writing(session.id))
        started.set()
        release.wait(5)
        if save_fails:
            raise OSError(errno.EIO, "disk")
        save(snapshot)

    monkeypatch.setattr(store, "apply_state_op", observe_apply)
    monkeypatch.setattr(store, "persist_snapshot", blocking_save)
    monkeypatch.setattr(
        manager.bus, "publish", lambda _sid, event: published.append(event["op"])
    )
    task = asyncio.create_task(
        manager.apply_ops(
            session.id, "c1", None, [{"op": "session_renamed", "name": "Renamed"}]
        )
    )
    await asyncio.to_thread(started.wait, 5)
    # Cancel every task, as asyncio.run's teardown does, not only the caller.
    for pending in asyncio.all_tasks() - {asyncio.current_task()}:
        pending.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    assert store.is_writing(session.id) and manager._lock(session.id).locked()
    with pytest.raises(LibraryConflict):
        library.put(
            "card",
            "Card",
            DEFINITION,
            identity="owner",
            session=session,
            apply=_reference,
        )
    release.set()
    with pytest.raises(OSError if save_fails else asyncio.CancelledError):
        await task
    assert marked == [True, True]
    assert not store.is_writing(session.id)
    assert not manager._lock(session.id).locked()
    on_disk = SessionStore(FileSessionPersistenceBackend(tmp_path / "sessions"))
    expected = name_before if save_fails else "Renamed"
    assert session.name == on_disk.get(session.id).name == expected
    assert [op["op"] for op in published] == ([] if save_fails else ["session_renamed"])


def _staging_writer(writer, manager, session_id, commit):
    if writer == "stage":
        return manager.stage_graph_change(
            session_id, expected_session_seq=None, commit=commit
        )
    # The layer change has already committed when this is called.
    return manager.announce_layer_change(
        session_id, {"op": "graph_changes_discarded", "removals": []}
    )


@pytest.mark.parametrize("writer", ["stage", "announce"])
def test_staging_and_layer_writers_apply_their_op_under_the_marker(
    tmp_path, monkeypatch, writer
):
    """The staging commit, the op and its persist run under the marker; a call
    nested in another section neither deadlocks nor ends it, and it clears."""
    store = SessionStore(FileSessionPersistenceBackend(tmp_path))
    manager = SessionManager(store)
    session = manager.create_session()
    seen = []

    def commit(seq):
        seen.append(("commit", store.is_writing(session.id)))
        return {"entries": [], "removals": []}

    def observe(name, original):
        def wrapper(*args, **kwargs):
            seen.append((name, store.is_writing(session.id)))
            return original(*args, **kwargs)

        return wrapper

    for name in ("apply_state_op", "persist"):
        monkeypatch.setattr(store, name, observe(name, getattr(store, name)))
    first = _staging_writer(writer, manager, session.id, commit)
    with store.writing(session.id):
        nested = _staging_writer(writer, manager, session.id, commit)
        assert store.is_writing(session.id)
    assert not store.is_writing(session.id)
    assert (first["seq"], nested["seq"], session.seq) == (1, 2, 2)
    span = ["apply_state_op", "persist"]
    assert [name for name, _ in seen] == (["commit"] * (writer == "stage") + span) * 2
    assert all(marked for _, marked in seen)


@pytest.mark.parametrize(
    "writer, failure",
    [("stage", "marker"), ("stage", "commit"), ("announce", "marker")],
)
def test_staging_and_layer_writers_release_the_marker_on_failure(
    tmp_path, monkeypatch, writer, failure
):
    """A marker that cannot be taken stops staging before its commit point and,
    after a layer change, is caught with no op; a failed commit releases it."""
    sessions = FileSessionPersistenceBackend(tmp_path)
    store = SessionStore(sessions)
    manager = SessionManager(store)
    session = manager.create_session()
    committed, published = [], []
    monkeypatch.setattr(manager.bus, "publish", lambda *args: published.append(args))

    def commit(seq):
        committed.append(seq)
        if failure == "commit":
            raise OSError(errno.EIO, "layer")
        return {"entries": [], "removals": []}

    def broken_transaction():
        raise OSError(errno.EIO, "lock")

    if failure == "marker":
        monkeypatch.setattr(sessions, "transaction", broken_transaction)
    if writer == "announce":
        assert _staging_writer(writer, manager, session.id, commit) is None
    else:
        with pytest.raises(OSError):
            _staging_writer(writer, manager, session.id, commit)
        assert committed == ([] if failure == "marker" else [1])
    assert session.seq == 0 and published == []
    assert not store.is_writing(session.id)


def test_commits_invalidate_referencing_streams_and_reconnect_reads_the_revision(
    tmp_path,
):
    """An edit or delete reaches only the streams of sessions referencing the
    entry, in memory mid-write or on disk, naming key and revision only;
    catch-up carries the current revision, None once unreadable, and the read
    endpoint serves no tombstone or owner."""
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    manager = SessionManager(store)
    manager.shape_library = library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        writes_enabled=True,
        writers=store,
        invalidate=manager.publish_library_invalidation,
    )
    using, other, plain, adding, idle = (manager.create_session() for _ in range(5))
    using.state["shape_overrides"] = {"a": {"ref": "saved:card"}}
    other.state["type_shapes"] = {"X": {"ref": "saved:other"}}
    all_sessions = (using, other, plain, adding, idle)
    for session in all_sessions:
        store.persist(session)
    streams = {s.id: manager.bus.subscribe(s.id) for s in all_sessions}
    library.put("card", "Card", DEFINITION, identity="owner")
    library.put("keep", "Keep", DEFINITION, identity="owner")
    with store.writing(adding.id), store.writing(idle.id):
        # Referenced only in memory until this write persists.
        adding.state["type_shapes"] = {"X": {"ref": "saved:card"}}
        library.put(
            "card",
            "Card",
            {**DEFINITION, "scale": 2.0},
            identity="editor",
            expected_revision=1,
            expected_usage=1,
        )
        assert streams[adding.id].queue.empty()
        store.persist(adding)
    library.delete("card", identity="editor", expected_revision=3, expected_usage=2)
    received = {}
    for sid, subscription in streams.items():
        received[sid] = []
        while not subscription.queue.empty():
            received[sid].append(subscription.queue.get_nowait())
    expected = [
        {"type": "library_invalidated", "key": "card", "revision": revision}
        for revision in (1, 3, 4)
    ]
    assert received == {
        using.id: expected,
        adding.id: expected[1:],
        other.id: [],
        plain.id: [],
        idle.id: [],
    }
    payloads = [
        manager.catch_up(using.id, since, manager.library_revision())
        for since in (None, using.seq)
    ]
    assert [p["type"] for p in payloads] == ["snapshot", "catch_up"]
    assert all(p["library_revision"] == 4 for p in payloads)
    service = GraphService(GraphStorage(json_path=str(tmp_path / "g.json")))
    router = create_rest_router(service, session_manager=manager)
    endpoint = next(r.endpoint for r in router.routes if r.path == "/shape-library")
    assert json.loads(endpoint().body) == {
        "revision": 4,
        "entries": {"keep": {"name": "Keep", "definition": DEFINITION, "revision": 2}},
    }
    (tmp_path / "library" / "library.json").write_text("{", encoding="utf-8")
    assert manager.library_revision() is None


def _library_client(tmp_path, **options):
    """REST client whose ``x-test-user`` header stands in for the principal the
    auth middleware records; no other input marks a request authenticated."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api_host.middleware import AUTHENTICATED_PRINCIPAL_SCOPE_KEY

    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    manager = SessionManager(store)
    manager.shape_library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        writes_enabled=True,
        writers=store,
        invalidate=manager.publish_library_invalidation,
        **options,
    )
    app = FastAPI()
    service = GraphService(GraphStorage(json_path=str(tmp_path / "g.json")))
    app.include_router(create_rest_router(service, session_manager=manager))

    @app.middleware("http")
    async def authenticate(request, call_next):
        if user := request.headers.get("x-test-user"):
            request.scope[AUTHENTICATED_PRINCIPAL_SCOPE_KEY] = {
                "kind": "user",
                "id": user,
            }
        return await call_next(request)

    return TestClient(app), manager


def test_library_routes_write_as_the_authenticated_principal_only(tmp_path):
    """Any signed-in user may create, edit and delete; anonymous writes and
    client-named owners or actors persist nothing, the owner is the creating
    principal, usage counts closed sessions without naming them, a stale
    revision conflicts unchanged and a deleted key is never reused."""
    client, manager = _library_client(tmp_path)
    library = manager.shape_library
    session = manager.create_session()
    session.state["shape_overrides"] = {"a": {"ref": "saved:card"}}
    manager.store.persist(session)
    # Persisted by another store: closed as far as this process knows.
    SessionStore(library.sessions).create(
        state={"type_shapes": {"X": {"ref": "saved:card"}}}
    )
    body = {"key": "card", "name": "Card", "definition": DEFINITION}
    anonymous = client.post(
        "/shape-library",
        json={**body, "owner": "mallory"},
        headers={"x-communityoverview-actor-id": "mallory"},
    )
    assert anonymous.status_code == 403
    assert not (tmp_path / "library" / "library.json").exists()
    created = client.post(
        "/shape-library",
        json={**body, "owner": "mallory", "identity": "mallory"},
        headers={"x-test-user": "alice"},
    )
    assert created.status_code == 201 and "owner" not in created.json()
    assert library.backend.read()["entries"]["card"]["owner"] == "user:alice"
    usage = client.get("/shape-library/card/usage")
    assert usage.json() == {
        "revision": 1,
        "usage": 2,
        "template_usage": 0,
        "template_usage_token": None,
    }
    edit = {"name": "Flat", "definition": {**DEFINITION, "base": "rectangle-flat"}}
    stale = client.put(
        "/shape-library/card",
        json={**edit, "expected_revision": 0, "expected_usage": 2},
        headers={"x-test-user": "bob"},
    )
    assert stale.status_code == 409
    assert (stale.json()["detail"]["revision"], stale.json()["detail"]["usage"]) == (
        1,
        2,
    )
    assert library.snapshot()["entries"]["card"]["name"] == "Card"
    edited = client.put(
        "/shape-library/card",
        json={**edit, "expected_revision": 1, "expected_usage": 2, "owner": "bob"},
        headers={"x-test-user": "bob"},
    )
    assert edited.status_code == 200 and edited.json()["revision"] == 2
    assert library.backend.read()["entries"]["card"]["owner"] == "user:alice"
    # A delete reviewed at an old revision, or against a usage count that has
    # changed since, conflicts with the current state and deletes nothing.
    stale_usage = client.put(
        "/shape-library/card",
        json={**edit, "expected_revision": 2, "expected_usage": 1},
        headers={"x-test-user": "bob"},
    )
    for params in (
        {"expected_revision": 1, "expected_usage": 2},
        {"expected_revision": 2, "expected_usage": 3},
    ):
        stale_delete = client.delete(
            "/shape-library/card", params=params, headers={"x-test-user": "bob"}
        )
        for refused in (stale_usage, stale_delete):
            assert refused.status_code == 409
            assert (
                refused.json()["detail"]["revision"],
                refused.json()["detail"]["usage"],
            ) == (2, 2)
    assert library.snapshot()["entries"]["card"]["deleted"] is False
    assert library.revision() == 2
    deleted = client.delete(
        "/shape-library/card",
        params={"expected_revision": 2, "expected_usage": 2},
        headers={"x-test-user": "bob"},
    )
    assert deleted.json()["deleted"] is True
    reused = client.post("/shape-library", json=body, headers={"x-test-user": "alice"})
    assert reused.status_code == 409
    assert client.get("/shape-library").json()["entries"] == {}
    assert client.get("/shape-library/card/usage").status_code == 404


def test_a_replaced_policy_governs_every_library_route(tmp_path, monkeypatch):
    """Swapping the one authorize function restricts create, edit and delete
    alike, a graph hook that refuses mutations refuses them all before the
    library policy runs, and a refused write persists nothing."""
    from backend.runtime.authorization import (
        AUTHORIZATION_MODE_ENV,
        AUTHORIZATION_MODE_READ_ONLY,
    )

    calls = []

    def owner_only(identity, action, entry):
        calls.append((identity, action, entry and entry["owner"]))
        return entry is None or entry["owner"] == identity

    client, manager = _library_client(tmp_path, authorize=owner_only)
    body = {"name": "Card", "definition": DEFINITION}
    as_alice, as_bob = ({"x-test-user": user} for user in ("alice", "bob"))
    assert (
        client.post(
            "/shape-library", json={"key": "card", **body}, headers=as_alice
        ).status_code
        == 201
    )
    expected = {"expected_revision": 1, "expected_usage": 0}
    assert (
        client.put(
            "/shape-library/card", json={**body, **expected}, headers=as_bob
        ).status_code
        == 403
    )
    assert (
        client.delete(
            "/shape-library/card", params=expected, headers=as_bob
        ).status_code
        == 403
    )
    assert manager.shape_library.revision() == 1
    monkeypatch.setenv(AUTHORIZATION_MODE_ENV, AUTHORIZATION_MODE_READ_ONLY)
    for method, path, kwargs in (
        ("post", "/shape-library", {"json": {"key": "other", **body}}),
        ("put", "/shape-library/card", {"json": {**body, **expected}}),
        ("delete", "/shape-library/card", {"params": expected}),
    ):
        refused = getattr(client, method)(path, headers=as_alice, **kwargs)
        assert refused.status_code == 403
    assert manager.shape_library.revision() == 1
    monkeypatch.delenv(AUTHORIZATION_MODE_ENV)
    assert (
        client.delete(
            "/shape-library/card", params=expected, headers=as_alice
        ).status_code
        == 200
    )
    assert calls == [
        ("user:alice", "create", None),
        ("user:bob", "edit", "user:alice"),
        ("user:bob", "delete", "user:alice"),
        ("user:alice", "delete", "user:alice"),
    ]


@pytest.mark.parametrize("action", ["put", "delete"])
def test_template_usage_rebuild_conflicts_and_shape_deletion(tmp_path, action):
    from backend.core.template_store import TemplateStore
    from backend.core.tests.test_template_store import document

    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    templates = TemplateStore(FileSessionPersistenceBackend(tmp_path / "templates"))
    backend = FileShapeLibraryPersistenceBackend(tmp_path / "library")
    library = ShapeLibrary(backend, sessions, templates=templates, writes_enabled=True)
    entry = library.put("card", "Card", DEFINITION, identity="owner")
    doc = templates.save(document(), 0)
    restarted = ShapeLibrary(
        backend,
        sessions,
        templates=TemplateStore(FileSessionPersistenceBackend(tmp_path / "templates")),
        writes_enabled=True,
    )
    assert restarted.usage("card") == 0
    assert restarted.template_usage("card") == 1
    with pytest.raises(LibraryConflict):
        restarted.delete(
            "card",
            identity="owner",
            expected_revision=entry["revision"],
            expected_usage=0,
        )
    token = restarted.template_usage_token("card")
    doc["nodes"][0]["shape"]["ref"] = None
    doc = templates.save(doc, doc["revision"])
    with pytest.raises(LibraryConflict):
        restarted.put(
            "card",
            "Card",
            DEFINITION,
            identity="owner",
            expected_revision=entry["revision"],
            expected_usage=0,
            expected_template_usage=1,
        )
    other = document()
    other["template_id"] = "9876-5432-9876-5432"
    other = templates.save(other, 0)
    assert restarted.template_usage("card") == 1
    with pytest.raises(LibraryConflict):
        options = dict(
            identity="owner",
            expected_revision=entry["revision"],
            expected_usage=0,
            expected_template_usage=1,
            expected_template_usage_token=token,
        )
        if action == "delete":
            restarted.delete("card", **options)
        else:
            restarted.put("card", "Card", DEFINITION, **options)
    templates.delete(other["template_id"], other["revision"])
    doc["nodes"][0]["shape"]["ref"] = "saved:card"
    doc = templates.save(doc, doc["revision"])
    with pytest.raises(LibraryConflict):
        options = dict(
            identity="owner",
            expected_revision=entry["revision"],
            expected_usage=0,
            expected_template_usage=1,
            expected_template_usage_token=token,
        )
        if action == "delete":
            restarted.delete("card", **options)
        else:
            restarted.put("card", "Card", DEFINITION, **options)
    before = (tmp_path / "templates" / (doc["template_id"] + ".json")).read_bytes()
    restarted.delete(
        "card",
        identity="owner",
        expected_revision=entry["revision"],
        expected_usage=0,
        expected_template_usage=1,
        expected_template_usage_token=restarted.template_usage_token("card"),
    )
    assert (
        tmp_path / "templates" / (doc["template_id"] + ".json")
    ).read_bytes() == before
    templates.delete(doc["template_id"], doc["revision"])
    assert restarted.template_usage("card") == 0


def test_session_creation_and_shape_writes_lock_sessions_before_templates(
    tmp_path, monkeypatch
):
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    manager = SessionManager(store)
    templates = manager.template_store
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        sessions,
        templates=templates,
        writes_enabled=True,
    )
    transaction = templates._backend.transaction
    acquisitions = []

    @contextmanager
    def checked_transaction():
        assert sessions._lock._is_owned()
        acquisitions.append(True)
        with transaction():
            yield

    monkeypatch.setattr(templates._backend, "transaction", checked_transaction)
    manager.create_session()
    entry = library.put("card", "Card", DEFINITION, identity="owner")
    library.delete(
        "card", identity="owner", expected_revision=entry["revision"], expected_usage=0
    )
    assert len(acquisitions) >= 3
