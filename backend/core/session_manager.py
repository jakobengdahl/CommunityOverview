"""
Orchestration for shared sessions: the op protocol, conflict rules and catch-up.

``SessionManager`` ties the persistent ``SessionStore`` to the ephemeral
``session_hub`` (event bus + presence + claims). It is the single entry point
used by the REST/SSE endpoints and by MCP pushes:

* ``apply_ops`` — validates a batch, applies each op under a per-session lock in
  arrival order (server-ordered last-write-wins, no CRDT — design D2), assigns a
  monotonic ``seq`` to each state op, persists once per batch, and broadcasts
  every applied op to all subscribers including the originator.
* selection claims (``selection_claimed`` / ``selection_released``) are handled
  inline but stay ephemeral — broadcast, never persisted, never sequenced.
* ``connect`` / ``disconnect`` manage presence and release a departing client's
  claims.
* ``catch_up`` returns either the missed ops (from the store ring buffer) or a
  full-state snapshot when the ring cannot prove continuity.
* a per-client token bucket bounds op throughput (429 on exhaustion).
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .image_ingest import (
    DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
    DEFAULT_MAX_SESSION_IMAGE_BYTES,
    data_url_byte_length,
)
from .session_hub import (
    ClaimMap,
    InProcessEventBus,
    PresenceRegistry,
    SessionEventBus,
    Subscription,
)
from .session_store import (
    STATE_OPS,
    OpError,
    Session,
    SessionStore,
    is_valid_session_id,
)

CLAIM_OPS = {"selection_claimed", "selection_released"}

# Ops carry node **references** + layout + annotations, never node copies, so
# these bounds are generous for the real op cadence (drag-end, D9): a single
# `layout_applied` op collapses a bulk re-layout, and normal editing produces a
# few ops/second. The token bucket (200 burst, 100/s refill) absorbs a big
# multi-select layout while a runaway client is throttled to 429 + backoff; the
# byte cap (§3.9) bounds a single oversized batch (e.g. a `layout_applied` with
# tens of thousands of positions) independently of the op *count* cap.
_DEFAULT_MAX_OPS_PER_BATCH = 500
_DEFAULT_MAX_SESSIONS = 10_000
_DEFAULT_BUCKET_CAPACITY = 200.0
_DEFAULT_BUCKET_REFILL_PER_SEC = 100.0
_DEFAULT_MAX_OP_BATCH_BYTES = 256 * 1024

# The session-lookup endpoints (GET /api/sessions/{id} and the SSE stream
# handshake) bypass Basic Auth and are guarded only by the session id, whose
# DDDD-DDDD shape is a 10^8 space (~26.6 bits — below credential strength). A
# per-source token bucket bounds guess throughput so brute-forcing that space is
# infeasible, while normal open/reconnect traffic (a few lookups per session)
# stays well under budget. 60 burst + 2/s sustained caps an attacker at ~2
# guesses/second, turning a full enumeration into years of effort.
_DEFAULT_LOOKUP_BUCKET_CAPACITY = 60.0
_DEFAULT_LOOKUP_REFILL_PER_SEC = 2.0


class SessionNotFound(Exception):
    pass


class RateLimited(Exception):
    pass


class OpBatchTooLarge(Exception):
    pass


class SessionLimitReached(Exception):
    pass


class AnnotationNotFound(Exception):
    """No annotation with the given id exists in this session (or it was just deleted)."""

    def __init__(self, annotation_id: str) -> None:
        super().__init__(f"annotation not found: {annotation_id!r}")
        self.annotation_id = annotation_id


class AnnotationRecentlyDeleted(Exception):
    """A create used an id another collaborator deleted moments ago.

    Mirrors the D-table rule in ``SessionStore.apply_state_op``: a create-op
    retry for an id still in the session's short deleted-ids memory must not
    resurrect it, so the write is refused rather than silently reviving stale
    state a collaborator just removed.
    """

    def __init__(self, annotation_id: Optional[str]) -> None:
        super().__init__(f"annotation was recently deleted: {annotation_id!r}")
        self.annotation_id = annotation_id


class ImageBudgetExceeded(Exception):
    """Embedding this image would exceed a configured image/document byte budget.

    Raised by ``upsert_image_annotation`` before the write is attempted, so a
    rejected image never touches the store — distinct from ``OpBatchTooLarge``,
    which guards the small generic op-batch cap that every other annotation
    write still uses (see ``upsert_image_annotation``'s docstring for why
    embedded images need their own, larger budget).
    """


class NoUndoableAction(Exception):
    """The requesting actor has no undoable activity in this session."""

    def __init__(self, actor: str) -> None:
        super().__init__(f"no undoable action for actor {actor!r}")
        self.actor = actor


class UndoConflict(Exception):
    """The actor's latest undoable action can no longer be safely reverted.

    Raised when the state it touched has changed since it was recorded (by
    the same actor or another collaborator) — replaying the stored inverse op
    would silently clobber that later change.
    """

    def __init__(self, activity_id: str, reason: str) -> None:
        super().__init__(f"undo conflict for activity {activity_id!r}: {reason}")
        self.activity_id = activity_id
        self.reason = reason


class LayoutBusy(Exception):
    """A concurrent op batch holds the session lock; a layout write should retry.

    ``apply_layout`` refuses rather than assigning a seq an in-flight ``apply_ops``
    batch has not broadcast yet — broadcasting a higher seq first would make
    seq-gating clients drop the batch's lower-seq ops permanently.
    """


class RevisionConflict(Exception):
    """The caller's ``expected_revision`` no longer matches the session seq."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected}, session is at {actual}")
        self.expected = expected
        self.actual = actual


