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
