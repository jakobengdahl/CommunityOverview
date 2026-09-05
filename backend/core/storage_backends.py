"""
Persistence backends for backend.core storage.

This module defines the public storage seam used by GraphStorage for
loading/saving graph state. The default backend remains file-backed JSON
persistence to preserve standalone behavior.

The seam has two layers. Every backend implements the snapshot contract
(`GraphPersistenceBackend`): load the whole graph, save the whole graph. A
backend that can land one entity at a time additionally implements the
incremental contract (`IncrementalGraphPersistenceBackend`) and says so in its
capability declaration; GraphStorage then routes a mutation to the entity
operations that describe it instead of rewriting the whole graph. Which
contract drives a backend is decided by what it declares, not by an isinstance
check against these protocols. See docs/PERSISTENCE_BACKENDS.md for the
contract a third-party backend has to meet.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Protocol, Sequence, runtime_checkable


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


@dataclass(frozen=True)
class BackendCapabilities:
    """What a persistence backend declares it can do.

    Every flag defaults to False, so a backend that declares nothing is a
    snapshot-only backend and is driven exactly as every backend was before
    the incremental contract existed.

    incremental_writes: the backend implements the entity operations and wants
        a mutation delivered as those rather than as a whole-graph snapshot.
    transactions: `apply_batch` lands all of its operations or none of them.
        A backend that writes incrementally but cannot promise that gets a
        snapshot for any mutation that touches more than one entity, since the
        snapshot write is atomic and the loop of single operations is not.
    change_notification: the backend can tell the application about changes
        made behind its back (another instance sharing the same store). Only
        declared here; the notification seam itself is a later change.
    """

    incremental_writes: bool = False
    transactions: bool = False
    change_notification: bool = False


SNAPSHOT_ONLY = BackendCapabilities()

EntityKindName = Literal["node", "edge"]
EntityActionName = Literal["upsert", "delete"]

_INCREMENTAL_METHODS = (
    "upsert_node",
    "delete_node",
    "upsert_edge",
    "delete_edge",
    "apply_batch",
)


@dataclass(frozen=True)
class EntityOperation:
    """One entity-level write.

    `payload` is the serialized entity for an upsert - the same dict the
    entity occupies in a snapshot's `nodes` or `edges` list - and None for a
    delete. An upsert replaces the stored entity whole; it is not a patch.
    """

    kind: EntityKindName
    action: EntityActionName
    entity_id: str
    payload: Optional[Dict[str, Any]] = None

    @classmethod
    def upsert_node(cls, payload: Dict[str, Any]) -> "EntityOperation":
        return cls("node", "upsert", payload["id"], payload)

    @classmethod
    def delete_node(cls, node_id: str) -> "EntityOperation":
        return cls("node", "delete", node_id)

    @classmethod
    def upsert_edge(cls, payload: Dict[str, Any]) -> "EntityOperation":
        return cls("edge", "upsert", payload["id"], payload)

    @classmethod
    def delete_edge(cls, edge_id: str) -> "EntityOperation":
        return cls("edge", "delete", edge_id)


@runtime_checkable
class GraphPersistenceBackend(Protocol):
    """Persistence seam for GraphStorage state: the snapshot contract.

    Required of every backend. The whole-graph shape is still the right one
    for startup, for export and for the bootstrap write of an empty graph, so
    an incremental backend implements this as well.
    """

    def exists(self) -> bool:
        """Return whether a persisted graph snapshot already exists."""

    def load_graph_data(self) -> Dict[str, Any]:
        """Load serialized graph data."""

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        """Persist serialized graph data."""

    def default_graph_name(self) -> str:
        """Return a safe default graph name for metadata fallbacks."""

    def capabilities(self) -> BackendCapabilities:
        """Declare what the backend supports beyond snapshots."""


@runtime_checkable
class IncrementalGraphPersistenceBackend(GraphPersistenceBackend, Protocol):
    """The entity contract, for a backend declaring `incremental_writes`.

    Each operation is complete on return. A backend declaring `transactions`
    must make `apply_batch` atomic; one that does not is never handed more
    than one operation at a time.
    """

    def upsert_node(self, node: Dict[str, Any]) -> None:
        """Create or replace one node from its serialized form."""

    def delete_node(self, node_id: str) -> None:
        """Remove one node. Removing a node that is absent is not an error."""

    def upsert_edge(self, edge: Dict[str, Any]) -> None:
        """Create or replace one edge from its serialized form."""

    def delete_edge(self, edge_id: str) -> None:
        """Remove one edge. Removing an edge that is absent is not an error."""

    def apply_batch(self, operations: Sequence[EntityOperation]) -> None:
        """Apply the operations in order, as one unit."""


def capabilities_of(backend: Any) -> BackendCapabilities:
    """The capabilities a backend declares, validated against what it implements.

    A backend written before the declaration existed has no `capabilities`
    at all; it is a snapshot-only backend and is treated as one. A backend
    that declares incremental writes without implementing the entity
    operations would fail on the first mutation, after the graph has already
    changed in memory, so it is refused here instead.
    """
    declare = getattr(backend, "capabilities", None)
    if declare is None:
        return SNAPSHOT_ONLY
    caps = declare()
    if not isinstance(caps, BackendCapabilities):
        raise TypeError(
            f"{type(backend).__name__}.capabilities() must return "
            f"BackendCapabilities, got {type(caps).__name__}"
        )
    if caps.incremental_writes:
        missing = [
            m for m in _INCREMENTAL_METHODS if not callable(getattr(backend, m, None))
        ]
        if missing:
            raise TypeError(
                f"{type(backend).__name__} declares incremental_writes but does "
                f"not implement: {', '.join(missing)}"
            )
    return caps


class FileGraphPersistenceBackend:
    """Default JSON file-backed persistence backend for standalone mode.

    Snapshot-only: a mutation of any size is written as the whole graph, with
    the temp-file-plus-rename below making each write atomic.
    """

    def __init__(self, json_path: str | Path = "graph.json"):
        self.json_path = Path(json_path)

    def capabilities(self) -> BackendCapabilities:
        return SNAPSHOT_ONLY

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
