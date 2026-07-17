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

    def rename_session(self, session_id: str, name: Optional[str]) -> Optional[Session]:
        session = self.store.rename(session_id, name)
        if session is not None:
            self.bus.publish(
                session_id,
                {"type": "session_renamed", "name": name, "seq": session.seq},
            )
        return session

    def delete_session(self, session_id: str, deleted_by: Optional[str] = None) -> bool:
        existed = self.store.delete(session_id)
        if existed:
            # Broadcast before tearing down so connected clients get the notice.
            self.bus.publish(
                session_id,
                {"type": "session_deleted", "deleted_by": deleted_by},
            )
        self._locks.pop(session_id, None)
        return existed

    def roster(self, session_id: str) -> List[Dict[str, Any]]:
        return self.presence.roster(session_id)

    def connected_count(self, session_id: str) -> int:
        return self.presence.count(session_id)

    def claimed_elements(self, session_id: str) -> List[str]:
        """Element ids with a live selection claim (advisory soft-locks, D3).

        The current per-user selection is expressed as claims, so this is what an
        MCP query tool reports as ``selected_node_ids`` now that the browser no
        longer uploads its selection (design §3.8).
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

        session = self.store.get(session_id)
        if session is None:
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
            # Snapshot for rollback. persist() is inside the protected region so
            # a persistence-layer failure (disk full, IO error) rolls back too —
            # otherwise in-memory state/seq/ring would advance while disk and all
            # subscribers stayed behind, duplicating non-idempotent ops on retry.
            # The deepcopy cost is negligible at drag-end op cadence (D9).
            saved_state = copy.deepcopy(session.state)
            saved_seq = session.seq
            saved_name = session.name
            saved_updated_at = session.updated_at
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
                    await asyncio.to_thread(self.store.persist, session)
            except Exception:
                session.state = saved_state
                session.seq = saved_seq
                session.name = saved_name
                session.updated_at = saved_updated_at
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
