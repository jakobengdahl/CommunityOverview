# covers: backend/core/shape_library.py
import copy
import errno
import json
import threading
from pathlib import Path

import pytest

from backend.config.node_shapes import resolve_shape
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
