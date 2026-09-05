"""
Persistence backends for backend.core storage.

This module defines the public storage seam used by GraphStorage for
loading/saving graph state. The default backend remains file-backed JSON
persistence to preserve standalone behavior: graph.json is still the graph,
written whole and atomically; between those writes each mutation is appended
to a journal beside it, so a mutation costs one small append rather than a
rewrite of every node, and a crash loses nothing that was written.

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
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)


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
    "checkpoint",
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

    def checkpoint(self) -> None:
        """Fold any write the backend has deferred into its durable snapshot.

        A backend that defers nothing implements this as a no-op. GraphStorage
        calls it from flush() and at shutdown, so what is on disk afterwards
        is the whole graph in its canonical form.
        """


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


class GraphJournalError(Exception):
    """The journal beside graph.json holds a record that cannot be applied."""


class FileGraphPersistenceBackend:
    """Default JSON file-backed persistence backend for standalone mode.

    graph.json is the graph, written whole and atomically (temp file plus
    rename). A mutation does not rewrite it: it is appended, as one JSON
    line, to ``<stem>.journal.ndjson`` beside it and applied to the in-memory
    mirror of the file. The journal is folded back into graph.json - a
    checkpoint - every ``checkpoint_interval`` appends, on ``checkpoint()``
    (flush and shutdown), and on every explicit whole-graph save. Loading
    reads graph.json and replays the journal, so a crash at any point loses
    at most the append that was in flight, and that one is detected and
    dropped whole: a batch is one line, so it lands entirely or not at all.

    The cost per mutation is therefore one small append plus fsync instead of
    a serialisation of every node. On a FUSE-mounted object store, where an
    append re-uploads the file, the journal stays small because the interval
    bounds it.
    """

    DEFAULT_CHECKPOINT_INTERVAL = 100

    def __init__(
        self,
        json_path: str | Path = "graph.json",
        journal_path: Optional[str | Path] = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    ):
        self.json_path = Path(json_path)
        self.journal_path = (
            Path(journal_path)
            if journal_path
            else self.json_path.with_name(self.json_path.stem + ".journal.ndjson")
        )
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be at least 1")
        self._checkpoint_interval = checkpoint_interval
        self._lock = threading.Lock()
        # The in-memory mirror of graph.json plus the journal. Valid only
        # after a load or a save; an entity operation before either loads.
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: Dict[str, Dict[str, Any]] = {}
        self._metadata: Dict[str, Any] = {}
        self._mirrored = False
        # Journal lines not yet folded into graph.json.
        self._journaled = 0

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(incremental_writes=True, transactions=True)

    def exists(self) -> bool:
        return self.json_path.exists()

    # -- snapshot contract ---------------------------------------------------

    def load_graph_data(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_locked()

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._nodes = {n["id"]: n for n in data.get("nodes", [])}
            self._edges = {e["id"]: e for e in data.get("edges", [])}
            self._metadata = dict(data.get("metadata") or {})
            self._mirrored = True
            self._write_snapshot(data)
            self._truncate_journal()

    def default_graph_name(self) -> str:
        return self.json_path.stem

    # -- incremental contract ------------------------------------------------

    def upsert_node(self, node: Dict[str, Any]) -> None:
        self._journal([EntityOperation.upsert_node(node)])

    def delete_node(self, node_id: str) -> None:
        self._journal([EntityOperation.delete_node(node_id)])

    def upsert_edge(self, edge: Dict[str, Any]) -> None:
        self._journal([EntityOperation.upsert_edge(edge)])

    def delete_edge(self, edge_id: str) -> None:
        self._journal([EntityOperation.delete_edge(edge_id)])

    def apply_batch(self, operations: Sequence[EntityOperation]) -> None:
        self._journal(list(operations))

    def checkpoint(self) -> None:
        with self._lock:
            if self._journaled:
                self._checkpoint_locked()

    # -- internals -----------------------------------------------------------

    def _load_locked(self) -> Dict[str, Any]:
        with open(self.json_path, "r", encoding="utf-8") as f:
            _lock_file(f, exclusive=False)
            try:
                raw = f.read()
            finally:
                _unlock_file(f)
        data = json.loads(raw)
        # Parsed twice on purpose: the caller's copy is handed to Node.from_dict
        # and Edge.from_dict, which rewrite fields in place (timestamps become
        # datetimes), and the mirror has to stay JSON-serialisable.
        mirror = json.loads(raw)
        self._nodes = {n["id"]: n for n in mirror.get("nodes", [])}
        self._edges = {e["id"]: e for e in mirror.get("edges", [])}
        self._metadata = dict(mirror.get("metadata") or {})
        self._mirrored = True
        self._journaled = 0

        for record in self._read_journal():
            for op in record:
                self._apply(op)
            self._journaled += 1
        if self._journaled:
            data = {
                "nodes": [json.loads(json.dumps(n)) for n in self._nodes.values()],
                "edges": [json.loads(json.dumps(e)) for e in self._edges.values()],
                "metadata": dict(self._metadata),
            }
        return data

    def _read_journal(self) -> List[List[EntityOperation]]:
        """The journal's records, oldest first.

        A last line that is incomplete or unparsable is the append a crash
        interrupted; it is dropped whole, which is what makes a batch atomic.
        A damaged line anywhere else is not a crash shape but damage, and the
        records after it may depend on it, so loading stops with an error
        that names the line rather than silently losing mutations.
        """
        if not self.journal_path.exists():
            return []
        with open(self.journal_path, "rb") as f:
            _lock_file(f, exclusive=False)
            try:
                raw = f.read()
            finally:
                _unlock_file(f)
        if not raw:
            return []
        lines = raw.split(b"\n")
        complete = raw.endswith(b"\n")
        if complete:
            lines.pop()
        records: List[List[EntityOperation]] = []
        last = len(lines) - 1
        for index, line in enumerate(lines):
            try:
                parsed = json.loads(line)
                ops = [EntityOperation(**op) for op in parsed["ops"]]
            except (ValueError, KeyError, TypeError) as exc:
                if index == last:
                    print(
                        f"Warning: dropping the incomplete last record of "
                        f"{self.journal_path} (interrupted write): {exc}"
                    )
                    break
                raise GraphJournalError(
                    f"{self.journal_path} line {index + 1} cannot be applied "
                    f"({exc}); the graph was not loaded. Repair or remove the "
                    f"journal - graph.json holds the graph as of the last "
                    f"checkpoint"
                ) from exc
            if index == last and not complete:
                print(
                    f"Warning: dropping the incomplete last record of "
                    f"{self.journal_path} (interrupted write)"
                )
                break
            records.append(ops)
        return records

    def _apply(self, op: EntityOperation) -> None:
        store = self._nodes if op.kind == "node" else self._edges
        if op.action == "upsert":
            store[op.entity_id] = op.payload
        else:
            store.pop(op.entity_id, None)

    def _journal(self, operations: List[EntityOperation]) -> None:
        # Serialised before anything is written or applied, so a payload that
        # cannot be represented fails here and touches neither disk nor mirror.
        line = json.dumps(
            {"ops": [asdict(op) for op in operations]}, ensure_ascii=False
        )
        # The mirror keeps its own copy, parsed back from the line, so a caller
        # that later mutates the dict it passed cannot change what was stored.
        stored = [EntityOperation(**op) for op in json.loads(line)["ops"]]
        with self._lock:
            if not self._mirrored:
                self._load_locked()
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    _unlock_file(f)
            for op in stored:
                self._apply(op)
            self._journaled += 1
            if self._journaled >= self._checkpoint_interval:
                # The record is durable in the journal already; folding it into
                # graph.json is maintenance and must not fail the mutation. A
                # failed checkpoint is retried on the next append.
                try:
                    self._checkpoint_locked()
                except Exception as exc:
                    print(f"Warning: graph checkpoint failed, will retry: {exc}")

    def _checkpoint_locked(self) -> None:
        data = {
            "nodes": list(self._nodes.values()),
            "edges": list(self._edges.values()),
            "metadata": {
                **self._metadata,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
        }
        self._write_snapshot(data)
        # Snapshot first, then the journal: a crash in between replays records
        # the snapshot already holds, and every operation is idempotent.
        self._truncate_journal()

    def _truncate_journal(self) -> None:
        self._journaled = 0
        if not self.journal_path.exists():
            return
        with open(self.journal_path, "w", encoding="utf-8") as f:
            _lock_file(f, exclusive=True)
            try:
                f.flush()
                os.fsync(f.fileno())
            finally:
                _unlock_file(f)

    def _write_snapshot(self, data: Dict[str, Any]) -> None:
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
