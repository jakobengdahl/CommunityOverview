"""
Persistence backends for backend.core storage.

This module defines the public storage seam used by GraphStorage for
loading/saving graph state. The default backend remains file-backed JSON
persistence to preserve standalone behavior.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Protocol, runtime_checkable


# Cross-platform file locking
if sys.platform == "win32":
    import msvcrt

    def _lock_file(f, exclusive: bool = True) -> None:
        """Acquire file lock on Windows."""
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if exclusive else msvcrt.LK_LOCK, 1)

    def _unlock_file(f) -> None:
        """Release file lock on Windows."""
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f, exclusive: bool = True) -> None:
        """Acquire file lock on Unix."""
        fcntl.flock(f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _unlock_file(f) -> None:
        """Release file lock on Unix."""
        fcntl.flock(f, fcntl.LOCK_UN)


@runtime_checkable
class GraphPersistenceBackend(Protocol):
    """Persistence seam for GraphStorage state."""

    def exists(self) -> bool:
        """Return whether a persisted graph snapshot already exists."""

    def load_graph_data(self) -> Dict[str, Any]:
        """Load serialized graph data."""

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        """Persist serialized graph data."""

    def default_graph_name(self) -> str:
        """Return a safe default graph name for metadata fallbacks."""


class FileGraphPersistenceBackend:
    """Default JSON file-backed persistence backend for standalone mode."""

    def __init__(self, json_path: str | Path = "graph.json"):
        self.json_path = Path(json_path)

    def exists(self) -> bool:
        return self.json_path.exists()

    def load_graph_data(self) -> Dict[str, Any]:
        with open(self.json_path, "r", encoding="utf-8") as f:
            _lock_file(f, exclusive=False)
            try:
                return json.load(f)
            finally:
                _unlock_file(f)

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".json",
            prefix="graph_",
            dir=self.json_path.parent,
        )

        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    _unlock_file(f)

            if sys.platform == "win32" and self.json_path.exists():
                os.replace(temp_path, self.json_path)
            else:
                os.rename(temp_path, self.json_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def default_graph_name(self) -> str:
        return self.json_path.stem
