"""
Orchestration for shared sessions: the op protocol, conflict rules and catch-up.

``SessionManager`` ties the persistent ``SessionStore`` to the ephemeral
``session_hub`` (event bus + presence + selection claims + edit leases). It is
the single entry point used by the REST/SSE endpoints and by MCP pushes:

* ``apply_ops`` — validates a batch, applies each op under a per-session lock in
  arrival order (server-ordered last-write-wins, no CRDT — design D2), assigns a
  monotonic ``seq`` to each state op, persists once per batch, and broadcasts
  every applied op to all subscribers including the originator. It also rejects
  (``LeaseConflict``) a batch op that would mutate an annotation another client
  currently holds a live *edit lease* on — one of three gated browser write
  paths, alongside ``undo_last_action`` and the image-ingest endpoint;
  see ``LeaseConflict`` for the full picture and for what stays out of scope.
  It also handles edit-lease acquisition/renewal/release itself
  (``edit_lease_acquired``/``edit_lease_released``) — see ``LEASE_OPS``.
* selection claims (``selection_claimed`` / ``selection_released``) are handled
  inline but stay ephemeral — broadcast, never persisted, never sequenced, and
  (since ``dec-mcp-agent-ops-vs-annotation-claimmap``) purely a cosmetic "who
  has this selected" presence marker — they never gate a write. Edit leases
  (``LEASE_OPS``) are the ones that do.
* ``connect`` / ``disconnect`` manage presence and release a departing client's
  claims and leases.
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
    DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES,
    DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
    DEFAULT_MAX_SESSION_IMAGE_BYTES,
    data_url_byte_length,
)
from .session_annotations import IMAGE_TYPE, annotation_type_of, is_embedded_image_url
from .session_hub import (
    ClaimMap,
    InProcessEventBus,
    LeaseMap,
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

# AnnotationFieldConflict is raised by SessionStore.apply_state_op and
# propagates through this module's write paths unchanged; callers import it
# straight from session_store (`from backend.core.session_store import
# AnnotationFieldConflict, OpError`), matching how OpError itself is already
# imported there rather than re-exported here.

CLAIM_OPS = {"selection_claimed", "selection_released"}
# Edit-lease acquisition/release — task-annotation-exclusive-edit-leases. Kept
# distinct from CLAIM_OPS: claim ops are LWW and never fail, while an acquire
# here can be (partially) denied, so apply_ops handles the two differently
# (see the LEASE_OPS branch there for how a denial is reported without
# aborting the rest of the batch).
LEASE_OPS = {"edit_lease_acquired", "edit_lease_released"}

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

# Stable marker distinct from any real browser `graph_client_id`, used only for
# the *broadcast* attribution of an undo's replayed inverse op — see
# `_HUMAN_IMAGE_INGEST_CLIENT_ID` in rest_api.py for the identical trap this
# mirrors. `undo_last_action` replays the inverse op under the requesting
# actor's own `client_id` for every check leading up to the replay (rate
# limit, claim conflict, `find_latest_undoable`'s actor scoping); if that same
# real `client_id` were also used to attribute the op handed to
# `_apply_op_sync`, the undoing browser's own SSE subscription would receive
# an op carrying its own client_id, and sessionSyncClient.js's "echo of our
# own op" check (the same one genuine self-authored echoes correctly rely on)
# would drop it before it ever reached the canvas — even though the undo
# genuinely changed server-side state and every *other* client sees it. A
# distinct marker for this one attribution is required, not optional.
_UNDO_REPLAY_CLIENT_ID = "undo-replay"


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


class ImageBudgetExceeded(OpBatchTooLarge):
    """Embedding this image would exceed a configured image/document byte budget.

    Raised before the write is attempted, so a rejected image never touches
    the store. A subclass of ``OpBatchTooLarge`` (not a sibling) so every
    existing caller that already catches ``OpBatchTooLarge`` to mean "this
    write was too big, tell the caller `too_large`" — the REST ops endpoint,
    ``duplicate_annotation`` — keeps doing the right thing for an embedded
    image without change; a caller that wants the more specific budget
    message can catch ``ImageBudgetExceeded`` first. See
    ``_check_image_budgets`` for the shared enforcement every path that can
    persist an already-embedded image (create, duplicate, a raw ops-batch
    write) now routes through, instead of each guarding a different cap
    (``smallfix-duplicate-image-annotation-op-cap``,
    ``smallfix-embedded-image-over-op-batch-cap-immovable``).
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


