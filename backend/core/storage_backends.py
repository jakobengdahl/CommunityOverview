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

import dataclasses
import json
import logging
import os
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from backend.runtime.errors import GraphWriteFailed as GraphWriteFailed

logger = logging.getLogger(__name__)


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
        made behind its back (another instance sharing the same store). It
        implements the notification protocol, and GraphStorage subscribes to
        it and refreshes what the reported entities touch.
    store_traversal: the backend can answer a bounded-depth traversal itself,
        so the application does not need the topology in memory to do it. It
        implements `traverse`. A backend that cannot say this is traversed
        the way it always was, in process.
    revision_enforcement: the store assigns every entity's `revision` from a
        counter of its own and checks a write's expectation in the statement
        that applies it (docs/SESSION_OVERLAY_CONTRACT.md §4.4). `apply_batch`
        then returns the revisions it stored, raises EntityRevisionConflict
        for an expectation that does not hold, and the backend implements
        `read_write_evidence`. A shared store needs it for revisions to mean
        anything: without it, two instances would each count on their own.
    staged_sessions: the backend holds session layers, staged-session records
        and merge records next to the graph (docs/SESSION_OVERLAY_CONTRACT.md
        §7, backend/core/layer_store.py). It implements the layer store, and
        `merge` applies a merge's graph writes and its layer removal as one
        unit. Needs `incremental_writes` and `transactions`, and a backend
        that also declares `change_notification` - a shared store - must
        declare `revision_enforcement` too (§4.4).
    """

    incremental_writes: bool = False
    transactions: bool = False
    change_notification: bool = False
    store_traversal: bool = False
    revision_enforcement: bool = False
    staged_sessions: bool = False


SNAPSHOT_ONLY = BackendCapabilities()

EntityKindName = Literal["node", "edge"]
EntityActionName = Literal["upsert", "delete"]

_TRAVERSAL_METHODS = ("traverse",)

_REVISION_METHODS = ("read_write_evidence",)

# backend.core.layer_store.LayerStoreBackend; named here so this module need
# not import that one, which imports this.
_STAGED_SESSION_METHODS = (
    "load_layer",
    "stage",
    "merge",
    "discard",
    "load_merge",
    "drop_layer",
    "record_staged",
    "staged_status",
    "retire",
    "layers_in_use",
)

_NOTIFICATION_METHODS = (
    "start_change_notification",
    "stop_change_notification",
)

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

    `expected_revision` and `expect_absent` are the write's expectation
    (docs/SESSION_OVERLAY_CONTRACT.md §4.3). Only a backend declaring
    `revision_enforcement` reads them; the instance has already checked them
    against its own model before the operation is issued. They are never
    journaled: they are a condition of applying the write, not part of it.

    `endpoint_types`, for an edge upsert, is `(node id, type)` for each
    endpoint the instance judged the edge's relationship type against. A
    backend declaring `revision_enforcement` refuses the write when the
    store holds another type for one of them, or no longer holds it - an
    endpoint the same batch upserts is judged by that upsert instead - so an
    edge judged before a merge retyped its endpoint on another instance
    cannot land after it.
    Not journaled either, for the same reason.
    """

    kind: EntityKindName
    action: EntityActionName
    entity_id: str
    payload: Optional[Dict[str, Any]] = None
    expected_revision: Optional[int] = None
    expect_absent: bool = False
    endpoint_types: Tuple[Tuple[str, str], ...] = ()

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


@dataclass(frozen=True)
class ExternalChange:
    """What a backend reports when the store changed behind the application.

    ``operations`` names the entities that changed and carries their new
    content, in the order the store applied them - the same operations a
    mutation is delivered as, read the other way round. A backend that learns
    only that *something* changed, and cannot say what, reports ``unknown()``
    instead and the application reloads the whole graph.

    A backend that can re-read its own store should report
    ``entities_read_on_demand`` instead of ``entities``, and it is worth
    understanding why before choosing. Content gathered when the report is
    DISPATCHED can be older than the receiving instance's own writes, which
    may still be queued at that moment. The application cannot tell, so it
    protects itself with a wall clock - it keeps whichever version carries the
    later ``updated_at`` - and that clock does not order the commits. When the
    write that committed last carries the earlier stamp, the instance holding
    the later stamp refuses the store's value and goes on refusing it: there
    is no further change to report, and the two instances stay apart for good.
    Measured on PostgreSQL 16, five racing renames from each of two
    instances: four runs in eight ended divergent on an idle machine, and none
    at all under load - so a race is not what catches this.

    Deferring the read removes the ambiguity rather than arbitrating it. The
    application settles its own writes FIRST and only then asks, so what comes
    back is the store's own answer with this instance's work already in it.
    There is then nothing of ours to protect and no clock to consult.
    """

    operations: Optional[Tuple[EntityOperation, ...]]
    read_content: Optional[Callable[[], Iterable[EntityOperation]]] = None

    @classmethod
    def entities(cls, operations: Iterable[EntityOperation]) -> "ExternalChange":
        """Named entities changed, content gathered now.

        For a backend that cannot re-read on demand. Its NODE upserts are
        arbitrated against the application's own by wall clock - an edge
        upsert has no stamp to arbitrate by and a delete has no payload, so
        both are applied as reported. See above for what the clock costs.
        """
        return cls(tuple(operations))

    @classmethod
    def entities_read_on_demand(
        cls, read_content: Callable[[], Iterable[EntityOperation]]
    ) -> "ExternalChange":
        """Named entities changed; the application asks for the content.

        ``read_content`` must return the operations describing what the store
        holds AT THE MOMENT IT IS CALLED. What it returns is applied as the
        store's own answer, with no clock consulted - which is sound precisely
        because it is called after this instance's writes have landed. Raising
        from it is not an error to swallow: it becomes a whole-graph reload.

        Three things about when it is called decide whether an implementation
        of it is correct:

        - It is normally called on **the thread the report was delivered on**,
          further down that call stack, because the application applies a
          report inline. A backend that dispatches from a poller it needs to
          keep polling has to hand the report to another thread itself.
          The exception is a report that arrives before the application's
          first load has returned: that one is held and replayed on the
          thread that finished the load, with the dispatching thread long
          gone. So `read_content` must not close over anything bound to the
          thread it was created on - a thread-local, a session, a cursor.
          Take a pooled or otherwise thread-agnostic connection.
        - The application's lock is held throughout, so it must not call back
          into the storage, and its LATENCY IS THAT INSTANCE'S WRITE STALL:
          every mutation waits for it. Bound the read - a pool with no
          timeout, or one long enough to wait out a hung server, stalls the
          instance for exactly that long. For a replayed report it is the
          BOOT that stalls instead: no mutation can be queued yet, and
          construction is not finished until the replay is.
        - It is called at most once per report; a second ask returns what the
          first read.
        """
        return cls(None, read_content)

    @classmethod
    def unknown(cls) -> "ExternalChange":
        """Something changed and the backend cannot say what."""
        return cls(None)

    def content_read_on_demand(self) -> bool:
        return self.read_content is not None

    def with_content(self) -> "ExternalChange":
        """This change with its content in hand. Callers must have settled.

        Read once, then remembered. A report is observed in more than one
        place - the application applies it, a test records what arrived - and
        asking twice would mean two reads of the store at two moments, of
        which only the first was made after the settle. So the second ask
        returns the first ask's answer rather than a fresher one.
        """
        if self.read_content is None:
            return self
        # Not a field, so that a report stays a value: what one happens to
        # have been asked for has no business in its equality, its hash or
        # its repr, and a memo declared as a field is in all three.
        content = getattr(self, "_content", None)
        if content is None:
            content = tuple(self.read_content())
            object.__setattr__(self, "_content", content)
        return ExternalChange(content)


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


