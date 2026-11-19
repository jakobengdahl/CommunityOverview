# covers: backend/core/shape_library.py
import pytest

from backend.config.node_shapes import resolve_shape
from backend.core.session_store import FileSessionPersistenceBackend, SessionStore
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


def test_disabled_and_unauthenticated_writes_are_refused(tmp_path):
    library = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        FileSessionPersistenceBackend(tmp_path / "sessions"),
    )
    with pytest.raises(LibraryForbidden):
        library.put("card", "Card", DEFINITION, identity="actor")
    library.writes_enabled = True
    with pytest.raises(LibraryForbidden):
        library.put("card", "Card", DEFINITION, identity=None)
    assert library.snapshot()["entries"] == {}


def test_revision_usage_tombstones_and_restart(tmp_path):
    sessions = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(sessions)
    active = store.create()
    closed = store.create()
    active.state["shape_overrides"] = {"a": {"ref": "saved:card"}}
    active.state["type_shapes"] = {"X": {"ref": "saved:card"}}
    closed.state["type_shapes"] = {"X": {"ref": "saved:card"}}
    store.persist(active)
    store.persist(closed)
    backend = FileShapeLibraryPersistenceBackend(tmp_path / "library")
    library = ShapeLibrary(backend, sessions, writes_enabled=True)
    entry = library.put("card", "Card", DEFINITION, identity="owner")
    assert library.usage("card") == 2
    with pytest.raises(LibraryConflict):
        library.put(
            "card",
            "New",
            DEFINITION,
            identity="editor",
            expected_revision=entry["revision"],
            expected_usage=1,
        )
    edited = library.put(
        "card",
        "New",
        DEFINITION,
        identity="editor",
        expected_revision=entry["revision"],
        expected_usage=2,
    )
    assert edited["owner"] == "owner"
    with pytest.raises(LibraryConflict):
        library.delete(
            "card",
            identity="editor",
            expected_revision=entry["revision"],
            expected_usage=2,
        )
    library.delete(
        "card",
        identity="editor",
        expected_revision=edited["revision"],
        expected_usage=2,
    )
    restarted = ShapeLibrary(
        FileShapeLibraryPersistenceBackend(tmp_path / "library"),
        FileSessionPersistenceBackend(tmp_path / "sessions"),
        writes_enabled=True,
    )
    assert restarted.usage("card") == 2
    assert restarted.snapshot()["entries"] == {}
    with pytest.raises(LibraryConflict):
        restarted.put("card", "Again", DEFINITION, identity="owner")
    result = resolve_shape(
        "a",
        "X",
        shape_overrides=active.state["shape_overrides"],
        type_shapes={"X": {"inline": DEFINITION}},
        saved_shapes={},
    )
    assert result["definition"] is None
    assert result["diagnostic"]["code"] == "missing_shape"
