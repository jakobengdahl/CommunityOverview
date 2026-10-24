"""
Graph storage with NetworkX and JSON persistence
Handles all CRUD operations on the graph

This module is part of graph_core - the core graph storage layer.
It provides the main GraphStorage class for persisting and querying the graph.

Concurrency Safety (PoC level):
- Uses threading.RLock for in-memory data structure protection
- Uses file locking (fcntl on Unix, msvcrt on Windows) for file access
- Implements atomic writes via temp file + rename
- Suitable for multiple concurrent users in a single-process deployment

Event System:
- Mutations emit events that can be delivered to webhooks
- EventSubscription nodes in the graph define webhook targets
- Event context (origin, session_id) enables loop prevention
"""

import importlib.util
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Dict, Optional, Any, TYPE_CHECKING, Callable, Tuple
from datetime import datetime, timezone
import networkx as nx
from pathlib import Path

from .models import (
    Node,
    Edge,
    NodeType,
    RelationshipType,
    SimilarNode,
    GraphStats,
    AddNodesResult,
    DeleteNodesResult,
    DeleteEdgesResult,
    _parse_datetime,
    metadata_depth_error,
)
from .embedding_sidecar import EmbeddingSidecarError, FileEmbeddingSidecar
from .storage_backends import (
    EntityOperation,
    EntityRevisionConflict,
    ExternalChange,
    ExternalChangeRefused,
    FileGraphPersistenceBackend,
    GraphPersistenceBackend,
    GraphWriteFailed,
    GraphWriteOutcomeUnknown,
    capabilities_of,
    refused_outright,
)
from .storage_merge import (
    ImportMergeUnsupported,
    MergeApplied,
    MergeWrites,
    SessionMergeLanded,
    SessionMergePlan,
)
from .vector_store import VectorStore, dominant_dimension, matching_dimension
from . import storage_search
from . import storage_history
from . import storage_events

# Event system imports
from .events.models import EventType, EntityKind, EventContext

logger = logging.getLogger(__name__)

# The origin stamped on events that report a change another writer made, so a
# subscriber reacting by writing can tell them from its own instance's work.
EXTERNAL_CHANGE_ORIGIN = "external-change"

# The expectation a create carries: no entity with that id exists (§4.3).
EXPECT_ABSENT = object()

if TYPE_CHECKING:
    from .events.dispatcher import EventDispatcher
    from .events.delivery import DeliveryWorker
    from .events.models import Event
    from .history_store import GraphHistoryStore


class StaleUpdateError(Exception):
    """Raised when an optimistic-concurrency guard on update_node fails.

    The caller passed ``expected_updated_at`` but the node's live
    ``updated_at`` no longer matches it, meaning the node changed since the
    caller read it. The write is rejected instead of silently clobbering the
    concurrent change. ``current_updated_at`` carries the live value so the
    caller can re-read and retry.
    """

    def __init__(self, node_id: str, expected: Any, current: datetime):
        self.node_id = node_id
        self.expected = expected
        self.current_updated_at = current
        super().__init__(
            f"Node '{node_id}' was modified since expected_updated_at="
            f"{expected!r}; current updated_at is {current.isoformat()!r}"
        )