class RevisionEnforcingBackend(Protocol):
    """The revision contract, for a backend declaring `revision_enforcement`.

    docs/SESSION_OVERLAY_CONTRACT.md §4.4. The store assigns each upsert the
    next value of its own revision counter, checks every operation's
    expectation in the statement that applies it, and stores `write_token`
    with every entity it writes. The token is the write's own evidence: it
    lets the instance tell, after a failure, whether that write committed.
    It is server-internal and never part of an entity a read returns.
    """

    def apply_batch(
        self,
        operations: Sequence[EntityOperation],
        write_token: Optional[str] = None,
    ) -> List[Optional[int]]:
        """Apply atomically; return each upsert's stored revision (None for
        a delete). Raise EntityRevisionConflict, applying nothing, when an
        expectation does not hold."""

    def read_write_evidence(
        self, keys: Sequence[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], Optional[Tuple[int, Optional[str]]]]:
        """For each (kind, id): (revision, write token) as stored, or None
        when the entity is absent. Raises when the store cannot be read."""


class EntityRevisionConflict(RuntimeError):
    """A write's expectation about an entity did not hold.

    Raised by GraphStorage when its own model refutes the expectation, and by
    a backend declaring `revision_enforcement` when the store does - in which
    case nothing in the batch was applied. `current_revision` is None when
    the entity does not exist (an update or delete of something absent) and
    the entity's revision when a create expected it absent.
    """

    code = "entity_revision_conflict"

    def __init__(
        self,
        kind: str,
        entity_id: str,
        expected: Optional[int],
        current: Optional[int],
    ):
        self.kind = kind
        self.entity_id = entity_id
        self.expected_revision = expected
        self.current_revision = current
        wanted = "absent" if expected is None else f"revision {expected}"
        found = "absent" if current is None else f"revision {current}"
        super().__init__(f"{kind} {entity_id!r}: expected {wanted}, found {found}")


class NodeTypeChanged(EntityRevisionConflict):
    """A node the write was judged on no longer has that type in the store:
    an edge's endpoint, or a node updated in place over a retype it has not
    seen.

    A conflict like any other expectation that did not hold: nothing in the
    batch was applied, and the caller re-reads before trying again. Without a
    revision: the node may be one the caller can no longer see.
    """

    def __init__(self, node_id: str, judged: Optional[str]):
        super().__init__("node", node_id, None, None)
        self.args = (f"node {node_id!r}: no longer of type {judged!r}",)


class WriteRefusedByStore(RuntimeError):
    """The store refused a write outright and applied none of it.

    A refusal is decided inside the transaction - a value the store cannot
    hold, a constraint, a row another scope owns - so there is nothing in
    flight to wait for: GraphStorage answers it with `write_failed` at once
    instead of reading back the write's evidence (§4.4), which would only
    hold the storage lock for nothing.
    """


# SQLSTATE classes that are a statement refused, never a commit lost: data
# exception, integrity constraint violation, a program limit (an id too large
# to index, say), syntax error or access rule.
REFUSAL_SQLSTATE_CLASSES = frozenset({"22", "23", "42", "54"})


def refused_outright(exc: BaseException) -> bool:
    """Whether a failed write is a refusal that provably applied nothing."""
    if isinstance(exc, WriteRefusedByStore):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in REFUSAL_SQLSTATE_CLASSES


class GraphWriteOutcomeUnknown(RuntimeError):
    """A shared-store write whose landing could neither be confirmed nor
    ruled out (§4.4: `write_outcome_unknown`).

    No event fired, and the instance has re-read the graph from the store.
    The caller re-reads before retrying.
    """

    code = "write_outcome_unknown"


class ExternalChangeRefused(RuntimeError):
    """Raised back at a backend that reported a change on a writing thread.

    Distinct from an I/O failure on purpose. A backend that breaks the
    threading rule below raises this out of its own write call, and the
    application must not read that as the write having failed: a failed write
    escalates to re-issuing the whole graph, which on a shared store would
    overwrite what the other writer just committed.
    """


@runtime_checkable
class TraversingBackend(Protocol):
    """A backend that can walk the graph itself, bounded by depth.

    Declared with ``store_traversal``. Without it the application walks its own
    in-memory topology, which is only possible while the whole graph is
    resident - the constraint this exists to remove.

    The contract is exact, because there are now two implementations of it and
    a difference between them is a difference in what the product returns. The
    in-memory walk in `storage_search.get_related_nodes` is the reference; a
    backend implementing this must agree with it on every graph, and
    `test_traversal_equivalence.py` is what holds them to that.

    Ids only, deliberately. What a traversal must not need is the payloads: the
    caller resolves them, from memory while the graph is resident and from the
    store when it is not. Returning documents here would move that decision
    into every backend.

    The rules, all of which the in-memory walk already follows and none of
    which are obvious:

    - The anchor is in the result even when it is archived, and even when no
      edge reaches it. An anchor absent from the graph returns nothing at all.
    - An edge is returned when ONE OF ITS ENDPOINTS WAS EXPANDED - reached at a
      distance strictly less than `depth` - not merely when both endpoints are
      in the result. Two nodes that are both exactly `depth` away are in the
      node set, and the edge between them is not.
    - An archived node is not traversed and not returned, and neither is the
      edge that would have reached it. It cannot re-enter at a later hop. The
      anchor is the exception.
    - An edge whose far endpoint is not in the graph at all IS returned; the
      missing id is not. A traversal continues through such an id, since the
      edges that name it are still edges.
    - `relationship_types` filters edges by type; an empty or absent list means
      every type.
    - `include_archived` lifts both archived rules at once, for nodes and for
      edges.
    """

    def traverse(
        self,
        anchor_id: str,
        depth: int,
        relationship_types: Optional[Sequence[str]] = None,
        include_archived: bool = False,
    ) -> Dict[str, List[str]]:
        """Return ``{"node_ids": [...], "edge_ids": [...]}`` for the traversal.

        Order is not part of the contract: the in-memory walk collects into
        sets, so the caller must not depend on it and the equivalence test
        compares as sets.
        """


