"""Remembered interface preferences of signed-in users.

Each identity's preferences live in one small JSON file whose name is a hash of
the identity, so the directory names no user. Only the known keys below are
stored; a missing key reads as its default, so an absent file or an older file
means the default layout.
"""

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from backend.core.session_store import _fsync_directory, _make_directory

PREFERENCE_DEFAULTS = {"hideClassicSurfaces": False}


class UserPreferencesStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def _path(self, identity: str) -> Path:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, identity: str) -> dict:
        path = self._path(identity)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            # A damaged file reads as the defaults; the next save replaces it.
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        return {
            key: stored[key] if isinstance(stored.get(key), type(default)) else default
            for key, default in PREFERENCE_DEFAULTS.items()
        }

    def update(self, identity: str, changes: dict) -> dict:
        """Write the known keys of ``changes``; return once durable."""
        with self._lock:
            preferences = self.get(identity)
            preferences.update(
                {key: changes[key] for key in PREFERENCE_DEFAULTS if key in changes}
            )
            _make_directory(self.directory)
            fd, temporary = tempfile.mkstemp(
                dir=self.directory, prefix="preferences-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(preferences, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self._path(identity))
                _fsync_directory(self.directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return preferences
