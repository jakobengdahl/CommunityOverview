"""Shared validation, store preflight and structured actor-attribution replacement."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable


def validate_erasure(actor_id: str, replacement_token: str) -> None:
    """Reject ambiguous replacements without echoing either input."""
    if (
        not isinstance(actor_id, str)
        or not actor_id.strip()
        or not isinstance(replacement_token, str)
        or not replacement_token.strip()
        or actor_id in replacement_token
    ):
        raise ValueError("Erasure requires a nonempty actor and a distinct replacement")


def preflight_file(path: str | Path, *, write: bool = False) -> None:
    """Open regular file *path* as an erasure will, changing nothing.

    Anything but a regular file is refused before the open, since opening a
    device can have side effects; a missing or dangling path raises there too.
    fstat() after the open catches a swap in between, and O_NONBLOCK keeps a
    FIFO swapped in from blocking it. Writers open with O_CREAT, which
    fs.protected_regular refuses in a sticky directory even for root, so a
    write open adds it too.
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise OSError("not a regular file")
    flags = os.O_RDWR | os.O_CREAT if write else os.O_RDONLY
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
    finally:
        os.close(fd)


def preflight_directory(path: str | Path, replaces: Iterable[str | Path] = ()) -> None:
    """Check that *path* is a directory an erasure can list and add files to.

    Write access is asked of the kernel for the effective ids rather than
    tried, since creating a probe file would change the directory. In a
    sticky directory, renaming over or removing a file in *replaces* also
    needs the process to own that file or the directory.
    """
    if not Path(path).is_dir():
        raise OSError("not a directory")
    os.listdir(path)
    effective = os.access in os.supports_effective_ids
    if not os.access(path, os.W_OK | os.X_OK, effective_ids=effective):
        raise PermissionError("directory is not writable")
    info = os.stat(path)
    if not info.st_mode & stat.S_ISVTX or not hasattr(os, "geteuid"):
        return
    euid = os.geteuid()
    if euid in (0, info.st_uid):
        return
    for target in replaces:
        if os.lstat(target).st_uid != euid:
            raise PermissionError("sticky directory holds a file of another owner")


def preflight_sqlite(path: str | Path, table: str, version: int) -> bool:
    """Preflight a read-write SQLite store: False if absent, raise if unusable.

    The database and existing -journal/-wal/-shm sidecars are opened
    read-write, the directory must take new journal files and let SQLite
    remove its own, and *path* must be a store owning *table* at schema
    *version*: the read-write constructors create and migrate schema in
    whatever file they are given.
    """
    if not Path(path).exists():
        return False
    # SQLite follows a symlinked database and keeps its sidecars beside the
    # target, so that is the directory and those are the files checked.
    path = os.path.realpath(path)
    preflight_file(path, write=True)
    sidecars = [
        f"{path}{s}"
        for s in ("-journal", "-wal", "-shm")
        if Path(f"{path}{s}").exists()
    ]
    preflight_directory(Path(path).parent, replaces=sidecars)
    for sidecar in sidecars:
        preflight_file(sidecar, write=True)
    if not _sqlite_schema_matches(path, table, version):
        raise ValueError("not a current-schema store")
    return True


def _sqlite_schema_matches(path: str | Path, table: str, version: int) -> bool:
    """Check that *path* is a current-schema store owning *table*.

    Nothing next to *path* is created or changed. immutable=1 keeps SQLite
    from creating -shm/-wal sidecars, but it also ignores a pending WAL,
    which may hold the schema; such a store is probed as a private copy.
    """
    with tempfile.TemporaryDirectory() as scratch:
        if Path(f"{path}-wal").exists():
            probe = Path(scratch) / "probe.db"
            shutil.copyfile(path, probe)
            shutil.copyfile(f"{path}-wal", f"{probe}-wal")
            # The copy is private, so SQLite may recover the WAL into it.
            uri = probe.as_uri()
        else:
            uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                found = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                current = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return False
    return found is not None and current == version


def replace_attribution(
    value: Any, actor_id: str, replacement_token: str, *, envelope: str = "record"
) -> Any:
    """Replace only schema-defined attribution paths, never arbitrary content."""
    if isinstance(value, list):
        return [
            replace_attribution(v, actor_id, replacement_token, envelope=envelope)
            for v in value
        ]
    if not isinstance(value, dict):
        return value
    children = {
        "run": {"payload": "event_data", "result": "event_data", "origin": "origin"},
        "event_data": {"event": "record", "attribution": "attribution"},
        "record": {
            "origin": "origin",
            "attribution": "attribution",
            "actor": "identity",
            "on_behalf_of": "identity",
            "inverse_op": "operation",
        },
        "origin": {"attribution": "attribution", "actor": "identity"},
        "attribution": {"actor": "identity", "on_behalf_of": "identity"},
    }
    slots = {
        "record": {"actor", "actor_id", "event_origin", "staged_by"},
        "origin": {"event_origin", "staged_by"},
        "identity": {"id", "actor_id"},
        "operation": {"client_id"},
    }
    result = dict(value)
    # Activity snapshots are annotations only when the affected entity says so.
    # Graph-history before/after values and arbitrary content stay untouched.
    if envelope == "activity" and isinstance(value.get("affected"), dict):
        if value["affected"].get("kind") == "annotation":
            for key in ("before", "after"):
                if key in value:
                    result[key] = replace_attribution(
                        value[key], actor_id, replacement_token, envelope="annotation"
                    )
    if (
        envelope == "activity_operation"
        and value.get("op") in {"annotation_created", "annotation_updated"}
        and "annotation" in value
    ):
        result["annotation"] = replace_attribution(
            value["annotation"], actor_id, replacement_token, envelope="annotation"
        )
    if envelope == "annotation" and value.get("created_by") == actor_id:
        result["created_by"] = replacement_token
    if envelope == "activity":
        for key in ("inverse_op", "forward_op"):
            if key in value:
                result[key] = replace_attribution(
                    value[key],
                    actor_id,
                    replacement_token,
                    envelope="activity_operation",
                )
        # Process ordinary record attribution without revisiting operations.
        value = {
            k: v for k, v in value.items() if k not in {"inverse_op", "forward_op"}
        }
        envelope = "record"
    elif envelope == "activity_operation":
        envelope = "operation"
    for key, item in value.items():
        if key in slots.get(envelope, set()) and isinstance(item, str):
            if item == actor_id:
                result[key] = replacement_token
            elif key == "event_origin" and item == f"agent:{actor_id}":
                result[key] = f"agent:{replacement_token}"
        child = children.get(envelope, {}).get(key)
        if child is not None:
            if key == "origin" and isinstance(item, str):
                if item == actor_id:
                    result[key] = replacement_token
                elif item == f"agent:{actor_id}":
                    result[key] = f"agent:{replacement_token}"
            elif isinstance(item, (dict, list)):
                result[key] = replace_attribution(
                    item, actor_id, replacement_token, envelope=child
                )
    return result