class LeaseConflict(Exception):
    """A browser write tried to mutate an annotation another client currently
    holds a live edit lease on (first-actual-editor-wins —
    ``dec-mcp-agent-ops-vs-annotation-claimmap``,
    ``task-annotation-exclusive-edit-leases``).

    Raised from the ``SessionManager`` paths that check live leases: the
    ``apply_ops`` batch (the REST ``/ops`` endpoint — this also covers a
    denied ``edit_lease_acquired`` attempt targeting an id another client
    already holds, reported in that op's own ``denied`` result rather than by
    raising, since an acquire denial must not abort the rest of the batch),
    ``undo_last_action`` (``/undo``), which replays a stored inverse op and
    is reachable from the browser only — no MCP tool calls it — and, since
    ``task-mcp-annotation-human-edit-guard``, every synchronous MCP write
    method that can mutate an existing annotation: ``upsert_annotation``,
    ``upsert_image_annotation``, ``update_annotation``, ``delete_annotation``
    and ``set_group_members`` (all keyed to the shared ``mcp-agent`` client
    id — see ``mcp_tools.py``). Each of those five checks
    ``self.leases.snapshot(session_id)`` against ``_claimed_annotation_target``
    of the op it is about to hand to ``_apply_op_sync`` — the same pattern
    ``undo_last_action`` uses — immediately before that call and after every
    other precondition (revision check, existence check, budget check), so a
    conflict is raised before anything is mutated and closes the same
    preflight-vs-mutation race the image-ingest endpoint's own pre-check
    (below) does not fully close on its own. None of the five ever calls
    ``self.leases.acquire`` — an MCP write only ever checks against a lease,
    it never takes, renews or releases one; agents do not hold edit leases in
    v1 (``dec-mcp-agent-ops-vs-annotation-claimmap``). This is a
    collaboration-courtesy refusal, not an authorization or identity
    mechanism: ``client_id`` is caller-supplied and unauthenticated, so
    ``held_by`` names who currently holds the lease, not a verified identity.
    A fourth, purely-browser write path is gated too without raising this
    class as its check's own outcome: the image-ingest endpoint
    (``POST /sessions/{id}/annotations/image``) replaces an annotation
    directly rather than through ``apply_ops``, so it checks the same lease
    snapshot itself *before* the (awaited) fetch/optimize step as a
    fail-fast UX nicety and only borrows this class to format that 409's
    detail — the authoritative check is the one inside
    ``upsert_image_annotation`` itself, below, which both the REST endpoint
    and the MCP ``create_image_annotation`` tool ultimately call.
    ``apply_layout``, ``add_node_refs``, ``rename_session_sync`` and
    ``delete_session_sync`` stay unguarded: none of their ops
    (``layout_applied``/``nodes_added``/``session_renamed``, or no op at all)
    can mutate an annotation, so ``_claimed_annotation_target`` always
    reports no target for them — out of scope by construction, not merely
    unaddressed.
    """

    def __init__(self, annotation_id: str, held_by: str) -> None:
        super().__init__(
            f"annotation {annotation_id!r} is being edited by another client "
            f"({held_by!r}); wait for the lease to release or expire before "
            "editing it"
        )
        self.annotation_id = annotation_id
        self.held_by = held_by


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


def _claimed_annotation_target(op: Dict[str, Any], session: Session) -> Optional[str]:
    """Return the id of the *existing* annotation ``op`` would mutate, if any.

    Used to check an op against the live edit-lease snapshot before applying
    it — a batch op in ``apply_ops``, the stored inverse op
    ``undo_last_action`` is about to replay, or the op one of the synchronous
    MCP write methods (``upsert_annotation``/``upsert_image_annotation``/
    ``update_annotation``/``delete_annotation``/``set_group_members``) is
    about to hand to ``_apply_op_sync``. Only ``annotation_updated``/
    ``annotation_deleted``/``group_membership_changed`` always target an
    existing annotation; ``annotation_created`` targets one only when its id
    already exists in the session (the upsert-as-replace case) — a genuinely
    new id has no prior lease to protect. Every other state op type (node/
    edge/layout/rename ops) is out of scope for this check — see
    ``LeaseConflict``'s docstring for why annotations only, for now.
    """
    op_type = op.get("op")
    if op_type == "annotation_updated":
        annotation = op.get("annotation")
        ann_id = annotation.get("id") if isinstance(annotation, dict) else None
    elif op_type == "annotation_deleted":
        ann_id = op.get("annotation_id") or op.get("id")
    elif op_type == "annotation_created":
        annotation = op.get("annotation")
        ann_id = annotation.get("id") if isinstance(annotation, dict) else None
    elif op_type == "group_membership_changed":
        # The group annotation itself is the thing being mutated (its
        # `member_node_ids`), not the member nodes it references — a live
        # lease on the group protects this the same way one on a note
        # protects `annotation_updated` against it.
        ann_id = op.get("group_id")
    else:
        return None
    if not isinstance(ann_id, str):
        return None
    exists = any(a.get("id") == ann_id for a in session.state.get("annotations", []))
    return ann_id if exists else None


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