@runtime_checkable
class ChangeNotifyingBackend(Protocol):
    """A backend that can report writes another writer made to the store.

    Declared with ``change_notification``. Without it the application's
    in-memory model is only ever as current as its own writes, which is why
    more than one instance cannot share a store.
    """

    def start_change_notification(
        self, listener: Callable[[ExternalChange], None]
    ) -> None:
        """Begin reporting external changes to ``listener``.

        The listener is called only with changes the store has already
        applied - it never writes back. Called once, BEFORE the application's
        first load: a write committed between the load and the start of
        listening would otherwise be announced to a channel nobody is
        listening on, and no transport here replays. No change is reported
        against a model that does not exist yet all the same - the
        application holds what arrives until its load has returned and then
        replays it, so from the listener's side the first call still comes
        after the load. A backend need do nothing about this; it is named
        here because it is why the call comes when it does.

        It applies the report inline, so a report handed over with
        `ExternalChange.entities_read_on_demand` has its content read on this
        same thread, further down this call stack - see that constructor for
        the three obligations that follow. A report held across the load is
        the one exception: it is replayed on the thread that finished the
        load, which is what makes the deferred read sound rather than which
        thread it started on.

        Report from a thread of the backend's own - the thread a notification
        channel, a poller or a watcher runs on. One kind of thread is
        forbidden: never a thread that is executing a write, whether it is
        this application's write or that of another application sharing the
        store. In practice that means never synchronously from inside a call
        an application made into the backend.

        The reason is that a refresh may have to wait for the refreshed
        application's write queue. Delivered from inside that application's
        own write it would be waiting for the call it is inside. Delivered
        from inside a *second* application's write it is worse and quieter:
        each one's refresh waits on its own queue while its queue waits for
        the other's refresh to return, and both stop for good.

        The application detects the one case it can see - a report arriving
        on the thread it runs its own writes on - and raises
        ExternalChangeRefused back at the backend. It cannot see a report
        delivered from another instance's write thread. That one is the
        backend's to get right, and every real transport does: a listener
        connection, a poller and a watcher all have a thread of their own.
        """
        ...

    def stop_change_notification(self) -> None:
        """Stop reporting. The listener is not called again after this returns."""
        ...


