"""Run-scoped webhook files: private temporary inputs of exactly one run.

Files live in a private directory beside the webhook credential database,
outside graph data and exports, named by opaque ids so no caller-chosen text
becomes a path. A run's directory is removed when the run ends, whatever the
outcome; ``remove_leftovers`` clears what a crashed process left behind.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass

# Fixed per-call caps in the open core; per-plan quotas belong to the hosted
# layer's execution admission hook, which sees ``input_bytes``.
MAX_RUN_FILES = 10
MAX_RUN_FILE_BYTES = 20 * 1024 * 1024


class RunFilesTooLarge(Exception):
    """The call exceeds the per-call file count or byte cap."""


@dataclass(frozen=True)
class RunFile:
    file_id: str
    filename: str
    declared_content_type: str
    size: int


def remove_leftovers(parent):
    """Delete every run directory under ``parent``; call only at startup."""
    try:
        entries = list(os.scandir(parent))
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            os.unlink(entry.path)


class RunFiles:
    def __init__(self, parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
        # mkdtemp creates the directory readable by this user only.
        self._directory = tempfile.mkdtemp(prefix="function-run-", dir=parent)
        self._files = {}
        self.total_bytes = 0

    def _path(self, file_id):
        return os.path.join(self._directory, file_id)

    def add(self, filename, declared_content_type):
        if len(self._files) >= MAX_RUN_FILES:
            raise RunFilesTooLarge()
        file_id = f"file-{len(self._files) + 1}"
        self._files[file_id] = RunFile(
            file_id, filename[:255], declared_content_type[:255], 0
        )
        with open(self._path(file_id), "xb"):
            pass
        return file_id

    def append(self, file_id, data):
        if self.total_bytes + len(data) > MAX_RUN_FILE_BYTES:
            raise RunFilesTooLarge()
        with open(self._path(file_id), "ab") as handle:
            handle.write(data)
        self.total_bytes += len(data)
        record = self._files[file_id]
        self._files[file_id] = RunFile(
            file_id,
            record.filename,
            record.declared_content_type,
            record.size + len(data),
        )

    def list(self):
        return list(self._files.values())

    def get(self, file_id):
        """The file's record and bytes, or None for an id this run does not own."""
        record = self._files.get(file_id) if isinstance(file_id, str) else None
        if record is None:
            return None
        with open(self._path(file_id), "rb") as handle:
            return record, handle.read()

    def close(self):
        self._files = {}
        shutil.rmtree(self._directory, ignore_errors=True)