def _annotation_embedded_image_bytes(annotation: Any) -> int:
    """Decoded bytes of a *validated-shape* embedded image *annotation*
    carries, else 0.

    Gated on the exact ``is_embedded_image_url`` prefix — stricter than
    ``_image_annotation_bytes``'s ";base64," sniff — so only a payload the
    server itself could have produced (server-side ingest's own optimized
    content type) is routed through the larger image budgets
    (``_check_image_budgets``) instead of the flat op-batch cap. A
    same-shaped but forged payload (e.g. a raw ``apply_ops`` call carrying an
    ``image`` type with an arbitrary ";base64," string) still falls under the
    flat cap and is rejected there, before the store's own image-shape
    validation (``session_annotations.image_annotation_error``) ever runs.
    """
    if not isinstance(annotation, dict) or annotation_type_of(annotation) != IMAGE_TYPE:
        return 0
    image = annotation.get("image")
    if not isinstance(image, dict):
        return 0
    url = image.get("url")
    if not is_embedded_image_url(url):
        return 0
    return data_url_byte_length(url)


def _op_embedded_image_bytes(op: Dict[str, Any]) -> int:
    """``_annotation_embedded_image_bytes`` for an ops-batch entry: only
    ``annotation_created``/``annotation_updated`` ops can carry an
    annotation payload at all.
    """
    if op.get("op") not in ("annotation_created", "annotation_updated"):
        return 0
    return _annotation_embedded_image_bytes(op.get("annotation"))


def _check_image_budgets(
    session: "Session",
    *,
    annotation_id: Optional[str],
    existing: Optional[Dict[str, Any]],
    new_annotation: Dict[str, Any],
    image_bytes: int,
    max_session_image_bytes: int,
    max_session_document_bytes: int,
    max_optimized_image_bytes: int = DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES,
) -> None:
    """Raise ``ImageBudgetExceeded`` if persisting *new_annotation* (whose
    embedded image is *image_bytes* decoded bytes) here would exceed the
    per-image, per-session or per-document budget.

    Shared by every path that can persist an already-embedded image payload
    — dedicated ingest (``upsert_image_annotation``), the generic create/
    duplicate path (``upsert_annotation``), and a raw ops-batch write
    (``apply_ops``) — so all three enforce the one regime instead of each
    guarding a different, uncoordinated cap.

    The per-image check is redundant for ``upsert_image_annotation`` (its
    caller already downsized the image with ``image_ingest.optimize_image``
    before calling in), but not for the other two paths: neither goes
    through the optimizer, so without this check here a caller could submit
    an oversized but correctly-prefixed payload directly and have it
    budgeted as if it were a validated single image.
    """
    if image_bytes > max_optimized_image_bytes:
        raise ImageBudgetExceeded(
            f"embedded image is {image_bytes} bytes, exceeding the "
            f"{max_optimized_image_bytes}-byte per-image limit"
        )
    other_image_bytes = _session_image_bytes(session, exclude_id=annotation_id)
    if other_image_bytes + image_bytes > max_session_image_bytes:
        raise ImageBudgetExceeded(
            f"embedding this image would bring the session's total "
            f"embedded image data to {other_image_bytes + image_bytes} "
            f"bytes, over the {max_session_image_bytes}-byte session limit"
        )
    current_doc_bytes = len(json.dumps(session.to_dict()))
    existing_bytes = len(json.dumps(existing)) if existing is not None else 0
    new_annotation_bytes = len(json.dumps(new_annotation))
    projected_doc_bytes = current_doc_bytes - existing_bytes + new_annotation_bytes
    if projected_doc_bytes > max_session_document_bytes:
        raise ImageBudgetExceeded(
            f"embedding this image would bring the session document to "
            f"{projected_doc_bytes} bytes, over the "
            f"{max_session_document_bytes}-byte document limit"
        )


