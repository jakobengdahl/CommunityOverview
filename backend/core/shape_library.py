"""Persistent named node shapes and their session references.

Storage core only: mutations stay refused until ``writes_enabled`` is set, which
no caller does yet. The identity passed to a write is recorded as the owner; it
must come from an authenticated boundary, never a browser client ID.

A mutation that also changes a session holds the session backend's transaction
throughout and makes the library durable before the session is written, so a
crash in between leaves at worst an unreferenced entry, never a session that
references one missing from disk.
"""

import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from pydantic import TypeAdapter

from backend.config.node_shapes import ShapeDefinition, ShapeName
from backend.core.session_store import (
    Session,
    SessionPersistenceBackend,
    _fsync_directory,
    _lock_file,
    _make_directory,
    _unlock_file,
)


class ShapeLibraryPersistenceBackend(Protocol):
    def read(self) -> dict: ...

    def update(self, change: Callable[[dict], object]) -> object:
        """Apply ``change`` and write; return only once the write is durable."""
        ...


class FileShapeLibraryPersistenceBackend:
    """Serialize read-modify-write transactions through a stable lock file."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._lock = threading.RLock()

    def _transaction(self, change=None):
        with self._lock:
            _make_directory(self.directory)
            with (self.directory / "library.lock").open("a+") as lock:
                _lock_file(lock)
                try:
                    path = self.directory / "library.json"
                    data = (
                        json.loads(path.read_text(encoding="utf-8"))
                        if path.exists()
                        else {"revision": 0, "entries": {}}
                    )
                    if change is None:
                        return data
                    result = change(data)
                    fd, temporary = tempfile.mkstemp(
                        dir=self.directory, prefix="library-", suffix=".tmp"
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as output:
                            json.dump(data, output, ensure_ascii=False)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, path)
                        _fsync_directory(self.directory)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                    return result
                finally:
                    _unlock_file(lock)

    def read(self):
        return self._transaction()

    def update(self, change):
        return self._transaction(change)


class LibraryConflict(Exception):
    """The reviewed entry or its usage has changed."""


class LibraryForbidden(Exception):
    """Library mutation is disabled or no identity was supplied."""


class ShapeLibrary:
    def __init__(
        self,
        backend: ShapeLibraryPersistenceBackend,
        sessions: SessionPersistenceBackend,
        *,
        writes_enabled=False,
    ):
        self.backend = backend
        self.sessions = sessions
        self.writes_enabled = writes_enabled

    def snapshot(self):
        data = self.backend.read()
        return {
            "revision": data["revision"],
            "entries": {
                key: copy.deepcopy(entry)
                for key, entry in data["entries"].items()
                if not entry["deleted"]
            },
        }

    def usage(self, key):
        """Rebuild the reverse index from persisted session snapshots."""
        index = {}
        for sid in self.sessions.list_ids():
            session = self.sessions.load(sid)
            if not session:
                continue
            state = session.get("state", {})
            refs = set()
            for name in ("shape_overrides", "type_shapes"):
                mapping = state.get(name, {})
                if not isinstance(mapping, dict):
                    continue
                for selection in mapping.values():
                    if isinstance(selection, dict):
                        ref = selection.get("ref")
                        if isinstance(ref, str) and ref.startswith("saved:"):
                            refs.add(ref[6:])
            for ref in refs:
                index.setdefault(ref, set()).add(sid)
        return len(index.get(key, ()))

    def put(
        self,
        key,
        name,
        definition,
        *,
        identity,
        expected_revision=None,
        expected_usage=None,
        session: Session | None = None,
        apply=None,
    ):
        TypeAdapter(ShapeName).validate_python(key)
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("invalid display name")
        definition = ShapeDefinition.model_validate(definition).model_dump()
        return self._mutate(
            key,
            identity,
            expected_revision,
            expected_usage,
            name=name,
            definition=definition,
            session=session,
            apply=apply,
        )

    def delete(
        self,
        key,
        *,
        identity,
        expected_revision,
        expected_usage,
        session: Session | None = None,
        apply=None,
    ):
        return self._mutate(
            key,
            identity,
            expected_revision,
            expected_usage,
            delete=True,
            session=session,
            apply=apply,
        )

    def _mutate(
        self,
        key,
        identity,
        expected_revision,
        expected_usage,
        *,
        name=None,
        definition=None,
        delete=False,
        session=None,
        apply=None,
    ):
        """Commit the entry and, when ``apply`` is given, a change to ``session``.

        ``apply(state, entry)`` edits a staged copy of the session state before
        anything is written. The library is made durable first, then the staged
        session; only then does the live session adopt the staged state as
        written. A failure at any step leaves the live session untouched.
        """
        if not self.writes_enabled or not identity:
            raise LibraryForbidden()
        if (apply is None) != (session is None):
            raise ValueError("session and apply go together")
        staged = {}

        def change(data):
            entry = data["entries"].get(key)
            if entry is None:
                if delete or expected_revision is not None:
                    raise LibraryConflict()
            elif entry["deleted"] or expected_revision != entry["revision"]:
                raise LibraryConflict()
            if entry is not None and (
                expected_usage is None or expected_usage != self.usage(key)
            ):
                raise LibraryConflict()
            now = datetime.now(timezone.utc).isoformat()
            data["revision"] += 1
            result = {
                "key": key,
                "name": name if entry is None or not delete else entry["name"],
                "definition": definition if not delete else entry["definition"],
                "owner": identity if entry is None else entry["owner"],
                "revision": data["revision"],
                "created_at": now if entry is None else entry["created_at"],
                "updated_at": now,
                "deleted": delete,
            }
            data["entries"][key] = result
            if session is not None:
                snapshot = copy.deepcopy(session.to_dict())
                apply(snapshot["state"], copy.deepcopy(result))
                snapshot["updated_at"] = now
                staged["session"] = snapshot
            return copy.deepcopy(result)

        # Sessions first, then the library, on every path: usage() inside
        # change() takes the session lock, so the reverse order could deadlock.
        with self.sessions.transaction():
            result = self.backend.update(change)
            if session is not None:
                snapshot = staged["session"]
                self.sessions.save(snapshot)
                session.state.clear()
                session.state.update(snapshot["state"])
                session.updated_at = snapshot["updated_at"]
            return result