def capabilities_of(backend: Any) -> BackendCapabilities:
    """The capabilities a backend declares, validated against what it implements.

    A backend written before the declaration existed has no `capabilities`
    at all; it is a snapshot-only backend and is treated as one. A backend
    that declares incremental writes without implementing the entity
    operations would fail on the first mutation, after the graph has already
    changed in memory, so it is refused here instead. A backend that declares
    store_traversal without `traverse` fails more quietly still - every
    traversal warns and falls back, for the life of the process - which is why
    that one is checked here too rather than left to the caller's except.
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
    if caps.change_notification:
        missing = [
            m for m in _NOTIFICATION_METHODS if not callable(getattr(backend, m, None))
        ]
        if missing:
            raise TypeError(
                f"{type(backend).__name__} declares change_notification but does "
                f"not implement: {', '.join(missing)}"
            )
    if caps.store_traversal:
        missing = [
            m for m in _TRAVERSAL_METHODS if not callable(getattr(backend, m, None))
        ]
        if missing:
            raise TypeError(
                f"{type(backend).__name__} declares store_traversal but does "
                f"not implement: {', '.join(missing)}"
            )
    if caps.revision_enforcement:
        # The store can only answer for revisions on the entity path; a
        # snapshot-only backend has nowhere to return them from.
        if not (caps.incremental_writes and caps.transactions):
            raise TypeError(
                f"{type(backend).__name__} declares revision_enforcement "
                f"without incremental_writes and transactions"
            )
        missing = [
            m for m in _REVISION_METHODS if not callable(getattr(backend, m, None))
        ]
        if missing:
            raise TypeError(
                f"{type(backend).__name__} declares revision_enforcement but "
                f"does not implement: {', '.join(missing)}"
            )
    if caps.staged_sessions:
        # A merge is graph writes plus a layer removal as one unit, which a
        # backend that cannot apply an atomic batch has no way to offer.
        if not (caps.incremental_writes and caps.transactions):
            raise TypeError(
                f"{type(backend).__name__} declares staged_sessions without "
                f"incremental_writes and transactions"
            )
        # A shared store without store-assigned revisions cannot check a
        # merge's expectations against what another instance wrote (§4.4).
        if caps.change_notification and not caps.revision_enforcement:
            raise TypeError(
                f"{type(backend).__name__} declares staged_sessions and "
                f"change_notification without revision_enforcement"
            )
        missing = [
            m
            for m in _STAGED_SESSION_METHODS
            if not callable(getattr(backend, m, None))
        ]
        if missing:
            raise TypeError(
                f"{type(backend).__name__} declares staged_sessions but does "
                f"not implement: {', '.join(missing)}"
            )
    return caps


def dumps_storable(value: Any, **kwargs: Any) -> str:
    """``value`` as JSON text that is guaranteed to encode as UTF-8.

    Ordinary content keeps its characters as they are. A string holding a
    lone UTF-16 surrogate (``"\\ud800"``, which a JSON body may carry as an
    escape) has no UTF-8 form, so that text is written with every non-ASCII
    character as a ``\\u`` escape instead: still valid JSON, and it reads
    back as the same string.
    """
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, **kwargs)
    return text


def _new_journal_id() -> str:
    return uuid.uuid4().hex


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

    A journal belongs to the graph.json it was written against: replayed onto
    a different one it would resurrect that graph's deleted nodes and overwrite
    same-id nodes with stale payloads. So graph.json carries a ``journal_id``
    in its metadata - minted by the first snapshot this backend writes, kept
    for the life of the file - and every journal record names the id it
    extends. A record whose id is not the snapshot's is refused at load
    rather than replayed. The id is the backend's own: it is not in the dict
    ``load_graph_data`` hands out, so it never reaches exports.

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
        *,
        staged_sessions: bool = False,
    ):
        self.json_path = Path(json_path)
        self.journal_path = (
            Path(journal_path)
            if journal_path
            else self.json_path.with_name(self.json_path.stem + ".journal.ndjson")
        )
        # The layer store's two sidecars (docs/SESSION_OVERLAY_CONTRACT.md
        # §7). Opt-in: a backend constructed without `staged_sessions`
        # declares nothing new, never reads or creates either file, and
        # behaves exactly as it did before the layer store existed.
        self._staged_sessions = staged_sessions
        self.layers_path = self.json_path.with_name(
            self.json_path.stem + ".layers.json"
        )
        self.merge_log_path = self.json_path.with_name(
            self.json_path.stem + ".merges.ndjson"
        )
        # Session id -> {"status": ..., "entries": {(kind, id): entry}}, and
        # merge id -> stored record. Valid once _layers_loaded.
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._merges: Dict[str, Dict[str, Any]] = {}
        self._layers_loaded = False
        self._merge_log_needs_repair = False
        # The merge whose write-ahead record is durable and whose outcome is
        # not yet: settled before any other write (see merge).
        self._pending_merge: Optional[Dict[str, Any]] = None
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
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            staged_sessions=self._staged_sessions,
        )

    def exists(self) -> bool:
        return self.json_path.exists()

    # -- snapshot contract ---------------------------------------------------

    def load_graph_data(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_locked()

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        with self._lock:
            # A pending merge is judged against the graph it was written
            # against; replacing that graph first would make it unjudgeable.
            if self._staged_sessions:
                if not self._mirrored and self.json_path.exists():
                    self._load_locked()
                if self._layers_loaded:
                    self._settle_merge_locked()
            metadata = dict(data.get("metadata") or {})
            # The file keeps its identity across whole-graph saves: the
            # journal is truncated in the same call, so the id only has to be
            # stable, and a mirrored one is what any journal on disk names.
            metadata["journal_id"] = (
                (self._metadata.get("journal_id") if self._mirrored else None)
                or metadata.get("journal_id")
                or _new_journal_id()
            )
            snapshot = {**data, "metadata": metadata}
            self._nodes = {n["id"]: n for n in data.get("nodes", [])}
            self._edges = {e["id"]: e for e in data.get("edges", [])}
            # The mirror takes the save's metadata now, as it takes its nodes,
            # but a freshly minted id only once both writes have landed - see
            # _checkpoint_locked for why.
            self._metadata = {
                k: v
                for k, v in metadata.items()
                if k != "journal_id" or v == self._metadata.get("journal_id")
            }
            self._mirrored = True
            self._write_snapshot(snapshot)
            self._truncate_journal()
            self._metadata = metadata

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
            if self._staged_sessions and not self._mirrored and self.json_path.exists():
                self._load_locked()
            if self._journaled:
                self._checkpoint_locked()

    def reserve_revisions(self, ceiling: int) -> None:
        """Durably record that revisions below `ceiling` may have been used.

        The single-writer half of docs/SESSION_OVERLAY_CONTRACT.md §4.2: the
        reservation lives in graph.json's metadata and is written by a
        checkpoint of its own, before the instance hands out any value at or
        above the previous one. A checkpoint rather than a journal record
        because the journal holds entity operations only, and an older build
        replaying an unknown record would refuse the whole file.
        """
        with self._lock:
            if not self._mirrored:
                self._load_locked()
            self._settle_merge_locked()
            previous = self._metadata.get("revision_reservation")
            self._metadata["revision_reservation"] = ceiling
            try:
                self._checkpoint_locked()
            except Exception:
                if previous is None:
                    self._metadata.pop("revision_reservation", None)
                else:
                    self._metadata["revision_reservation"] = previous
                raise

    # -- layer store (staged_sessions) ---------------------------------------
    #
    # Two sidecars beside graph.json. `<stem>.layers.json` holds every
    # staged-session record and layer, rewritten whole and atomically by each
    # operation that changes one - a layer is capped (§6.4), so the file is
    # bounded by the number of staged sessions, not by the graph.
    # `<stem>.merges.ndjson` is the merge log: appended to, one JSON line per
    # step, never rewritten - so marking a record costs one append.
    #
    # A merge is the write-ahead sequence of §11.6, under this backend's lock
    # throughout: (1) a `pending` line holding everything recovery needs,
    # synced; (2) the graph operations as one journal record; (3) the layers
    # file without the merged entries; (4) an `applied` line. The journal
    # record is the commit point. A failure before it aborts the merge; a
    # failure after it does not fail the merge, which has landed - the
    # remaining steps are completed before this backend accepts any other
    # write (`_settle_merge_locked`), or at the next load if the process
    # dies first. Holding every other write back until then is what makes the
    # load-time judgement sound: nothing can have touched the merged entities
    # between the commit and the judgement.

    def load_layer(self, session_id: str) -> List[Dict[str, Any]]:
        session_id = _layer.check_session_id(session_id)
        with self._lock:
            self._ensure_layers_locked()
            entries = self._settled_entries_locked().get(session_id)
            if not entries:
                return []
            return [_layer.json_copy(e) for e in entries.values()]

    def staged_status(self, session_id: str) -> str:
        session_id = _layer.check_session_id(session_id)
        with self._lock:
            self._ensure_layers_locked()
            session = self._sessions.get(session_id)
            return session["status"] if session else _layer.STATUS_NONE

    def layers_in_use(self) -> List[str]:
        with self._lock:
            self._ensure_layers_locked()
            return sorted(
                sid
                for sid, entries in self._settled_entries_locked().items()
                if entries
            )

    def load_merge(self, merge_id: str) -> Optional[Dict[str, Any]]:
        merge_id = _layer.check_merge_id(merge_id)
        with self._lock:
            self._ensure_layers_locked()
            # A pending record is resolved first (§11.6). Best effort: if the
            # remaining steps cannot be written now, the answer is still the
            # judged outcome, and the next write retries them.
            pending = self._pending_merge
            try:
                self._settle_merge_locked()
            except GraphJournalError:
                raise
            except Exception as exc:
                logger.warning(f"could not settle a pending merge: {exc}")
                # Judged landed, completion still owed: the answer is the
                # record that merge will be completed with.
                if (
                    pending is not None
                    and pending["merge_id"] == merge_id
                    and self._judge_pending_locked() == "landed"
                ):
                    return _layer.json_copy(pending["record"])
            record = self._merges.get(merge_id)
            return _layer.json_copy(record) if record is not None else None

    def record_staged(self, session_id: str) -> None:
        session_id = _layer.check_session_id(session_id)
        with self._lock:
            self._begin_layer_write_locked()
            session = self._sessions.get(session_id)
            if session is not None:
                if session["status"] == _layer.STATUS_RETIRED:
                    raise _layer.StagedSessionRetired(session_id)
                return
            self._commit_sessions_locked(
                {session_id: {"status": _layer.STATUS_ACTIVE, "entries": {}}}
            )

    def retire(self, session_id: str, *, require_empty: bool = False) -> None:
        session_id = _layer.check_session_id(session_id)
        with self._lock:
            self._begin_layer_write_locked()
            session = self._sessions.get(session_id)
            if session is not None and session["status"] == _layer.STATUS_RETIRED:
                return
            if require_empty and session is not None and session["entries"]:
                raise _layer.LayerNotEmpty(session_id, len(session["entries"]))
            self._commit_sessions_locked(
                {session_id: {"status": _layer.STATUS_RETIRED, "entries": {}}}
            )

    def stage(
        self,
        session_id: str,
        entries: Sequence[Any],
        removals: Sequence[Any] = (),
        *,
        max_entries: Optional[int] = None,
        expected: Optional[Any] = None,
        exact: bool = False,
    ) -> None:
        session_id = _layer.check_session_id(session_id)
        by_key, removed = _layer.check_entries(entries, removals)
        limit = _layer.check_max_entries(max_entries)
        expectations = _layer.check_expected(expected)
        with self._lock:
            self._begin_layer_write_locked()
            session = self._active_session_locked(session_id)
            _layer.verify_expected(
                session_id, session["entries"], expectations, bool(exact)
            )
            updated = dict(session["entries"])
            for key in removed:
                updated.pop(key, None)
            updated.update(by_key)
            if limit is not None and len(updated) > limit:
                raise _layer.LayerFull(session_id, limit, len(updated))
            self._commit_sessions_locked(
                {session_id: {"status": session["status"], "entries": updated}}
            )

    def discard(
        self,
        session_id: str,
        removals: Optional[Sequence[Any]] = None,
        *,
        expected: Optional[Any] = None,
    ) -> List[Tuple[str, str]]:
        session_id = _layer.check_session_id(session_id)
        keys = None if removals is None else _layer.check_keys(removals)
        expectations = _layer.check_expected(expected)
        with self._lock:
            self._begin_layer_write_locked()
            session = self._active_session_locked(session_id)
            _layer.verify_expected(session_id, session["entries"], expectations)
            present = session["entries"]
            gone = list(present) if keys is None else [k for k in keys if k in present]
            if not gone:
                return []
            dropped = set(gone)
            updated = {k: v for k, v in present.items() if k not in dropped}
            self._commit_sessions_locked(
                {session_id: {"status": session["status"], "entries": updated}}
            )
            return gone

    def drop_layer(self, session_id: str) -> None:
        session_id = _layer.check_session_id(session_id)
        with self._lock:
            self._begin_layer_write_locked()
            session = self._sessions.get(session_id)
            if session is None or not session["entries"]:
                return
            self._commit_sessions_locked(
                {session_id: {"status": session["status"], "entries": {}}}
            )

    def merge(
        self,
        session_id: str,
        merge_id: str,
        operations: Sequence[EntityOperation],
        checks: Sequence[Any],
        removals: Sequence[Any],
        record: Any,
    ) -> "_layer.MergeResult":
        session_id = _layer.check_session_id(session_id)
        merge_id = _layer.check_merge_id(merge_id)
        ops, verified, removed, caller_record = _layer.check_merge_request(
            operations, checks, removals, record
        )
        journal_ops, copies = self._journal_ops(ops)
        # The journal's copies carry no expectation; the merge checks them.
        stored_ops = [
            dataclasses.replace(op, payload=copied.payload)
            for op, copied in zip(ops, copies)
        ]
        with self._lock:
            self._begin_layer_write_locked()
            existing = self._merges.get(merge_id)
            if existing is not None:
                return _layer.replay(existing, session_id, merge_id)
            session = self._active_session_locked(session_id)
            nodes, edges = dict(self._nodes), dict(self._edges)
            for op in stored_ops:
                if op.action == "upsert":
                    self._check_fresh_revision(op)
            _layer.apply_merge_to_model(
                nodes, edges, session["entries"], stored_ops, verified, removed
            )
            revisions = [
                op.payload["revision"] if op.action == "upsert" else None
                for op in stored_ops
            ]
            applied = _layer.build_record(
                caller_record,
                merge_id=merge_id,
                session_id=session_id,
                status=_layer.MERGE_APPLIED,
                applied=_layer.applied_items(stored_ops, revisions),
            )
            if journal_ops and not self._metadata.get("journal_id"):
                # A graph.json from before the stamp gets it now, not in the
                # middle of step 2: the pending record names the graph it
                # will be judged against.
                self._checkpoint_locked()
            pending = {
                "merge_id": merge_id,
                "session_id": session_id,
                "journal_id": self._metadata.get("journal_id"),
                "operations": journal_ops,
                "removals": [list(key) for key in removed],
                "record": applied,
            }
            # Step 1. The line can be durable even when the append raises
            # (an error after the sync, an interrupt), so on any failure the
            # backend forgets what it holds and the next operation re-reads
            # the log - a second pending line written behind an unseen first
            # would make the store unloadable.
            try:
                self._append_merge_log_locked(
                    {"merge_id": merge_id, "status": _layer.MERGE_PENDING, **pending}
                )
            except BaseException as exc:
                self._mirrored = False
                self._layers_loaded = False
                # Without graph operations the pending line is the commit
                # point. Report an ordinary append error by its disk outcome,
                # just as step 2 does for a merge that changes the graph.
                if not journal_ops and isinstance(exc, Exception):
                    landed = self._reload_after_failed_commit_locked(merge_id)
                    if landed is not None:
                        return _layer.MergeResult(record=_layer.json_copy(landed))
                raise
            self._pending_merge = pending
            # Step 2, the commit point.
            try:
                if journal_ops:
                    self._append_journal_locked(journal_ops, stored_ops)
            except Exception:
                # Not taken as "did not land": the record can be durable with
                # the error raised after it (a close that reports a failed
                # upload, an unlock). So the backend re-reads what is on
                # disk, which settles the pending merge on what is there.
                landed = self._reload_after_failed_commit_locked(merge_id)
                if landed is not None:
                    return _layer.MergeResult(record=_layer.json_copy(landed))
                raise
            except BaseException:
                # Interrupted with the outcome unknown: memory may lag the
                # disk, so the next operation re-reads it before anything.
                self._mirrored = False
                self._layers_loaded = False
                raise
            # Steps 3 and 4. The merge has landed whatever happens here.
            try:
                self._complete_pending_locked()
            except Exception as exc:
                logger.warning(
                    f"merge {merge_id!r} landed; completing its layer removal "
                    f"failed and is retried before the next write: {exc}"
                )
            return _layer.MergeResult(record=_layer.json_copy(applied))

    # The pieces the operations above are made of. Every one runs under
    # self._lock.

    def _check_fresh_revision(self, op: EntityOperation) -> None:
        """An upsert must carry a revision the stored entity does not have.

        Recovery decides whether a merge landed by finding each upsert's
        content, revision included, in the graph (§11.6). The instance hands
        every merge write a fresh revision (§4.2), so the stored one always
        differs; one that did not would make "landed" and "not landed" the
        same picture.
        """
        revision = op.payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise _layer.InvalidLayerRequest(
                f"{op.kind} {op.entity_id!r}: a merge upsert carries the "
                f"revision the instance assigned it"
            )
        table = self._nodes if op.kind == "node" else self._edges
        if _layer.stored_revision(table.get(op.entity_id)) == revision:
            raise _layer.InvalidLayerRequest(
                f"{op.kind} {op.entity_id!r}: a merge upsert carries a fresh revision"
            )

    def _settled_entries_locked(self) -> Dict[str, Dict[Tuple[str, str], Any]]:
        """Each session's entries as the reads must show them.

        A pending merge is settled first, best effort. If its completion
        cannot be written yet and it is judged landed, its removals are left
        out all the same: the merge has applied, and a read that showed its
        entries as still staged would show one change twice (I8).
        """
        pending = self._pending_merge
        try:
            self._settle_merge_locked()
        except GraphJournalError:
            raise
        except Exception as exc:
            logger.warning(f"could not settle a pending merge: {exc}")
        view = {sid: s["entries"] for sid, s in self._sessions.items()}
        if (
            pending is not None
            and self._pending_merge is pending
            and self._judge_pending_locked() == "landed"
        ):
            removed = {tuple(key) for key in pending["removals"]}
            sid = pending["session_id"]
            if sid in view:
                view[sid] = {k: v for k, v in view[sid].items() if k not in removed}
        return view

    def _require_staged_sessions(self) -> None:
        if not self._staged_sessions:
            raise RuntimeError(
                "this backend was constructed without staged_sessions; it "
                "holds no layers"
            )

    def _ensure_layers_locked(self) -> None:
        self._require_staged_sessions()
        if not self._mirrored:
            # Loads the layers too, after the graph they are judged against.
            self._load_locked()
        if not self._layers_loaded:
            self._load_layers_locked()

    def _begin_layer_write_locked(self) -> None:
        self._ensure_layers_locked()
        self._settle_merge_locked()

    def _active_session_locked(self, session_id: str) -> Dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None or session["status"] != _layer.STATUS_ACTIVE:
            raise _layer.StagedSessionNotActive(
                session_id, session["status"] if session else _layer.STATUS_NONE
            )
        return session

    def _commit_sessions_locked(self, changed: Dict[str, Dict[str, Any]]) -> None:
        """Write the layers file with `changed` laid over the current state,
        then adopt it. A failed write leaves memory and disk as they were."""
        sessions = {**self._sessions, **changed}
        self._write_layers_file(sessions)
        self._sessions = sessions

    def _write_layers_file(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        data = {
            "format": 1,
            "sessions": {
                sid: {"status": s["status"], "entries": list(s["entries"].values())}
                for sid, s in sorted(sessions.items())
            },
        }
        self._write_json_atomically(self.layers_path, data, prefix="layers_")

    def _load_layers_locked(self) -> None:
        sessions: Dict[str, Dict[str, Any]] = {}
        if self.layers_path.exists():
            try:
                raw = json.loads(self.layers_path.read_text(encoding="utf-8"))
                for sid, s in raw["sessions"].items():
                    _layer.check_session_id(sid)
                    status = s["status"]
                    if status not in (_layer.STATUS_ACTIVE, _layer.STATUS_RETIRED):
                        raise ValueError(f"unknown status {status!r}")
                    entries = {}
                    for entry in s["entries"]:
                        key, stored = _layer.check_entry(entry)
                        entries[key] = stored
                    sessions[sid] = {"status": status, "entries": entries}
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise GraphJournalError(
                    f"{self.layers_path} cannot be read ({exc}); the graph was "
                    f"not loaded. It holds the staged sessions' layers, so it "
                    f"is not safe to discard silently"
                ) from exc
        merges, pending = self._read_merge_log()
        self._sessions = sessions
        self._merges = merges
        self._pending_merge = pending
        self._layers_loaded = True
        if pending is not None:
            if pending.get("journal_id") != self._metadata.get("journal_id"):
                raise GraphJournalError(
                    f"{self.merge_log_path} holds a pending merge written "
                    f"against a different graph.json; the graph was not loaded"
                )
            try:
                self._settle_merge_locked()
            except GraphJournalError:
                raise
            except Exception as exc:
                logger.warning(
                    f"could not complete pending merge {pending['merge_id']!r} "
                    f"at load; retried before the next write: {exc}"
                )

    def _read_merge_log(
        self,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Every merge record the log holds, and the one still pending.

        An unterminated last line is the append a crash interrupted and is
        cut off; a damaged complete line stops the load.
        """
        merges: Dict[str, Dict[str, Any]] = {}
        pending: Optional[Dict[str, Any]] = None
        if not self.merge_log_path.exists():
            return merges, pending
        raw = self.merge_log_path.read_bytes()
        lines = raw.split(b"\n")
        complete = raw.endswith(b"\n")
        if complete:
            lines.pop()
        kept = 0
        last = len(lines) - 1
        last_mark: Optional[Tuple[str, str]] = None
        for index, line in enumerate(lines):
            # Only an unterminated last line can be an interrupted append.
            try:
                if index == last and not complete:
                    raise ValueError("incomplete line")
                parsed = json.loads(line)
                merge_id = parsed["merge_id"]
                status = parsed["status"]
            except (ValueError, KeyError, TypeError) as exc:
                if index == last and not complete:
                    logger.warning(
                        f"dropping the incomplete last record of "
                        f"{self.merge_log_path} (interrupted write): {exc}"
                    )
                    break
                raise GraphJournalError(
                    f"{self.merge_log_path} line {index + 1} cannot be read "
                    f"({exc}); the graph was not loaded"
                ) from exc
            # An append can land and still raise (an error after the sync),
            # and its caller then writes the same line again. So a line that
            # repeats the one before it exactly is that retry, and is skipped;
            # anything else out of sequence is damage.
            try:
                if status == _layer.MERGE_PENDING:
                    pending_line = {k: v for k, v in parsed.items() if k != "status"}
                    if pending is not None and pending != pending_line:
                        raise ValueError("two merges pending at once")
                    _layer.check_merge_id(merge_id)
                    _layer.check_session_id(parsed["session_id"])
                    record = parsed["record"]
                    if (
                        record["merge_id"] != merge_id
                        or record["session_id"] != parsed["session_id"]
                        or record["status"] != _layer.MERGE_APPLIED
                    ):
                        raise ValueError("pending merge does not match its record")
                    if merge_id in merges:
                        raise ValueError("pending merge id is already applied")
                    if pending is None:
                        # A mark may only repeat the line right before it.
                        last_mark = None
                    pending = pending_line
                elif status in (_layer.MERGE_APPLIED, "aborted"):
                    if pending is None or pending["merge_id"] != merge_id:
                        if last_mark != (merge_id, status):
                            raise ValueError(f"a {status} mark for no pending merge")
                    else:
                        if status == _layer.MERGE_APPLIED:
                            merges[merge_id] = pending["record"]
                        pending = None
                        last_mark = (merge_id, status)
                else:
                    raise ValueError(f"unknown status {status!r}")
            except (ValueError, KeyError, TypeError) as exc:
                raise GraphJournalError(
                    f"{self.merge_log_path} line {index + 1} does not fit the "
                    f"log ({exc}); the graph was not loaded"
                ) from exc
            kept += len(line) + 1
        if kept < len(raw):
            with open(self.merge_log_path, "r+b") as f:
                f.truncate(kept)
                f.flush()
                os.fsync(f.fileno())
        return merges, pending

    def _append_merge_log_locked(self, line: Dict[str, Any]) -> None:
        if self._merge_log_needs_repair:
            # A failed rollback may have left a fragment. Repair it before
            # another append can glue a complete line onto that fragment.
            self._read_merge_log()
            self._merge_log_needs_repair = False
        payload = (dumps_storable(line) + "\n").encode("utf-8")
        self.merge_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.merge_log_path, "ab", buffering=0) as f:
            _lock_file(f, exclusive=True)
            try:
                start = os.fstat(f.fileno()).st_size
                try:
                    written = f.write(payload)
                    if written != len(payload):
                        raise OSError(
                            f"short merge log write: {written} of {len(payload)} bytes"
                        )
                    os.fsync(f.fileno())
                except BaseException:
                    self._merge_log_needs_repair = True
                    os.ftruncate(f.fileno(), start)
                    raise
            finally:
                _unlock_file(f)

    def _settle_merge_locked(self) -> None:
        """Finish the pending merge, if there is one, before anything else.

        Judged against the graph (§11.6), never the journal, whose lines a
        checkpoint removes: every operation's content present - each upsert's
        payload, revision included, and each delete's entity absent - means
        the batch landed, and steps 3 and 4 are completed; every one absent
        means it did not, and the record is marked aborted, leaving the layer
        as it was. Anything else cannot come from an atomic batch under this
        lock, and refuses to guess.
        """
        if self._pending_merge is None:
            return
        if self._judge_pending_locked() == "landed":
            self._complete_pending_locked()
        else:
            self._abort_pending_locked()

    def _judge_pending_locked(self) -> str:
        """`landed` or `not_landed` for the pending merge, or raise.

        Sound because every delete a merge applies names an entity present
        when it was issued (a merge refuses one that is absent), so "absent"
        tells the two outcomes apart for a delete as "carries this revision"
        does for an upsert.
        """
        pending = self._pending_merge
        ops = [EntityOperation(**op) for op in pending["operations"]]

        def present(op: EntityOperation) -> bool:
            table = self._nodes if op.kind == "node" else self._edges
            doc = table.get(op.entity_id)
            if op.action == "delete":
                return doc is None
            return doc == op.payload

        landed = [present(op) for op in ops]
        if all(landed):
            return "landed"
        if not any(landed):
            return "not_landed"
        raise GraphJournalError(
            f"merge {pending['merge_id']!r} is pending and the graph holds "
            f"part of it; the layer and the graph need an operator's look"
        )

    def _reload_after_failed_commit_locked(
        self, merge_id: str
    ) -> Optional[Dict[str, Any]]:
        """Re-read graph, journal and sidecars after step 2 raised.

        The load judges the pending merge on disk and settles it. Returns the
        record when it landed, None when it did not or the outcome could not
        be read yet - then the backend stays unmirrored, and its next
        operation loads and settles again before doing anything else.
        """
        self._mirrored = False
        self._layers_loaded = False
        try:
            self._load_locked()
        except Exception as exc:
            logger.warning(f"could not re-read the store after a failed merge: {exc}")
            self._mirrored = False
            return None
        if (
            self._pending_merge is not None
            and self._pending_merge["merge_id"] == merge_id
        ):
            # Judged, but the mark could not be written; report what was judged.
            try:
                if self._judge_pending_locked() == "landed":
                    return self._pending_merge["record"]
            except GraphJournalError:
                return None
            return None
        return self._merges.get(merge_id)

    def _complete_pending_locked(self) -> None:
        pending = self._pending_merge
        session_id = pending["session_id"]
        session = self._sessions.get(session_id)
        if session is not None:
            removed = {tuple(key) for key in pending["removals"]}
            entries = {k: v for k, v in session["entries"].items() if k not in removed}
            if len(entries) != len(session["entries"]):
                self._commit_sessions_locked(
                    {session_id: {"status": session["status"], "entries": entries}}
                )
        self._append_merge_log_locked(
            {"merge_id": pending["merge_id"], "status": _layer.MERGE_APPLIED}
        )
        self._merges[pending["merge_id"]] = pending["record"]
        self._pending_merge = None

    def _abort_pending_locked(self) -> None:
        pending = self._pending_merge
        self._append_merge_log_locked(
            {"merge_id": pending["merge_id"], "status": "aborted"}
        )
        self._pending_merge = None

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
        # Not a mirror until the journal is in it too: a refused journal
        # leaves the backend unmirrored, so an append after a refused load
        # loads again and is refused again, and writes nothing behind the
        # records that were refused.
        self._mirrored = False
        self._journaled = 0

        for record in self._read_journal():
            for op in record:
                self._apply(op)
            self._journaled += 1
        self._mirrored = True
        if self._staged_sessions:
            # After the journal, so a pending merge is judged against the
            # whole graph as it stands (§11.6). A refusal here leaves the
            # backend unmirrored, as a refused journal does: every write
            # loads again first and is refused again, so nothing lands while
            # a pending merge is unjudged or the sidecars are unreadable.
            self._layers_loaded = False
            try:
                self._load_layers_locked()
            except BaseException:
                self._mirrored = False
                raise
        if self._journaled:
            data = {
                "nodes": [json.loads(json.dumps(n)) for n in self._nodes.values()],
                "edges": [json.loads(json.dumps(e)) for e in self._edges.values()],
                "metadata": self._public_metadata(),
            }
        elif isinstance(data.get("metadata"), dict):
            data["metadata"].pop("journal_id", None)
        return data

    def _public_metadata(self) -> Dict[str, Any]:
        """The mirror's metadata without the backend's own journal id."""
        return {k: v for k, v in self._metadata.items() if k != "journal_id"}

    def _read_journal(self) -> List[List[EntityOperation]]:
        """The journal's records, oldest first.

        A last line that is incomplete or unparsable is the append a crash
        interrupted; it is dropped whole, which is what makes a batch atomic -
        and it is cut off the file too, or the next append would be glued
        onto it and become the next "incomplete last record". A damaged line
        anywhere else is not a crash shape but damage, and the records after
        it may depend on it, so loading stops with an error that names the
        line rather than silently losing mutations.

        A complete record that names a different journal id than graph.json
        carries was written against another graph.json, and loading stops
        too. The one exception is a record with no id at all beside a
        graph.json with no id: both predate the stamp, which is what an
        upgrade in place looks like, and the file is stamped at its next
        checkpoint.
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
        # Bytes covered by the records kept; anything past it is the tail.
        kept = 0
        last = len(lines) - 1
        snapshot_id = self._metadata.get("journal_id")
        for index, line in enumerate(lines):
            try:
                parsed = json.loads(line)
                ops = [EntityOperation(**op) for op in parsed["ops"]]
                record_id = parsed.get("journal_id")
            except (ValueError, KeyError, TypeError) as exc:
                if index == last:
                    logger.warning(
                        f"dropping the incomplete last record of "
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
                logger.warning(
                    f"dropping the incomplete last record of "
                    f"{self.journal_path} (interrupted write)"
                )
                break
            if record_id != snapshot_id:
                remedy = (
                    "If graph.json was replaced deliberately, delete the "
                    "journal - its mutations belong to the graph it was written "
                    "against. Otherwise put back the graph.json the journal "
                    "belongs to"
                )
                if record_id is None:
                    # The stamp is written by a checkpoint that folds the
                    # pre-stamp records in first, or by a whole-graph save
                    # that supersedes them, and the backend only starts
                    # writing stamped records once that write has also
                    # emptied the journal; so a crash between its snapshot
                    # and its truncate leaves exactly this shape, and nothing
                    # else the backend does leaves a pre-stamp record beside
                    # a stamped file.
                    remedy += (
                        ". If nobody replaced graph.json, this is the write "
                        "that stamped the file, interrupted before it emptied "
                        "the journal: the records were folded into graph.json "
                        "by that checkpoint or superseded by that whole-graph "
                        "save, and either way deleting the journal loses "
                        "nothing"
                    )
                raise GraphJournalError(
                    f"{self.journal_path} line {index + 1} was written against "
                    f"a different graph.json (journal id {record_id or 'none'}, "
                    f"{self.json_path} id {snapshot_id or 'none'}); the graph "
                    f"was not loaded. {remedy}"
                )
            records.append(ops)
            kept += len(line) + 1
        if kept < len(raw):
            self._cut_journal_to(kept)
        return records

    def _cut_journal_to(self, length: int) -> None:
        with open(self.journal_path, "r+b") as f:
            _lock_file(f, exclusive=True)
            try:
                f.truncate(length)
                f.flush()
                os.fsync(f.fileno())
            finally:
                _unlock_file(f)

    def _apply(self, op: EntityOperation) -> None:
        store = self._nodes if op.kind == "node" else self._edges
        if op.action == "upsert":
            store[op.entity_id] = op.payload
        else:
            store.pop(op.entity_id, None)

    def _journal(self, operations: List[EntityOperation]) -> None:
        ops, stored = self._journal_ops(operations)
        with self._lock:
            if not self._mirrored:
                self._load_locked()
            self._settle_merge_locked()
            self._append_journal_locked(ops, stored)

    @staticmethod
    def _journal_ops(
        operations: Sequence[EntityOperation],
    ) -> Tuple[List[Dict[str, Any]], List[EntityOperation]]:
        # Serialised before anything is written or applied, so a payload that
        # cannot be represented fails here and touches neither disk nor mirror.
        # The four fields every build reads. An expectation is not part of
        # the write, and a key an older build does not know would make it
        # refuse the whole journal at load.
        ops = json.loads(
            json.dumps(
                [
                    {
                        "kind": op.kind,
                        "action": op.action,
                        "entity_id": op.entity_id,
                        "payload": op.payload,
                    }
                    for op in operations
                ],
                ensure_ascii=False,
            )
        )
        # The mirror keeps its own copy, parsed back from the line, so a caller
        # that later mutates the dict it passed cannot change what was stored.
        return ops, [EntityOperation(**op) for op in ops]

    def _append_journal_locked(
        self, ops: List[Dict[str, Any]], stored: List[EntityOperation]
    ) -> None:
        """Append one record and apply it to the mirror; the lock is held."""
        if not self._metadata.get("journal_id"):
            # A graph.json from before the stamp. It gets its id from a
            # checkpoint before the first record that will name it lands;
            # if that fails the append fails, or the record would name an
            # id the file does not carry.
            self._checkpoint_locked()
        line = dumps_storable({"journal_id": self._metadata["journal_id"], "ops": ops})
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered, so a failed write leaves nothing queued to flush at
        # close; and the length is restored on failure, so a short write
        # leaves no bytes for the next append to be glued onto.
        with open(self.journal_path, "ab", buffering=0) as f:
            _lock_file(f, exclusive=True)
            try:
                start = os.fstat(f.fileno()).st_size
                payload = (line + "\n").encode("utf-8")
                try:
                    # A raw write may land fewer bytes than given without
                    # raising (a full disk reports the shortfall first and
                    # the error on the next call); that is a failure here.
                    written = f.write(payload)
                    if written != len(payload):
                        raise OSError(
                            f"short journal write: {written} of {len(payload)} bytes"
                        )
                    os.fsync(f.fileno())
                except Exception:
                    os.ftruncate(f.fileno(), start)
                    raise
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
                logger.warning(f"graph checkpoint failed, will retry: {exc}")

    def _checkpoint_locked(self) -> None:
        metadata = {
            **self._metadata,
            "journal_id": self._metadata.get("journal_id") or _new_journal_id(),
        }
        data = {
            "nodes": list(self._nodes.values()),
            "edges": list(self._edges.values()),
            "metadata": {
                **metadata,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
        }
        self._write_snapshot(data)
        # Snapshot first, then the journal: a crash in between replays records
        # the snapshot already holds, and every operation is idempotent.
        self._truncate_journal()
        # The id enters the mirror only once both have landed. Taken earlier,
        # a failed snapshot would leave the next record naming an id the file
        # lacks, and a failed truncate would leave that record behind the
        # pre-stamp ones - a journal no reading can make sense of. Failing
        # here fails the append that asked for the stamp; the next one asks
        # again.
        self._metadata = metadata

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
        self._write_json_atomically(self.json_path, data, prefix="graph_")

    @staticmethod
    def _write_json_atomically(
        target: Path, data: Dict[str, Any], *, prefix: str
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".json",
            prefix=prefix,
            dir=target.parent,
        )

        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    try:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    except UnicodeEncodeError:
                        # A lone surrogate; see dumps_storable.
                        f.seek(0)
                        f.truncate()
                        json.dump(data, f, indent=2, ensure_ascii=True)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    _unlock_file(f)

            if sys.platform == "win32" and target.exists():
                os.replace(temp_path, target)
            else:
                os.rename(temp_path, target)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise


# Last, because that module imports this one: by now every name it takes from
# here exists. Read at call time only.
from . import layer_store as _layer  # noqa: E402