class SessionManager:
    """High-level façade over the session store, event bus, presence, selection
    claims and edit leases."""

    def __init__(
        self,
        store: SessionStore,
        *,
        event_bus: Optional[SessionEventBus] = None,
        presence: Optional[PresenceRegistry] = None,
        claims: Optional[ClaimMap] = None,
        leases: Optional[LeaseMap] = None,
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
        self.leases = leases or LeaseMap()
        self._max_ops = max_ops_per_batch
        self._max_sessions = max_sessions
        self._max_op_batch_bytes = max_op_batch_bytes
        self._bucket = _TokenBucket(bucket_capacity, bucket_refill_per_sec)
        self._lookup_bucket = _TokenBucket(
            lookup_bucket_capacity, lookup_refill_per_sec
        )
        # Separate keyspace, same sizing as the op bucket. The human image
        # ingest endpoint keys this on the request source rather than on a
        # client-declared id (see ``upsert_image_annotation``'s
        # ``rate_limit_key``); mixing source-derived keys into ``_bucket``
        # would let a client pick a ``client_id`` equal to a victim's source
        # key and drain that victim's image budget through ``/ops``.
        self._image_bucket = _TokenBucket(bucket_capacity, bucket_refill_per_sec)
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
        """Byte cap enforced on a single *non-image* ops batch (§3.9)."""
        return self._max_op_batch_bytes

    @property
    def max_request_body_bytes(self) -> int:
        """Sanity ceiling for the raw HTTP body of one ``POST .../ops`` request.

        ``apply_ops`` itself now applies two different caps depending on what
        each op carries (the flat ``max_op_batch_bytes`` for everything else,
        the larger image/session/document budgets for a validated embedded
        image), so the REST layer's pre-parse size check — which runs before
        the body is even JSON-decoded, and so cannot tell which case applies
        — has to admit the *larger* of the two: the full session document
        budget, since no legitimate batch can need to carry more than the
        document it is writing into could ever hold. ``apply_ops``'s own
        checks still enforce the tighter, per-case bound once the body is
        parsed.
        """
        return max(self._max_op_batch_bytes, DEFAULT_MAX_SESSION_DOCUMENT_BYTES)

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
        *,
        max_session_image_bytes: int = DEFAULT_MAX_SESSION_IMAGE_BYTES,
        max_session_document_bytes: int = DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
    ) -> Dict[str, Any]:
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(ops, list):
            raise OpError("'ops' must be a list")
        if len(ops) > self._max_ops:
            raise OpBatchTooLarge()
        # Bound the batch by *size* as well as *count* (§3.9): a single op such
        # as `layout_applied` can carry a large positions map that the op-count
        # cap alone would not catch. An op carrying a validated-shape embedded
        # image (the browser echoing a whole `image` annotation back on every
        # move/resize/relayer/lock, per sessionSyncClient.js) is charged
        # against the image budgets below instead of this flat cap — sized for
        # reference-only ops, not a multi-hundred-KB picture
        # (smallfix-embedded-image-over-op-batch-cap-immovable) — so it is
        # excluded from this sum rather than making every non-image op share a
        # cap wide enough to admit one.
        non_image_bytes = 0
        for op in ops:
            if not isinstance(op, dict):
                continue
            image_bytes = _op_embedded_image_bytes(op)
            if image_bytes:
                if image_bytes > DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES:
                    raise ImageBudgetExceeded(
                        f"embedded image is {image_bytes} bytes, exceeding "
                        f"the {DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES}-byte "
                        "per-image limit"
                    )
                continue
            non_image_bytes += len(json.dumps(op))
        if non_image_bytes > self._max_op_batch_bytes:
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
            if op_type in CLAIM_OPS or op_type in LEASE_OPS:
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

            # Read once, before this batch applies anything: acquire()/release()
            # for this session only ever commit to the real map later in this
            # same critical section (below, under this same lock; see the
            # "commit succeeded" block) — so a fresh copy taken here is a
            # consistent view of who held what when the batch started, matching
            # apply_ops' own all-or-nothing semantics (see LeaseConflict's
            # docstring for what this does not cover yet). Walked as a mutable
            # working copy (not re-read) so that an `edit_lease_acquired`/
            # `edit_lease_released` op earlier in *this* batch is visible to a
            # mutating op — or another lease op — later in the same batch,
            # exactly the way `self.store.apply_state_op` below sees each
            # state op's effect on `session.state` as it walks the same list.
            working_leases: Dict[str, str] = dict(self.leases.snapshot(session_id))

            # ("state", applied) | ("claim", op_type, element_ids)
            # | ("lease_acquire", granted, denied) | ("lease_release", element_ids),
            # in arrival order
            pending: List[Tuple[Any, ...]] = []
            state_changed = False
            try:
                for op in ops:
                    op_type = op["op"]
                    if op_type in CLAIM_OPS:
                        pending.append(("claim", op_type, list(op["element_ids"])))
                    elif op_type == "edit_lease_acquired":
                        granted: List[str] = []
                        denied: Dict[str, str] = {}
                        # De-duplicated so a caller listing the same id twice in
                        # one request cannot inflate `granted`/broadcast it twice.
                        for eid in dict.fromkeys(op["element_ids"]):
                            holder = working_leases.get(eid)
                            if holder is not None and holder != client_id:
                                denied[eid] = holder
                            else:
                                working_leases[eid] = client_id
                                granted.append(eid)
                        pending.append(("lease_acquire", granted, denied))
                    elif op_type == "edit_lease_released":
                        released: List[str] = []
                        for eid in dict.fromkeys(op["element_ids"]):
                            if working_leases.get(eid) == client_id:
                                del working_leases[eid]
                                released.append(eid)
                        pending.append(("lease_release", released))
                    else:
                        conflict_id = _claimed_annotation_target(op, session)
                        if conflict_id is not None:
                            holder = working_leases.get(conflict_id)
                            if holder is not None and holder != client_id:
                                raise LeaseConflict(conflict_id, holder)
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

                    # Cumulative budget check (smallfix-session-ops-path-ignores-
                    # image-budgets): the per-batch caps above only bound *one*
                    # request. Without this, many small batches — each legally
                    # under the flat cap, or each carrying one budget-legal
                    # image — can still walk the session's total document size
                    # (and total embedded-image bytes) arbitrarily far past the
                    # configured budgets over time. Checked against the
                    # *resulting* state (the snapshot, after every op in this
                    # batch has been applied in-memory) rather than the
                    # arriving ops, so it catches growth from any op type —
                    # not just images — before that snapshot is persisted; the
                    # `except Exception` below rolls it back like any other
                    # mid-batch failure.
                    total_image_bytes = _session_image_bytes(session)
                    if total_image_bytes > max_session_image_bytes:
                        raise ImageBudgetExceeded(
                            f"this batch would bring the session's total "
                            f"embedded image data to {total_image_bytes} "
                            f"bytes, over the {max_session_image_bytes}-byte "
                            "session limit"
                        )
                    total_doc_bytes = len(json.dumps(snapshot))
                    if total_doc_bytes > max_session_document_bytes:
                        raise ImageBudgetExceeded(
                            f"this batch would bring the session document to "
                            f"{total_doc_bytes} bytes, over the "
                            f"{max_session_document_bytes}-byte document limit"
                        )
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

            # Commit succeeded: apply ephemeral claim/lease effects and broadcast
            # every op in arrival order (originator included; clients apply
            # idempotently). Lease grants/releases are committed to the real
            # `self.leases` map only here — nothing else can have touched it
            # since the snapshot above, this whole method still holding the
            # per-session lock, so committing exactly what `working_leases`
            # simulation already decided cannot itself be refused.
            applied: List[Dict[str, Any]] = []
            for entry in pending:
                kind = entry[0]
                if kind == "state":
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
                elif kind == "claim":
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
                elif kind == "lease_acquire":
                    _, granted, denied = entry
                    if granted:
                        self.leases.acquire(session_id, client_id, granted)
                        # Only the ids this client now actually holds are
                        # broadcast — a denial is meaningful only to the
                        # requester, which learns it from `applied` below, not
                        # from the fan-out other clients receive.
                        self.bus.publish(
                            session_id,
                            {
                                "type": "op",
                                "client_id": client_id,
                                "op": {
                                    "op": "edit_lease_acquired",
                                    "client_id": client_id,
                                    "element_ids": granted,
                                },
                            },
                        )
                    applied.append(
                        {
                            "op": "edit_lease_acquired",
                            "element_ids": granted,
                            "denied": denied,
                        }
                    )
                else:  # "lease_release"
                    _, element_ids = entry
                    if element_ids:
                        self.leases.release(session_id, client_id, element_ids)
                        self.bus.publish(
                            session_id,
                            {
                                "type": "op",
                                "client_id": client_id,
                                "op": {
                                    "op": "edit_lease_released",
                                    "client_id": client_id,
                                    "element_ids": element_ids,
                                },
                            },
                        )
                    applied.append(
                        {"op": "edit_lease_released", "element_ids": element_ids}
                    )

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
        max_session_image_bytes: int = DEFAULT_MAX_SESSION_IMAGE_BYTES,
        max_session_document_bytes: int = DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
    ) -> Dict[str, Any]:
        """Create, or replace by id, one annotation **synchronously** (MCP write path).

        Shares ``apply_layout``/``add_node_refs``' atomicity contract: it never
        awaits, so it is atomic on a single-threaded loop, and a held per-session
        lock (an ``apply_ops`` batch mid-flight) means ``LayoutBusy`` rather than a
        seq assigned out of broadcast order.

        When ``annotation["id"]`` matches an existing annotation (the
        upsert-as-replace case), a live human edit lease on that id blocks
        this write with ``LeaseConflict`` — see ``_reject_if_leased`` and
        ``LeaseConflict``'s docstring. A genuinely new id has no prior lease
        to protect.

        ``annotation_created`` is already an upsert in ``SessionStore`` when
        ``annotation["id"]`` matches one already in the session (idempotent
        client retries), so create and upsert share this one op and this one
        method. Omit ``annotation["id"]`` to have the store mint one.

        This is also the path ``duplicate_annotation`` copies an image
        annotation through. An annotation carrying a validated-shape embedded
        image (``_annotation_embedded_image_bytes``) is routed through the
        same image/session/document budgets ``upsert_image_annotation``
        enforces, instead of the small flat op-batch cap every other
        annotation still uses — otherwise a duplicate of a realistic image
        (larger than 256KB but well inside the per-image budget) fails with
        ``too_large`` even though creating that same image succeeded
        (``smallfix-duplicate-image-annotation-op-cap``).
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(annotation, dict):
            raise OpError("'annotation' must be an object")
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

        image_bytes = _annotation_embedded_image_bytes(annotation)
        if image_bytes:
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
            _check_image_budgets(
                session,
                annotation_id=annotation_id if isinstance(annotation_id, str) else None,
                existing=existing,
                new_annotation=annotation,
                image_bytes=image_bytes,
                max_session_image_bytes=max_session_image_bytes,
                max_session_document_bytes=max_session_document_bytes,
            )
        elif len(json.dumps(annotation)) > self._max_op_batch_bytes:
            raise OpBatchTooLarge()

        op: Dict[str, Any] = {
            "op": "annotation_created",
            "annotation": annotation,
            "client_id": client_id,
        }
        self._reject_if_leased(session, session_id, client_id, op)
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
        rate_limit_key: Optional[str] = None,
        lease_client_id: Optional[str] = None,
        max_session_image_bytes: int = DEFAULT_MAX_SESSION_IMAGE_BYTES,
        max_session_document_bytes: int = DEFAULT_MAX_SESSION_DOCUMENT_BYTES,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create, or replace by id, one `image` annotation (MCP write path).

        Shares ``upsert_annotation``'s shape, atomicity contract and lease
        check: a replace-by-id targeting an annotation another client
        currently holds a live edit lease on raises ``LeaseConflict`` — see
        ``_reject_if_leased``. This is the *authoritative* lease check for
        both this method's callers (the REST image-ingest endpoint and the
        MCP ``create_image_annotation`` tool); the REST endpoint's own
        earlier pre-check exists only to fail fast before the (awaited)
        fetch/optimize step and does not substitute for the check here.

        ``lease_client_id`` separates *whose lease this checks against* from
        *who the op is attributed to* (``client_id``) — the same split
        ``rate_limit_key`` already makes for throttling, and for the same
        underlying reason: the REST image-ingest endpoint always attributes
        the op to the shared ``_HUMAN_IMAGE_INGEST_CLIENT_ID`` marker (so the
        posting browser's own SSE subscription does not drop it as a
        self-echo — see that constant's docstring), but the *lease* check
        must use the posting browser's real ``client_id`` or that browser
        would be unable to replace an image it holds its own lease on. When
        omitted (the MCP path, where attribution and identity are the same
        fixed marker) the lease check falls back to ``client_id``.

        A single optimized image (up to a few MB by default — see
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

        ``rate_limit_key`` separates *who is throttled* from *who the op is
        attributed to*. Callers that attribute their ops to a fixed marker
        rather than to the originating caller must pass it, or every such op
        server-wide would draw from that one marker's bucket and one caller
        could lock out everyone else. The REST ingest endpoint passes the
        request's source key for exactly that reason.

        When it is omitted the throttle falls back to ``client_id`` in the
        shared op bucket. That is the MCP path, and it is a known instance of
        the same shared-marker problem, not an exemption from it: every MCP
        tool passes one fixed agent marker, so all MCP callers share a bucket.
        Fixing that needs a decision about what an MCP caller should be keyed
        on (no request source exists at those call sites), so it is tracked
        separately rather than settled here.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(annotation, dict):
            raise OpError("'annotation' must be an object")
        if rate_limit_key is not None:
            if not self._image_bucket.consume(rate_limit_key, 1):
                raise RateLimited()
        elif not self._bucket.consume(client_id, 1):
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

        _check_image_budgets(
            session,
            annotation_id=annotation_id if isinstance(annotation_id, str) else None,
            existing=existing,
            new_annotation=annotation,
            image_bytes=optimized_image_bytes,
            max_session_image_bytes=max_session_image_bytes,
            max_session_document_bytes=max_session_document_bytes,
        )

        op: Dict[str, Any] = {
            "op": "annotation_created",
            "annotation": annotation,
            "client_id": client_id,
        }
        # Checked here, not by the caller (rest_api.py's own pre-check runs
        # earlier, before the awaited fetch/optimize step, purely as a
        # fail-fast UX nicety) — this is the authoritative check, taken fresh
        # and immediately before the mutation, so a lease acquired during
        # that earlier await cannot slip through. See _reject_if_leased and
        # lease_client_id's docstring for why the identity checked here can
        # differ from client_id (the op's broadcast attribution).
        self._reject_if_leased(
            session,
            session_id,
            lease_client_id if lease_client_id is not None else client_id,
            op,
        )
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
        base_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Patch one existing annotation **synchronously** (MCP write path).

        ``patch`` is merged onto the stored annotation with a shallow
        ``dict.update`` in ``SessionStore`` — only the keys present in ``patch``
        change, so a caller wanting to touch just one field (e.g. ``text``) must
        still carry forward any nested field (e.g. ``geometry``) it does not
        want overwritten with a partial value. Shares ``apply_layout``'s
        atomicity contract; see its docstring for the ``LayoutBusy`` rationale.
        A live human edit lease on ``patch["id"]`` blocks this write with
        ``LeaseConflict``, checked immediately before the mutation — see
        ``_reject_if_leased``.

        ``base_version`` is the field-level counterpart of ``expected_revision``
        (dec-annotation-field-patches-and-conflicts): where ``expected_revision``
        rejects the whole write the instant *anything* in the session has
        changed, ``base_version`` only rejects it when a field this ``patch``
        actually changes has itself changed server-side since the caller last
        read this annotation — a concurrent edit to an unrelated field on the
        same annotation still merges silently. Give the annotation's current
        ``version`` (from a prior read/write's ``annotation.version``) to opt
        in; omitted, this write applies unconditionally the same way it always
        has (see ``AnnotationFieldConflict`` for exactly what that trades
        away). Raises ``AnnotationFieldConflict`` — a 409 in every HTTP-facing
        caller, alongside ``LeaseConflict`` — when the check fails.
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
        if base_version is not None:
            op["base_version"] = base_version
        self._reject_if_leased(session, session_id, client_id, op)
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
        contract; see its docstring for the ``LayoutBusy`` rationale. A live
        human edit lease on ``annotation_id`` blocks this write with
        ``LeaseConflict``, checked immediately before the mutation — see
        ``_reject_if_leased``.
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
        self._reject_if_leased(session, session_id, client_id, op)
        applied = self._apply_op_sync(session, session_id, client_id, op)
        return {"applied": applied, "revision": session.seq}

    def set_group_members(
        self,
        session_id: str,
        client_id: str,
        group_id: str,
        member_node_ids: List[str],
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Replace a ``group`` annotation's ``member_node_ids`` **synchronously**
        (the MCP write path for group membership).

        Applies the same ``group_membership_changed`` op the browser's own
        add/remove-from-group actions use. ``SessionStore.apply_state_op``
        requires the op to name an id that already exists as a ``group``
        annotation and raises ``OpError`` otherwise — this method does not
        pre-check that itself, matching how ``update_annotation``/
        ``delete_annotation`` above let the store be the single place that
        judges existence. Shares ``apply_layout``'s atomicity contract; see
        its docstring for the ``LayoutBusy`` rationale. A live human edit
        lease on ``group_id`` blocks this write with ``LeaseConflict``,
        checked immediately before the mutation (and, since a missing
        ``group_id`` has no lease to protect, only after existence is
        implicitly established) — see ``_reject_if_leased``.

        Unlike the annotation write methods above, ``group_membership_changed``
        is deliberately outside ``session_activity.UNDOABLE_OPS`` (see that
        module's docstring), so this write is not undoable through
        ``undo_last_action``.
        """
        if not is_valid_session_id(session_id):
            raise SessionNotFound()
        if not isinstance(client_id, str) or not client_id:
            raise OpError("'client_id' is required")
        if not isinstance(group_id, str) or not group_id:
            raise OpError("'group_id' is required")
        if not isinstance(member_node_ids, list) or not all(
            isinstance(m, str) for m in member_node_ids
        ):
            raise OpError("'member_node_ids' must be a list of strings")
        if len(json.dumps(member_node_ids)) > self._max_op_batch_bytes:
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
            "op": "group_membership_changed",
            "group_id": group_id,
            "member_node_ids": member_node_ids,
            "client_id": client_id,
        }
        self._reject_if_leased(session, session_id, client_id, op)
        applied = self._apply_op_sync(session, session_id, client_id, op)
        group = next(
            (
                a
                for a in session.state.get("annotations", [])
                if a.get("id") == group_id
            ),
            None,
        )
        return {"applied": applied, "revision": session.seq, "annotation": group}

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

        The replay's *broadcast* attribution uses ``_UNDO_REPLAY_CLIENT_ID``,
        not ``client_id`` — see that constant's docstring for why. Every check
        above this (rate limit, claim conflict, actor-scoped
        ``find_latest_undoable``) still uses the real ``client_id``; only the
        inverse op handed to ``_apply_op_sync`` is re-attributed, right before
        it is applied.
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
        # Undo is a browser write like any other, so it answers to the same
        # edit-lease rule apply_ops does. Actor-scoping is not a substitute:
        # undo reverts *your own* past action, but the annotation it touches
        # may be under someone else's live edit lease since you made it —
        # participating in the *new* lease semantics rather than the old
        # advisory ClaimMap is exactly the point of this check (PR #456
        # originally gated undo on the claim map; the mechanism underneath
        # changed, not this call site's intent). Placed ahead of every
        # mutation below so a refusal leaves the session as it found it; the
        # two do not actually overlap today (the _deleted_annotation_ids
        # branch runs only for an annotation_created inverse, whose id
        # undo_conflict_reason has already rejected if it still exists), but
        # the ordering should not rely on that.
        conflict_id = _claimed_annotation_target(inverse_op, session)
        if conflict_id is not None:
            holder = self.leases.snapshot(session_id).get(conflict_id)
            if holder is not None and holder != client_id:
                raise LeaseConflict(conflict_id, holder)

        if inverse_op.get("op") == "annotation_created":
            ann_id = (inverse_op.get("annotation") or {}).get("id")
            if isinstance(ann_id, str):
                try:
                    session._deleted_annotation_ids.remove(ann_id)
                except ValueError:
                    pass
        # Attribution for the broadcast only — see _UNDO_REPLAY_CLIENT_ID's
        # docstring. Every check above (claim conflict included) has already
        # used the real client_id; only the op that goes to _apply_op_sync
        # (and therefore the SSE event's client_id) is re-attributed here, so
        # the requesting browser's own subscription does not drop it as a
        # self-echo.
        inverse_op["client_id"] = _UNDO_REPLAY_CLIENT_ID

        applied = self._apply_op_sync(
            session,
            session_id,
            _UNDO_REPLAY_CLIENT_ID,
            inverse_op,
            record_activity=False,
            trusted_replay=True,
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

    def _reject_if_leased(
        self, session: Session, session_id: str, client_id: str, op: Dict[str, Any]
    ) -> None:
        """Raise ``LeaseConflict`` if ``op`` would mutate an annotation a
        *different* client currently holds a live edit lease on.

        Shared by every synchronous MCP write method that can mutate an
        existing annotation (``upsert_annotation``/``upsert_image_annotation``/
        ``update_annotation``/``delete_annotation``/``set_group_members``) —
        each calls this immediately before its own ``_apply_op_sync``, after
        every other precondition (revision/existence/budget checks), so a
        conflict is raised before anything is mutated. Reads
        ``self.leases.snapshot`` fresh at call time rather than an
        earlier-cached view, and this method's caller never awaits between
        that read and the ``_apply_op_sync`` call that follows it — on the
        single-threaded event loop, no other coroutine can interleave a
        lease acquisition into that gap, closing the same race a
        preflight-only check (e.g. one run before an ``await``ed fetch) would
        leave open. Mirrors the check ``undo_last_action`` and ``apply_ops``
        already do — see ``LeaseConflict``'s docstring for the full picture,
        including why an MCP caller never *acquires* a lease here.
        """
        conflict_id = _claimed_annotation_target(op, session)
        if conflict_id is not None:
            holder = self.leases.snapshot(session_id).get(conflict_id)
            if holder is not None and holder != client_id:
                raise LeaseConflict(conflict_id, holder)

    def _apply_op_sync(
        self,
        session: Session,
        session_id: str,
        client_id: str,
        op: Dict[str, Any],
        *,
        record_activity: bool = True,
        trusted_replay: bool = False,
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
        replaying an inverse op does not itself become a new undoable action,
        together with ``trusted_replay=True`` so restoring the session's own
        prior state is not re-judged as fresh caller input.
        """
        saved_state = copy.deepcopy(session.state)
        saved_seq = session.seq
        saved_updated_at = session.updated_at
        saved_activity_log = copy.deepcopy(session.activity_log)
        ring = self.store.ring(session_id)
        saved_ring = list(ring) if ring is not None else None
        try:
            applied = self.store.apply_state_op(
                session,
                op,
                record_activity=record_activity,
                trusted_replay=trusted_replay,
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
        """Shared shape check for CLAIM_OPS and LEASE_OPS: both carry only an
        ``element_ids`` list of strings, so one validator covers both."""
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
        # returns None and we must not release claims/leases or broadcast
        # presence_left, because the still-open sibling connection owns both.
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
            released_leases = self.leases.release_all(session_id, client_id)
            if released_leases:
                self.bus.publish(
                    session_id,
                    {
                        "type": "op",
                        "client_id": client_id,
                        "op": {
                            "op": "edit_lease_released",
                            "client_id": client_id,
                            "element_ids": released_leases,
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
        leases = self.leases.snapshot(session_id)
        if since_seq is not None:
            missed = self.store.ops_since(session_id, since_seq)
            if missed is not None:
                return {
                    "type": "catch_up",
                    "seq": session.seq,
                    "ops": missed,
                    "roster": roster,
                    "claims": claims,
                    "leases": leases,
                }
        return {
            "type": "snapshot",
            "seq": session.seq,
            "session": session.to_dict(),
            "roster": roster,
            "claims": claims,
            "leases": leases,
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