def _apply_metadata_patch(
    base: Dict[str, Any], patch: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge ``patch`` onto ``base`` at the top level (JSON-Merge-Patch style).

    Each key in ``patch`` replaces that key in ``base``; a key whose patch value
    is ``None`` is removed from the result (RFC 7386 null-means-delete). Keys not
    mentioned in ``patch`` are preserved. Nested objects are replaced wholesale,
    not merged recursively — the merge is intentionally top-level only.
    """
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


# A node update may set these; "metadata" is handled apart, and any key in
# neither set nor _NODE_UPDATE_RESERVED folds into metadata.
_NODE_UPDATE_FIELDS = frozenset(
    {"name", "description", "summary", "tags", "subtypes", "aliases", "metadata"}
)
# "archived" is reserved so a generic update can neither set it directly nor
# fold it into metadata — archiving goes through set_nodes_archived.
# "revision" is server-owned (§4.1): dropped, never folded into metadata.
_NODE_UPDATE_RESERVED = frozenset(
    {"id", "type", "embedding", "created_at", "updated_at", "archived", "revision"}
)
EDGE_UPDATE_FIELDS = frozenset({"type", "label", "metadata"})


def build_node_update(
    current: Dict[str, Any], updates: Dict[str, Any], *, metadata_merge: bool
) -> Node:
    """The node ``current`` (a ``to_dict()``) becomes under ``updates``, validated.

    Raises ValueError for an update the model or the metadata depth limit
    refuses. Shared by a direct update and a staged one, which must be
    validated exactly alike (docs/SESSION_OVERLAY_CONTRACT.md §6.3).
    """
    # Build the candidate state and validate it through the model BEFORE
    # mutating the live node. setattr on a pydantic model does not re-run
    # field validators, so without this an over-limit field (e.g. a
    # description longer than 2000 chars, or oversized tags/aliases) would
    # silently bypass the model's limits on write and only fail at the next
    # graph load — bricking startup. Validating a candidate first means an
    # invalid update is rejected here, atomically, with the live node untouched.
    candidate = dict(current)
    for key, value in updates.items():
        if key in _NODE_UPDATE_FIELDS and key != "metadata":
            candidate[key] = value

    # Schema-defined extra fields (anything outside the base model) fold
    # into metadata, same as an explicit `metadata` update.
    extra = {
        k: v
        for k, v in updates.items()
        if k not in _NODE_UPDATE_FIELDS and k not in _NODE_UPDATE_RESERVED
    }

    if metadata_merge:
        # Opt-in field-level merge: patch existing metadata top-level.
        merged = dict(candidate.get("metadata") or {})
        if "metadata" in updates:
            meta_update = updates["metadata"] or {}
            if not isinstance(meta_update, dict):
                raise ValueError(
                    "metadata must be an object when metadata_merge is set"
                )
            merged = _apply_metadata_patch(merged, meta_update)
        if extra:
            merged = _apply_metadata_patch(merged, extra)
        candidate["metadata"] = merged
    else:
        # Legacy/default: an explicit `metadata` replaces it wholesale.
        # Assign the value as-is (no None->{} coercion) so a bad
        # metadata=None with no extra fields is rejected by model
        # validation below, exactly as before merge mode existed. When an
        # extra field is present the fold step normalizes None to {},
        # also matching the prior behaviour.
        if "metadata" in updates:
            candidate["metadata"] = updates["metadata"]
        if extra:
            meta = dict(candidate.get("metadata") or {})
            meta.update(extra)
            candidate["metadata"] = meta

    try:
        validated = Node.from_dict(candidate)
    except Exception as exc:
        raise ValueError(f"Invalid node update: {exc}") from exc
    # Only when metadata is written: a node that already holds deeper
    # metadata keeps taking updates to its other fields.
    if "metadata" in updates or extra:
        depth_error = metadata_depth_error(validated.metadata)
        if depth_error:
            raise ValueError(f"Invalid node update: {depth_error}")
    return validated


def build_edge_update(current: Dict[str, Any], updates: Dict[str, Any]) -> Edge:
    """The edge ``current`` (a ``to_dict()``) becomes under ``updates``,
    validated; applicability against its endpoints is the caller's check."""
    candidate = dict(current)
    for key, value in updates.items():
        if key in EDGE_UPDATE_FIELDS:
            if key == "type" and (value is None or value == ""):
                value = "RELATES_TO"
            candidate[key] = value

    validated_edge = Edge.from_dict(candidate)
    if "metadata" in updates:
        depth_error = metadata_depth_error(validated_edge.metadata)
        if depth_error:
            raise ValueError(f"Invalid edge update: {depth_error}")
    return validated_edge


def edge_applicability_error(
    edge_type: str, source_node: Optional[Node], target_node: Optional[Node]
) -> Optional[str]:
    """Why the schema refuses ``edge_type`` between the two nodes, or None."""
    if source_node is None or target_node is None:
        return None

    from backend.config import config_loader

    decision = config_loader.relationship_type_allows_node_types(
        edge_type,
        source_node.type_str,
        target_node.type_str,
        source_node.subtypes,
        target_node.subtypes,
    )
    if not decision.get("allowed"):
        return decision.get("message") or "Relationship type not allowed"
    return None


# How many reports may be held while the first load runs before the buffer is
# abandoned for a whole-graph reload. The reload is correct at any size, so
# this trades a bounded amount of memory against the cost of reloading; a boot
# that is overtaken by more writes than this was going to be expensive anyway.
_BOOT_BUFFER_LIMIT = 1000


class _BootGate:
    """Holds reports that arrive before the first load has finished.

    Listening starts BEFORE the load, because a write committed in the gap
    between them is announced on a channel nobody is listening to and
    LISTEN/NOTIFY does not replay it: narrow in time, unbounded in
    consequence, since an entity written in that gap and never written again
    is never reported and the instance serves a stale value for good.

    Starting earlier would break the seam's promise that no change is reported
    against a model that does not exist yet - so this keeps that promise on
    the seam's behalf. The backend reports to the gate; the gate calls the
    application only once the load has returned. What arrives in between is
    held and replayed in arrival order.

    Replaying late is sound for a report whose content is read on demand: it
    carries identifiers, so the replay reads what the store holds at replay
    time, which is at least as new as what the announcement described. The
    same property makes a duplicate harmless - a write committed during the
    load is both in the load and in the buffer, and applying it twice reads
    the same value twice.

    A report built with `ExternalChange.entities` carries content gathered
    when it was DISPATCHED, and holding one lengthens the window its own
    docstring warns about: the application arbitrates such an upsert against
    its own by wall clock. The end state still converges - a stale node
    upsert loses to `_is_newer`, and a delete or an edge upsert is saved by
    arrival order, since a later write to the same entity is announced later
    and so replayed later - but it converges by arbitration rather than by
    reading the store, which is the weaker of the two routes.
    """

    def __init__(self, listener: Callable[["ExternalChange"], None]) -> None:
        self._listener = listener
        self._lock = threading.Lock()
        self._held: List["ExternalChange"] = []
        self._overflowed = False
        self._open = False

    def __call__(self, change: "ExternalChange") -> None:
        with self._lock:
            if not self._open:
                if self._overflowed:
                    return
                self._held.append(change)
                if len(self._held) > _BOOT_BUFFER_LIMIT:
                    # One reload subsumes any number of held reports, so drop
                    # them rather than grow without bound. Dropping the list
                    # is safe only because `unknown()` is dispatched in its
                    # place below - never because the writes stopped mattering.
                    self._held.clear()
                    self._overflowed = True
                    # Said out loud, because the reload that replaces these
                    # is best-effort: `_reload_from_store` swallows a failed
                    # read and leaves memory as it was. An overflowed boot
                    # that also met an unreadable store loses exactly what
                    # this gate exists to keep, and nothing else would show
                    # it. Every comparable degradation in this file warns.
                    logger.warning(
                        f"more than {_BOOT_BUFFER_LIMIT} external "
                        "changes arrived while loading; dropping them for a "
                        "whole-graph reload"
                    )
                return
        self._listener(change)

    def open(self) -> None:
        """Replay what was held, then let reports through directly.

        The flag is raised only once the buffer is empty, and the drain runs
        outside the lock so a report arriving mid-drain is buffered rather
        than overtaking the ones already held. Raising the flag first would
        let it be delivered ahead of them and put the replay out of order.
        """
        while True:
            with self._lock:
                if self._overflowed:
                    self._open = True
                    self._overflowed = False
                    held: List["ExternalChange"] = []
                    overflowed = True
                elif self._held:
                    held = self._held
                    self._held = []
                    overflowed = False
                else:
                    self._open = True
                    return
            if overflowed:
                # Everything held is subsumed by the reload this asks for,
                # including anything that arrives from here on: the flag is
                # already raised, so later reports go straight through and a
                # reload does not need them.
                self._listener(ExternalChange.unknown())
                return
            for change in held:
                self._listener(change)


class _Undo:
    """What a write's entities were before it, for putting memory back.

    `nodes` and `edges` map each touched id to its prior serialized form, or
    None where the write created it; `vectors` holds the prior vector of each
    touched node. Captured before memory changes, consumed only when a
    write is refused or did not land (§4.4), and applied without events:
    none has fired for the write yet.
    """

    __slots__ = ("nodes", "edges", "vectors")

    def __init__(self) -> None:
        self.nodes: Dict[str, Optional[Dict[str, Any]]] = {}
        self.edges: Dict[str, Optional[Dict[str, Any]]] = {}
        self.vectors: Dict[str, Optional[List[float]]] = {}


class GraphStorage:
    """
    Manages graph storage with NetworkX + JSON persistence.

    Thread-safety:
    - All public methods that modify state are protected by _lock (threading.RLock)
    - File operations use OS-level file locking for multi-process safety
    - Writes are atomic (temp file + rename) to prevent corruption
    """

    # How far a single-writer store's revision reservation is raised at a
    # time (docs/SESSION_OVERLAY_CONTRACT.md §4.2). Each raise is one durable
    # write of its own - on the file backend a checkpoint of the whole graph -
    # so the block is large: at 1,000 every 1,000-node add_nodes paid for a
    # checkpoint, measured as more than doubling its latency. A crash or a
    # restart skips at most this many values and never reuses one; revisions
    # are only compared, so skipped values cost nothing.
    REVISION_RESERVATION_BLOCK = 100_000

    # How long a shared-store write that failed is given to finish landing
    # before the second evidence read (§4.4). It has to exceed the store's own
    # commit path - a commit that was flushing when the connection dropped -
    # and it is paid only on a failed write, while holding the storage lock.
    WRITE_RESOLVE_DELAY_SECONDS = 1.0

    def __init__(
        self,
        json_path: str = "graph.json",
        embeddings_path: str = None,
        persistence_backend: Optional[GraphPersistenceBackend] = None,
        history_max_events: Optional[int] = None,
        history_max_age_days: Optional[float] = None,
        history_compaction_interval: Optional[int] = None,
    ):
        """
        Initialize GraphStorage.

        Args:
            json_path: Path to the JSON file for graph persistence when using the
                default file-backed persistence backend.
            embeddings_path: Path to the binary embedding sidecar holding the
                node vectors. Defaults to "<graph stem>.embeddings.bin" next to
                the graph file. Only honoured by the file-backed backend; other
                backends keep vectors inline in the node payload.
            persistence_backend: Optional persistence backend adapter. Defaults to
                the file-backed JSON backend to preserve standalone behavior.
            history_max_events: Optional cap on retained history records. When set
                (> 0), the history sidecar is compacted down to the newest N
                records. Default None keeps unbounded history (current behaviour).
            history_max_age_days: Optional cap on history record age in days. When
                set (> 0), records older than the cutoff are dropped on compaction.
                Default None applies no age-based retention.
            history_compaction_interval: Optional number of appends between
                compaction passes (throttle). Defaults to an amortised value when
                retention is enabled; ignored when retention is disabled.
        """
        self._persistence_backend = persistence_backend or FileGraphPersistenceBackend(
            json_path
        )
        # Read once: what the backend can take decides the shape of every
        # write (see _persist), and a backend that declares more than it
        # implements is refused here rather than on the first mutation.
        self._backend_capabilities = capabilities_of(self._persistence_backend)
        # Who chooses an entity's revision (§4.4). A store that enforces
        # revisions assigns them in the statement that writes, and every write
        # waits for it. Any other backend has this instance as its only
        # writer, so the counter is kept here, behind a durable reservation.
        self._store_assigns_revisions = self._backend_capabilities.revision_enforcement
        # A durable reservation is a single writer's: on a store others write
        # too, persisting it would mean a whole-graph write over theirs.
        self._reserves_revisions = not (
            self._store_assigns_revisions
            or self._backend_capabilities.change_notification
        )
        if (
            self._backend_capabilities.change_notification
            and not self._store_assigns_revisions
        ):
            logger.warning(
                f"{type(self._persistence_backend).__name__} is shared "
                f"(change_notification) but does not enforce revisions; "
                f"revisions are counted per instance and are not unique "
                f"across instances"
            )
        # The next revision this instance hands out, and the durable ceiling
        # it may hand out up to (exclusive). Single-writer stores only.
        self._next_revision = 1
        self._revision_reservation = 0
        self.json_path = getattr(
            self._persistence_backend, "json_path", Path(json_path)
        )

        # Node vectors live in their own binary file rather than in the node
        # payload, so a mutation that changes no vector rewrites none of them.
        # Only the file-backed backend owns a concrete path to put it next to;
        # for any other backend vectors stay inline in the serialized node, as
        # they were before the split, so no backend silently loses them.
        self._embedding_sidecar = self._init_embedding_sidecar(embeddings_path)
        self._persisted_vector_revision: Optional[int] = None
        # Raw vectors read out of graph.json that no sidecar write has covered
        # yet, keyed by node id. While an id is in here graph.json is its ONLY
        # durable copy, so the payload keeps carrying it. A vector this process
        # generated was never durable in graph.json, so it never enters this
        # map and is dropped from the first save - which is what keeps the file
        # small immediately.
        #
        # One map rather than "ids the index took" plus "values it refused":
        # those were mutually exclusive branches, so a vector that moved from
        # the first group to the second - evicted when a model width change
        # reset the index - belonged to neither and was deleted from graph.json
        # with nothing written in its place.
        self._inline_fallback: Dict[str, Any] = {}
        # Set when a save has already captured a revision for writing but the
        # background write has not landed yet. Without it a second save before
        # the first lands snapshots and rewrites the identical matrix.
        self._snapshotted_vector_revision: Optional[int] = None

        # Durable append-only mutation history sidecar. Only enabled for the
        # file-backed standalone backend, which owns a concrete json_path next
        # to which the history NDJSON lives. Non-file backends own their own
        # persistence and history strategy, so we leave this None there.
        self._history_max_events = history_max_events
        self._history_max_age_days = history_max_age_days
        self._history_compaction_interval = history_compaction_interval
        self._history_store = self._init_history_store()

        # Thread lock for in-memory data structure protection
        # RLock allows same thread to acquire lock multiple times (reentrant)
        self._lock = threading.RLock()
        # Set, under _lock, once this instance is being torn down. A backend
        # promises not to call the listener after stop returns, but nothing
        # here can make it keep that promise; this is what stops a late
        # report from refreshing a model nobody owns any more.
        self._shut_down = False

        # Executor for background I/O operations (saving to disk)
        # Using max_workers=1 to ensure sequential writes
        # Identified so a change reported on it - which would be a report
        # from inside the write it is running - is refused rather than left
        # to wait on the queue this thread *is*.
        self._writer_thread_id: Optional[int] = None
        self._io_executor = ThreadPoolExecutor(
            max_workers=1, initializer=self._mark_writer_thread
        )
        # Set when an entity write or a whole-graph write failed: the
        # backend's image then lacks what memory has. Consumed on the
        # caller's thread - by the next write, flush() or shutdown - which
        # re-issues the whole graph. The writer never takes _lock itself:
        # load() and save() wait on the queue while holding it, so a writer
        # that did would deadlock the process.
        self._resync_pending = False
        # The most recent write of any kind - incremental or whole-graph.
        # Writes go to a background executor, so `self.nodes` is ahead of the
        # store between a mutation and its write landing. A traversal answered
        # BY the store in that window would not see the caller's own write -
        # the two engines answering different questions rather than
        # disagreeing. This is what get_related_nodes checks before it uses
        # the store, and it has to cover every route a write can take.
        self._last_write: Optional["Future[None]"] = None

        # The VectorStore owns the vectors in memory; GraphStorage persists them
        # through the embedding sidecar (see _load_embeddings / _serialize_nodes).
        self.vector_store = VectorStore()

        self.graph = (
            nx.MultiDiGraph()
        )  # MultiDiGraph allows multiple edges between same nodes
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.edges: Dict[str, Edge] = {}  # edge_id -> Edge

        # Bumped once, atomically, every time `replace_all_nodes_and_edges`
        # swaps in a whole new graph. A caller that kicks off slow background
        # work against "the graph as it stood when I started" (the import
        # worker's embedding generation) captures this value first and
        # compares it again before writing anything back, so work computed
        # against a graph a later replace has already superseded is detected
        # and discarded rather than silently corrupting the live index. See
        # `generation` and `commit_generation_embeddings` below.
        self._generation: int = 0

        # Cache for searchable text to speed up search_nodes
        # Keyed by node id; the value is everything matching and ranking read
        # off that node, lowered once. It was the flat searchable string alone
        # until the scorer's per-query re-lowering showed up as a third of a
        # large query's cost.
        self._searchable_text_cache = storage_search.LexicalIndex()

        # Cache: node_type_key -> "typeName label1 label2 ..." (lowercased)
        self._type_searchable_text: Dict[str, str] = {}
        self._build_type_searchable_text()

        self.graph_metadata: Dict[str, Any] = {
            "version": "1.0",
            "graph_name": self._default_graph_name(),
        }

        # Event system (initialized lazily via setup_events())
        self._event_dispatcher: Optional["EventDispatcher"] = None
        self._delivery_worker: Optional["DeliveryWorker"] = None
        self._events_enabled = False
        self._system_listeners: List[Callable[["Event"], None]] = []

        # Listening starts BEFORE the load, and the gate keeps the seam's
        # promise that nothing is reported against a model that does not exist
        # yet: it holds what arrives until the load has returned. Starting
        # after the load instead leaves a gap in which another instance's
        # write is announced to nobody and never replayed - see _BootGate.
        boot_gate: Optional[_BootGate] = None
        if self._backend_capabilities.change_notification:
            boot_gate = _BootGate(self.apply_external_change)
            self._persistence_backend.start_change_notification(boot_gate)

        try:
            self.load()
            if boot_gate is not None:
                boot_gate.open()
            self._maybe_backfill_missing_embeddings_async()
            # Last, so no failure path is left holding a thread already
            # running: a model load cannot be cancelled once it has begun.
            self.vector_store.preload_model()
        except BaseException:
            # The replay is inside the guard, not after it. A raise out of
            # `open()` leaves the rest of the buffer undelivered AND the gate
            # shut, so every later report from the still-live backend thread
            # would buffer into an object nobody will ever open - a worse leak
            # than the one this guard was written for. It is also the one
            # delivery the backend's own `except` around the listener does not
            # cover, because the backend is no longer on the stack.
            #
            # Failing construction is the honest outcome: an instance that
            # could not apply what it was told while loading would otherwise
            # serve a graph with silent gaps in it.
            #
            # The listener stops first: it is the one that can still call
            # into this half-built object. The writer goes whatever that
            # does, or its thread outlives an instance nothing can reach.
            with self._lock:
                self._shut_down = True
            try:
                if boot_gate is not None:
                    self._persistence_backend.stop_change_notification()
            finally:
                self._io_executor.shutdown(wait=False)
            raise

    def _mark_writer_thread(self) -> None:
        self._writer_thread_id = threading.get_ident()

    def _default_graph_name(self) -> str:
        """Return the backend-specific default graph name."""
        return self._persistence_backend.default_graph_name()

    def _persistence_destination(self) -> str:
        """Describe where the graph actually lives, for the log lines.

        Only the file-backed backend owns a concrete path — `self.json_path`
        is a synthetic default for every other backend (see its assignment
        in `__init__`), so naming it there would claim a save landed in
        graph.json when it went to PostgreSQL, or nowhere on disk at all.
        The load path reads the same store the save path writes, so both
        describe it through this one helper.
        """
        if isinstance(self._persistence_backend, FileGraphPersistenceBackend):
            return str(self.json_path)
        return (
            f"the {type(self._persistence_backend).__name__} backend "
            f"(graph '{self._default_graph_name()}')"
        )

    def _init_embedding_sidecar(
        self, embeddings_path: Optional[str]
    ) -> Optional[FileEmbeddingSidecar]:
        """Create the vector sidecar for file-backed standalone mode."""
        if not isinstance(self._persistence_backend, FileGraphPersistenceBackend):
            return None
        derived = self.json_path.with_name(self.json_path.stem + ".embeddings.bin")
        if not embeddings_path:
            return FileEmbeddingSidecar(derived, owns_path=True)

        path = Path(embeddings_path)
        # The graph file is the one collision the refusal in save() cannot
        # catch, because the sidecar is written first and the graph write then
        # lands on top of it — and on the bootstrap path, where the graph file
        # is momentarily absent, the sidecar write finds nothing to refuse.
        # The migration script rejects the same shape outright for the same
        # reason: the second write silently destroys the first.
        if path.resolve() == self.json_path.resolve():
            logger.warning(
                f"EMBEDDINGS_FILE names the graph file itself "
                f"({path}); using {derived} instead"
            )
            return FileEmbeddingSidecar(derived, owns_path=True)
        # Ownership is a property of the NAME, not of whether anyone passed one.
        # start-dev.sh exports EMBEDDINGS_FILE on every run, set to exactly this
        # derived path, and .env.example documents the same pairing - so asking
        # "was a value supplied" put the app's own file in the mode meant for an
        # operator's, and the self-heal never fired where it actually matters.
        # Comparing resolved paths also covers a relative value and a symlink
        # that land on the same file.
        return FileEmbeddingSidecar(path, owns_path=path.resolve() == derived.resolve())

    @property
    def vectors_persisted(self) -> bool:
        """Whether the vector index as it stands has been written to disk.

        A sidecar write failure is deliberately not fatal to a graph save, so
        callers that report success to an operator — or act on it, like the
        pickle migration renaming its source — must ask rather than assume.
        """
        if self._embedding_sidecar is None:
            return True
        return self._persisted_vector_revision == self.vector_store.revision

    @property
    def embeddings_path(self) -> Optional[Path]:
        """Where node vectors are persisted, or None for a backend with no sidecar."""
        if self._embedding_sidecar is None:
            return None
        return self._embedding_sidecar.path

    @property
    def layer_store(self) -> Optional[GraphPersistenceBackend]:
        """The backend, as the layer store of staged sessions, when it declares
        `staged_sessions` (docs/SESSION_OVERLAY_CONTRACT.md §7); else None."""
        if self._backend_capabilities.staged_sessions:
            return self._persistence_backend
        return None

    @property
    def staged_records_check(self) -> Optional[Callable[[], bool]]:
        """Asks a shared backend whether its store holds staged-session records,
        where it can tell; None for a backend no other instance writes."""
        return getattr(self._persistence_backend, "holds_staged_sessions", None)

    def _init_history_store(self) -> Optional["GraphHistoryStore"]:
        """Create the append-only history sidecar for file-backed standalone mode."""
        if not isinstance(self._persistence_backend, FileGraphPersistenceBackend):
            return None
        from .history_store import GraphHistoryStore

        history_path = self.json_path.with_name(self.json_path.stem + ".history.ndjson")
        return GraphHistoryStore(
            history_path,
            max_events=self._history_max_events,
            max_age_days=self._history_max_age_days,
            compaction_interval=self._history_compaction_interval,
        )

    def _build_type_searchable_text(self) -> None:
        """Build a lookup of node type -> searchable text including localized labels."""
        try:
            from backend.config.config_loader import get_schema

            schema = get_schema()
            for type_name, type_config in schema.get("node_types", {}).items():
                labels = type_config.get("labels", {})
                label_values = " ".join(labels.values()) if labels else ""
                self._type_searchable_text[type_name] = (
                    f"{type_name} {label_values}".lower().strip()
                )
        except Exception:
            # Config not available; type matching will use type name only
            pass

    def _build_match_fields(self, node: "Node") -> storage_search.MatchFields:
        """Prepare everything search matches and ranks a node on, lowered once.

        Renamed from `_build_searchable_text`, which returned only the flat
        string: keeping that name while widening what it returns is the quiet
        kind of break - a reader would go on believing the cache holds a str.
        """
        return storage_search.build_match_fields(node, self._type_searchable_text)

    def add_system_listener(self, listener: Callable[["Event"], None]) -> None:
        """
        Add a system-level event listener.
        This listener receives all events directly, bypassing filters/subscriptions.
        Used for internal system components like the Agent Registry.
        """
        with self._lock:
            self._system_listeners.append(listener)

    def setup_events(
        self,
        enabled: bool = True,
        max_attempts: int = 3,
        backoff_times: Optional[List[float]] = None,
    ) -> None:
        """
        Initialize the event system for webhook delivery.

        This must be called after the graph is loaded to enable event delivery.
        Events are dispatched to EventSubscription nodes in the graph.

        Args:
            enabled: Whether to enable event delivery
            max_attempts: Maximum delivery attempts per event
            backoff_times: Wait times between retries (seconds)
        """
        if not enabled:
            self._events_enabled = False
            return

        # Import here to avoid circular imports
        from .events.dispatcher import EventDispatcher
        from .events.delivery import DeliveryWorker

        # Create delivery worker
        self._delivery_worker = DeliveryWorker(
            max_attempts=max_attempts,
            backoff_times=backoff_times,
        )
        self._delivery_worker.start()

        # Create dispatcher with delivery callback
        self._event_dispatcher = EventDispatcher(
            storage=self,
            on_deliver=self._delivery_worker.enqueue,
        )

        self._events_enabled = True
        logger.info(f"Event system initialized with max_attempts={max_attempts}")

    def set_agent_delivery_callback(
        self,
        callback: Callable[["Event", str], bool],
    ) -> None:
        """
        Set the callback for agent event delivery.

        This allows the agent registry to receive events for agent-linked
        subscriptions directly, bypassing webhook delivery.

        Args:
            callback: Function that receives (event, subscription_id) and
                     returns True if handled by an agent, False otherwise.
        """
        if self._event_dispatcher:
            self._event_dispatcher.set_agent_delivery_callback(callback)

    def shutdown_events(self) -> None:
        """Shutdown the event system and I/O executor gracefully."""
        # First, so nothing arrives to refresh a model that is being torn down:
        # the flag refuses a report the backend makes after this point, and
        # taking _lock to set it waits out a refresh already under way.
        with self._lock:
            self._shut_down = True
        if self._backend_capabilities.change_notification:
            try:
                self._persistence_backend.stop_change_notification()
            except Exception as exc:
                logger.warning(f"stopping change notification failed: {exc}")

        if self._delivery_worker:
            self._delivery_worker.stop(wait=True)
            self._delivery_worker = None

        self._event_dispatcher = None
        self._events_enabled = False

        # Let every queued write land, heal a failed one, fold deferred
        # writes into the snapshot, then shut down the I/O executor and wait
        # for everything queued before it.
        checkpoint = None
        try:
            self._io_executor.submit(lambda: None).result()
            self._heal_if_needed()
            if self._backend_capabilities.incremental_writes:
                checkpoint = self._io_executor.submit(
                    self._persistence_backend.checkpoint
                )
        except RuntimeError:
            pass  # already shut down: nothing can be queued, nothing is pending
        self._io_executor.shutdown(wait=True)
        # A write that failed after the heal above ran - or the heal itself,
        # which re-raises the flag - has nowhere to queue a retry; write the
        # graph on this thread instead, or it is lost.
        if self._resync_pending:
            self._save_now()
        # Data is safe either way - the journal still holds the difference and
        # is replayed on the next start - but an operator reading "clean
        # shutdown means graph.json is complete" must be told when it is not.
        if checkpoint is not None and checkpoint.exception() is not None:
            logger.warning(
                f"graph checkpoint at shutdown failed: {checkpoint.exception()}"
            )

    def _save_now(self) -> None:
        """A whole-graph write on the calling thread, for after the executor
        is gone. Reports rather than raises: this runs from a shutdown hook."""
        with self._lock:
            vectors, vector_revision = self._take_vector_snapshot()
            data = self._snapshot_data()
            node_count, edge_count = len(self.nodes), len(self.edges)
            self._resync_pending = False
        try:
            self._do_save_to_disk(
                data, node_count, edge_count, vectors, vector_revision
            )
        except Exception as exc:
            # _do_save_to_disk has re-raised the flag; it stays raised, which
            # is the honest state - the journal still holds what it can.
            logger.warning(f"graph write at shutdown failed: {exc}")

    def _emit_event(
        self,
        event_type: EventType,
        entity_kind: EntityKind,
        entity_id: str,
        entity_type: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        context: Optional[EventContext] = None,
    ) -> None:
        """Emit a graph mutation event.  Delegates to storage_events."""
        storage_events.emit_event(
            self._history_store,
            self._system_listeners,
            self._events_enabled,
            self._event_dispatcher,
            event_type,
            entity_kind,
            entity_id,
            entity_type,
            before=before,
            after=after,
            context=context,
        )

    def emit_federated_node_event(
        self,
        operation: str,
        node_before: Optional[Node] = None,
        node_after: Optional[Node] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache changes so subscriptions can react."""
        storage_events.emit_federated_node_event(
            self._emit_event, operation, node_before, node_after, event_origin
        )

    def emit_federated_edge_event(
        self,
        operation: str,
        edge_before: Optional[Edge] = None,
        edge_after: Optional[Edge] = None,
        event_origin: str = "federation-sync",
    ) -> None:
        """Emit an event for federated cache edge changes."""
        storage_events.emit_federated_edge_event(
            self._emit_event, operation, edge_before, edge_after, event_origin
        )

    def get_recent_history(
        self, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return recent graph mutation history, newest first.  Delegates to storage_history."""
        return storage_history.get_recent_history(self._history_store, limit, offset)

    def get_node_history(
        self, node_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return mutation history for a single node id, newest first."""
        return storage_history.get_node_history(
            self._history_store, node_id, limit, offset
        )

    def get_edge_history(
        self, edge_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return mutation history for a single edge id, newest first."""
        return storage_history.get_edge_history(
            self._history_store, edge_id, limit, offset
        )

    def load(self, *, bootstrap_if_missing: bool = True) -> None:
        """
        Load graph from the configured persistence backend.

        Thread-safe: Uses lock for in-memory updates.

        `bootstrap_if_missing` is what makes a first start work: a store that
        does not exist yet is created from whatever is in memory. A refresh
        passes False, because there the store belongs to somebody else - it
        may be missing only for a moment, mid-restore, and writing this
        instance's graph over it is the one thing a refresh must never do.
        """
        with self._lock:
            if not self._persistence_backend.exists():
                if not bootstrap_if_missing:
                    logger.warning(
                        f"cannot refresh from {self._persistence_destination()}: it is "
                        f"not there. Serving the graph in memory unchanged."
                    )
                    return
                logger.info(
                    f"No graph data found in {self._persistence_destination()}, "
                    f"creating new empty graph"
                )
                # An empty index has nothing to contribute, and marking it
                # persisted is what keeps this bootstrap write from putting an
                # EMPTY sidecar over a real one — a graph file that is merely
                # missing for now (wrong GRAPH_FILE, a restore in progress) must
                # not destroy the only copy of the vectors.
                #
                # A populated index is the opposite case. load() is also the body
                # of reload(), which re-saves the in-memory graph; if the sidecar
                # has gone too, those vectors are now the only copy anywhere and
                # this save is the one chance to write them. The revision alone
                # cannot tell: it still matches the last successful write, of a
                # file that no longer exists. Both markers have to go — the
                # snapshot one records that this revision was already handed to
                # a save, and on its own it keeps needs_write false forever.
                if self.vector_store.embeddings:
                    self._persisted_vector_revision = None
                    self._snapshotted_vector_revision = None
                else:
                    self._persisted_vector_revision = self.vector_store.revision
                # The new store's first reservation rides on its first write,
                # so ordinary writes never wait for one (see _reserve_ahead).
                reserving = (
                    self._reserves_revisions
                    and self._next_revision >= self._revision_reservation
                )
                if reserving:
                    ceiling = self._next_revision + self.REVISION_RESERVATION_BLOCK
                    self.graph_metadata["revision_reservation"] = ceiling
                # Wait for the initial file write to complete so callers can
                # immediately open the file (e.g. creating a second storage instance).
                self.save().result()
                if reserving:
                    self._revision_reservation = ceiling
                return

            try:
                data = self._persistence_backend.load_graph_data()

                metadata = data.get("metadata") if isinstance(data, dict) else None
                if isinstance(metadata, dict):
                    graph_metadata = {
                        "version": metadata.get("version", "1.0"),
                        "graph_name": metadata.get("graph_name")
                        or self._default_graph_name(),
                        **{
                            k: v
                            for k, v in metadata.items()
                            if k not in {"version", "graph_name"}
                        },
                    }
                else:
                    graph_metadata = {
                        "version": "1.0",
                        "graph_name": self._default_graph_name(),
                    }

                # Everything is parsed beside the live model and swapped in
                # only once all of it parsed. Clearing first and refilling
                # would mean an entity this build cannot read leaves the graph
                # truncated - and a refresh, which cannot raise at its caller,
                # would report that as merely being behind the store.
                nodes: Dict[str, Node] = {}
                edges: Dict[str, Edge] = {}
                searchable: Dict[str, storage_search.MatchFields] = {}

                for node_data in data.get("nodes", []):
                    node = Node.from_dict(node_data)
                    nodes[node.id] = node
                    searchable[node.id] = self._build_match_fields(node)

                for edge_data in data.get("edges", []):
                    edge = Edge.from_dict(edge_data)
                    if edge.source not in nodes or edge.target not in nodes:
                        # add_edge would invent the missing endpoint as a node
                        # with no data, matching the refresh-path guard here.
                        absent = (
                            edge.source if edge.source not in nodes else edge.target
                        )
                        logger.warning(
                            f"ignoring stored edge {edge.id}: "
                            f"endpoint {absent} is not present"
                        )
                        continue
                    edges[edge.id] = edge

                # Past here nothing can fail: the containers are replaced in
                # place, so anything holding a reference to one still sees the
                # graph this instance serves.
                self.graph_metadata = graph_metadata
                # `graph_generation` is this instance's only durable record of
                # `_generation` (see the property below): it rides in
                # `graph_metadata` exactly like every other extra key here -
                # written by `replace_all_nodes_and_edges`, preserved
                # unchanged by every other save via `_snapshot_data`'s
                # `**self.graph_metadata` spread, and read back here on every
                # load, including a real process restart. A file with no such
                # key predates this field or was never imported into, and
                # both mean the same thing: no import job anywhere could be
                # carrying a generation to collide with, so 0 is the correct
                # default rather than a guess.
                raw_generation = graph_metadata.get("graph_generation", 0)
                try:
                    self._generation = int(raw_generation)
                except (TypeError, ValueError):
                    logger.warning(
                        f"ignoring non-integer graph_generation "
                        f"{raw_generation!r} in metadata; defaulting to 0"
                    )
                    self._generation = 0
                # Order matters three ways here, and two of them fight.
                #
                # The index must never be MISSING an id `nodes` holds, or the
                # search's candidate path silently drops that node. And it must
                # iterate in step with `nodes`, or the `-index` tie-break puts
                # equal-scoring results in a different order than the walk
                # does - with a limit, a different set of results entirely.
                #
                # Unioning the new records in and pruning afterwards keeps the
                # first and breaks the second: `dict.update` leaves an existing
                # key in its existing slot, so every id that survives a reload
                # keeps its OLD position while `nodes` is rebuilt in the
                # store's order.
                #
                # Emptying the index first keeps both. While it is empty it is
                # shorter than `nodes`, which is exactly what the size test in
                # `search_nodes` declines on, so the walk answers for that
                # window and reads the live dict. Then it refills in
                # `searchable` order - the same order `nodes` was just built
                # in. At no point are the two the same size with different ids.
                #
                # The refill only lands in that order because the walk does not
                # write records back (see `search_nodes`). A walk in this window
                # would otherwise insert the ids that passed its filters, in its
                # own order, and `update` would leave them there.
                self._searchable_text_cache.clear()
                self.nodes.clear()
                self.nodes.update(nodes)
                self.edges.clear()
                self.edges.update(edges)
                self._searchable_text_cache.update(searchable)

                self.graph.clear()
                for node in nodes.values():
                    self.graph.add_node(node.id, data=node)
                for edge in edges.values():
                    self.graph.add_edge(
                        edge.source, edge.target, key=edge.id, data=edge
                    )

                self._raise_revision_counter(
                    max(
                        [n.revision for n in nodes.values()]
                        + [e.revision for e in edges.values()]
                        + [0]
                    )
                )
                if bootstrap_if_missing:
                    # Not on a refresh, which must never write.
                    self._reserve_ahead()

                self._load_embeddings()

                logger.info(
                    f"Loaded {len(self.nodes)} nodes and {len(self.edges)} edges from "
                    f"{self._persistence_destination()}"
                )

            except Exception as e:
                logger.error(f"Error loading graph: {e}")
                raise

    # -- revisions (docs/SESSION_OVERLAY_CONTRACT.md §4) ---------------------

    def _raise_revision_counter(self, highest: int) -> None:
        """Bring a single-writer counter past what the store holds.

        Callers must hold _lock. Never lowered: the counter starts at the
        persisted reservation - values below it may have been handed out
        before a crash - or one past the highest revision held, whichever is
        higher, and stays where it is if it is already beyond both.
        """
        if self._store_assigns_revisions:
            return
        raw = (self.graph_metadata or {}).get("revision_reservation", 0)
        try:
            reserved = int(raw)
        except (TypeError, ValueError):
            logger.warning(f"ignoring non-integer revision_reservation {raw!r}")
            reserved = 0
        self._revision_reservation = max(self._revision_reservation, reserved)
        self._next_revision = max(
            self._next_revision, self._revision_reservation, highest + 1, 1
        )
        if self._revision_reservation > reserved:
            # A reload must not hand the next snapshot a lower ceiling than
            # the one already made durable.
            self.graph_metadata["revision_reservation"] = self._revision_reservation

    def _reserve_ahead(self) -> None:
        """Reserve a block at load, so ordinary writes never wait for it.

        Callers must hold _lock. Without this the first write after every
        start would pay for a durable reservation - a checkpoint of the whole
        file - on its own latency. Best effort: a failure here is reported
        and the first write reserves instead.
        """
        if not self._reserves_revisions:
            return
        if self._next_revision < self._revision_reservation:
            return
        try:
            self._reserve_revisions(
                self._next_revision + self.REVISION_RESERVATION_BLOCK
            )
        except Exception as exc:
            logger.warning(
                f"could not reserve revisions at load ({exc}); the first "
                f"write will reserve them instead"
            )

    def _take_revisions(self, count: int) -> int:
        """Hand out `count` consecutive revisions; return the first.

        Single-writer stores only; callers must hold _lock. Before a value at
        or above the durable reservation leaves here, the reservation is
        raised past it in a durable write of its own and waited for, so a
        crash afterwards skips values rather than repeating one (§4.2).
        """
        first = self._next_revision
        if count <= 0:
            return first
        last = first + count - 1
        if self._reserves_revisions and last >= self._revision_reservation:
            self._reserve_revisions(last + 1 + self.REVISION_RESERVATION_BLOCK)
        self._next_revision = last + 1
        return first

    def _reserve_revisions(self, ceiling: int) -> None:
        """Durably raise the reservation to `ceiling`. Callers hold _lock.

        Through the write queue, so it lands after every write already
        issued; waited for, which is safe under the lock because queued work
        never takes it. A backend with no reservation of its own gets a
        whole-graph save, which carries the reservation in its metadata.
        """
        previous = self.graph_metadata.get("revision_reservation")
        self.graph_metadata["revision_reservation"] = ceiling
        try:
            reserve = getattr(self._persistence_backend, "reserve_revisions", None)
            if callable(reserve) and not self._resync_pending:
                self._io_executor.submit(reserve, ceiling).result()
            else:
                self.save().result()
        except Exception:
            if previous is None:
                self.graph_metadata.pop("revision_reservation", None)
            else:
                self.graph_metadata["revision_reservation"] = previous
            raise
        self._revision_reservation = ceiling

    @staticmethod
    def _check_expectation(
        kind: str, entity_id: str, entity: Any, expected: Any
    ) -> None:
        """Refuse a write whose expectation this instance's model refutes.

        `expected` is an int (`expected_revision`) or EXPECT_ABSENT. An
        absent entity never matches a revision, so an update or delete
        expecting one is refused with no current revision.
        """
        current = None if entity is None else entity.revision
        if expected is EXPECT_ABSENT:
            if current is not None:
                raise EntityRevisionConflict(kind, entity_id, None, current)
            return
        if current is None or current != expected:
            raise EntityRevisionConflict(kind, entity_id, expected, current)

    def _capture(self, node_ids=(), edge_ids=()) -> _Undo:
        """The current state of the given entities, before a write changes
        them. Callers must hold _lock."""
        undo = _Undo()
        for node_id in node_ids:
            node = self.nodes.get(node_id)
            undo.nodes[node_id] = None if node is None else node.to_dict()
            undo.vectors[node_id] = (
                None if node is None else self.vector_store.get_vector_list(node_id)
            )
        for edge_id in edge_ids:
            edge = self.edges.get(edge_id)
            undo.edges[edge_id] = None if edge is None else edge.to_dict()
        return undo

    def _undo(self, undo: _Undo) -> None:
        """Put memory back to `undo`, silently. Callers must hold _lock.

        No event: the write being undone has not fired one. Edges that must
        end absent go first and edges that must come back go last, so no
        edge is ever installed without both endpoints.
        """
        for edge_id, before in undo.edges.items():
            if before is None:
                edge = self.edges.pop(edge_id, None)
                if edge is not None and self.graph.has_edge(
                    edge.source, edge.target, key=edge_id
                ):
                    self.graph.remove_edge(edge.source, edge.target, key=edge_id)
        touched: Dict[str, Tuple[Optional[Node], Any]] = {}
        for node_id, before in undo.nodes.items():
            if before is None:
                self.nodes.pop(node_id, None)
                if self.graph.has_node(node_id):
                    self.graph.remove_node(node_id)
                self._searchable_text_cache.pop(node_id, None)
                touched[node_id] = (None, None)
                continue
            node = Node.from_dict(dict(before))
            node.embedding = None
            # Index before `nodes`, as everywhere that adds.
            self._searchable_text_cache[node_id] = self._build_match_fields(node)
            self.nodes[node_id] = node
            self.graph.add_node(node_id, data=node)
            touched[node_id] = (node, undo.vectors.get(node_id))
        for edge_id, before in undo.edges.items():
            if before is None:
                continue
            edge = Edge.from_dict(dict(before))
            existing = self.edges.get(edge_id)
            if existing is not None and self.graph.has_edge(
                existing.source, existing.target, key=edge_id
            ):
                self.graph.remove_edge(existing.source, existing.target, key=edge_id)
            self.edges[edge_id] = edge
            self.graph.add_edge(edge.source, edge.target, key=edge_id, data=edge)
        try:
            self._settle_vector_index(touched)
        except Exception as exc:
            logger.warning(f"could not restore vectors after an undone write: {exc}")

    def _commit(
        self,
        changes: List[Tuple[str, str, Any]],
        undo: _Undo,
        expectations: Optional[Dict[Tuple[str, str], Any]] = None,
    ) -> None:
        """Stamp revisions on a write already applied to memory, and persist it.

        Callers must hold _lock and must not have fired the write's events
        yet. `changes` lists ("node" | "edge", "upsert" | "delete", entity or
        id) in the order the store applies them; `undo` is the state captured
        before memory changed; `expectations` maps (kind, id) to the
        expected revision or EXPECT_ABSENT, already checked against memory.

        A single-writer store takes revisions from this instance's counter and
        keeps today's fire-and-forget write, except that a write carrying an
        expectation waits for the store. A store that enforces revisions is
        always waited for: it assigns the revisions, may refuse the
        expectation (memory is undone, EntityRevisionConflict raised), and a
        failure is resolved on the write's own evidence (§4.4).
        """
        expectations = expectations or {}
        upserted = [entity for _, action, entity in changes if action == "upsert"]

        if not self._store_assigns_revisions:
            # An empty write still goes to _persist, as it always has: on a
            # snapshot backend that is a save, which callers rely on.
            try:
                first = self._take_revisions(len(upserted))
            except Exception as exc:
                self._undo(undo)
                logger.warning(f"could not reserve revisions: {exc}")
                raise GraphWriteFailed(
                    "could not reserve revisions for the write"
                ) from exc
            for offset, entity in enumerate(upserted):
                entity.revision = first + offset
            future = self._persist(self._operations(changes, expectations))
            if expectations:
                self._await_landing(future)
            return

        if not changes:
            return
        operations = self._operations(changes, expectations)
        token = uuid.uuid4().hex
        future = self._io_executor.submit(
            self._do_apply, tuple(operations), token=token
        )
        self._last_write = future
        try:
            revisions = future.result()
        except EntityRevisionConflict:
            self._undo(undo)
            raise
        except ExternalChangeRefused:
            raise
        except Exception as exc:
            if refused_outright(exc):
                # Refused inside the transaction: nothing landed and nothing
                # is in flight, so there is no evidence worth waiting for.
                logger.warning(f"the store refused a write: {exc}")
                self._undo(undo)
                raise GraphWriteFailed("the store refused the write") from exc
            revisions = self._resolve_failed_write(operations, undo, token, exc)
        stamps = iter(revisions or [])
        for (_, action, entity), revision in zip(changes, stamps):
            if action == "upsert" and revision is not None:
                entity.revision = revision

    def _operations(
        self,
        changes: List[Tuple[str, str, Any]],
        expectations: Dict[Tuple[str, str], Any],
    ) -> List[EntityOperation]:
        """The entity operations for `changes`. Callers must hold _lock."""
        operations: List[EntityOperation] = []
        for kind, action, entity in changes:
            entity_id = entity if action == "delete" else entity.id
            expected = expectations.get((kind, entity_id))
            if action == "delete":
                payload = None
            elif kind == "node":
                payload = self._serialize_node(entity)
            else:
                payload = entity.to_dict()
            operations.append(
                EntityOperation(
                    kind,  # type: ignore[arg-type]
                    action,  # type: ignore[arg-type]
                    entity_id,
                    payload,
                    expected_revision=(
                        expected
                        if expected is not None and expected is not EXPECT_ABSENT
                        else None
                    ),
                    expect_absent=expected is EXPECT_ABSENT,
                    endpoint_types=self._endpoint_types(kind, action, payload),
                )
            )
        return operations

    def _endpoint_types(
        self, kind: str, action: str, payload: Optional[Dict[str, Any]]
    ) -> Tuple[Tuple[str, str], ...]:
        """What an edge upsert's endpoints are in memory, where its relationship
        type was judged. Callers must hold _lock."""
        if kind != "edge" or action != "upsert" or not payload:
            return ()
        ends = {payload.get("source"), payload.get("target")}
        return tuple(
            sorted(
                (end, self.nodes[end].type_str)
                for end in ends
                if end is not None and end in self.nodes
            )
        )

    def _await_landing(self, future: "Future[Any]") -> None:
        """Wait for a single-writer write with an expectation (§4.4).

        A write that failed is still in memory, and is re-issued with the
        whole graph exactly as an unawaited one would be - here, before
        returning, so the caller is told only once it has landed.
        """
        try:
            future.result()
            return
        except Exception:
            heal = self._heal_if_needed()
            if heal is None:
                raise
        heal.result()

    def _resolve_failed_write(
        self,
        operations: List[EntityOperation],
        undo: _Undo,
        token: str,
        failure: BaseException,
    ) -> List[Optional[int]]:
        """Decide a failed shared-store write on its own evidence (§4.4).

        Callers must hold _lock. Returns the stored revisions when the write
        landed after all; otherwise raises GraphWriteFailed (memory undone)
        or GraphWriteOutcomeUnknown (graph re-read from the store).
        """
        keys = [(op.kind, op.entity_id) for op in operations]
        upsert_keys = [
            (op.kind, op.entity_id) for op in operations if op.action == "upsert"
        ]
        before: Dict[Tuple[str, str], Optional[int]] = {}
        for kind, entity_id in keys:
            prior = (undo.nodes if kind == "node" else undo.edges).get(entity_id)
            before[(kind, entity_id)] = (
                None if prior is None else int(prior.get("revision") or 0)
            )

        def unchanged(evidence) -> bool:
            for key in keys:
                now = evidence.get(key)
                if before[key] is None:
                    if now is not None:
                        return False
                elif now is None or now[0] != before[key]:
                    return False
            return True

        readings = []
        for attempt in range(2):
            if attempt:
                time.sleep(self.WRITE_RESOLVE_DELAY_SECONDS)
            try:
                evidence = self._persistence_backend.read_write_evidence(keys)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning(f"could not read back a failed write: {exc}")
                readings = None
                break
            if upsert_keys and all(
                evidence.get(key) is not None and evidence[key][1] == token
                for key in upsert_keys
            ):
                # Every entity it wrote carries its token, and the batch is
                # atomic: it committed, only the acknowledgement was lost.
                logger.warning(
                    f"a write reported a failure but landed ({failure}); completing it"
                )
                return [
                    evidence[(op.kind, op.entity_id)][0]
                    if op.action == "upsert"
                    else None
                    for op in operations
                ]
            readings.append(evidence)

        # The store's own error text stays in the log: it can name hosts,
        # roles and schemas, and the caller is owed only the outcome.
        logger.warning(f"a write failed: {failure}")
        if readings and all(unchanged(r) for r in readings):
            self._undo(undo)
            raise GraphWriteFailed("the write did not land") from failure

        # Neither confirmed nor ruled out. Memory may hold a write the store
        # does not, or lack one it does; the store is the authority, so the
        # graph is re-read from it. A delete always ends here: no token
        # survives on a row that is gone.
        try:
            self.load(bootstrap_if_missing=False)
        except Exception as exc:
            logger.warning(
                f"could not re-read the graph after a write of unknown "
                f"outcome ({exc}); memory may differ from the store until "
                f"the next reported change"
            )
        raise GraphWriteOutcomeUnknown(
            "could not confirm whether the write landed; re-read before retrying"
        ) from failure

    def save(self) -> "Future[None]":
        """
        Save graph through the configured persistence backend.

        Captures graph state while holding the lock, then offloads the actual
        file I/O to a background thread to prevent blocking the event loop.
        Returns the Future representing the background write — callers that
        must know when the write completes can call .result() on it.

        Thread-safe: Uses lock for reading in-memory data.
        """
        with self._lock:
            vectors, vector_revision = self._take_vector_snapshot()
            if vectors is not None:
                # Migration is the one case where what the node payload may
                # contain depends on whether the sidecar write has landed:
                # those ids keep their inline copy until it has, because
                # graph.json is still their only durable home. Landing it
                # here — before serialising, rather than on the background
                # thread afterwards — is what lets THIS save drop them.
                #
                # Deferring it costs a whole extra save, which a long-lived app
                # eventually makes but a one-shot caller never does: both
                # maintenance scripts save once and exit, so they reported a
                # successful migration while leaving every vector in graph.json
                # and the file the size it always was.
                #
                # Only ids this write actually covers count. A vector the
                # index refused, or a deleted node's, can never appear in a
                # later matrix, so gating on the map being non-empty would
                # route EVERY subsequent save down this path and turn a
                # one-time migration into permanent blocking I/O on the
                # caller's thread.
                if self._inline_fallback.keys() & vectors.keys():
                    # Ordered through the same single-worker queue as every
                    # other write, so a save already in flight cannot land on
                    # top of this one — waiting on the result is what keeps it
                    # before _serialize_nodes. Safe to wait while holding the
                    # lock: the queued work never takes it.
                    self._io_executor.submit(
                        self._persist_vectors, vectors, vector_revision
                    ).result()
                    # Either outcome ends this save's sidecar attempt, so the
                    # background write must not repeat it. A failure has already
                    # cleared the snapshot marker, and that is what leaves the
                    # retry open for the next save.
                    vectors = None

            data = self._snapshot_data()
            node_count = len(self.nodes)
            edge_count = len(self.edges)
            # Cleared under the lock, with the capture: a write issued after
            # this point is issued after the clear, so its failure cannot be
            # erased by a snapshot that does not contain it.
            self._resync_pending = False

            # Submitted under the same lock as the capture, like every entity
            # write in _persist: the single-worker queue then lands snapshots
            # in capture order. Submitted after the release, two callers
            # could queue in the opposite order of their captures and the
            # older image would land last. The submit is cheap and the queued
            # work never takes the lock.
            #
            # Offload blocking I/O to background thread to avoid blocking event
            # loop. Returns the Future so callers that must wait (e.g. load())
            # can call .result().
            # Recorded here as well as in _persist. A snapshot save is a write
            # like any other as far as a reader is concerned, and _persist
            # routes here for four documented cases - so watching only the
            # incremental path left the guard open on exactly the writes that
            # had already gone wrong once.
            self._last_write = self._io_executor.submit(
                self._do_save_to_disk,
                data,
                node_count,
                edge_count,
                vectors,
                vector_revision,
            )
            return self._last_write

    def _snapshot_data(self) -> Dict[str, Any]:
        """The whole graph as a backend snapshot. Callers must hold _lock."""
        return {
            "nodes": self._serialize_nodes(),
            "edges": [edge.to_dict() for edge in self.edges.values()],
            "metadata": {
                **(self.graph_metadata or {}),
                "version": (self.graph_metadata or {}).get("version", "1.0"),
                "graph_name": (self.graph_metadata or {}).get(
                    "graph_name", self._default_graph_name()
                ),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _take_vector_snapshot(self) -> Tuple[Optional[Dict[str, Any]], int]:
        """The vectors the write being issued must carry to the sidecar, if any.

        Callers must hold _lock. Only when they have actually moved since the
        last successful sidecar write, and are not already in flight: an
        ordinary metadata edit changes no vector and must not rewrite the
        matrix. Returns (vectors or None, the revision they are).
        """
        vector_revision = self.vector_store.revision
        needs_write = (
            self._embedding_sidecar is not None
            and vector_revision != self._persisted_vector_revision
            and vector_revision != self._snapshotted_vector_revision
        )
        if not needs_write:
            return None, vector_revision
        # No filtering here: VectorStore keeps the index uniform, so what it
        # exports is always a matrix the sidecar accepts. An extra vote at
        # this boundary would only re-decide the dimension, and it decided it
        # by raw count — which favours whichever vectors are the more
        # numerous, i.e. the stale ones.
        vectors = self.vector_store.export_vectors()
        self._snapshotted_vector_revision = vector_revision
        return vectors, vector_revision

    def _migration_pending(self) -> bool:
        """Whether graph.json is still the only durable home of some vector.

        Callers must hold _lock. True while a vector read out of a pre-split
        graph file is in the index but not yet in the sidecar. Ids whose
        vector the index refused are in the fallback map too, and stay there;
        they are not a pending migration, or every write would be one.
        """
        if self._embedding_sidecar is None or not self._inline_fallback:
            return False
        # Gated on the sidecar actually needing a write, exactly as save() is.
        # Once a sidecar write lands it pops the ids it carried from the
        # fallback map, so after that the intersection below is empty on its
        # own; the gate matters in the window BEFORE it lands - a write
        # already queued behind this one, its vectors snapshotted but not yet
        # written - where the map still holds those ids and, without the
        # gate, every mutation issued meanwhile would be a whole-graph write.
        revision = self.vector_store.revision
        if revision in (
            self._persisted_vector_revision,
            self._snapshotted_vector_revision,
        ):
            return False
        return bool(self._inline_fallback.keys() & self.vector_store.embeddings.keys())

    def _serialize_node(self, node: "Node") -> Dict[str, Any]:
        """Serialize one node for persistence, routing its vector to the sidecar.

        Callers must hold _lock. This is the node's shape in a snapshot and in
        an upsert alike, so an incremental backend stores for a node exactly
        what a snapshot would have held for it. Backends without a sidecar keep
        the vector in the node payload, exactly as every backend did before the
        split, so a custom backend does not silently stop persisting them.
        """
        payload = node.to_dict()
        if self._embedding_sidecar is None:
            payload["embedding"] = self._vector_for_payload(node.id)
            return payload

        # Dropping the vector is what makes graph.json small, and it is safe
        # for every vector this process generated. It is NOT safe for one
        # read out of a pre-split graph.json before the sidecar has it:
        # there graph.json is the only durable copy, and the sidecar write
        # is allowed to fail without failing the graph write, so removing it
        # would destroy the vector with nothing written in its place.
        vector = (
            self._vector_for_payload(node.id)
            if node.id in self._inline_fallback
            else None
        )
        if vector is None:
            payload.pop("embedding", None)
        else:
            payload["embedding"] = vector
        return payload

    def _serialize_nodes(self) -> List[Dict[str, Any]]:
        """Serialize every node for a snapshot. Callers must hold _lock."""
        return [self._serialize_node(node) for node in self.nodes.values()]

    def _vector_for_payload(self, node_id: str) -> Optional[List[float]]:
        """The vector to write into a node's payload, or None.

        The live index vector whenever there is one — it is what the node means
        now — and otherwise the raw copy the node was loaded with. The index
        refusing a vector for its width, or evicting it when the model width
        changes, does not make the file it came from any less its only home.
        """
        vector = self.vector_store.get_vector_list(node_id)
        if vector is not None:
            return vector
        raw = self._inline_fallback.get(node_id)
        if raw is None:
            return None
        return raw.tolist() if hasattr(raw, "tolist") else list(raw)

    def _adopt_supplied_vectors(self, nodes) -> None:
        """Move vectors carried on node objects into the vector index.

        The serialized payload no longer carries `embedding`, so a vector that
        arrived on a node object is persisted nowhere unless it is adopted
        here. What happens next is the caller's: `add_nodes` generates over
        the whole batch afterwards, so generation wins there, while a refresh
        generates only where the store supplied nothing.

        The vectors come off the nodes only after the index export and load
        have returned: until then the node is their only copy, so a raise
        there must leave them where they were for the caller to retry or
        settle. A vector the index refuses for its width is still cleared.
        """
        nodes = list(nodes)
        self._adopt_vectors(
            {
                node.id: node.embedding
                for node in nodes
                if node.embedding is not None and len(node.embedding) > 0
            }
        )
        self._take_inline_vectors(nodes)

    def _adopt_vectors(
        self, supplied: Dict[str, Any], anchor: Optional[int] = None
    ) -> None:
        """Move supplied vectors, keyed by node id, into the index.

        Works on a dict rather than on nodes because the refresh takes a
        vector off its node when the operation is applied - so the event it
        emits does not carry it - and adopts it only when the batch ends.
        _adopt_supplied_vectors passes vectors still on their nodes.

        `anchor` is the width to judge the supplied vectors against, for a
        caller that has already emptied the index of what would otherwise
        have set it.
        """
        if not supplied:
            return
        # The index already in memory anchors the dimension; a caller passing
        # vectors of some other width must never evict the vectors that are
        # already correct.
        existing = self.vector_store.export_vectors()
        dimension = (
            anchor or dominant_dimension(existing) or dominant_dimension(supplied)
        )
        accepted = matching_dimension(supplied, dimension)
        if len(accepted) != len(supplied):
            logger.warning(
                f"ignored {len(supplied) - len(accepted)} supplied "
                f"embedding(s) whose dimension is not {dimension}"
            )
        if accepted:
            self.vector_store.load_vectors({**existing, **accepted})

    def _take_inline_vectors(self, nodes) -> Dict[str, Any]:
        """Move any vector carried on a node object into a plain dict.

        A vector reaches a node object two ways: a graph file written before the
        split, and a caller passing `embedding` to add_nodes. Both have to end
        up in the vector store, because the serialized payload no longer carries
        the field — otherwise the value is written nowhere at all.
        """
        taken = {}
        for node in nodes:
            if node.embedding is None:
                continue
            # A zero-length vector is not a usable vector, and carrying one
            # through would make the whole sidecar read back empty (dim 0).
            if len(node.embedding) > 0:
                taken[node.id] = node.embedding
            node.embedding = None
        return taken

    def _load_embeddings(self) -> None:
        """Populate the vector index after nodes have been loaded.

        The sidecar is authoritative. A graph written before the split still
        carries vectors on its nodes; those are adopted for any node the sidecar
        does not cover and the sidecar is marked for rewrite, which is what
        migrates them on the next save. Vectors are never left on the node
        objects — that inline copy is what made a node cost 51 kB resident.
        """
        inline = self._take_inline_vectors(self.nodes.values())

        if self._embedding_sidecar is None:
            accepted = matching_dimension(inline, dominant_dimension(inline))
            self.vector_store.load_vectors(accepted)
            # Same reason as the sidecar path below: a vector the index refuses
            # for its width is still the node's only durable copy, and here
            # there is no sidecar it could ever move to. Without this the next
            # ordinary save writes None over it, silently — the drop happens in
            # matching_dimension, so not even load_vectors' warning fires.
            self._inline_fallback = {
                node_id: vector
                for node_id, vector in inline.items()
                if node_id not in accepted
            }
            self._persisted_vector_revision = self.vector_store.revision
            return

        stored = {}
        if self._embedding_sidecar.exists():
            try:
                stored = self._embedding_sidecar.load()
            except EmbeddingSidecarError as exc:
                # Vectors are derived data: a damaged sidecar costs semantic
                # search until they are regenerated, never a failed load.
                logger.warning(f"ignoring unreadable embedding sidecar: {exc}")

        migrated = {
            node_id: vector
            for node_id, vector in inline.items()
            if node_id not in stored
        }
        merged = {**stored, **migrated}
        orphaned = [node_id for node_id in merged if node_id not in self.nodes]
        for node_id in orphaned:
            del merged[node_id]

        # The two sources can disagree on dimension — a sidecar written by one
        # embedding model merged with inline vectors from another. Stacking
        # those raises in numpy and would take the whole load down, so drop the
        # odd ones out. The sidecar decides which dimension that is whenever it
        # has one: it is the authoritative source, and a graph file full of
        # stale inline vectors must not outvote it.
        #
        # Only the entries that SURVIVED pruning get that vote. A sidecar
        # sharing no live node id with the graph contributes nothing to merged,
        # and letting it still dictate the width would drop every inline vector
        # for disagreeing with a file whose contents are all orphans — taking
        # the graph.json copy with it and then writing an empty sidecar over
        # the old one. That is the shape of restoring a graph.json from another
        # dataset, or from before a model change, onto an existing sidecar.
        live_stored = {
            node_id: vector
            for node_id, vector in stored.items()
            if node_id in self.nodes
        }
        dimension = dominant_dimension(live_stored) or dominant_dimension(migrated)
        vectors = matching_dimension(merged, dimension)
        dropped = len(merged) - len(vectors)
        if dropped:
            logger.warning(
                f"dropped {dropped} embedding(s) whose dimension did not "
                f"match the rest; they will be regenerated on next update"
            )

        self.vector_store.load_vectors(vectors)
        # Every vector that came out of graph.json, whether or not the index
        # took it. One the index refused for its width will never be carried by
        # a sidecar write at all, since it is not in the index to be written;
        # one the index took can still be evicted later by a model change. Both
        # keep graph.json as their only durable copy, so both are held.
        self._inline_fallback = dict(migrated)
        if migrated or orphaned or dropped:
            # Leave the persisted revision behind the live one so the next save
            # writes the migrated or pruned matrix out.
            self._persisted_vector_revision = None
        else:
            self._persisted_vector_revision = self.vector_store.revision

        logger.info(f"Loaded {len(vectors)} embeddings for {len(self.nodes)} nodes")

    def _missing_embedding_node_ids(self) -> List[str]:
        """Ids of nodes the vector index carries no vector for."""
        return [
            node_id
            for node_id in self.nodes
            if not self.vector_store.has_embedding(node_id)
        ]

    def embedding_coverage(self) -> Tuple[int, int]:
        """(nodes carrying an embedding, total nodes).

        add_nodes and its siblings try to embed a node as it lands and, by
        design, swallow the failure when the optional ML stack is unavailable
        at that moment (see add_nodes) - which leaves that node with no
        vector and nothing that revisits it on its own. This is what makes
        the gap visible instead of it only showing up as missing search
        results, and what `backfill_missing_embeddings` below drains.
        """
        total = len(self.nodes)
        return total - len(self._missing_embedding_node_ids()), total

    def backfill_missing_embeddings(self) -> int:
        """Embed every node with no vector yet, and persist the result.

        The counterpart to the gap `add_nodes` (and friends) can leave
        behind: they try once, inline, and move on if the ML stack was not
        ready at that moment. This retries later - on demand, or from the
        startup pass below - and is a no-op once the index is complete.

        Returns how many nodes were embedded; 0 when there was nothing
        missing or the ML stack is still unavailable (the same
        optional-dependency degradation as every other embedding call in
        this module).
        """
        with self._lock:
            missing_ids = self._missing_embedding_node_ids()
            if not missing_ids:
                return 0
            missing_nodes = [self.nodes[node_id] for node_id in missing_ids]
            try:
                self.vector_store.update_nodes_embeddings(missing_nodes)
            except Exception as embed_error:
                logger.warning(f"could not backfill missing embeddings: {embed_error}")
                return 0
            self.save()
            return len(missing_nodes)

    def _maybe_backfill_missing_embeddings_async(self) -> None:
        """Start `backfill_missing_embeddings` in the background at startup
        when there is a gap to close and the optional ML extra is actually
        installed.

        Probing with `importlib.util.find_spec` costs no more than a
        filesystem lookup, unlike loading the model itself (see
        `VectorStore.preload_model`), so a base install - the common case,
        and every CI run - takes this branch for free instead of spawning a
        thread that would fail on the same ImportError every time.
        """
        try:
            embedded, total = self.embedding_coverage()
            if embedded == total:
                return
            logger.warning(
                f"{total - embedded} of {total} node(s) have no "
                f"embedding; semantic search will skip them until backfilled"
            )
            if importlib.util.find_spec("sentence_transformers") is None:
                return

            def _run() -> None:
                try:
                    count = self.backfill_missing_embeddings()
                    if count:
                        logger.info(
                            f"Backfilled {count} missing embedding(s) at startup."
                        )
                except Exception as exc:
                    logger.warning(f"background embedding backfill failed: {exc}")

            threading.Thread(
                target=_run, name="embedding-backfill", daemon=True
            ).start()
        except Exception as exc:
            logger.warning(f"could not start embedding backfill: {exc}")

    def _persist_vectors(self, vectors: Dict[str, Any], vector_revision: int) -> bool:
        """Write the vector matrix to the sidecar. Returns whether it landed.

        A failure is deliberately not fatal to the graph save, so it leaves the
        persisted revision behind and clears the snapshot marker — otherwise the
        retry it opens would be skipped by the next save as "already in flight".
        """
        try:
            self._embedding_sidecar.save(vectors)
        except Exception as e:
            self._snapshotted_vector_revision = None
            logger.warning(f"could not save embedding sidecar: {e}")
            return False
        self._persisted_vector_revision = vector_revision
        # The sidecar is now these vectors' durable home, so graph.json stops
        # having to carry them. This is what ends the migration.
        for node_id in vectors:
            self._inline_fallback.pop(node_id, None)
        return True

    def _do_save_to_disk(
        self,
        data: Dict[str, Any],
        node_count: int,
        edge_count: int,
        vectors: Optional[Dict[str, Any]] = None,
        vector_revision: Optional[int] = None,
    ) -> None:
        """
        Internal method: delegate serialized graph data to the persistence backend.
        Runs in the background executor so file I/O doesn't block the event loop.

        Exceptions are intentionally re-raised so the ThreadPoolExecutor stores
        them in the returned Future. Callers that call .result() (e.g. load())
        will then receive the exception rather than a silent no-op.
        """
        if vectors is not None:
            # Written first: an orphaned vector is harmless, a node whose vector
            # never landed is a silent gap in semantic search.
            self._persist_vectors(vectors, vector_revision)

        try:
            self._persistence_backend.save_graph_data(data)
            logger.info(
                f"Saved {node_count} nodes and {edge_count} edges to "
                f"{self._persistence_destination()}"
            )
        except ExternalChangeRefused:
            # Same reason as in _do_apply, and it has to be said in both write
            # paths or the one without it undoes the other: this is a backend
            # reporting on a thread it may not report on, told so out of a
            # listener it called itself. Flagging a resync would answer that
            # by re-issuing the whole graph over a store another writer is
            # committing to.
            raise
        except Exception as e:
            logger.error(f"Error saving graph to disk: {e}")
            # The backend's image may now lack what memory has, exactly as
            # after a failed entity write: the next write is the whole graph
            # again, until one lands. Before the entity path existed every
            # write was, so this is the same "next successful write carries
            # everything" as before.
            self._resync_pending = True
            raise

    def _persist(self, operations: List[EntityOperation]) -> "Future[None]":
        """Persist a mutation, as entity operations where the backend takes them.

        Callers must hold _lock and pass the operations that describe exactly
        what they changed. After a failed entity write - or a failed
        whole-graph write - the next write here is the whole graph, whatever
        its own shape would have been, so the backend's image catches up
        with memory (see _do_apply and _do_save_to_disk). Otherwise which
        shape reaches the backend is decided by its declared capabilities:

        - no incremental support: the whole graph, exactly as before the
          entity contract existed;
        - one operation: that operation;
        - several operations and transactions: one atomic batch;
        - several operations and no transactions: the whole graph, since the
          snapshot write is atomic and a loop of single operations is not.

        The vector sidecar, where there is one, travels with either shape:
        the write carries whatever vectors moved since the last sidecar
        write, and lands them first. The one exception is a pending
        migration - vectors read out of a pre-split graph.json that the
        sidecar does not hold yet - which only the snapshot path knows how to
        complete, so a write during one is a snapshot.

        Same single-worker executor as save(), so entity writes and snapshots
        land in the order they were issued and flush() drains both.
        """
        caps = self._backend_capabilities
        if (
            not caps.incremental_writes
            or self._resync_pending
            or self._migration_pending()
        ):
            return self.save()
        if not operations:
            return self._io_executor.submit(lambda: None)
        if len(operations) > 1 and not caps.transactions:
            return self.save()
        vectors, vector_revision = self._take_vector_snapshot()
        # Kept so a read can ask whether the store has caught up with us.
        self._last_write = self._io_executor.submit(
            self._do_apply, tuple(operations), vectors, vector_revision
        )
        return self._last_write

    def _do_apply(
        self,
        operations: Tuple[EntityOperation, ...],
        vectors: Optional[Dict[str, Any]] = None,
        vector_revision: Optional[int] = None,
        token: Optional[str] = None,
    ) -> Optional[List[Optional[int]]]:
        """Hand entity operations to the backend, on the executor thread.

        A single operation goes to its own method; more than one go to
        apply_batch, which the backend has declared atomic. Exceptions are
        re-raised, as in _do_save_to_disk, so the Future carries them.

        A backend that enforces revisions always gets apply_batch with the
        write's token, and its stored revisions are the result. Its failures
        raise no resync: the caller is waiting and resolves them itself, and
        re-issuing this instance's graph over a shared store is the overwrite
        §4.4 exists to remove.
        """
        if vectors is not None:
            # Written first, as in _do_save_to_disk: an orphaned vector is
            # harmless, a node whose vector never landed is a silent gap.
            self._persist_vectors(vectors, vector_revision)

        backend = self._persistence_backend
        if self._store_assigns_revisions:
            return backend.apply_batch(operations, write_token=token)  # type: ignore[call-arg]
        try:
            if len(operations) > 1:
                backend.apply_batch(operations)
                return
            op = operations[0]
            if op.kind == "node" and op.action == "upsert":
                backend.upsert_node(op.payload)
            elif op.kind == "node":
                backend.delete_node(op.entity_id)
            elif op.action == "upsert":
                backend.upsert_edge(op.payload)
            else:
                backend.delete_edge(op.entity_id)
        except ExternalChangeRefused:
            # Not a write failure: the backend broke the reporting rule and
            # got told so, on its own call stack, out of a listener it called
            # itself. Flagging a resync here would answer that with a
            # whole-graph write over a store that by definition has another
            # writer. The Future still carries it, so the mutation is not
            # reported as having landed.
            raise
        except Exception as e:
            logger.error(f"Error applying {len(operations)} entity operation(s): {e}")
            # The mutation is in memory and nowhere else now, and the backend's
            # own image lacks it - a later checkpoint would write that image
            # and truncate the journal, and the mutation would be gone at the
            # next start. Flag it; the caller's thread re-issues the whole
            # graph, the way every failed write was healed before the entity
            # path existed. Nothing else happens here, so the Future carries
            # this exception and this thread never touches _lock.
            self._resync_pending = True
            raise

    def flush(self) -> None:
        """
        Wait for any pending background write to complete, and have the
        backend fold whatever it has deferred into its canonical snapshot.

        Useful in tests and in code that reads the graph file directly, or
        reloads from disk, immediately after mutating the graph.

        Correctness relies on max_workers=1 (FIFO task ordering): the
        checkpoint and the no-op sentinel run only after every previously
        submitted write. A checkpoint that fails surfaces here.
        """
        # Drain first, so a failed entity write already queued has had its
        # say; then heal it, so the checkpoint folds a complete image. A heal
        # that fails surfaces here, like a failed checkpoint.
        self._io_executor.submit(lambda: None).result()
        heal = self._heal_if_needed()
        if heal is not None:
            heal.result()
        if self._backend_capabilities.incremental_writes:
            self._io_executor.submit(self._persistence_backend.checkpoint).result()
        # Submit a no-op sentinel and block until it runs — drains the queue.
        self._io_executor.submit(lambda: None).result()

    def _heal_if_needed(self) -> "Optional[Future[None]]":
        """Re-issue the whole graph after a failed entity write or a failed
        whole-graph write, if one failed.

        Runs on the caller's thread. save() clears the flag; the write it
        queues carries every mutation in memory, including the failed one.
        """
        with self._lock:
            if not self._resync_pending:
                return None
            return self.save()

    def get_graph_name(self) -> str:
        """Return configured graph name from graph metadata."""
        name = (self.graph_metadata or {}).get("graph_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return self._default_graph_name()

    def apply_external_change(self, change: "ExternalChange") -> None:
        """Bring the in-memory model in step with a write someone else made.

        A backend that declares `change_notification` calls this, from a
        thread of its own, when the store changed behind this instance's
        back. Which threads count as its own is the backend's obligation and
        is stated on ChangeNotifyingBackend; only one violation of it is
        visible from here, and that one is refused below. A report that
        arrives once shutdown_events() has begun - or once construction has
        failed - is dropped without refreshing anything.
        Nothing here is persisted: the change is already in the store, and
        writing it back would fight the writer that made it.

        Every derived structure a read path serves has to move with it, not
        just the node dictionary: the NetworkX graph, the searchable-text
        cache behind lexical search, and the vector index behind semantic
        search. A stale entry in any of them is worse than a missing one,
        because a miss is refilled on demand and a stale hit is served.

        Events are emitted so subscriptions, agents and the history see the
        change, labelled with a distinct origin (`EXTERNAL_CHANGE_ORIGIN`) so
        a subscription can opt out via `ignore_origins` if reacting to it
        would bounce the change back between instances forever; nothing acts
        on the origin automatically, so an agent whose subscription omits it
        from `ignore_origins` will still bounce. A change the backend could
        not describe is a reload, and a reload emits nothing: a backend that
        wants subscribers to see individual changes has to report them as
        operations.
        """
        if threading.get_ident() == self._writer_thread_id:
            # This thread is the write queue. A refresh may have to wait for
            # that queue, and waiting here would be waiting for ourselves;
            # not waiting would reload over the write we are inside. Handing
            # it to another thread was tried and is worse: two such refreshes
            # race each other, and one can outlive the shutdown that was
            # supposed to have stopped them. So it is a contract violation,
            # and said plainly - every real transport reports from a thread
            # of its own.
            raise ExternalChangeRefused(
                "a change was reported from the thread the application issued "
                "a write on; report changes from the backend's own thread"
            )

        with self._lock:
            # A report after shutdown has nothing to refresh: the model is
            # being torn down, and the drain below would find the queue gone
            # and go ahead anyway. Refused quietly, not raised - the only
            # thread to raise into is the backend's.
            if self._shut_down:
                return
            if not self._settle_before_refresh():
                return
            # Read AFTER the settle, never before. A backend that gathers the
            # content when it dispatches gathers it without this instance's
            # queued writes in it, and the only defence left is the wall clock
            # below - which does not order the commits, and which leaves the
            # two instances apart for good when it disagrees with them. Asking
            # here instead means the answer is the store's, with our own work
            # already landed in it.
            authoritative = change.content_read_on_demand()
            if authoritative:
                try:
                    change = change.with_content()
                except Exception as exc:
                    logger.warning(
                        f"could not read the content of an external "
                        f"change ({exc}); reloading the graph instead"
                    )
                    self._reload_from_store()
                    return
            if change.operations is None:
                self._reload_from_store()
                return
            # Every change to the vector index rebuilds its matrix whole,
            # so doing that once per reported node is linear in the index per
            # operation and quadratic over a batch. Collect what the batch
            # touches and settle the index once, at the end - the shape
            # add_nodes already has. A dict gives last-operation-wins per id,
            # which is what the store applied.
            touched: Dict[str, Tuple[Optional[Node], Any]] = {}
            failure: Optional[Exception] = None
            try:
                for op in change.operations:
                    # Anything this build does not recognise is a resync, not
                    # a delete: reading an unknown action as one would drop
                    # the entity and tell every subscriber it was deleted.
                    if op.action not in ("upsert", "delete"):
                        raise ValueError(f"unknown entity action {op.action!r}")
                    if op.kind == "node":
                        if op.action == "upsert":
                            self._external_upsert_node(op, touched, authoritative)
                        else:
                            self._external_delete_node(op.entity_id, touched)
                    elif op.kind == "edge":
                        if op.action == "upsert":
                            self._external_upsert_edge(op)
                        else:
                            self._external_delete_edge(op.entity_id)
                    else:
                        raise ValueError(f"unknown entity kind {op.kind!r}")
            except Exception as exc:
                failure = exc

            # Whatever did apply, settled before any reload. A reload that
            # lands supersedes this, but _reload_from_store deliberately does
            # not land in two cases - a store reporting it is not there, and a
            # read that fails - and then the applied prefix stays in memory.
            # Its vectors have to match it, or a renamed node goes on matching
            # the description it no longer has.
            try:
                self._settle_vector_index(touched)
            except Exception as exc:
                if failure is None:
                    failure = exc

            if failure is not None:
                # A payload this build cannot read - two instances mid-upgrade,
                # say - would otherwise leave the batch half applied and throw
                # into whatever backend thread called us, where the writing
                # instance would read it as its own write having failed.
                # Resync instead, and keep it to ourselves.
                logger.warning(
                    f"could not apply an external change ({failure}); "
                    f"reloading the graph instead"
                )
                self._reload_from_store()

    def _settle_vector_index(
        self, touched: "Dict[str, Tuple[Optional[Node], Any]]"
    ) -> None:
        """Bring the vector index in step with one batch, in one pass.

        `touched` maps an entity id to the node the batch left there and the
        vector the store carried for it - (None, None) where the batch left
        nothing. Doing this per entity instead would rebuild the index matrix
        once per operation, and the rebuild is linear in the index, so a batch
        would cost the square of it.

        No writer interleaves: every mutation path takes _lock and the whole
        batch holds it. Readers are another matter, and this is the cost of
        settling once - get_node, semantic_search_nodes and find_similar_nodes
        take no lock at all - nor does any other query path that reads the
        node dictionary and the index together - before this change as after,
        so a concurrent reader can see a node the batch updated while its
        vector is still the one the batch found, until the settle lands.
        save() is the exception that matters, and it does hold _lock across
        both, so a half-settled batch is never what gets written. Settling per entity
        narrowed that window to one entity rather than closing it. A system
        listener runs inside the window too, on this thread, and sees its own
        entity half applied that way: node updated, vector not.

        Callers must hold _lock.
        """
        if not touched:
            return

        # Read before the eviction destroys it, and remember whether any id
        # this batch upserts actually held a vector - that is what makes the
        # old width authoritative rather than a ghost. A batch that empties
        # the index by *replacing* everything must still be judged against
        # the width it replaced; one that empties it by *deleting* everything
        # must not, or a supplied vector is refused for disagreeing with
        # vectors that no longer exist.
        upserted = [
            (node, vector) for node, vector in touched.values() if node is not None
        ]
        before = self.vector_store.dimension
        replacing = any(
            self.vector_store.has_embedding(node.id) for node, _ in upserted
        )

        # Whatever these ids had described the text they used to have.
        self.vector_store.remove_nodes_embeddings(list(touched))
        if not upserted:
            return

        supplied = {node.id: vector for node, vector in upserted if vector is not None}
        anchor = self.vector_store.dimension
        if anchor is None and replacing:
            anchor = before
        if anchor is None and supplied:
            # Nothing local anchors the width, so this batch establishes it.
            # A majority vote is the wrong tie-breaker: it would adopt the
            # commonest width and refuse the rest. Judging every supplied
            # vector against the first is what the per-operation path did,
            # one node at a time.
            anchor = len(next(iter(supplied.values())))
        self._adopt_vectors(supplied, anchor)

        def generate(nodes) -> bool:
            try:
                self.vector_store.update_nodes_embeddings(nodes)
                return True
            except Exception as embed_error:
                logger.warning(f"could not update embeddings: {embed_error}")
                return False

        missing = [
            node for node, _ in upserted if not self.vector_store.has_embedding(node.id)
        ]
        if not missing or not generate(missing):
            return

        # Generating at a width the batch did not adopt reads as a model
        # change, and the index is emptied of everything just adopted. Those
        # ids were not in `missing`, so nothing above brings them back. One
        # more pass does, and it cannot recur: this one generates at the width
        # the index now holds.
        stranded = [
            node for node, _ in upserted if not self.vector_store.has_embedding(node.id)
        ]
        if stranded:
            generate(stranded)

    def _settle_before_refresh(self) -> bool:
        """Wait for this instance's own writes; say whether to refresh at all.

        A mutation is in memory before it is in the store, so a refresh that
        overtook one still queued would leave memory holding the store's
        value while our write goes on to land in it. Nothing would ever put
        that right: an instance is not told about its own writes, so the
        disagreement would outlive every later report. Both halves of a
        refresh wait, the reload and the named operations alike.

        Holding _lock is what makes the drain enough: every mutation path
        submits under it, so nothing new can be queued while we wait, and
        queued work never takes _lock. Waiting is safe only because the
        report arrives on a thread that is not running a write - see
        ChangeNotifyingBackend, which is where that obligation is stated.

        The flag is read after the drain, not before. A write already queued
        raises it while we are waiting, so a check made first would pass on a
        value that stopped being true before the refresh used it.

        Callers must hold _lock.
        """
        try:
            self._io_executor.submit(lambda: None).result()
        except RuntimeError:
            pass  # already shut down: nothing can be queued, nothing is pending

        if not self._resync_pending:
            return True

        # An entity write failed, so a mutation is in memory and nowhere
        # else. Refreshing over it drops it. Writing it first is worse: the
        # only write that carries it is the whole graph, and re-issuing this
        # instance's whole image over a store that by definition has another
        # writer destroys what that writer just committed - including the
        # change being reported. So neither: stay put and say so. The next
        # write, flush or shutdown heals the flag, and the next report brings
        # this instance forward.
        logger.warning(
            "not refreshing the graph: a local write failed and "
            "is still only in memory. This instance stays behind the "
            "store until that write has been re-issued."
        )
        return False

    def _reload_from_store(self) -> None:
        """Re-read the whole graph. Callers must hold _lock, and must have
        settled this instance's own writes first (_settle_before_refresh).

        Nothing here writes. A refresh that wrote would re-assert this
        instance's image over a store whose whole point is that someone else
        is writing it too, which is why a failed local write stops the
        refresh instead of being healed into it.

        Bootstrapping is off: a store reporting that it is not there is not
        an invitation to write this instance's graph over it. A store that
        cannot be read at all is reported and left; nothing is raised, because
        the only thread to raise into is the backend's.
        """
        try:
            self.load(bootstrap_if_missing=False)
        except Exception as exc:
            # The store can be unreadable for a moment - being replaced as we
            # read it, a network read failing. Throwing here would put that in
            # the caller's lap, and the caller is the backend's thread, where
            # the instance that made the write would read it as its own write
            # having failed. What is in memory is behind, not wrong: whatever
            # a half-applied batch already applied is real store state.
            logger.warning(
                f"could not reload the graph ({exc}); the graph in "
                f"memory is unchanged and behind the store until the next "
                f"change is reported"
            )

    @staticmethod
    def _is_newer(held, reported) -> bool:
        """Is the version we hold later than the one being reported?

        Wall-clock last-writer-wins: the instances share no other ordering,
        so their clocks have to be roughly in step for this to mean anything.
        A pair that cannot be compared - one naive and one aware, which a
        backend handing over datetime objects of its own could produce - is
        not an answer, and the report is applied. Equal stamps defer to the
        report too: a tie is unresolvable, and taking the store's side is
        what converges the two instances. (A payload with no stamp at all
        never reaches here as a gap: the model fills one in at parse time,
        stamped now - so it is normally the newer one and applies, but it
        loses to a held stamp dated in the future, which is what a
        clock-skewed peer produces.)
        """
        try:
            return bool(held > reported)
        except TypeError:
            return False

    @staticmethod
    def _entity_type_name(entity) -> str:
        value = getattr(entity, "type", None)
        return value.value if hasattr(value, "value") else str(value)

    def _already_held(self, existing: Node, reported: Node) -> bool:
        """Is the store's answer the value this instance already holds?

        The vector is compared where it lives rather than where it is written:
        an embedding is moved off the node and into the index as soon as it is
        adopted, so a node held in memory carries none while the store's copy
        of the same node carries one. Comparing the two dictionaries whole
        would therefore call every vectored node changed.
        """
        held = existing.to_dict()
        answer = reported.to_dict()
        held.pop("embedding", None)
        vector = answer.pop("embedding", None)
        if held != answer:
            return False
        # Both directions: an answer carrying no embedding for a node the
        # index has a vector for is a change too - the store dropped it - and
        # reading that as "nothing to do" would leave semantic search matching
        # a description the store no longer keeps.
        return self.vector_store.get_vector_list(reported.id) == vector

    def _external_upsert_node(
        self,
        op: EntityOperation,
        touched: "Dict[str, Tuple[Optional[Node], Any]]",
        authoritative: bool = False,
    ) -> None:
        """Callers must hold _lock, and settle `touched` when the batch ends.

        `authoritative` says the payload was read from the store AFTER this
        instance settled its own writes, so it already contains them. There is
        then nothing of ours for the clock below to protect, and consulting it
        would be worse than useless - it is what keeps the two instances apart.
        """
        # from_dict rewrites its argument in place - timestamps parsed,
        # defaults filled in - and the argument here belongs to the backend,
        # which may still be holding the record it reported. Read it, do not
        # take it. The top level is all from_dict touches.
        node = Node.from_dict(dict(op.payload))
        existing = self.nodes.get(node.id)
        if (
            authoritative
            and existing is not None
            and self._already_held(existing, node)
        ):
            # Reading after the settle means the answer is often this
            # instance's own write, and there is nothing in that to apply. Not
            # a no-op if it were. The event would tell every subscriber a node
            # changed when nothing about it did; and the settle would go to
            # work on a description that did not change - rebuilding the index
            # twice for a node that has a vector, once to evict it and once to
            # put the same one back from the payload, and for a node that has
            # none, asking the model for one, which is the whole cost on an
            # install with no model to ask.
            return
        if (
            not authoritative
            and existing is not None
            and self._is_newer(existing.updated_at, node.updated_at)
        ):
            # Both instances wrote this node, and this report was gathered
            # before we settled - so it may predate our own write, which we
            # are never told about. Applying it would leave us serving a value
            # the store does not hold, with nothing left to report that would
            # put it right. Last writer wins, by the only ordering two
            # instances that cannot re-read their store share.
            #
            # It is a poor ordering, and it is why a backend that CAN re-read
            # should report `entities_read_on_demand` and never reach this:
            # the clock does not order the commits, so when the write that
            # committed last carries the earlier stamp this keeps the two
            # instances apart permanently rather than resolving anything.
            logger.warning(
                f"ignoring an external change to node {node.id}: this "
                f"instance holds a newer version of it"
            )
            return
        before = existing.to_dict() if existing is not None else None

        # The vector describes the text this node used to have, and keeping
        # it would let semantic search go on matching a description that is
        # gone. The index is settled for the whole batch at the end; the
        # graph file's own inline copy is dropped here, since that costs a
        # dict pop rather than a matrix rebuild.
        # Taken here, not at the settle: _emit_event below builds `after`
        # from the node, and every other mutation path has already moved the
        # vector into the index by then. Leaving it on would put a raw
        # embedding into every webhook and agent payload - the one thing the
        # history store strips by name, on the one path that has no such
        # filter.
        touched[node.id] = (node, self._take_inline_vectors([node]).get(node.id))
        self._inline_fallback.pop(node.id, None)

        # Index BEFORE `nodes`, here and everywhere that adds; removals are the
        # mirror image, `nodes` first and the index after. Together those keep
        # the index a superset of `nodes` through every incremental write,
        # which is what lets the search's candidate path - which can only offer
        # ids it holds records for, and takes no lock - be trusted. `load`
        # replaces everything at once and is the one path that steps outside
        # it, deliberately and visibly: it empties the index first, so the size
        # test declines for the whole swap. See the comment there.
        #
        # One half-done write on its own would be caught anyway: `nodes` first
        # would leave the index SHORTER, and the size test in `search_nodes`
        # sends the query to the walk. What the ordering is really for is two
        # writes in flight at once, where the sizes cancel out and that test
        # cannot tell the two dicts apart.
        #
        # The two halves are not equally urgent, and it is worth being exact
        # about which. Reverse the ADD and a node that has reached `nodes` -
        # one a reader is owed - is missing from the index, and the candidate
        # path returns [] for it. Measured. Reverse the REMOVAL and the node
        # that goes missing is the one whose delete is already in flight,
        # which a search racing that delete could fail to see anyway; no
        # reader is owed it.
        #
        # The removal half is still written this way, because what it keeps is
        # the invariant rather than any single query: index a superset of
        # `nodes` for every incremental write, which is what the size test is a
        # backstop FOR. An invariant that holds except where somebody has
        # argued the exception away is one nobody can reason about later -
        # which is why `load`'s exception is not argued away but arranged to
        # fail the size test outright.
        self._searchable_text_cache[node.id] = self._build_match_fields(node)
        self.nodes[node.id] = node
        # add_node on an existing id replaces the attributes, which is what
        # repoints `data` at the new object; every read path that walks the
        # graph would otherwise still hand out the old one.
        self.graph.add_node(node.id, data=node)

        self._emit_event(
            event_type=EventType.NODE_UPDATE if before else EventType.NODE_CREATE,
            entity_kind=EntityKind.NODE,
            entity_id=node.id,
            entity_type=self._entity_type_name(node),
            before=before,
            after=node.to_dict(),
            context=EventContext(event_origin=EXTERNAL_CHANGE_ORIGIN),
        )

    def _external_delete_node(
        self, node_id: str, touched: "Dict[str, Tuple[Optional[Node], Any]]"
    ) -> None:
        """Callers must hold _lock."""
        node = self.nodes.get(node_id)
        if node is None:
            return
        before = node.to_dict()

        # An edge cannot outlive an endpoint. The store may well have reported
        # the edge deletions too; deleting one twice is a no-op.
        incident = []
        if self.graph.has_node(node_id):
            seen_edges = set()
            for _, _, edge_id in self.graph.out_edges(node_id, keys=True):
                incident.append(edge_id)
                seen_edges.add(edge_id)
            for _, _, edge_id in self.graph.in_edges(node_id, keys=True):
                if edge_id not in seen_edges:
                    incident.append(edge_id)
                    seen_edges.add(edge_id)

        for edge_id in incident:
            self._external_delete_edge(edge_id)

        if self.graph.has_node(node_id):
            self.graph.remove_node(node_id)
        del self.nodes[node_id]
        self._searchable_text_cache.pop(node_id, None)
        # Settled with the rest of the batch; see _settle_vector_index. The
        # inline copy goes now - a dict pop, not a matrix rebuild - or an id
        # created again later would inherit the departed node's vector.
        touched[node_id] = (None, None)
        self._inline_fallback.pop(node_id, None)

        self._emit_event(
            event_type=EventType.NODE_DELETE,
            entity_kind=EntityKind.NODE,
            entity_id=node_id,
            entity_type=self._entity_type_name(node),
            before=before,
            after=None,
            context=EventContext(event_origin=EXTERNAL_CHANGE_ORIGIN),
        )

    def _external_upsert_edge(self, op: EntityOperation) -> None:
        """Callers must hold _lock."""
        edge = Edge.from_dict(dict(op.payload))  # the backend's dict; see above
        if edge.source not in self.nodes or edge.target not in self.nodes:
            # add_edge would invent the missing endpoint as a node with no
            # data, which every read path walking the graph would then trip
            # over. Report it and leave the edge out of both structures.
            absent = edge.source if edge.source not in self.nodes else edge.target
            logger.warning(
                f"ignoring external edge {edge.id}: endpoint {absent} is not present"
            )
            return

        existing = self.edges.get(edge.id)
        before = existing.to_dict() if existing is not None else None
        if existing is not None and self.graph.has_edge(
            existing.source, existing.target, key=existing.id
        ):
            # An endpoint may have moved; the old graph edge is keyed on where
            # it used to point and would otherwise stay.
            self.graph.remove_edge(existing.source, existing.target, key=existing.id)

        self.edges[edge.id] = edge
        self.graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)

        self._emit_event(
            event_type=EventType.EDGE_UPDATE if before else EventType.EDGE_CREATE,
            entity_kind=EntityKind.EDGE,
            entity_id=edge.id,
            entity_type=self._entity_type_name(edge),
            before=before,
            after=edge.to_dict(),
            context=EventContext(event_origin=EXTERNAL_CHANGE_ORIGIN),
        )

    def _external_delete_edge(self, edge_id: str) -> None:
        """Callers must hold _lock."""
        edge = self.edges.pop(edge_id, None)
        if edge is None:
            return
        if self.graph.has_edge(edge.source, edge.target, key=edge_id):
            self.graph.remove_edge(edge.source, edge.target, key=edge_id)

        self._emit_event(
            event_type=EventType.EDGE_DELETE,
            entity_kind=EntityKind.EDGE,
            entity_id=edge_id,
            entity_type=self._entity_type_name(edge),
            before=edge.to_dict(),
            after=None,
            context=EventContext(event_origin=EXTERNAL_CHANGE_ORIGIN),
        )

    def reload(self) -> None:
        """
        Reload graph from disk, discarding any in-memory changes.

        Useful for refreshing state after external modifications.
        """
        self.load()

    def _score_node_match(self, node: "Node", query_lower: str) -> int:
        return storage_search.score_node_match(
            node, query_lower, self._type_searchable_text
        )

    def search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        limit: int = 50,
        include_archived: bool = False,
        match_mode: str = storage_search.MATCH_MODE_SUBSTRING,
    ) -> List[Node]:
        """Search nodes based on text query.  Delegates to storage_search."""
        return storage_search.search_nodes(
            self.nodes,
            self._searchable_text_cache,
            self._type_searchable_text,
            query,
            node_types,
            limit,
            include_archived=include_archived,
            match_mode=match_mode,
        )

    def semantic_search_nodes(
        self,
        query: str,
        node_types: Optional[List[NodeType]] = None,
        limit: int = 50,
        threshold: float = storage_search.DEFAULT_SEMANTIC_THRESHOLD,
        include_archived: bool = False,
    ) -> List[Node]:
        """Embedding-ranked search over nodes.  Delegates to storage_search.

        Reuses the vector-store path shared with find_similar_nodes; returns
        an empty list when embeddings are unavailable (ML-free install).
        """
        return storage_search.semantic_search_nodes(
            self.nodes,
            self.vector_store,
            query,
            node_types,
            limit,
            threshold,
            include_archived=include_archived,
        )

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a specific node"""
        return self.nodes.get(node_id)

    def get_all_nodes(self) -> List[Node]:
        """Get all nodes in the graph"""
        return list(self.nodes.values())

    def get_all_edges(self) -> List[Edge]:
        """Get all edges in the graph"""
        return list(self.edges.values())

    def _validate_edge_applicability(self, edge: Edge) -> None:
        error = edge_applicability_error(
            edge.type_str, self.nodes.get(edge.source), self.nodes.get(edge.target)
        )
        if error:
            raise ValueError(error)

    def audit_relationship_applicability(self) -> List[Dict[str, Any]]:
        """Return existing edges that violate configured relationship applicability."""
        violations = []
        for edge in self.edges.values():
            source_node = self.nodes.get(edge.source)
            target_node = self.nodes.get(edge.target)
            if source_node is None or target_node is None:
                violations.append(
                    {
                        "edge_id": edge.id,
                        "type": edge.type_str,
                        "source": edge.source,
                        "target": edge.target,
                        "source_type": source_node.type_str if source_node else None,
                        "target_type": target_node.type_str if target_node else None,
                        "message": "Edge references a missing source or target node.",
                    }
                )
                continue
            try:
                self._validate_edge_applicability(edge)
            except ValueError as exc:
                violations.append(
                    {
                        "edge_id": edge.id,
                        "type": edge.type_str,
                        "source": edge.source,
                        "target": edge.target,
                        "source_type": source_node.type_str,
                        "target_type": target_node.type_str,
                        "message": str(exc),
                    }
                )
        return violations

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        depth: int = 1,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        """Get nodes connected to the given node.

        Two engines answer this, and which one does is not visible in the
        result: `test_traversal_equivalence.py` holds the store's traversal to
        the in-memory walk's answer, set for set, and neither may change
        without the other (`dec-oc-traversal-recursive-cte`).

        The store answers only when it is CURRENT. Writes go to a background
        executor, so between a mutation and its write landing `self.nodes` is
        ahead of the store; a traversal answered there would not see the
        caller's own write. That is not the two engines disagreeing, it is them
        being asked about different moments, and no equivalence test can cover
        it. So a pending write sends the query to the walk, which reads the
        live dictionaries - correct, and no slower than it was before any of
        this existed.
        """
        if self._store_traversal_is_current():
            try:
                found = self._persistence_backend.traverse(  # type: ignore[union-attr]
                    node_id,
                    depth,
                    relationship_types=[
                        getattr(t, "value", t) for t in (relationship_types or [])
                    ]
                    or None,
                    include_archived=include_archived,
                )
            except Exception as exc:
                # A store that cannot answer is not a failed request. The walk
                # is still here and still right; the store is the optimisation.
                logger.warning(f"store traversal failed, walking instead: {exc}")
            else:
                # Membership was decided by the store, at the store's moment;
                # the payloads are resolved here, at ours. Between the two the
                # graph can change, and then there is no sound way to patch
                # the store's answer into one: dropping a node that has since
                # been archived leaves behind whatever was only reachable
                # THROUGH it, which is a state neither engine ever held - the
                # same objection that made returning the archived node wrong.
                # Recomputing reachability here would be reimplementing the
                # walk beside the walk.
                #
                # So this does not patch. It asks one question - did anything
                # the store decided on stop being what it was - and when the
                # answer is yes it falls through to the walk, which decides and
                # resolves from one dictionary in one pass and is therefore
                # self-consistent by construction. That is the fallback G3
                # already relies on; this is one more reason to take it.
                #
                # Only disappearances count. A node ADDED since the store
                # answered is absent from the result, but that is a read at an
                # earlier instant rather than an inconsistent one, which is
                # what any snapshot read gives you.
                def _hidden(node: "Node", nid: str) -> bool:
                    if include_archived or nid == node_id:
                        return False
                    return bool(getattr(node, "archived", False))

                # Resolved in the SAME pass that checks, not a second one.
                # This path takes no lock while every mutator holds one, so a
                # delete landing between a `nid in self.nodes` and a
                # `self.nodes[nid]` turns a traversal into a KeyError out of
                # the API. One dict lookup per id, its result carried forward,
                # makes the check and the use the same observation.
                # `storage_search.get_related_nodes` resolves the same way.
                nodes = []
                for nid in found["node_ids"]:
                    node = self.nodes.get(nid)
                    if node is None or _hidden(node, nid):
                        nodes = None
                        break
                    nodes.append(node)
                edges = None
                if nodes is not None:
                    edges = []
                    for eid in found["edge_ids"]:
                        edge = self.edges.get(eid)
                        if edge is None or (
                            not include_archived
                            and bool(getattr(edge, "archived", False))
                        ):
                            edges = None
                            break
                        edges.append(edge)
                if nodes is not None and edges is not None:
                    return {"nodes": nodes, "edges": edges}

        return storage_search.get_related_nodes(
            self.nodes,
            self.edges,
            self.graph,
            node_id,
            relationship_types,
            depth,
            include_archived=include_archived,
        )

    def _store_traversal_is_current(self) -> bool:
        """Whether the store can be trusted to answer a traversal right now."""
        if not self._backend_capabilities.store_traversal:
            return False
        write = self._last_write
        if write is not None and not write.done():
            return False
        # Read AFTER done(), not before. _do_apply sets this flag and then
        # raises, so the flag is set before the Future finishes. Reading it
        # first admits: flag False here -> the worker sets it and finishes ->
        # done() True -> the guard calls a store it has just been told is
        # stale. In this order, done() being true means the flag write has
        # already happened, so the window closes for free.
        if self._resync_pending:
            # The last write failed and the whole graph is owed to the store;
            # what is there now is not what we hold.
            return False
        return True

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> List[SimilarNode]:
        """Find similar nodes by name (Levenshtein + vector).  Delegates to storage_search."""
        return storage_search.find_similar_nodes(
            self.nodes, self.vector_store, name, node_type, threshold, limit
        )

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[NodeType] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, List[SimilarNode]]:
        """Batch variant of find_similar_nodes.  Delegates to storage_search."""
        return storage_search.find_similar_nodes_batch(
            self.nodes, self.vector_store, names, node_type, threshold, limit
        )

    def add_nodes(
        self,
        nodes: List[Node],
        edges: List[Edge],
        event_context: Optional[EventContext] = None,
        *,
        expect_absent: bool = False,
    ) -> AddNodesResult:
        """
        Add nodes and edges.
        Validates and saves to JSON.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            nodes: List of nodes to add
            edges: List of edges to add
            event_context: Optional context for event tracking and loop prevention
            expect_absent: Every create carries the expectation that no entity
                with its id exists (§4.3). Checked for the whole call before
                anything changes, and again by a store that enforces
                revisions; a refusal raises EntityRevisionConflict and adds
                nothing.

        Raises:
            EntityRevisionConflict, GraphWriteFailed, GraphWriteOutcomeUnknown:
                the write was refused or could not be completed; nothing was
                added and no event fired.
        """
        with self._lock:
            expectations: Dict[Tuple[str, str], Any] = {}
            if expect_absent:
                for node in nodes:
                    self._check_expectation(
                        "node", node.id, self.nodes.get(node.id), EXPECT_ABSENT
                    )
                    expectations[("node", node.id)] = EXPECT_ABSENT
                for edge in edges:
                    self._check_expectation(
                        "edge", edge.id, self.edges.get(edge.id), EXPECT_ABSENT
                    )
                    expectations[("edge", edge.id)] = EXPECT_ABSENT
            added_node_ids = []
            added_edge_ids = []
            # What has landed in memory but not yet reached the store. A
            # failure part-way - a rejected id, an unresolvable endpoint, a
            # raise - returns a failure result while leaving what was added in
            # place, as it always has; every failure exit first persists
            # exactly that, so the store never falls behind the memory image.
            unpersisted_nodes: List[Node] = []
            unpersisted_edges: List[Edge] = []

            def commit_created(created_nodes, created_edges) -> None:
                undo = _Undo()
                for node in created_nodes:
                    undo.nodes[node.id] = None
                    undo.vectors[node.id] = None
                for edge in created_edges:
                    undo.edges[edge.id] = None
                store_expectations = expectations
                if self._store_assigns_revisions:
                    # Everything here was absent from memory, but on a shared
                    # store another instance may have created the same id in
                    # a write this one has not applied yet. Unconditioned, the
                    # upsert would replace that entity unseen; expecting
                    # absence makes the store refuse it instead.
                    store_expectations = {
                        **{("node", node.id): EXPECT_ABSENT for node in created_nodes},
                        **{("edge", edge.id): EXPECT_ABSENT for edge in created_edges},
                    }
                self._commit(
                    [("node", "upsert", node) for node in created_nodes]
                    + [("edge", "upsert", edge) for edge in created_edges],
                    undo,
                    store_expectations,
                )

            def persist_landed() -> None:
                if not (unpersisted_nodes or unpersisted_edges):
                    return
                try:
                    commit_created(unpersisted_nodes, unpersisted_edges)
                except EntityRevisionConflict:
                    # A shared store refused an id another instance created:
                    # nothing landed, and the caller is owed the refusal
                    # rather than a local failure that hides why.
                    raise
                except Exception as persist_error:
                    # The failure being handled may be the persist itself
                    # (executor shut down); the caller still gets its result.
                    logger.warning(f"could not persist what was added: {persist_error}")

            nodes_to_embed: List[Node] = []
            generation_attempted = False

            def still_landed() -> List[str]:
                # After persist_landed: a store that refused it has undone
                # the nodes (or re-read the graph), and those did not stay.
                return [n.id for n in nodes_to_embed if self.nodes.get(n.id) is n]

            def generate_landed_vectors() -> None:
                # Once per call: an exit after the attempt - a rejected edge -
                # must not encode the batch a second time.
                nonlocal generation_attempted
                if generation_attempted or not nodes_to_embed:
                    return
                generation_attempted = True
                try:
                    self.vector_store.update_nodes_embeddings(nodes_to_embed)
                except Exception as embed_error:
                    # Embedding generation is optional - log but don't fail
                    logger.warning(f"could not generate embeddings: {embed_error}")

            def settle_landed_vectors() -> None:
                # A node that landed stays, and every later event about it -
                # an update's `before`, an external upsert's - is built from
                # the object, with no by-name filter for `embedding`. So on a
                # failure exit too the vector goes to the index or nowhere,
                # never stays on the node.
                try:
                    self._adopt_supplied_vectors(nodes_to_embed)
                except Exception as adopt_error:
                    logger.warning(
                        f"could not adopt supplied embeddings: {adopt_error}"
                    )
                for landed in nodes_to_embed:
                    landed.embedding = None
                # A landed node stays and is persisted, so it is generated for
                # as on success; otherwise semantic search misses it until the
                # next startup backfill.
                generate_landed_vectors()

            try:
                # Add nodes
                for node in nodes:
                    if node.id in self.nodes:
                        settle_landed_vectors()
                        persist_landed()
                        return AddNodesResult(
                            added_node_ids=[],
                            added_edge_ids=[],
                            success=False,
                            message=f"Node with ID {node.id} already exists",
                            landed_node_ids=still_landed(),
                        )

                    # Index before `nodes` - see _external_upsert_node for why.
                    self._searchable_text_cache[node.id] = self._build_match_fields(
                        node
                    )
                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)
                    added_node_ids.append(node.id)
                    nodes_to_embed.append(node)
                    unpersisted_nodes.append(node)

                # A caller may pass `embedding` on the node itself. The
                # serialized payload no longer carries that field, so adopt it
                # into the vector store or it is persisted nowhere. Generation
                # below still wins when the ML stack is available, as before.
                self._adopt_supplied_vectors(nodes_to_embed)

                generate_landed_vectors()

                # Nodes and edges go to the store as one write, after the
                # edges: on a snapshot backend each write is the whole graph.
                # A rejected edge still leaves these nodes in place - every
                # failure exit persists them through persist_landed().

                # Create name-to-ID mapping for newly added nodes and existing nodes
                name_to_id = {}
                for node_id, node in self.nodes.items():
                    name_to_id[node.name] = node_id

                # Add edges
                for edge in edges:
                    # Resolve source and target - they might be names or IDs
                    source_id = edge.source
                    target_id = edge.target

                    # If source is not a valid ID, try to resolve it as a name
                    if source_id not in self.nodes:
                        if source_id in name_to_id:
                            source_id = name_to_id[source_id]
                        else:
                            raise ValueError(
                                f"Source node '{edge.source}' does not exist (not found by ID or name)"
                            )

                    # If target is not a valid ID, try to resolve it as a name
                    if target_id not in self.nodes:
                        if target_id in name_to_id:
                            target_id = name_to_id[target_id]
                        else:
                            raise ValueError(
                                f"Target node '{edge.target}' does not exist (not found by ID or name)"
                            )

                    # Update edge with resolved IDs
                    edge.source = source_id
                    edge.target = target_id

                    if edge.id in self.edges:
                        persist_landed()
                        return AddNodesResult(
                            added_node_ids=[],
                            added_edge_ids=[],
                            success=False,
                            message=f"Edge with ID {edge.id} already exists",
                            landed_node_ids=still_landed(),
                        )

                    self._validate_edge_applicability(edge)

                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source, edge.target, key=edge.id, data=edge
                    )
                    added_edge_ids.append(edge.id)
                    unpersisted_edges.append(edge)

                commit_created(
                    nodes_to_embed,
                    [self.edges[edge_id] for edge_id in added_edge_ids],
                )
                unpersisted_nodes.clear()
                unpersisted_edges.clear()

                # Emit events for added nodes
                for node_id in added_node_ids:
                    node = self.nodes.get(node_id)
                    if node:
                        node_type = (
                            node.type.value
                            if hasattr(node.type, "value")
                            else str(node.type)
                        )
                        self._emit_event(
                            event_type=EventType.NODE_CREATE,
                            entity_kind=EntityKind.NODE,
                            entity_id=node_id,
                            entity_type=node_type,
                            before=None,
                            after=node.to_dict(),
                            context=event_context,
                        )

                # Emit events for added edges
                for edge_id in added_edge_ids:
                    edge = self.edges.get(edge_id)
                    if edge:
                        edge_type = (
                            edge.type.value
                            if hasattr(edge.type, "value")
                            else str(edge.type)
                        )
                        self._emit_event(
                            event_type=EventType.EDGE_CREATE,
                            entity_kind=EntityKind.EDGE,
                            entity_id=edge_id,
                            entity_type=edge_type,
                            before=None,
                            after=edge.to_dict(),
                            context=event_context,
                        )

                return AddNodesResult(
                    added_node_ids=added_node_ids,
                    added_edge_ids=added_edge_ids,
                    success=True,
                    message=f"Added {len(added_node_ids)} nodes and {len(added_edge_ids)} edges",
                )

            except (
                EntityRevisionConflict,
                GraphWriteFailed,
                GraphWriteOutcomeUnknown,
            ):
                # The write itself was refused or failed, and _commit has
                # already put memory where the store is. Persisting "what
                # landed" now would write exactly what was just refused.
                raise
            except Exception as e:
                settle_landed_vectors()
                persist_landed()
                return AddNodesResult(
                    added_node_ids=[],
                    added_edge_ids=[],
                    success=False,
                    message=f"Error during add: {str(e)}",
                    landed_node_ids=still_landed(),
                )

    @property
    def generation(self) -> int:
        """Monotonic counter bumped once, atomically, on every whole-graph
        replace (see ``replace_all_nodes_and_edges``).

        A caller that starts slow background work against "the graph as it
        stands right now" — the import worker's embedding generation is the
        one caller today — reads this first and passes it to
        ``commit_generation_embeddings`` afterwards, so a replace that lands
        while that work is in flight is detected before its result is written
        back, rather than silently overwriting a newer graph's content.

        Persisted as ``graph_generation`` in ``graph_metadata`` (written by
        ``replace_all_nodes_and_edges``, read back by ``load()``), so this
        value survives a process restart rather than resetting to 0 — a
        crash-recovered import job (``recover_import_jobs``) is stamped with
        a real generation from before the crash, and comparing it against an
        in-memory counter that forgot everything on restart would make every
        such job look superseded even when no later import ever happened.
        """
        with self._lock:
            return self._generation

    def replace_all_nodes_and_edges(self, nodes: List[Node], edges: List[Edge]) -> None:
        """
        Atomically replace the ENTIRE graph with ``nodes``/``edges``.

        This is the commit step of a whole-graph import (see
        ``backend/service/graph_import.py``): the caller has already validated
        the document (types, duplicate ids, dangling references) before this is
        ever invoked, so this method assumes ``nodes``/``edges`` are internally
        consistent and only does the swap-and-persist. It does NOT merge into
        the existing graph and does not resolve names to ids the way
        ``add_nodes`` does — every id here becomes the id in the live graph.

        Thread-safe and, on a persistence failure, self-healing: the in-memory
        image is snapshotted before the swap and restored if the durable write
        fails partway, so a failed import never leaves the live graph pointing
        at content that was never made durable. The on-disk file itself is
        never at risk either way — ``save()`` writes it atomically (temp file +
        rename), so a write that fails leaves the previous file untouched.

        The swap itself is a handful of single-statement pointer reassignments
        (``self.nodes = new_nodes`` etc.), never an in-place clear-then-refill
        of the live containers. Every OTHER read path in this class (get_node,
        get_all_nodes, get_all_edges, get_stats, ...) reads ``self.nodes`` /
        ``self.edges`` / ``self.graph`` without taking ``_lock`` — safe only
        because every other writer mutates a single dict key at a time, which
        is atomic under the GIL. A multi-statement clear-then-repopulate of a
        live, readable container is not: it lets a concurrent unlocked reader
        observe an empty node set, a nodes/edges count mismatch, or nodes
        without their vectors, for as long as the rebuild takes. Building the
        new nodes/edges/graph/search-index off to the side FIRST and then
        publishing each as one attribute assignment means an unlocked reader
        of THOSE containers only ever sees the fully-old graph or the
        fully-new one, never a window in between — and it is what lets a
        failed save simply put the old references back (see ``_restore``
        below) instead of having to rebuild them.

        Stale vectors are dropped rather than carried over: a replaced graph is
        a new dataset, and keeping an old vector under a reused id would hand a
        new node someone else's embedding (see docs/DATA_MANAGEMENT.md,
        "Replacing the graph"). The caller is expected to enqueue a background
        job that regenerates them; this method never blocks on embeddings, and
        bumps ``generation`` (persisted into ``graph_metadata`` — see the
        property below and ``load()`` — so it survives this process
        restarting) so that job can later tell whether it is still working
        against the graph this call committed.

        The vector-store clear (``self.vector_store.load_vectors({})`` below)
        is NOT part of the same atomic-pointer-swap guarantee: ``load_vectors``
        is pre-existing ``VectorStore`` code that makes two separate
        assignments (the embeddings dict, then the search matrix) with no lock
        of its own, and ``find_similar_nodes`` (unlike ``find_similar_nodes_batch``)
        reads it without ``GraphStorage._lock`` either. For this specific call
        — clearing to ``{}`` — that window can only ever be observed as "no
        results" on both sides of it, so it cannot produce a wrong answer here;
        it just means the vector clear does not carry the same hard real-time
        guarantee as the nodes/edges/graph/search-index swap above.
        """
        with self._lock:
            previous_nodes = self.nodes
            previous_edges = self.edges
            previous_graph = self.graph
            previous_searchable = self._searchable_text_cache
            previous_embeddings = dict(self.vector_store.embeddings)
            previous_metadata = self.graph_metadata
            previous_generation = self._generation

            def _restore() -> None:
                # Each of these is the same kind of single-statement pointer
                # reassignment as the swap below, so a concurrent unlocked
                # reader sees either the fully-new graph (if it read before
                # this ran) or the fully-restored-old one — never a mix. None
                # of the old objects were ever mutated in place, so putting
                # the references back is enough; nothing needs rebuilding.
                self.nodes = previous_nodes
                self.edges = previous_edges
                self.graph = previous_graph
                self._searchable_text_cache = previous_searchable
                self.vector_store.load_vectors(previous_embeddings)
                self.graph_metadata = previous_metadata
                self._generation = previous_generation

            # Replacing a graph restamps it (§4.2): an expectation taken
            # against the old content must not match the new. Reserved in one
            # durable step, before anything is swapped in. A store that
            # assigns revisions itself does not restamp; see below and
            # docs/PERSISTENCE_BACKENDS.md.
            if not self._store_assigns_revisions:
                first = self._take_revisions(len(nodes) + len(edges))
                for offset, entity in enumerate([*nodes, *edges]):
                    entity.revision = first + offset
            else:
                # The document's revisions are not this store's. Kept, a
                # whole-graph save would raise the store's counter to them -
                # to its ceiling, for a document that asks - so the imported
                # entities read as never written under revisions instead.
                for entity in [*nodes, *edges]:
                    entity.revision = 0

            new_nodes = {node.id: node for node in nodes}
            new_edges = {edge.id: edge for edge in edges}

            new_graph = nx.MultiDiGraph()
            for node in new_nodes.values():
                new_graph.add_node(node.id, data=node)
            for edge in new_edges.values():
                new_graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)

            new_searchable = storage_search.LexicalIndex()
            new_searchable.update(
                {node.id: self._build_match_fields(node) for node in new_nodes.values()}
            )
            # Stamped into metadata so it survives this process exiting - see
            # `generation` and `load()`. Without this, a process restart reads
            # `_generation` back as 0 even though the graph on disk really is
            # at this generation, and a crash-recovered job legitimately
            # stamped with it is wrongly treated as superseded.
            new_metadata = {
                **previous_metadata,
                "graph_generation": previous_generation + 1,
            }

            # From here on every assignment is a single atomic pointer swap —
            # see the docstring above and `_restore` for why that matters.
            self.nodes = new_nodes
            self.edges = new_edges
            self.graph = new_graph
            self._searchable_text_cache = new_searchable
            self.graph_metadata = new_metadata

            # Drop every existing vector. The background embedding job (kind
            # IMPORT) regenerates them against the new content; nothing here
            # regenerates them synchronously.
            self.vector_store.load_vectors({})
            self._generation += 1

            try:
                self.save().result()
            except Exception:
                _restore()
                raise

    def commit_generation_embeddings(
        self, generation: Optional[int], vectors: Dict[str, Any]
    ) -> bool:
        """Absorb freshly computed embeddings and persist them, but only if
        the graph is still at ``generation`` — the generation the caller
        captured before starting the (slow) work that produced ``vectors``.

        This is the second half of the staleness guard ``generation``
        documents: the import worker computes embeddings off to the side
        (``VectorStore.compute_node_embeddings``, which does not touch the
        index) and then calls this, inside the SAME lock
        ``replace_all_nodes_and_edges`` holds for its whole body, to check and
        commit as one atomic step. That is what closes the race a check-then-
        act without a shared lock would leave open: a replace landing between
        the check and the write.

        Returns whether the vectors were committed. False means a later
        ``replace_all_nodes_and_edges`` has already superseded ``generation``
        — the caller's job is stale, its vectors describe content that is no
        longer live, and it must report itself as superseded rather than
        succeeded. ``generation`` of ``None`` always commits (an older/foreign
        caller that never captured a generation to check against).

        Incremental stores receive only the embedded nodes, with the revisions
        this instance holds as expectations. A whole-graph save here would
        overwrite writes from other instances that have not reached us yet.
        """
        with self._lock:
            if generation is not None and self._generation != generation:
                return False
            if vectors:
                if self._backend_capabilities.incremental_writes:
                    nodes = [
                        self.nodes[node_id]
                        for node_id in vectors
                        if node_id in self.nodes
                    ]
                    if nodes:
                        undo = self._capture(node_ids=[node.id for node in nodes])
                        expectations = {
                            ("node", node.id): node.revision for node in nodes
                        }
                        self.vector_store.absorb_embeddings(
                            {node.id: vectors[node.id] for node in nodes}
                        )
                        self._commit(
                            [("node", "upsert", node) for node in nodes],
                            undo,
                            expectations,
                        )
                else:
                    self.vector_store.absorb_embeddings(vectors)
                    self.save().result()
            return True

    @property
    def supports_import_merge(self) -> bool:
        """Whether ``apply_import_merge`` can run on this backend (ADR 0013
        §6): not on a store that assigns revisions, until slice 3."""
        return not self._store_assigns_revisions

    def apply_import_merge(
        self,
        prepare: Callable[[], MergeWrites],
        event_context: Optional[EventContext] = None,
    ) -> MergeApplied:
        """Apply one merge import commit all-or-nothing (ADR 0013 §4).

        ``prepare`` runs under ``_lock``, before anything changes, and returns
        the writes; the caller recomputes its plan there, so every live read
        the plan relied on is taken from a model no other writer can move
        until this returns, and raises to refuse the commit. Each create then
        expects its id absent and each overwrite the revision the plan read.
        Memory is changed, the vectors of created nodes and of overwritten
        nodes whose embedding text changed are dropped, and the whole merge
        goes to the store as one batch through ``_commit``: fresh revisions
        for the written entities only, ``generation`` unchanged. The dropped
        vectors travel with that batch on the file backend: the sidecar is
        written before the journal record, as for every write that moves a
        vector.

        A refused expectation, or any failure before the batch is handed to
        the store, puts memory back and fires nothing. A batch that failed
        after hand-over and could not be re-saved keeps the merge in memory
        and raises ``GraphWriteOutcomeUnknown``; no event fires. Otherwise one
        ordinary create or update event fires per written entity.

        Raises:
            ImportMergeUnsupported: the store assigns revisions.
            EntityRevisionConflict, GraphWriteFailed, GraphWriteOutcomeUnknown:
                as for every direct write (§4.4).
            Whatever ``prepare`` raises, with nothing changed.
        """
        with self._lock:
            if self._store_assigns_revisions:
                raise ImportMergeUnsupported(
                    "merge import is not supported on a store that assigns "
                    "revisions yet"
                )
            writes = prepare()

            expectations: Dict[Tuple[str, str], Any] = {}
            for node in writes.node_creates:
                self._check_expectation(
                    "node", node.id, self.nodes.get(node.id), EXPECT_ABSENT
                )
                expectations[("node", node.id)] = EXPECT_ABSENT
            for overwrite in writes.node_overwrites:
                self._check_expectation(
                    "node",
                    overwrite.node_id,
                    self.nodes.get(overwrite.node_id),
                    overwrite.expected_revision,
                )
                expectations[("node", overwrite.node_id)] = overwrite.expected_revision
            for edge in writes.edge_creates:
                self._check_expectation(
                    "edge", edge.id, self.edges.get(edge.id), EXPECT_ABSENT
                )
                expectations[("edge", edge.id)] = EXPECT_ABSENT
            for overwrite in writes.edge_overwrites:
                self._check_expectation(
                    "edge",
                    overwrite.edge_id,
                    self.edges.get(overwrite.edge_id),
                    overwrite.expected_revision,
                )
                expectations[("edge", overwrite.edge_id)] = overwrite.expected_revision

            undo = self._capture(
                node_ids=[node.id for node in writes.node_creates]
                + [overwrite.node_id for overwrite in writes.node_overwrites],
                edge_ids=[edge.id for edge in writes.edge_creates]
                + [overwrite.edge_id for overwrite in writes.edge_overwrites],
            )
            # A vector under an id no node holds yet: dropped with the create,
            # and put back if the merge is undone, which _undo cannot do for
            # an entity that did not exist.
            strays: Dict[str, List[float]] = {}
            for node in writes.node_creates:
                vector = self.vector_store.get_vector_list(node.id)
                if vector is not None:
                    strays[node.id] = vector

            def undone() -> None:
                if strays:
                    self.vector_store.absorb_embeddings(strays)

            node_befores: Dict[str, Dict[str, Any]] = {}
            edge_befores: Dict[str, Dict[str, Any]] = {}
            changes: List[Tuple[str, str, Any]] = []
            try:
                now = datetime.now(timezone.utc)
                for node in writes.node_creates:
                    # Vectors come from the embedding job, never the document.
                    node.embedding = None
                    # Index before `nodes`, as everywhere that adds.
                    self._searchable_text_cache[node.id] = self._build_match_fields(
                        node
                    )
                    self.nodes[node.id] = node
                    self.graph.add_node(node.id, data=node)
                    changes.append(("node", "upsert", node))
                for overwrite in writes.node_overwrites:
                    node = self.nodes[overwrite.node_id]
                    node_befores[node.id] = node.to_dict()
                    for name, value in overwrite.fields.items():
                        setattr(node, name, value)
                    node.updated_at = now
                    self.graph.nodes[node.id]["data"] = node
                    self._searchable_text_cache[node.id] = self._build_match_fields(
                        node
                    )
                    changes.append(("node", "upsert", node))
                for edge in writes.edge_creates:
                    # networkx would add a missing endpoint as a bare node.
                    if edge.source not in self.nodes or edge.target not in self.nodes:
                        raise ValueError(f"edge '{edge.id}' names a missing node")
                    self.edges[edge.id] = edge
                    self.graph.add_edge(
                        edge.source, edge.target, key=edge.id, data=edge
                    )
                    changes.append(("edge", "upsert", edge))
                for overwrite in writes.edge_overwrites:
                    edge = self.edges[overwrite.edge_id]
                    edge_befores[edge.id] = edge.to_dict()
                    for name, value in overwrite.fields.items():
                        setattr(edge, name, value)
                    changes.append(("edge", "upsert", edge))
                self.vector_store.remove_nodes_embeddings(
                    [node.id for node in writes.node_creates]
                    + [o.node_id for o in writes.node_overwrites if o.drop_vector]
                )
            except BaseException:
                self._undo(undo)
                undone()
                raise

            try:
                self._commit(changes, undo, expectations)
            except (EntityRevisionConflict, GraphWriteFailed):
                # _commit has undone memory.
                undone()
                raise
            except GraphWriteOutcomeUnknown:
                raise
            except Exception as exc:
                # The batch reached the store and neither it nor the
                # whole-graph re-save landed: memory keeps the merge, and the
                # next write carries the whole graph again.
                self._resync_pending = True
                logger.warning(f"a merge import could not be persisted: {exc}")
                raise GraphWriteOutcomeUnknown(
                    "the merge is applied in memory but could not be confirmed "
                    "written; re-read before retrying"
                ) from exc

            revisions: Dict[Tuple[str, str], int] = {}
            for kind, _, entity in changes:
                revisions[(kind, entity.id)] = entity.revision
                if kind == "node":
                    before = node_befores.get(entity.id)
                    self._emit_event(
                        event_type=(
                            EventType.NODE_CREATE
                            if before is None
                            else EventType.NODE_UPDATE
                        ),
                        entity_kind=EntityKind.NODE,
                        entity_id=entity.id,
                        entity_type=entity.type_str,
                        before=before,
                        after=entity.to_dict(),
                        context=event_context,
                    )
                else:
                    before = edge_befores.get(entity.id)
                    self._emit_event(
                        event_type=(
                            EventType.EDGE_CREATE
                            if before is None
                            else EventType.EDGE_UPDATE
                        ),
                        entity_kind=EntityKind.EDGE,
                        entity_id=entity.id,
                        entity_type=entity.type_str,
                        before=before,
                        after=entity.to_dict(),
                        context=event_context,
                    )
            return MergeApplied(
                revisions=revisions, events=len(changes), generation=self._generation
            )

    @property
    def write_lock(self) -> "threading.RLock":
        """The lock every write to this instance's graph holds - a session
        merge from its plan until memory and events follow the store. A
        staged write holds it too, so it never reads main between a merge's
        commit and memory catching up."""
        return self._lock

    # The fields a node's embedding text is built from: a write that changes
    # one of them re-embeds the node, as update_node does.
    _EMBEDDED_FIELDS = ("name", "description", "summary", "tags", "aliases")

    def apply_session_merge(
        self,
        session_id: str,
        merge_id: str,
        prepare: Callable[[], SessionMergePlan],
        event_context: Optional[EventContext] = None,
    ) -> SessionMergeLanded:
        """Apply a session merge as one unit of the layer store (§7, §11).

        Under ``_lock``: a merge id already recorded returns its record and
        writes nothing (I9; another session's id raises MergeIdTaken).
        Otherwise ``prepare`` runs, on a model no other writer on this
        instance can move until this returns, and returns the plan; it raises
        to refuse the merge. The plan's writes, checks, removals and record go
        to the store's ``merge`` through the I/O queue, after every write
        queued before it, and this waits: the store verifies every
        expectation and check inside the unit and applies all or nothing
        (I7, I8). Memory changes only once the store has committed, then one
        ordinary event fires per write, in the unit's order, carrying
        ``event_context`` and the entry's ``staged_by``.

        Raises:
            MergeRaced: an expectation or check failed in the unit; nothing
                was written.
            MergeIdTaken, StagedSessionNotActive, InvalidLayerRequest: refused
                before anything was written.
            GraphWriteFailed, GraphWriteOutcomeUnknown: the store failed and
                the merge was ruled out, or could not be confirmed (§4.4);
                for the latter memory was re-read from the store.
            ValueError: a planned entity does not validate.
            Whatever ``prepare`` raises.
        """
        from . import layer_store as _layer

        backend = self.layer_store
        if backend is None:
            raise RuntimeError("this graph backend holds no staged sessions")
        with self._lock:
            stored = backend.load_merge(merge_id)
            if stored is not None:
                replayed = _layer.replay(stored, session_id, merge_id)
                return SessionMergeLanded(record=replayed.record, replayed=True)
            plan = prepare()
            heal = self._heal_if_needed()
            if heal is not None:
                # The store's image must be this instance's before the unit
                # judges expectations against it.
                heal.result()

            entities: List[Any] = []
            for write in plan.writes:
                if write.action != "upsert":
                    entities.append(None)
                    continue
                doc = dict(write.payload)
                doc.pop("embedding", None)
                entity = (Node if write.kind == "node" else Edge).from_dict(doc)
                if write.kind == "node":
                    entity.embedding = None
                entities.append(entity)
            upserted = [entity for entity in entities if entity is not None]
            if not self._store_assigns_revisions and upserted:
                first = self._take_revisions(len(upserted))
                for offset, entity in enumerate(upserted):
                    entity.revision = first + offset
            operations = [
                EntityOperation(
                    write.kind,  # type: ignore[arg-type]
                    write.action,  # type: ignore[arg-type]
                    write.entity_id,
                    (
                        None
                        if entity is None
                        else self._serialize_node(entity)
                        if write.kind == "node"
                        else entity.to_dict()
                    ),
                    expected_revision=write.expected_revision,
                    expect_absent=write.expect_absent,
                    endpoint_types=self._endpoint_types(
                        write.kind, write.action, write.payload
                    ),
                )
                for write, entity in zip(plan.writes, entities)
            ]
            before = {
                (op.kind, op.entity_id): (
                    None
                    if (self.nodes if op.kind == "node" else self.edges).get(
                        op.entity_id
                    )
                    is None
                    else (self.nodes if op.kind == "node" else self.edges)[
                        op.entity_id
                    ].revision
                )
                for op in operations
            }
            future = self._io_executor.submit(
                backend.merge,
                session_id,
                merge_id,
                operations,
                plan.checks,
                plan.removals,
                plan.record,
            )
            self._last_write = future
            try:
                result = future.result()
            except (
                _layer.MergeRaced,
                _layer.MergeIdTaken,
                _layer.StagedSessionNotActive,
                _layer.InvalidLayerRequest,
            ):
                raise
            except Exception as exc:
                if refused_outright(exc):
                    logger.warning(f"the store refused a merge: {exc}")
                    raise GraphWriteFailed("the store refused the merge") from exc
                result = self._resolve_failed_merge(
                    session_id, merge_id, operations, before, exc
                )
            if result.replayed:
                # Recorded by another instance between the lookup above and
                # the unit; its writes reach memory as its announcement does.
                return SessionMergeLanded(record=result.record, replayed=True)

            stamps = [item.get("revision") for item in result.record["applied"]]
            fired: List[Tuple[Any, ...]] = []
            for write, entity, revision in zip(plan.writes, entities, stamps):
                fired.append(self._apply_merged_write(write, entity, revision))
            for (
                event_type,
                kind,
                entity_id,
                entity_type,
                prior,
                after,
                staged_by,
            ) in fired:
                context = (event_context or EventContext()).model_copy(
                    update={"staged_by": staged_by}
                )
                self._emit_event(
                    event_type=event_type,
                    entity_kind=kind,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    before=prior,
                    after=after,
                    context=context,
                )
            return SessionMergeLanded(
                record=result.record,
                replayed=False,
                revisions={
                    (write.kind, write.entity_id): revision
                    for write, revision in zip(plan.writes, stamps)
                },
            )

    def _apply_merged_write(self, write, entity, revision) -> Tuple[Any, ...]:
        """Put one landed merge write into memory. Callers hold _lock.

        Returns the event it fires, as the arguments of _emit_event."""
        if write.kind == "node":
            prior_node = self.nodes.get(write.entity_id)
            prior = None if prior_node is None else prior_node.to_dict()
            if write.action == "delete":
                if self.graph.has_node(write.entity_id):
                    self.graph.remove_node(write.entity_id)
                self.nodes.pop(write.entity_id, None)
                self._searchable_text_cache.pop(write.entity_id, None)
                self.vector_store.remove_nodes_embeddings([write.entity_id])
                self._inline_fallback.pop(write.entity_id, None)
                return (
                    EventType.NODE_DELETE,
                    EntityKind.NODE,
                    write.entity_id,
                    (prior or {}).get("type", "Unknown"),
                    prior,
                    None,
                    write.staged_by,
                )
            entity.revision = revision
            # Index before `nodes`, as everywhere that adds.
            self._searchable_text_cache[entity.id] = self._build_match_fields(entity)
            self.nodes[entity.id] = entity
            self.graph.add_node(entity.id, data=entity)
            if prior is None or any(
                prior.get(field) != getattr(entity, field)
                for field in self._EMBEDDED_FIELDS
            ):
                try:
                    self.vector_store.update_node_embedding(entity)
                except Exception as embed_error:
                    logger.warning(f"could not update embedding: {embed_error}")
            return (
                EventType.NODE_CREATE if prior is None else EventType.NODE_UPDATE,
                EntityKind.NODE,
                entity.id,
                entity.type_str,
                prior,
                entity.to_dict(),
                write.staged_by,
            )
        prior_edge = self.edges.get(write.entity_id)
        prior = None if prior_edge is None else prior_edge.to_dict()
        if prior_edge is not None and self.graph.has_edge(
            prior_edge.source, prior_edge.target, key=write.entity_id
        ):
            self.graph.remove_edge(
                prior_edge.source, prior_edge.target, key=write.entity_id
            )
        if write.action == "delete":
            self.edges.pop(write.entity_id, None)
            return (
                EventType.EDGE_DELETE,
                EntityKind.EDGE,
                write.entity_id,
                (prior or {}).get("type", "RELATES_TO"),
                prior,
                None,
                write.staged_by,
            )
        entity.revision = revision
        self.edges[entity.id] = entity
        self.graph.add_edge(entity.source, entity.target, key=entity.id, data=entity)
        return (
            EventType.EDGE_CREATE if prior is None else EventType.EDGE_UPDATE,
            EntityKind.EDGE,
            entity.id,
            entity.type_str,
            prior,
            entity.to_dict(),
            write.staged_by,
        )

    def _resolve_failed_merge(
        self,
        session_id: str,
        merge_id: str,
        operations: List[EntityOperation],
        before: Dict[Tuple[str, str], Optional[int]],
        failure: BaseException,
    ):
        """Decide a merge whose unit failed on its own evidence (§4.4): its
        merge record, which exists if and only if the unit committed, and, in
        a shared store, the revisions of every entity it names. Callers hold
        _lock. Returns the store's MergeResult when it landed after all;
        otherwise raises GraphWriteFailed (memory untouched) or
        GraphWriteOutcomeUnknown (graph re-read from the store)."""
        from . import layer_store as _layer

        backend = self._persistence_backend
        keys = [(op.kind, op.entity_id) for op in operations]
        shared = self._store_assigns_revisions
        readings: Optional[List[bool]] = []
        for attempt in range(2 if shared else 1):
            if attempt:
                time.sleep(self.WRITE_RESOLVE_DELAY_SECONDS)
            try:
                stored = backend.load_merge(merge_id)  # type: ignore[attr-defined]
                if stored is not None and stored.get("session_id") == session_id:
                    logger.warning(
                        f"a merge reported a failure but landed ({failure}); "
                        f"completing it"
                    )
                    return _layer.MergeResult(record=stored)
                unchanged = True
                if shared and keys:
                    evidence = backend.read_write_evidence(keys)  # type: ignore[attr-defined]
                    unchanged = all(
                        (evidence.get(key) or (None,))[0] == before[key] for key in keys
                    )
            except Exception as exc:
                logger.warning(f"could not read back a failed merge: {exc}")
                readings = None
                break
            readings.append(unchanged)
        logger.warning(f"a merge failed: {failure}")
        if readings and all(readings):
            raise GraphWriteFailed("the merge did not land") from failure
        try:
            self.load(bootstrap_if_missing=False)
        except Exception as exc:
            logger.warning(
                f"could not re-read the graph after a merge of unknown outcome "
                f"({exc}); memory may differ from the store until the next "
                f"reported change"
            )
        raise GraphWriteOutcomeUnknown(
            "could not confirm whether the merge landed; re-read before retrying"
        ) from failure

    def update_node(
        self,
        node_id: str,
        updates: Dict,
        event_context: Optional[EventContext] = None,
        *,
        metadata_merge: bool = False,
        expected_updated_at: Optional[Any] = None,
        expected_revision: Optional[int] = None,
        validate: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]
        ] = None,
    ) -> Optional[Node]:
        """
        Update an existing node.

        ``validate(before, after)`` - the node as it stands and as the update
        would leave it - runs under the lock, on the node this write actually
        replaces; a reason it returns refuses the update as ``ValueError``.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            node_id: ID of the node to update
            updates: Dict with fields to update
            event_context: Optional context for event tracking and loop prevention
            metadata_merge: When True, ``metadata`` (and any schema-defined extra
                fields folded into metadata) is deep-merged at the top level onto
                the node's existing metadata instead of replacing it wholesale.
                A key whose value is ``None`` deletes that key (RFC 7386
                null-means-delete). Keys not mentioned are preserved. Default is
                False, i.e. the legacy replace-whole-metadata behaviour.
            expected_updated_at: Optional optimistic-concurrency guard. When set
                (an ISO 8601 string — as returned in a node's ``updated_at`` — or
                a timezone-aware datetime), the update only proceeds if the node's
                current ``updated_at`` still equals this value; a mismatch raises
                :class:`StaleUpdateError` instead of clobbering a concurrent write.
                Note ``updated_at`` is a UTC wall-clock timestamp used as the
                version token: two writes within the same microsecond tick would
                share a timestamp, so this guards against realistic interleaving,
                not adversarial same-tick races.
            expected_revision: Optional expectation (§4.3): the update applies
                only while the node's ``revision`` equals this. Checked here
                and again by a store that enforces revisions. When both this
                and ``expected_updated_at`` are given, both must hold.

        Raises:
            StaleUpdateError: if ``expected_updated_at`` no longer matches the
                node's live ``updated_at``.
            EntityRevisionConflict: if ``expected_revision`` does not hold,
                including for a node that does not exist.
            GraphWriteFailed, GraphWriteOutcomeUnknown: a shared-store write
                that did not land, or whose landing is unknown (§4.4).
            ValueError: if the resulting node fails model validation.
        """
        with self._lock:
            if expected_revision is not None:
                self._check_expectation(
                    "node", node_id, self.nodes.get(node_id), expected_revision
                )
            if node_id not in self.nodes:
                return None

            node = self.nodes[node_id]

            # Optimistic-concurrency guard: reject the write if the node changed
            # since the caller read it. Checked inside the lock so the compare and
            # the subsequent mutation are atomic with respect to other writers.
            if expected_updated_at is not None:
                expected = (
                    _parse_datetime(expected_updated_at)
                    if isinstance(expected_updated_at, str)
                    else expected_updated_at
                )
                if node.updated_at != expected:
                    raise StaleUpdateError(
                        node_id, expected_updated_at, node.updated_at
                    )

            # Capture before state for events
            before_state = node.to_dict()

            validated = build_node_update(
                node.to_dict(), updates, metadata_merge=metadata_merge
            )
            if validate is not None:
                refused = validate(node.to_dict(), validated.to_dict())
                if refused:
                    raise ValueError(refused)
            allowed_fields = _NODE_UPDATE_FIELDS

            undo = self._capture(node_ids=[node_id])

            # Apply the validated field values to the live node.
            for key in allowed_fields:
                setattr(node, key, getattr(validated, key))

            node.updated_at = datetime.now(timezone.utc)

            # Update in graph
            self.graph.nodes[node_id]["data"] = node

            # Update searchable text cache
            self._searchable_text_cache[node.id] = self._build_match_fields(node)

            # Update embedding if text fields or tags changed (non-blocking)
            if any(
                k in updates
                for k in ["name", "description", "summary", "tags", "aliases"]
            ):
                try:
                    self.vector_store.update_node_embedding(node)
                except Exception as embed_error:
                    logger.warning(f"could not update embedding: {embed_error}")

            self._commit(
                [("node", "upsert", node)],
                undo,
                {("node", node_id): expected_revision}
                if expected_revision is not None
                else None,
            )

            # Emit update event
            node_type = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            self._emit_event(
                event_type=EventType.NODE_UPDATE,
                entity_kind=EntityKind.NODE,
                entity_id=node_id,
                entity_type=node_type,
                before=before_state,
                after=node.to_dict(),
                context=event_context,
            )

            return node

    def set_nodes_archived(
        self,
        node_ids: List[str],
        archived: bool,
        event_context: Optional[EventContext] = None,
        *,
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> List[Node]:
        """Set the ``archived`` flag on the given nodes.

        Archiving hides a node from search/traversal by default while keeping it
        (and its edges) in the graph, unlike deletion which is permanent. Returns
        every node that was found (whether or not its state changed) so callers
        get an idempotent view; only nodes whose flag actually changed emit an
        update event, and the graph is saved once if anything changed.

        ``expected_revisions`` maps node ids to the revision each must still
        have (§4.3). Every one is checked before anything changes; one that
        does not hold raises EntityRevisionConflict and archives nothing.

        Thread-safe: protected by _lock for the entire operation.
        """
        with self._lock:
            for node_id, expected in (expected_revisions or {}).items():
                self._check_expectation(
                    "node", node_id, self.nodes.get(node_id), expected
                )
            found: List[Node] = []
            # A node whose flag already matches takes no write, so its
            # revision does not move; an expectation on it was checked above.
            to_change: Dict[str, Node] = {}
            for node_id in node_ids:
                node = self.nodes.get(node_id)
                if node is None:
                    continue
                found.append(node)
                if node.archived != archived:
                    to_change.setdefault(node_id, node)

            undo = self._capture(node_ids=list(to_change))
            before_states: Dict[str, Dict[str, Any]] = {}
            for node in to_change.values():
                before_states[node.id] = node.to_dict()
                node.archived = archived
                node.updated_at = datetime.now(timezone.utc)
                self.graph.nodes[node.id]["data"] = node

            self._commit(
                [("node", "upsert", node) for node in to_change.values()],
                undo,
                {
                    ("node", node_id): expected
                    for node_id, expected in (expected_revisions or {}).items()
                    if node_id in before_states
                },
            )

            # After the write, so each event carries the revision it landed at.
            for node in to_change.values():
                node_type = (
                    node.type.value if hasattr(node.type, "value") else str(node.type)
                )
                self._emit_event(
                    event_type=EventType.NODE_UPDATE,
                    entity_kind=EntityKind.NODE,
                    entity_id=node.id,
                    entity_type=node_type,
                    before=before_states[node.id],
                    after=node.to_dict(),
                    context=event_context,
                )

            return found

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False,
        event_context: Optional[EventContext] = None,
        *,
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> DeleteNodesResult:
        """
        Delete nodes (max 10 at a time for safety).
        Requires confirmed=True.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            node_ids: List of node IDs to delete
            confirmed: Must be True to execute deletion
            event_context: Optional context for event tracking and loop prevention
            expected_revisions: node id -> the revision it must still have
                (§4.3). All are checked before anything is deleted; one that
                does not hold raises EntityRevisionConflict. Incident edges
                deleted with a node carry no expectation.
        """
        with self._lock:
            if len(node_ids) > 10:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message="Max 10 nodes can be deleted at a time. Contact admin for bulk deletion.",
                )

            if not confirmed:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message="Deletion requires confirmed=True parameter",
                )

            for node_id, expected in (expected_revisions or {}).items():
                self._check_expectation(
                    "node", node_id, self.nodes.get(node_id), expected
                )

            deleted_node_ids = []
            affected_edge_ids = []

            # Capture before states for events
            node_before_states: Dict[str, Dict[str, Any]] = {}
            edge_before_states: Dict[str, Dict[str, Any]] = {}

            present = [nid for nid in dict.fromkeys(node_ids) if nid in self.nodes]
            incident = [edge.id for edge in self.get_incident_edges(present)]
            undo = self._capture(node_ids=present, edge_ids=incident)

            try:
                for node_id in node_ids:
                    if node_id not in self.nodes:
                        continue

                    # Capture node before state
                    node = self.nodes[node_id]
                    node_before_states[node_id] = node.to_dict()

                    # Find all edges connected to this node
                    edges_to_remove = []
                    if self.graph.has_node(node_id):
                        seen_edges = set()
                        for _, _, edge_id in self.graph.out_edges(node_id, keys=True):
                            edges_to_remove.append(edge_id)
                            seen_edges.add(edge_id)
                        for _, _, edge_id in self.graph.in_edges(node_id, keys=True):
                            if edge_id not in seen_edges:
                                edges_to_remove.append(edge_id)
                                seen_edges.add(edge_id)

                    for edge_id in edges_to_remove:
                        affected_edge_ids.append(edge_id)
                        # Capture edge before state
                        if edge_id not in edge_before_states:
                            edge_before_states[edge_id] = self.edges[edge_id].to_dict()

                    # Remove edges
                    for edge_id in edges_to_remove:
                        edge = self.edges[edge_id]
                        self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                        del self.edges[edge_id]

                    # Remove node
                    self.graph.remove_node(node_id)
                    del self.nodes[node_id]
                    self._searchable_text_cache.pop(node_id, None)
                    deleted_node_ids.append(node_id)

                # Remove embeddings
                self.vector_store.remove_nodes_embeddings(deleted_node_ids)
                # And the graph.json fallback copies, or an id created again
                # later would inherit the departed node's vector.
                for node_id in deleted_node_ids:
                    self._inline_fallback.pop(node_id, None)

                # Edges first, for the same reason the events below go out in
                # that order: an edge must never outlive an endpoint in the store.
                self._commit(
                    [("edge", "delete", edge_id) for edge_id in edge_before_states]
                    + [("node", "delete", node_id) for node_id in deleted_node_ids],
                    undo,
                    {
                        ("node", node_id): expected
                        for node_id, expected in (expected_revisions or {}).items()
                    },
                )

                # Emit delete events for edges (before nodes, to maintain referential integrity info)
                for edge_id, before_state in edge_before_states.items():
                    edge_type = before_state.get("type", "RELATES_TO")
                    self._emit_event(
                        event_type=EventType.EDGE_DELETE,
                        entity_kind=EntityKind.EDGE,
                        entity_id=edge_id,
                        entity_type=edge_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                # Emit delete events for nodes
                for node_id, before_state in node_before_states.items():
                    node_type = before_state.get("type", "Unknown")
                    self._emit_event(
                        event_type=EventType.NODE_DELETE,
                        entity_kind=EntityKind.NODE,
                        entity_id=node_id,
                        entity_type=node_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                return DeleteNodesResult(
                    deleted_node_ids=deleted_node_ids,
                    affected_edge_ids=affected_edge_ids,
                    success=True,
                    message=f"Deleted {len(deleted_node_ids)} nodes and {len(affected_edge_ids)} edges",
                )

            except (
                EntityRevisionConflict,
                GraphWriteFailed,
                GraphWriteOutcomeUnknown,
            ):
                raise
            except Exception as e:
                return DeleteNodesResult(
                    deleted_node_ids=[],
                    affected_edge_ids=[],
                    success=False,
                    message=f"Error during deletion: {str(e)}",
                )

    def get_node_count(self) -> int:
        """Total node count, for callers that only need a count and should not
        pay for building the full per-type breakdown get_stats() computes."""
        return len(self.nodes)

    def get_stats(self) -> GraphStats:
        """Get statistics for the graph"""
        # Count nodes per type
        nodes_by_type = {}
        for node in self.nodes.values():
            type_name = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            nodes_by_type[type_name] = nodes_by_type.get(type_name, 0) + 1

        # Count edges per type, for the metamodel explorer's relationship counts.
        edges_by_type = {}
        for edge in self.edges.values():
            type_name = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            edges_by_type[type_name] = edges_by_type.get(type_name, 0) + 1

        return GraphStats(
            total_nodes=len(self.nodes),
            total_edges=len(self.edges),
            nodes_by_type=nodes_by_type,
            edges_by_type=edges_by_type,
            last_updated=datetime.now(timezone.utc),
        )

    def get_subtypes_by_node_type(
        self, node_type: Optional[str] = None
    ) -> Dict[str, List[str]]:
        """Get all unique subtypes grouped by node type.

        Args:
            node_type: If provided, only return subtypes for this node type.

        Returns:
            Dict mapping node type names to sorted lists of unique subtypes.
        """
        result: Dict[str, set] = {}
        for node in self.nodes.values():
            type_name = (
                node.type.value if hasattr(node.type, "value") else str(node.type)
            )
            if node_type and type_name != node_type:
                continue
            if hasattr(node, "subtypes") and node.subtypes:
                if type_name not in result:
                    result[type_name] = set()
                result[type_name].update(node.subtypes)
        return {k: sorted(v) for k, v in result.items()}

    def get_edges_between_nodes(self, node_ids: List[str]) -> List[Edge]:
        """Get all edges where both source and target are in the given node IDs"""
        node_id_set = set(node_ids)
        valid_nodes = [nid for nid in node_id_set if nid in self.graph]
        subgraph = self.graph.subgraph(valid_nodes)
        return [data["data"] for _, _, data in subgraph.edges(data=True)]

    def get_edges_from_to(self, source: str, target: str) -> List[Edge]:
        """Edges directed from ``source`` to ``target``: a hash lookup, so a
        caller checking many pairs pays for the pairs, not the graph."""
        bundle = self.graph.get_edge_data(source, target) or {}
        return [data["data"] for data in list(bundle.values())]

    def get_edges_for_node(self, node_id: str) -> List[Edge]:
        """Get all edges connected to a specific node"""
        if node_id not in self.graph:
            return []

        collected_edges = {}
        # Outgoing edges
        for _, _, _, edge_data in self.graph.out_edges(node_id, keys=True, data=True):
            edge = edge_data["data"]
            collected_edges[edge.id] = edge

        # Incoming edges
        for _, _, _, edge_data in self.graph.in_edges(node_id, keys=True, data=True):
            edge = edge_data["data"]
            collected_edges[edge.id] = edge

        return list(collected_edges.values())

    def update_edge(
        self,
        edge_id: str,
        updates: Dict,
        event_context: Optional[EventContext] = None,
        *,
        expected_revision: Optional[int] = None,
    ) -> Optional[Edge]:
        """
        Update an existing edge.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge_id: ID of the edge to update
            updates: Dict with fields to update (type, label, metadata)
            event_context: Optional context for event tracking
            expected_revision: Optional expectation (§4.3); a mismatch, or an
                edge that does not exist, raises EntityRevisionConflict.
        """
        with self._lock:
            if expected_revision is not None:
                self._check_expectation(
                    "edge", edge_id, self.edges.get(edge_id), expected_revision
                )
            if edge_id not in self.edges:
                return None

            edge = self.edges[edge_id]
            before_state = edge.to_dict()

            allowed_fields = EDGE_UPDATE_FIELDS
            validated_edge = build_edge_update(edge.to_dict(), updates)
            self._validate_edge_applicability(validated_edge)

            undo = self._capture(edge_ids=[edge_id])
            for key in allowed_fields:
                setattr(edge, key, getattr(validated_edge, key))

            self._commit(
                [("edge", "upsert", edge)],
                undo,
                {("edge", edge_id): expected_revision}
                if expected_revision is not None
                else None,
            )

            # Emit update event
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            self._emit_event(
                event_type=EventType.EDGE_UPDATE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge_id,
                entity_type=edge_type,
                before=before_state,
                after=edge.to_dict(),
                context=event_context,
            )

            return edge

    def set_edges_archived(
        self,
        edge_ids: List[str],
        archived: bool,
        event_context: Optional[EventContext] = None,
        *,
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> List[Edge]:
        """Set the ``archived`` flag on the given edges.

        Mirrors :meth:`set_nodes_archived`: archived edges are hidden from
        search/traversal by default but remain in the graph. Returns every edge
        that was found; only edges whose flag changed emit an update event, and
        the graph is saved once if anything changed. ``expected_revisions``
        works as it does there.

        Thread-safe: protected by _lock for the entire operation.
        """
        with self._lock:
            for edge_id, expected in (expected_revisions or {}).items():
                self._check_expectation(
                    "edge", edge_id, self.edges.get(edge_id), expected
                )
            found: List[Edge] = []
            to_change: Dict[str, Edge] = {}
            for edge_id in edge_ids:
                edge = self.edges.get(edge_id)
                if edge is None:
                    continue
                found.append(edge)
                if edge.archived != archived:
                    to_change.setdefault(edge_id, edge)

            undo = self._capture(edge_ids=list(to_change))
            before_states: Dict[str, Dict[str, Any]] = {}
            for edge in to_change.values():
                before_states[edge.id] = edge.to_dict()
                edge.archived = archived

            self._commit(
                [("edge", "upsert", edge) for edge in to_change.values()],
                undo,
                {
                    ("edge", edge_id): expected
                    for edge_id, expected in (expected_revisions or {}).items()
                    if edge_id in before_states
                },
            )

            for edge in to_change.values():
                edge_type = (
                    edge.type.value if hasattr(edge.type, "value") else str(edge.type)
                )
                self._emit_event(
                    event_type=EventType.EDGE_UPDATE,
                    entity_kind=EntityKind.EDGE,
                    entity_id=edge.id,
                    entity_type=edge_type,
                    before=before_states[edge.id],
                    after=edge.to_dict(),
                    context=event_context,
                )

            return found

    def delete_edge(
        self,
        edge_id: str,
        event_context: Optional[EventContext] = None,
        *,
        expected_revision: Optional[int] = None,
    ) -> bool:
        """
        Delete a single edge.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge_id: ID of the edge to delete
            event_context: Optional context for event tracking
            expected_revision: Optional expectation (§4.3); a mismatch, or an
                edge that does not exist, raises EntityRevisionConflict.

        Returns:
            True if edge was deleted, False if not found
        """
        with self._lock:
            if expected_revision is not None:
                self._check_expectation(
                    "edge", edge_id, self.edges.get(edge_id), expected_revision
                )
            if edge_id not in self.edges:
                return False
            undo = self._capture(edge_ids=[edge_id])

            edge = self.edges[edge_id]
            before_state = edge.to_dict()
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )

            # Remove from graph
            try:
                self.graph.remove_edge(edge.source, edge.target, key=edge_id)
            except Exception:
                pass  # Edge might not exist in graph

            # Remove from edges dict
            del self.edges[edge_id]

            self._commit(
                [("edge", "delete", edge_id)],
                undo,
                {("edge", edge_id): expected_revision}
                if expected_revision is not None
                else None,
            )

            # Emit delete event
            self._emit_event(
                event_type=EventType.EDGE_DELETE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge_id,
                entity_type=edge_type,
                before=before_state,
                after=None,
                context=event_context,
            )

            return True

    def delete_edges(
        self,
        edge_ids: List[str],
        event_context: Optional[EventContext] = None,
        *,
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> DeleteEdgesResult:
        """Delete edges by ID. Limit enforcement is the caller's responsibility.

        ``expected_revisions`` maps edge ids to the revision each must still
        have (§4.3), all checked before anything is deleted.
        """
        with self._lock:
            for edge_id, expected in (expected_revisions or {}).items():
                self._check_expectation(
                    "edge", edge_id, self.edges.get(edge_id), expected
                )
            deleted_edge_ids = []
            edge_before_states: Dict[str, Dict[str, Any]] = {}
            undo = self._capture(
                edge_ids=[eid for eid in dict.fromkeys(edge_ids) if eid in self.edges]
            )

            try:
                for edge_id in edge_ids:
                    edge = self.edges.get(edge_id)
                    if edge is None:
                        continue

                    edge_before_states[edge_id] = edge.to_dict()

                    try:
                        self.graph.remove_edge(edge.source, edge.target, key=edge_id)
                    except Exception:
                        pass

                    del self.edges[edge_id]
                    deleted_edge_ids.append(edge_id)

                self._commit(
                    [("edge", "delete", edge_id) for edge_id in deleted_edge_ids],
                    undo,
                    {
                        ("edge", edge_id): expected
                        for edge_id, expected in (expected_revisions or {}).items()
                    },
                )

                for edge_id in deleted_edge_ids:
                    before_state = edge_before_states[edge_id]
                    edge_type = before_state.get("type", "RELATES_TO")
                    self._emit_event(
                        event_type=EventType.EDGE_DELETE,
                        entity_kind=EntityKind.EDGE,
                        entity_id=edge_id,
                        entity_type=edge_type,
                        before=before_state,
                        after=None,
                        context=event_context,
                    )

                return DeleteEdgesResult(
                    deleted_edge_ids=deleted_edge_ids,
                    success=True,
                    message=f"Deleted {len(deleted_edge_ids)} edges",
                )
            except (
                EntityRevisionConflict,
                GraphWriteFailed,
                GraphWriteOutcomeUnknown,
            ):
                raise
            except Exception as e:
                return DeleteEdgesResult(
                    deleted_edge_ids=[],
                    success=False,
                    message=f"Error during edge deletion: {str(e)}",
                )

    def add_edge(
        self,
        edge: Edge,
        event_context: Optional[EventContext] = None,
        *,
        expect_absent: bool = False,
    ) -> Optional[str]:
        """
        Add a single edge between existing nodes.

        Thread-safe: Protected by _lock for the entire operation.

        Args:
            edge: Edge object to add
            event_context: Optional context for event tracking
            expect_absent: the create expects no edge with this id to exist
                (§4.3); one that does raises EntityRevisionConflict.

        Returns:
            Edge ID if successful, None if failed
        """
        with self._lock:
            if expect_absent:
                self._check_expectation(
                    "edge", edge.id, self.edges.get(edge.id), EXPECT_ABSENT
                )
            # Validate source and target exist
            if edge.source not in self.nodes:
                # Try name resolution
                name_to_id = {n.name: nid for nid, n in self.nodes.items()}
                if edge.source in name_to_id:
                    edge.source = name_to_id[edge.source]
                else:
                    return None

            if edge.target not in self.nodes:
                name_to_id = {n.name: nid for nid, n in self.nodes.items()}
                if edge.target in name_to_id:
                    edge.target = name_to_id[edge.target]
                else:
                    return None

            if edge.id in self.edges:
                return None

            self._validate_edge_applicability(edge)

            undo = _Undo()
            undo.edges[edge.id] = None
            self.edges[edge.id] = edge
            self.graph.add_edge(edge.source, edge.target, key=edge.id, data=edge)

            # On a shared store the create expects absence whatever the
            # caller asked - see add_nodes.
            self._commit(
                [("edge", "upsert", edge)],
                undo,
                {("edge", edge.id): EXPECT_ABSENT}
                if expect_absent or self._store_assigns_revisions
                else None,
            )

            # Emit create event
            edge_type = (
                edge.type.value if hasattr(edge.type, "value") else str(edge.type)
            )
            self._emit_event(
                event_type=EventType.EDGE_CREATE,
                entity_kind=EntityKind.EDGE,
                entity_id=edge.id,
                entity_type=edge_type,
                before=None,
                after=edge.to_dict(),
                context=event_context,
            )

            return edge.id

    def get_incident_edges(self, node_ids: List[str]) -> List[Edge]:
        """
        Get all edges connected to any of the given nodes (incoming or outgoing).
        More efficient than iterating through all edges.
        """
        collected_edges = {}
        valid_node_ids = [
            node_id for node_id in node_ids if node_id in self.nodes and node_id in self.graph
        ]

        if valid_node_ids:
            # Outgoing edges
            for _, _, _, edge_data in self.graph.out_edges(
                valid_node_ids, keys=True, data=True
            ):
                edge = edge_data["data"]
                collected_edges[edge.id] = edge

            # Incoming edges
            for _, _, _, edge_data in self.graph.in_edges(
                valid_node_ids, keys=True, data=True
            ):
                edge = edge_data["data"]
                collected_edges[edge.id] = edge

        return list(collected_edges.values())