_DEFAULT_BUCKET_IDLE_TTL = 3600.0  # evict a key after 1 h of silence
_DEFAULT_BUCKET_SWEEP_INTERVAL = 300.0  # run the eviction sweep at most every 5 min


class _TokenBucket:
    """Per-client token bucket bounding ops/second.

    Idle entries (no consume() call for ``idle_ttl`` seconds) are evicted in a
    periodic sweep so the internal dicts don't grow without bound under rotating
    client keys (e.g. the per-IP lookup bucket).
    """

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        *,
        time_fn=time.monotonic,
        idle_ttl: float = _DEFAULT_BUCKET_IDLE_TTL,
        sweep_interval: float = _DEFAULT_BUCKET_SWEEP_INTERVAL,
    ) -> None:
        self._capacity = capacity
        self._refill = refill_per_sec
        self._time = time_fn
        self._tokens: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._idle_ttl = idle_ttl
        self._sweep_interval = sweep_interval
        self._last_sweep: float = 0.0

    def consume(self, client_id: str, amount: float) -> bool:
        now = self._time()
        if now - self._last_sweep >= self._sweep_interval:
            self._evict(now)
        tokens = self._tokens.get(client_id, self._capacity)
        last = self._last.get(client_id, now)
        tokens = min(self._capacity, tokens + (now - last) * self._refill)
        self._last[client_id] = now
        if tokens < amount:
            self._tokens[client_id] = tokens
            return False
        self._tokens[client_id] = tokens - amount
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - self._idle_ttl
        stale = [k for k, t in self._last.items() if t < cutoff]
        for k in stale:
            del self._tokens[k]
            del self._last[k]
        self._last_sweep = now


def _image_annotation_bytes(annotation: Dict[str, Any]) -> int:
    """Decoded byte size of an `image` annotation's embedded data URI, else 0."""
    if annotation.get("type") != "image":
        return 0
    image = annotation.get("image")
    if not isinstance(image, dict):
        return 0
    return data_url_byte_length(image.get("url"))


def _session_image_bytes(
    session: "Session", *, exclude_id: Optional[str] = None
) -> int:
    """Total embedded-image bytes across a session's annotations.

    ``exclude_id`` omits one annotation (the one about to be replaced) so a
    same-id upsert is budgeted against the *replacement*, not double-counted
    against both the old and new copy.
    """
    total = 0
    for annotation in session.state.get("annotations", []):
        if exclude_id is not None and annotation.get("id") == exclude_id:
            continue
        total += _image_annotation_bytes(annotation)
    return total


