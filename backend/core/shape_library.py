"""Persistent named node shapes and their session references.

Mutations stay refused until ``writes_enabled`` is set (the API host sets it)
and are refused for a write without an identity. The identity is recorded as
the owner of a new entry; callers derive it with ``library_identity`` from the
principal the authentication middleware recorded, never from a client field.
Who may create, edit or delete is decided by the one ``authorize`` policy, by
default ``authorize_library_write``.

A mutation that also changes a session holds the session backend's transaction
throughout and makes the library durable before the session is written, so a
crash in between leaves at worst an unreferenced entry, never a session that
references one missing from disk.

A committed mutation is announced through ``invalidate(key, revision,
session_ids)`` to the sessions that reference the entry; the notice carries no
definition, so a client fetches the current one (see
docs/design/node-shapes/live-writers.md).
"""

import copy
import json
import logging
import os
import tempfile
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

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

logger = logging.getLogger(__name__)


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


LIBRARY_CREATE = "create"
LIBRARY_EDIT = "edit"
LIBRARY_DELETE = "delete"


def library_identity(principal):
    """The identity for a principal set by the authentication middleware, else None."""
    if not isinstance(principal, Mapping):
        return None
    kind, ident = principal.get("kind"), principal.get("id")
    if not (isinstance(kind, str) and kind and isinstance(ident, str) and ident):
        return None
    return f"{kind}:{ident}"


def authorize_library_write(identity, action, entry):
    """The library write policy: any authenticated identity may create, edit
    and delete every saved shape (owner decision of 2026-11-12).

    Called inside the library transaction with the current entry (None on
    create), only once an identity is present. Replace it through
    ``ShapeLibrary(authorize=...)`` to restrict writes, e.g. to ``entry["owner"]``.
    """
    return True


class ShapeLibrary:
    def __init__(
        self,
        backend: ShapeLibraryPersistenceBackend,
        sessions: SessionPersistenceBackend,
        *,
        templates=None,
        writes_enabled=False,
        writers=None,
        invalidate=None,
        authorize=authorize_library_write,
    ):
        self.backend = backend
        self.sessions = sessions
        self.templates = templates
        self.writes_enabled = writes_enabled
        # ``authorize(identity, action, entry)``; False refuses the write.
        self.authorize = authorize
        # The SessionStore holding the live sessions (``is_writing``).
        self.writers = writers
        # ``invalidate(key, revision, session_ids)`` after each commit.
        self.invalidate = invalidate

    def revision(self):
        return self.backend.read()["revision"]

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
        return len(self.references().get(key, ()))

    def template_usage(self, key):
        return len(self.template_references().get(key, ()))

    def template_references(self):
        index = {}
        if self.templates is not None:
            for document in self.templates.list():
                for node in document["nodes"]:
                    selection = node.get("shape") or {}
                    ref = selection.get("ref")
                    if isinstance(ref, str) and ref.startswith("saved:"):
                        index.setdefault(ref[6:], set()).add(document["template_id"])
        return index

    def references(self):
        """Rebuild the reverse index of persisted session references."""
        index = {}
        with self.sessions.transaction():
            for sid in self.sessions.list_ids():
                session = self.sessions.load(sid)
                if not session:
                    continue
                for ref in _saved_refs(session.get("state", {})):
                    index.setdefault(ref, set()).add(sid)
        return index

    def put(
        self,
        key,
        name,
        definition,
        *,
        identity,
        expected_revision=None,
        expected_usage=None,
        expected_template_usage=0,
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
            expected_template_usage,
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
        expected_template_usage=0,
        session: Session | None = None,
        apply=None,
    ):
        return self._mutate(
            key,
            identity,
            expected_revision,
            expected_usage,
            expected_template_usage,
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
        expected_template_usage,
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
            action = (
                LIBRARY_CREATE
                if entry is None
                else LIBRARY_DELETE
                if delete
                else LIBRARY_EDIT
            )
            if not self.authorize(identity, action, copy.deepcopy(entry)):
                raise LibraryForbidden()
            if entry is not None and (
                expected_usage is None
                or expected_usage != self.usage(key)
                or expected_template_usage != self.template_usage(key)
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

        # Sessions, templates, then library: hold both stores through the check.
        template_lock = (
            self.templates._backend.transaction() if self.templates else nullcontext()
        )
        with self.sessions.transaction(), template_lock:
            # A live writer's rollback would erase the adopted state; the
            # marker is only taken under this transaction, so the check holds.
            if (
                session is not None
                and self.writers is not None
                and self.writers.is_writing(session.id)
            ):
                raise LibraryConflict()
            result = self.backend.update(change)
            if session is not None:
                snapshot = staged["session"]
                self.sessions.save(snapshot)
                session.state.clear()
                session.state.update(snapshot["state"])
                session.updated_at = snapshot["updated_at"]
            users = sorted(self.references().get(key, ())) if self.invalidate else []
            if self.invalidate and self.writers is not None:
                # A session mid-write may reference the key only in memory; it
                # is judged on its state once that write persists or rolls back.
                self.writers.after_writes(
                    lambda live: self._invalidate_late(live, key, result, users)
                )
        if users:
            # The commit stands either way; a client that misses the notice
            # still fetches the revision on its next connect.
            try:
                self.invalidate(key, result["revision"], users)
            except Exception:
                logger.exception("library invalidation failed for %s", key)
        return result

    def _invalidate_late(self, session, key, result, notified):
        if session.id in notified or key not in _saved_refs(session.state):
            return
        self.invalidate(key, result["revision"], [session.id])


def _saved_refs(state):
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
    return refs
