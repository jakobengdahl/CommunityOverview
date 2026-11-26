"""Backup integration for an immutable, coordinated file-store snapshot.

The source paths must be private, immutable copies from one offline snapshot;
this module never opens a live store. The authorization policy remains owned
by the deployment and is loaded from SESSION_BACKUP_AUTH_MODULE.
"""

import importlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from backend.core.session_manager import SessionManager
from backend.core.session_store import FileSessionPersistenceBackend, SessionStore
from backend.core.storage import GraphStorage
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.write_modes import WriteModes
from backend.service import GraphService
from backend.service.session_backup import BackupRefused


def authorization_hook():
    return importlib.import_module(
        os.environ["SESSION_BACKUP_AUTH_MODULE"]
    ).authorization_hook()


class SnapshotModes(WriteModes):
    @contextmanager
    def backup_snapshot(self):
        # The graph and session files here are private, immutable copies.
        yield self


class SnapshotGraphBackend(FileGraphPersistenceBackend):
    def _cut_journal_to(self, length):
        # Recovery would authorize references against an incomplete graph.
        raise BackupRefused("incomplete snapshot journal")


@contextmanager
def open_backup(hook):
    source_graph = Path(os.environ["SESSION_BACKUP_GRAPH"])
    source_sessions = Path(os.environ["SESSION_BACKUP_SESSIONS"])
    custom_journal = os.environ.get("SESSION_BACKUP_GRAPH_JOURNAL")
    source_journal = (
        Path(custom_journal) if custom_journal else
        source_graph.with_name(source_graph.stem + ".journal.ndjson")
    )
    if (
        source_graph.suffix != ".json"
        or not source_graph.is_file()
        or not source_sessions.is_dir()
        or (custom_journal is not None and not source_journal.is_file())
    ):
        raise BackupRefused("snapshot unavailable")
    with tempfile.TemporaryDirectory(prefix="session-backup-") as temp:
        root = Path(temp)
        graph = root / source_graph.name
        # Include the journal and staged sidecars. Copying into a private working
        # directory isolates any backend load-time cleanup from the artifact.
        for suffix in (
            ".json",
            ".layers.json",
            ".merges.ndjson",
            ".embeddings.bin",
        ):
            source = source_graph.with_name(source_graph.stem + suffix)
            if source.exists():
                shutil.copyfile(source, root / source.name)
        journal = root / "snapshot.journal.ndjson"
        if source_journal.exists():
            shutil.copyfile(source_journal, journal)
        if not graph.is_file():
            raise BackupRefused("snapshot unavailable")
        shutil.copytree(source_sessions, root / "sessions")
        layer = SnapshotGraphBackend(graph, journal_path=journal, staged_sessions=True)
        for path in (layer.journal_path, layer.merge_log_path):
            if path.exists():
                data = path.read_bytes()
                if data and not data.endswith(b"\n"):
                    raise BackupRefused("incomplete snapshot journal")
        _, pending = layer._read_merge_log()
        if pending is not None or layer._merge_log_needs_repair:
            raise BackupRefused("unreadable session mode")
        storage = GraphStorage(str(graph), persistence_backend=layer)
        sessions = FileSessionPersistenceBackend(root / "sessions")
        modes = SnapshotModes(layer)
        service = GraphService(storage, authorization_hook=hook)
        service.attach_sessions(
            SessionManager(
                SessionStore(sessions, staged_status=modes.staged_status),
                write_modes=modes,
            )
        )
        yield service, sessions, modes