class SessionManager:
    """High-level façade over the session store, event bus, presence and claims."""

    def __init__(
        self,
        store: SessionStore,
        *,
        event_bus: Optional[SessionEventBus] = None,
        presence: Optional[PresenceRegistry] = None,
        claims: Optional[ClaimMap] = None,
        max_ops_per_batch: int = _DEFAULT_MAX_OPS_PER_BATCH,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        max_op_batch_bytes: int = _DEFAULT_MAX_OP_BATCH_BYTES,
        bucket_capacity: float = _DEFAULT_BUCKET_CAPACITY,
        bucket_refill_per_sec: float = _DEFAULT_BUCKET_REFILL_PER_SEC,
        lookup_bucket_capacity: float = _DEFAULT_LOOKUP_BUCKET_CAPACITY,
        lookup_refill_per_sec: float = _DEFAULT_LOOKUP_REFILL_PER_SEC,
    ) -> None:
        self.store = store
        self.bus = event_bus or InProcessEventBus()
        self.presence = presence or PresenceRegistry()
        self.claims = claims or ClaimMap()
        self._max_ops = max_ops_per_batch
        self._max_sessions = max_sessions
        self._max_op_batch_bytes = max_op_batch_bytes
        self._bucket = _TokenBucket(bucket_capacity, bucket_refill_per_sec)
        self._lookup_bucket = _TokenBucket(
            lookup_bucket_capacity, lookup_refill_per_sec
        )
        self._locks: Dict[str, asyncio.Lock] = {}

    def check_lookup_rate(self, client_key: str) -> None:
        """Throttle unauthenticated session-id lookups by source.

        Consumes one token from ``client_key``'s bucket, raising ``RateLimited``
        when the source has exhausted its budget. Called by the auth-bypassed
        ``GET /api/sessions/{id}`` and SSE stream-handshake endpoints so the
        short session id cannot be brute-forced (see the bucket sizing note).
        """
        if not self._lookup_bucket.consume(client_key, 1.0):
            raise RateLimited()

    @property
    def max_op_batch_bytes(self) -> int:
        """Byte cap enforced on a single ops batch (§3.9)."""
        return self._max_op_batch_bytes

    @property
    def max_ops_per_batch(self) -> int:
        """Op-count cap enforced on a single ops batch (§3.9).

        Exposed so a caller that does per-item work *before* handing the batch
        over (resolving node ids, say) can reject an oversized request first
        instead of paying for it and failing the cap afterwards.
        """
        return self._max_ops

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # ---------------- session CRUD ----------------

    def create_session(self, name: Optional[str] = None) -> Session:
        if self.store.session_count() >= self._max_sessions:
            raise SessionLimitReached()
        return self.store.create(name)

    def get_or_create(self, session_id: str) -> Tuple[Session, bool]:
        session = self.store.get(session_id)
        if session is not None:
            return session, False
        if self.store.session_count() >= self._max_sessions:
            raise SessionLimitReached()
        return self.store.get_or_create(session_id)

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.store.get(session_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        return self.store.list_meta()

    async def rename_session(
        self, session_id: str, name: Optional[str], client_id: Optional[str] = None
    ) -> Session:
        """Rename a session, materialising it if it only exists client-side.

        Routed through ``apply_ops`` as a ``session_renamed`` state op (R8)
        instead of writing the store directly: this gives the rename a
        ``seq`` and a ring-buffer entry, so a client reconnecting via the
        ``since_seq`` catch-up path (not just a full snapshot) observes it —
        previously the op was a documented-but-unreachable STATE_OP with no
        emitter. Routing through the same locked/rollback-safe path as any
        other op also closes the rename-vs-in-flight-batch race (R10).
        ``get_or_create`` first (R7): a PATCH for a session id that only
        exists in the browser's URL/recents (never saved server-side) must
        still take effect rather than 404, or the name is lost the moment the
        session later materialises with a null server name.
        """
        self.get_or_create(session_id)
        await self.apply_ops(
            session_id,
            client_id or "rest",
            None,
            [
                {"op": "session_renamed", "name": name},
            ],
        )
        return self.store.get(session_id)

    async def delete_session(
        self, session_id: str, deleted_by: Optional[str] = None
    ) -> bool:
        # Same per-session lock apply_ops uses (R10): without it, an in-flight
        # apply_ops batch that already holds a `Session` reference can persist()
        # after this delete, resurrecting the file on disk.
        #
        # The lock object is deliberately *not* popped from `self._locks` here
        # (unlike the pre-R10 code): popping it right after release would let a
        # concurrent waiter that already holds a reference to this exact lock
        # object (obtained via `_lock()` before the pop) go on to acquire it
        # after a *different*, newly created lock has taken over serialising
        # this session_id (e.g. a rename that recreated the session in between)
        # — two coroutines then mutate the same session under two different
        # locks, defeating mutual exclusion. Leaving stale lock objects behind
        # is a bounded, harmless memory cost (one small `asyncio.Lock` per
        # session_id ever deleted); `max_sessions` already bounds how many
        # sessions can exist at once.
        async with self._lock(session_id):
            existed = self.store.delete(session_id)
        if existed:
            # Broadcast before tearing down so connected clients get the notice.
            self.bus.publish(
                session_id,
                {"type": "session_deleted", "deleted_by": deleted_by},
            )
        return existed

    def rename_session_sync(
        self, session_id: str, name: Optional[str], client_id: Optional[str] = None
    ) -> Session:
        """Rename a session **synchronously** (the MCP tool path).

        The async ``rename_session`` routes through ``apply_ops`` so the rename
        gets a ``seq`` + ring entry and reaches a client reconnecting via
        ``since_seq`` catch-up (R8). MCP tools are synchronous — they cannot
        await — so this mirrors ``apply_layout`` instead: it emits the same
        ``session_renamed`` state op inline on the event-loop thread (atomic
        w.r.t. every coroutine on a single-threaded loop) and refuses with
        ``LayoutBusy`` when an ``apply_ops`` batch holds the lock, so it never
        assigns a ``seq`` that batch has not broadcast yet (the same seq-ordering
        rule ``apply_layout`` documents).

        ``get_or_create`` first (R7): a rename for an id that only exists in a
        browser URL/recents must materialise it rather than raise, matching the
        async path and the REST ``PATCH``.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if name is not None and not isinstance(name, str):
            raise OpError("'name' must be a string or null")
        self.get_or_create(session_id)
        if self._lock(session_id).locked():
            raise LayoutBusy()
        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()

        op = {"op": "session_renamed", "name": name, "client_id": client_id or "rest"}
        saved_state = copy.deepcopy(session.state)
        saved_seq = session.seq
        saved_name = session.name
        saved_updated_at = session.updated_at
        ring = self.store.ring(session_id)
        saved_ring = list(ring) if ring is not None else None
        try:
            applied = self.store.apply_state_op(session, op)
            self.store.persist(session)
        except Exception:
            session.state = saved_state
            session.seq = saved_seq
            session.name = saved_name
            session.updated_at = saved_updated_at
            if ring is not None and saved_ring is not None:
                ring.clear()
                ring.extend(saved_ring)
            raise

        self.bus.publish(
            session_id,
            {
                "type": "op",
                "client_id": op["client_id"],
                "op": applied,
                "seq": applied["seq"],
            },
        )
        return session

    def delete_session_sync(
        self, session_id: str, deleted_by: Optional[str] = None
    ) -> bool:
        """Delete a session **synchronously** (the MCP tool path).

        The async ``delete_session`` takes the per-session lock to stop an
        in-flight ``apply_ops`` batch from resurrecting the file via ``persist()``
        after the delete. A sync tool cannot await that lock, so — like
        ``apply_layout`` — it refuses with ``LayoutBusy`` when the lock is held and
        otherwise deletes inline on the event-loop thread, where no coroutine can
        interleave. Stale lock objects are left in ``self._locks`` for the same
        reason ``delete_session`` documents.
        """
        if self._lock(session_id).locked():
            raise LayoutBusy()
        existed = self.store.delete(session_id)
        if existed:
            self.bus.publish(
                session_id,
                {"type": "session_deleted", "deleted_by": deleted_by},
            )
        return existed

    def roster(self, session_id: str) -> List[Dict[str, Any]]:
        return self.presence.roster(session_id)

    def connected_count(self, session_id: str) -> int:
        return self.presence.count(session_id)

    def claimed_elements(self, session_id: str) -> List[str]:
        """Element ids with a live selection claim (advisory soft-locks, D3).

        The current per-user selection is expressed as claims, so this is the
        source the MCP query tools read now that the browser no longer uploads
        its selection (design §3.8). These are *element* ids: a claim may be
        taken on an edge as well as a node, which is why the tools reporting a
        ``selected_node_ids`` field narrow this to the session's nodes first.
        """
        return list(self.claims.snapshot(session_id).keys())

    # ---------------- ops ----------------

    async def apply_ops(
        self,
        session_id: str,
        client_id: str,
        base_seq: Optional[int],
        ops: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(ops, list):
            raise OpError("'ops' must be a list")
        if len(ops) > self._max_ops:
            raise OpBatchTooLarge()
        # Bound the batch by *size* as well as *count* (§3.9): a single op such
        # as `layout_applied` can carry a large positions map that the op-count
        # cap alone would not catch.
        if len(json.dumps(ops)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not self._bucket.consume(client_id, max(1, len(ops))):
            raise RateLimited()

        if self.store.get(session_id) is None:
            raise SessionNotFound()

        # Reject a malformed batch up front, before any op is applied or
        # broadcast: op envelopes and claim payloads are structurally validated
        # here, and state ops in ``apply_state_op`` always validate before they
        # mutate. Combined with the rollback below, a batch is all-or-nothing —
        # a mid-batch failure never leaves a partial prefix applied, persisted or
        # broadcast (which would diverge subscribers and duplicate non-idempotent
        # ops such as annotation_created on client retry).
        for op in ops:
            if not isinstance(op, dict):
                raise OpError("each op must be an object")
            op_type = op.get("op")
            if op_type in CLAIM_OPS:
                self._validate_claim_op(op)
            elif op_type not in STATE_OPS:
                raise OpError(f"unknown op: {op_type!r}")

        async with self._lock(session_id):
            # Re-fetch inside the lock rather than reusing the check above
            # (R10): `delete_session` takes this same per-session lock, so if
            # it ran between that check and here, the session is gone and this
            # batch must not resurrect it via persist() below.
            session = self.store.get(session_id)
            if session is None:
                raise SessionNotFound()

            # Snapshot for rollback. persist() is inside the protected region so
            # a persistence-layer failure (disk full, IO error) rolls back too —
            # otherwise in-memory state/seq/ring would advance while disk and all
            # subscribers stayed behind, duplicating non-idempotent ops on retry.
            # The deepcopy cost is negligible at drag-end op cadence (D9).
            saved_state = copy.deepcopy(session.state)
            saved_seq = session.seq
            saved_name = session.name
            saved_updated_at = session.updated_at
            saved_activity_log = copy.deepcopy(session.activity_log)
            ring = self.store.ring(session_id)
            saved_ring = list(ring) if ring is not None else None

            # ("state", applied) | ("claim", op_type, element_ids), in arrival order
            pending: List[Tuple[Any, ...]] = []
            state_changed = False
            try:
                for op in ops:
                    op_type = op["op"]
                    if op_type in CLAIM_OPS:
                        pending.append(("claim", op_type, list(op["element_ids"])))
                    else:
                        result = self.store.apply_state_op(
                            session, {**op, "client_id": client_id}
                        )
                        if result is None:
                            continue  # legitimate no-op (e.g. update on deleted annotation)
                        state_changed = True
                        pending.append(("state", result))
                if state_changed:
                    # Snapshot on the loop thread, persist the copy off-thread:
                    # the worker must never read ``session.state`` while a
                    # loop-thread writer (the synchronous ``apply_layout`` path)
                    # may be mutating it during this await. See persist_snapshot.
                    snapshot = copy.deepcopy(session.to_dict())
                    await asyncio.to_thread(self.store.persist_snapshot, snapshot)
            except Exception:
                session.state = saved_state
                session.seq = saved_seq
                session.name = saved_name
                session.updated_at = saved_updated_at
                session.activity_log = saved_activity_log
                if ring is not None and saved_ring is not None:
                    ring.clear()
                    ring.extend(saved_ring)
                raise

            # Commit succeeded: apply ephemeral claim effects and broadcast every
            # op in arrival order (originator included; clients apply idempotently).
            applied: List[Dict[str, Any]] = []
            for entry in pending:
                if entry[0] == "state":
                    result = entry[1]
                    self.bus.publish(
                        session_id,
                        {
                            "type": "op",
                            "client_id": client_id,
                            "op": result,
                            "seq": result["seq"],
                        },
                    )
                    applied.append(result)
                else:
                    _, op_type, element_ids = entry
                    if op_type == "selection_claimed":
                        self.claims.claim(session_id, client_id, element_ids)
                    else:
                        element_ids = self.claims.release(
                            session_id, client_id, element_ids
                        )
                    self.bus.publish(
                        session_id,
                        {
                            "type": "op",
                            "client_id": client_id,
                            "op": {
                                "op": op_type,
                                "client_id": client_id,
                                "element_ids": element_ids,
                            },
                        },
                    )
                    applied.append({"op": op_type, "element_ids": element_ids})

        return {"applied": applied, "seq": session.seq}

    def apply_layout(
        self,
        session_id: str,
        client_id: str,
        *,
        positions: Optional[Dict[str, Any]] = None,
        deltas: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        animation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Apply one ``layout_applied`` op **synchronously** (the MCP write path).

        Unlike ``apply_ops`` this never awaits: it runs to completion on the event
        loop thread, so on a single-threaded loop it is atomic with respect to
        every coroutine. That atomicity is what makes it safe without the async
        per-session lock — but only while no ``apply_ops`` batch is mid-flight for
        this session. Such a batch holds the lock across its seq-assign→persist→
        broadcast region and may have assigned seqs it has not broadcast yet;
        assigning a higher seq here and broadcasting it first would make
        seq-gating clients (sessionSyncClient R15) drop the batch's lower-seq ops
        permanently. So a held lock ⇒ ``LayoutBusy`` and the caller retries.

        Exactly one of ``positions`` (absolute targets) or ``deltas`` (dx/dy from
        the current server-owned position, unknown ⇒ origin) must be given.

        Persistence is synchronous here (not offloaded like apply_ops): a
        file-backend fsync briefly stalls the loop. That is the deliberate price
        of lock-free atomicity, and layout writes are infrequent (agent-driven).
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if (positions is None) == (deltas is None):
            raise OpError("provide exactly one of 'positions' or 'deltas'")
        moves = positions if positions is not None else deltas
        if not isinstance(moves, dict) or not moves:
            raise OpError("'positions'/'deltas' must be a non-empty object")
        if len(moves) > self._max_ops:
            raise OpBatchTooLarge()
        if len(json.dumps(moves)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()
        if not self._bucket.consume(client_id, max(1, len(moves))):
            raise RateLimited()

        # A held lock means an apply_ops batch is mid-flight for this session
        # (see the seq-ordering rationale in this method's docstring).
        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        if deltas is not None:
            current = session.state.get("positions", {})
            target: Dict[str, Dict[str, float]] = {}
            for node_id, d in deltas.items():
                if (
                    not isinstance(d, dict)
                    or not isinstance(d.get("dx"), (int, float))
                    or not isinstance(d.get("dy"), (int, float))
                ):
                    raise OpError("each delta must be an object with numeric dx and dy")
                base = current.get(node_id) or {"x": 0.0, "y": 0.0}
                target[node_id] = {
                    "x": float(base["x"]) + float(d["dx"]),
                    "y": float(base["y"]) + float(d["dy"]),
                }
        else:
            target = positions

        op: Dict[str, Any] = {
            "op": "layout_applied",
            "positions": target,
            "client_id": client_id,
        }
        if animation is not None:
            op["animation"] = animation

        applied = self._apply_op_sync(session, session_id, client_id, op)
        return {"applied": applied, "revision": session.seq, "moved": len(target)}

    def add_node_refs(
        self,
        session_id: str,
        client_id: str,
        node_ids: List[str],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Add node references to a session **synchronously** (the MCP write path).

        Places a known set of nodes on the canvas directly, instead of having to
        craft a search that happens to return exactly that set. State is
        server-owned (design §3.8), so the resulting ``nodes_added`` op is what
        every connected browser converges on — a client that is not connected
        yet picks the nodes up from the session state on join.

        Shares ``apply_layout``'s atomicity contract: it never awaits, so it is
        atomic on a single-threaded loop, and a held per-session lock (an
        ``apply_ops`` batch mid-flight) means ``LayoutBusy`` rather than a seq
        assigned out of broadcast order.

        Ids already in the session are not re-added and do not advance the
        session's revision: when the set adds nothing new the call is a no-op
        that broadcasts nothing.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(node_ids, list) or not node_ids:
            raise OpError("'node_ids' must be a non-empty list")
        if len(node_ids) > self._max_ops:
            raise OpBatchTooLarge()
        if len(json.dumps(node_ids)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()
        if not self._bucket.consume(client_id, max(1, len(node_ids))):
            raise RateLimited()

        # A held lock means an apply_ops batch is mid-flight for this session
        # (see apply_layout's docstring for the seq-ordering rationale).
        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        # Dedupe against the session *and* against the rest of this call, in
        # order: a repeated id would otherwise be reported as added twice and
        # ride the broadcast twice, while the stored state (a union) holds it
        # once.
        present = set(session.state.get("node_refs", []))
        added: List[str] = []
        for node_id in node_ids:
            if not isinstance(node_id, str) or node_id in present:
                continue
            present.add(node_id)
            added.append(node_id)
        if not added:
            return {
                "applied": None,
                "revision": session.seq,
                "added": [],
                "node_count": len(session.state.get("node_refs", [])),
            }

        op: Dict[str, Any] = {
            "op": "nodes_added",
            "node_ids": added,
            "client_id": client_id,
        }
        applied = self._apply_op_sync(session, session_id, client_id, op)
        return {
            "applied": applied,
            "revision": session.seq,
            "added": added,
            "node_count": len(session.state.get("node_refs", [])),
        }

    def upsert_annotation(
        self,
        session_id: str,
        client_id: str,
        annotation: Dict[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create, or replace by id, one annotation **synchronously** (MCP write path).

        Shares ``apply_layout``/``add_node_refs``' atomicity contract: it never
        awaits, so it is atomic on a single-threaded loop, and a held per-session
        lock (an ``apply_ops`` batch mid-flight) means ``LayoutBusy`` rather than a
        seq assigned out of broadcast order.

        ``annotation_created`` is already an upsert in ``SessionStore`` when
        ``annotation["id"]`` matches one already in the session (idempotent
        client retries), so create and upsert share this one op and this one
        method. Omit ``annotation["id"]`` to have the store mint one.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(annotation, dict):
            raise OpError("'annotation' must be an object")
        if len(json.dumps(annotation)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()
        if not self._bucket.consume(client_id, 1):
            raise RateLimited()

        # A held lock means an apply_ops batch is mid-flight for this session
        # (see apply_layout's docstring for the seq-ordering rationale).
        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        op: Dict[str, Any] = {
            "op": "annotation_created",
            "annotation": annotation,
            "client_id": client_id,
        }
        applied = self._apply_op_sync(session, session_id, client_id, op)
        if applied is None:
            raise AnnotationRecentlyDeleted(annotation.get("id"))
        return {
            "applied": applied,
            "revision": session.seq,
            "annotation": applied.get("annotation"),
        }

    def upsert_image_annotation(
        self,
        session_id: str,
        client_id: str,
        annotation: Dict[str, Any],
        *,
        optimized_image_bytes: int,
        max_session_image_bytes: int = DEFAULT_MAX_SESSION_IMAGE_BYTES,
        max_session_document_bytes: int = DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create, or replace by id, one `image` annotation (MCP write path).

        Shares ``upsert_annotation``'s shape and atomicity contract, but a
        single optimized image (up to a few MB by default — see
        ``image_ingest.py``) is already far bigger than
        ``_max_op_batch_bytes`` (256KB, sized for reference-only ops — see
        its definition above), so this method does not apply that cap at
        all. Instead it enforces two image-specific budgets before writing
        anything: the session's total embedded-image bytes
        (`max_session_image_bytes`) and the full persisted document size
        (`max_session_document_bytes`), both raising ``ImageBudgetExceeded``.
        Every other annotation type still goes through ``upsert_annotation``
        and its small generic cap.

        ``optimized_image_bytes`` is the caller's already-computed decoded
        size of the image being embedded (``len(OptimizedImage.data)`` from
        ``image_ingest.optimize_image``) — passed in rather than re-derived
        from ``annotation`` so the budget check reflects the exact bytes that
        were validated, not a re-parse of the data URI.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(annotation, dict):
            raise OpError("'annotation' must be an object")
        if not self._bucket.consume(client_id, 1):
            raise RateLimited()

        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        annotation_id = annotation.get("id")
        existing = (
            next(
                (
                    a
                    for a in session.state.get("annotations", [])
                    if a.get("id") == annotation_id
                ),
                None,
            )
            if isinstance(annotation_id, str)
            else None
        )

        other_image_bytes = _session_image_bytes(
            session,
            exclude_id=annotation_id if isinstance(annotation_id, str) else None,
        )
        if other_image_bytes + optimized_image_bytes > max_session_image_bytes:
            raise ImageBudgetExceeded(
                f"embedding this image would bring the session's total "
                f"embedded image data to "
                f"{other_image_bytes + optimized_image_bytes} bytes, over "
                f"the {max_session_image_bytes}-byte session limit"
            )

        current_doc_bytes = len(json.dumps(session.to_dict()))
        existing_bytes = len(json.dumps(existing)) if existing is not None else 0
        new_annotation_bytes = len(json.dumps(annotation))
        projected_doc_bytes = current_doc_bytes - existing_bytes + new_annotation_bytes
        if projected_doc_bytes > max_session_document_bytes:
            raise ImageBudgetExceeded(
                f"embedding this image would bring the session document to "
                f"{projected_doc_bytes} bytes, over the "
                f"{max_session_document_bytes}-byte document limit"
            )

        op: Dict[str, Any] = {
            "op": "annotation_created",
            "annotation": annotation,
            "client_id": client_id,
        }
        applied = self._apply_op_sync(session, session_id, client_id, op)
        if applied is None:
            raise AnnotationRecentlyDeleted(annotation.get("id"))
        return {
            "applied": applied,
            "revision": session.seq,
            "annotation": applied.get("annotation"),
        }

    def update_annotation(
        self,
        session_id: str,
        client_id: str,
        patch: Dict[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Patch one existing annotation **synchronously** (MCP write path).

        ``patch`` is merged onto the stored annotation with a shallow
        ``dict.update`` in ``SessionStore`` — only the keys present in ``patch``
        change, so a caller wanting to touch just one field (e.g. ``text``) must
        still carry forward any nested field (e.g. ``geometry``) it does not
        want overwritten with a partial value. Shares ``apply_layout``'s
        atomicity contract; see its docstring for the ``LayoutBusy`` rationale.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(patch, dict) or not isinstance(patch.get("id"), str):
            raise OpError("'patch' must be an object with a string 'id'")
        if len(json.dumps(patch)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()
        if not self._bucket.consume(client_id, 1):
            raise RateLimited()

        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        op: Dict[str, Any] = {
            "op": "annotation_updated",
            "annotation": patch,
            "client_id": client_id,
        }
        applied = self._apply_op_sync(session, session_id, client_id, op)
        if applied is None:
            raise AnnotationNotFound(patch["id"])
        return {
            "applied": applied,
            "revision": session.seq,
            "annotation": applied.get("annotation"),
        }

    def delete_annotation(
        self,
        session_id: str,
        client_id: str,
        annotation_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Delete one annotation by id **synchronously** (MCP write path).

        Unlike the store's raw ``annotation_deleted`` op (which removes an id
        unconditionally, present or not), this checks existence first so a
        delete of an id that is not there is reported to the caller rather than
        silently advancing the revision. Shares ``apply_layout``'s atomicity
        contract; see its docstring for the ``LayoutBusy`` rationale.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(annotation_id, str) or not annotation_id:
            raise OpError("'annotation_id' is required")
        if not self._bucket.consume(client_id, 1):
            raise RateLimited()

        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        existing = next(
            (
                a
                for a in session.state.get("annotations", [])
                if a.get("id") == annotation_id
            ),
            None,
        )
        if existing is None:
            raise AnnotationNotFound(annotation_id)

        op: Dict[str, Any] = {
            "op": "annotation_deleted",
            "annotation_id": annotation_id,
            "client_id": client_id,
        }
        applied = self._apply_op_sync(session, session_id, client_id, op)
        return {"applied": applied, "revision": session.seq}

    def list_activity(
        self, session_id: str, actor: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Recent per-session activity records, newest first (MCP/REST read path)."""
        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        return self.store.list_activity(session, actor=actor, limit=limit)

    def undo_last_action(
        self,
        session_id: str,
        client_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Revert the requesting actor's latest eligible action **synchronously**.

        "Eligible" means: the actor's most recent not-yet-undone activity
        record with an inverse op (``NoUndoableAction`` if there is none), and
        the state it touched has not changed since (``UndoConflict`` if it
        has — see ``session_activity.undo_conflict_reason``). Only the single
        latest record is considered; a conflicted latest action is reported
        rather than silently falling back to an older one, so the caller
        decides whether to retry.

        Replays the record's stored ``inverse_op`` through the same
        synchronous, lock-guarded path as ``delete_annotation``/``apply_layout``
        (``LayoutBusy`` while an ``apply_ops`` batch is mid-flight), with
        ``record_activity=False`` so the undo itself is not appended as a new
        undoable action. Deleting an annotation records the id in the store's
        short-lived "recently deleted" memory (so a stale create retry cannot
        resurrect it) — undoing that delete replays an ``annotation_created``
        for the same id, so that memory is cleared first here, or the store
        would otherwise refuse to recreate it.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not self._bucket.consume(client_id, 1):
            raise RateLimited()

        if self._lock(session_id).locked():
            raise LayoutBusy()

        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        if expected_revision is not None and expected_revision != session.seq:
            raise RevisionConflict(expected_revision, session.seq)

        record = self.store.find_latest_undoable(session, client_id)
        if record is None:
            raise NoUndoableAction(client_id)
        conflict_reason = self.store.undo_conflict_reason(session, record)
        if conflict_reason is not None:
            raise UndoConflict(record["id"], conflict_reason)

        inverse_op = dict(record["inverse_op"])
        if inverse_op.get("op") == "annotation_created":
            ann_id = (inverse_op.get("annotation") or {}).get("id")
            if isinstance(ann_id, str):
                try:
                    session._deleted_annotation_ids.remove(ann_id)
                except ValueError:
                    pass
        inverse_op["client_id"] = client_id

        applied = self._apply_op_sync(
            session, session_id, client_id, inverse_op, record_activity=False
        )
        if applied is None:
            # The conflict check above passed but the replay still turned out
            # to be a no-op (e.g. the store dropped it as an update on a
            # since-deleted annotation) — surface it the same way any other
            # unsafe-to-undo state does, rather than marking the record undone.
            raise UndoConflict(record["id"], "action could not be reverted")
        record["undone"] = True
        record["undone_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.store.persist(session)

        return {
            "undone_activity_id": record["id"],
            "undone_op": record["op"],
            "applied": applied,
            "revision": session.seq,
        }

    def _apply_op_sync(
        self,
        session: Session,
        session_id: str,
        client_id: str,
        op: Dict[str, Any],
        *,
        record_activity: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Apply, persist and broadcast one state op on the calling thread.

        Snapshots for rollback so a persistence failure leaves in-memory state,
        seq and ring untouched — mirroring apply_ops' all-or-nothing guarantee.
        Returns ``None`` when ``apply_state_op`` reports a legitimate no-op (e.g.
        an update on an already-deleted annotation, or a create retry for an id
        another collaborator just deleted) — nothing is persisted or broadcast,
        and the caller decides how to surface that (the two annotation callers
        above raise a typed exception; ``apply_layout``/``add_node_refs`` never
        hit this branch, since their ops always apply). ``record_activity=False``
        is passed through to ``apply_state_op`` by ``undo_last_action`` so
        replaying an inverse op does not itself become a new undoable action.
        """
        saved_state = copy.deepcopy(session.state)
        saved_seq = session.seq
        saved_updated_at = session.updated_at
        saved_activity_log = copy.deepcopy(session.activity_log)
        ring = self.store.ring(session_id)
        saved_ring = list(ring) if ring is not None else None
        try:
            applied = self.store.apply_state_op(
                session, op, record_activity=record_activity
            )
            if applied is not None:
                self.store.persist(session)
        except Exception:
            session.state = saved_state
            session.seq = saved_seq
            session.updated_at = saved_updated_at
            session.activity_log = saved_activity_log
            if ring is not None and saved_ring is not None:
                ring.clear()
                ring.extend(saved_ring)
            raise

        if applied is None:
            return None

        self.bus.publish(
            session_id,
            {
                "type": "op",
                "client_id": client_id,
                "op": applied,
                "seq": applied["seq"],
            },
        )
        return applied

    @staticmethod
    def _validate_claim_op(op: Dict[str, Any]) -> None:
        element_ids = op.get("element_ids")
        if not isinstance(element_ids, list) or not all(
            isinstance(e, str) for e in element_ids
        ):
            raise OpError(f"{op.get('op')}: 'element_ids' must be a list of strings")

    # ---------------- realtime connect / catch-up ----------------

    def connect(
        self, session_id: str, client_id: str, display_name: Optional[str]
    ) -> Tuple[Subscription, Dict[str, Any]]:
        """Register presence, subscribe to the bus and return the join event.

        The subscription is created *before* the join is broadcast so the new
        client's own ``presence_joined`` echo is captured too, keeping every
        client's roster convergent.
        """
        subscription = self.bus.subscribe(session_id)
        member = self.presence.join(session_id, client_id, display_name)
        self.bus.publish(session_id, {"type": "presence_joined", "member": member})
        return subscription, member

    def disconnect(
        self, session_id: str, client_id: str, subscription: Subscription
    ) -> None:
        self.bus.unsubscribe(subscription)
        # presence.leave() returns the member only when the *last* live
        # connection for this client_id has closed.  On a fast reconnect the
        # old SSE closes while the new one is already open; in that case leave()
        # returns None and we must not release claims or broadcast presence_left,
        # because the still-open sibling connection owns both.
        member = self.presence.leave(session_id, client_id)
        if member is not None:
            released = self.claims.release_all(session_id, client_id)
            if released:
                self.bus.publish(
                    session_id,
                    {
                        "type": "op",
                        "client_id": client_id,
                        "op": {
                            "op": "selection_released",
                            "client_id": client_id,
                            "element_ids": released,
                        },
                    },
                )
            self.bus.publish(
                session_id, {"type": "presence_left", "client_id": client_id}
            )

    def catch_up(self, session_id: str, since_seq: Optional[int]) -> Dict[str, Any]:
        """Return the initial payload for a (re)connecting stream.

        With a usable ``since_seq`` and an intact ring buffer, returns the missed
        ops; otherwise returns a full snapshot the client applies wholesale.
        """
        session = self.store.get(session_id)
        if session is None:
            raise SessionNotFound()
        roster = self.presence.roster(session_id)
        claims = self.claims.snapshot(session_id)
        if since_seq is not None:
            missed = self.store.ops_since(session_id, since_seq)
            if missed is not None:
                return {
                    "type": "catch_up",
                    "seq": session.seq,
                    "ops": missed,
                    "roster": roster,
                    "claims": claims,
                }
        return {
            "type": "snapshot",
            "seq": session.seq,
            "session": session.to_dict(),
            "roster": roster,
            "claims": claims,
        }

    # ---------------- MCP push ----------------

    def push_command(self, session_id: str, command: Dict[str, Any]) -> bool:
        """Broadcast an MCP visualization command to all subscribers.

        Returns ``False`` when the session is unknown so callers can fall back to
        the legacy single-consumer registry during the transition.
        """
        if self.store.get(session_id) is None:
            return False
        self.bus.publish(session_id, {"type": "command", "command": command})
        return True
