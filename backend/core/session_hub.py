"""
Realtime fan-out, presence and selection claims for shared sessions.

This module holds the *ephemeral* half of the feature — nothing here is
persisted. It ships the ``SessionEventBus`` seam (design D5) so a SaaS
deployment can replace the single-instance ``InProcessEventBus`` with a
Redis-backed one for cross-instance fan-out. The core makes no attempt to work
across processes and documents that constraint.

Three concerns:

* ``InProcessEventBus`` — per-subscriber asyncio queues with a broadcast
  ``publish``. Slow consumers are dropped and told to resync (a full snapshot)
  rather than blocking the whole session.
* ``PresenceRegistry`` — the roster of connected clients with server-assigned
  colours. Ephemeral, keyed by ``(session_id, client_id)``.
* ``ClaimMap`` — advisory selection soft-locks with a 30 s TTL (D3). Expiry is
  lazy (pruned on every read/write) so no background task is needed and a
  departed user never freezes an element. The map itself stays advisory —
  ``claim()`` always takes over an existing claim (LWW) and never refuses —
  but ``session_manager.SessionManager.apply_ops`` now reads a snapshot of it
  and rejects (``ClaimConflict``) a browser batch op that would mutate an
  annotation another client currently holds. That check runs only for the
  ``apply_ops`` batch path (the REST ``/ops`` endpoint): whether an
  MCP-agent-issued write (the separate synchronous ``upsert_annotation`` /
  ``update_annotation`` / ``delete_annotation`` path, all keyed to the shared
  ``mcp-agent`` client id) should also be checked against a live claim, or
  should keep bypassing it the way it already bypasses the client-side
  exclusivity UI and the `locked` flag today, is an open product decision —
  not yet made, not implemented either way.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from typing import Any, Dict, List, Optional, Protocol

# Distinct, high-contrast marker colours assigned round-robin to joiners.
_PRESENCE_COLORS = [
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#46f0f0",
    "#f032e6",
    "#bcf60c",
    "#fabebe",
    "#008080",
]

_CLAIM_TTL_SECONDS = 30.0
_DEFAULT_SUBSCRIBER_QUEUE_MAX = 1000


class Subscription:
    """A single subscriber's queue plus a resync flag.

    When the bus drops events for a slow consumer it sets ``needs_resync`` and
    pushes a ``{"type": "resync"}`` sentinel so the stream handler can send a
    fresh snapshot and resume cleanly.
    """

    def __init__(self, session_id: str, sub_id: int, maxsize: int) -> None:
        self.session_id = session_id
        self.sub_id = sub_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        try:
            self.loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.needs_resync = False

    async def get(self) -> Dict[str, Any]:
        return await self.queue.get()


class SessionEventBus(Protocol):
    """Fan-out seam. Core ships in-process; SaaS swaps a Redis-backed bus."""

    def publish(self, session_id: str, event: Dict[str, Any]) -> None: ...

    def subscribe(self, session_id: str) -> Subscription: ...

    def unsubscribe(self, subscription: Subscription) -> None: ...


class InProcessEventBus:
    """Single-instance broadcast bus with per-subscriber queues."""

    def __init__(self, *, queue_max: int = _DEFAULT_SUBSCRIBER_QUEUE_MAX) -> None:
        self._queue_max = queue_max
        self._subscribers: Dict[str, Dict[int, Subscription]] = {}
        self._ids = itertools.count()
        self._lock = threading.Lock()

    def subscribe(self, session_id: str) -> Subscription:
        sub = Subscription(session_id, next(self._ids), self._queue_max)
        with self._lock:
            self._subscribers.setdefault(session_id, {})[sub.sub_id] = sub
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            subs = self._subscribers.get(subscription.session_id)
            if subs is not None:
                subs.pop(subscription.sub_id, None)
                if not subs:
                    self._subscribers.pop(subscription.session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(session_id, {}))

    def _publish_to_subscriber(self, sub: Subscription, event: Dict[str, Any]) -> None:
        """Deliver a single event to one subscriber on that subscriber's loop thread."""
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: drop the backlog and force a snapshot resync so
            # one stalled client cannot back-pressure the whole session.
            _drain(sub.queue)
            sub.needs_resync = True
            try:
                sub.queue.put_nowait({"type": "resync"})
            except asyncio.QueueFull:
                pass

    def publish(self, session_id: str, event: Dict[str, Any]) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        with self._lock:
            subscribers = list(self._subscribers.get(session_id, {}).values())

        for sub in subscribers:
            loop = sub.loop
            if loop is None or loop is current_loop:
                self._publish_to_subscriber(sub, event)
                continue
            if loop.is_closed():
                self.unsubscribe(sub)
                continue
            try:
                loop.call_soon_threadsafe(self._publish_to_subscriber, sub, event)
            except RuntimeError:
                # The subscriber loop shut down after we snapshotted it; prune the
                # dead subscription instead of surfacing a cross-thread shutdown race.
                self.unsubscribe(sub)


def _drain(queue: asyncio.Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


class PresenceRegistry:
    """Ephemeral roster of connected clients, keyed by session then client.

    A client may have more than one live connection at the same time (fast
    reconnect: new SSE opens before the old one is torn down).  A ref-count
    per ``(session_id, client_id)`` pair tracks how many connections are
    currently open.  The roster entry and colour slot are only freed when the
    last connection for that client departs.
    """

    def __init__(self) -> None:
        self._rosters: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._color_cursor: Dict[str, int] = {}
        # session_id -> client_id -> number of live connections
        self._conn_counts: Dict[str, Dict[str, int]] = {}

    def _assign_color(self, session_id: str, roster: Dict[str, Dict[str, Any]]) -> str:
        used = {m["color"] for m in roster.values()}
        for _ in range(len(_PRESENCE_COLORS)):
            idx = self._color_cursor.get(session_id, 0) % len(_PRESENCE_COLORS)
            self._color_cursor[session_id] = idx + 1
            color = _PRESENCE_COLORS[idx]
            if color not in used:
                return color
        # More clients than colours — reuse round-robin.
        idx = self._color_cursor.get(session_id, 0) % len(_PRESENCE_COLORS)
        self._color_cursor[session_id] = idx + 1
        return _PRESENCE_COLORS[idx]

    def join(
        self, session_id: str, client_id: str, display_name: Optional[str]
    ) -> Dict[str, Any]:
        roster = self._rosters.setdefault(session_id, {})
        counts = self._conn_counts.setdefault(session_id, {})
        existing = roster.get(client_id)
        color = (
            existing["color"] if existing else self._assign_color(session_id, roster)
        )
        member = {
            "client_id": client_id,
            "display_name": display_name or f"Guest-{len(roster) + 1}",
            "color": color,
        }
        roster[client_id] = member
        counts[client_id] = counts.get(client_id, 0) + 1
        return member

    def leave(self, session_id: str, client_id: str) -> Optional[Dict[str, Any]]:
        """Decrement the connection ref-count for ``client_id``.

        Returns the roster member dict only when the **last** live connection
        for this client has closed (count → 0), so callers can broadcast
        ``presence_left`` exactly once.  Returns ``None`` when other connections
        are still open or the client was not registered.
        """
        counts = self._conn_counts.get(session_id, {})
        count = counts.get(client_id, 0)
        if count > 1:
            counts[client_id] = count - 1
            return None  # sibling connection still open — keep the roster entry
        # count == 0 (not tracked) or count == 1 (last connection) → fully depart
        counts.pop(client_id, None)
        if not counts:
            self._conn_counts.pop(session_id, None)
        roster = self._rosters.get(session_id)
        if not roster:
            return None
        member = roster.pop(client_id, None)
        if not roster:
            self._rosters.pop(session_id, None)
            self._color_cursor.pop(session_id, None)
        return member

    def roster(self, session_id: str) -> List[Dict[str, Any]]:
        return list(self._rosters.get(session_id, {}).values())

    def count(self, session_id: str) -> int:
        return len(self._rosters.get(session_id, {}))


class ClaimMap:
    """Advisory selection soft-locks with a 30 s TTL and disconnect release."""

    def __init__(
        self, ttl: float = _CLAIM_TTL_SECONDS, *, time_fn=time.monotonic
    ) -> None:
        self._ttl = ttl
        self._time = time_fn
        # session_id -> element_id -> {client_id, expires_at}
        self._claims: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _prune(self, session_id: str) -> None:
        now = self._time()
        claims = self._claims.get(session_id)
        if not claims:
            return
        expired = [eid for eid, c in claims.items() if c["expires_at"] <= now]
        for eid in expired:
            del claims[eid]
        if not claims:
            self._claims.pop(session_id, None)

    def claim(
        self, session_id: str, client_id: str, element_ids: List[str]
    ) -> List[str]:
        """Claim (or renew) ``element_ids`` for ``client_id``.

        A claim held by another client is taken over — claims are advisory, so
        the map always reflects the most recent selector (LWW), matching the
        server-ordered conflict model.
        """
        self._prune(session_id)
        claims = self._claims.setdefault(session_id, {})
        expires_at = self._time() + self._ttl
        for eid in element_ids:
            claims[eid] = {"client_id": client_id, "expires_at": expires_at}
        return element_ids

    def release(
        self, session_id: str, client_id: str, element_ids: List[str]
    ) -> List[str]:
        self._prune(session_id)
        claims = self._claims.get(session_id, {})
        released = []
        for eid in element_ids:
            held = claims.get(eid)
            if held and held["client_id"] == client_id:
                del claims[eid]
                released.append(eid)
        if not claims:
            self._claims.pop(session_id, None)
        return released

    def release_all(self, session_id: str, client_id: str) -> List[str]:
        claims = self._claims.get(session_id, {})
        released = [eid for eid, c in claims.items() if c["client_id"] == client_id]
        for eid in released:
            del claims[eid]
        if not claims:
            self._claims.pop(session_id, None)
        return released

    def snapshot(self, session_id: str) -> Dict[str, str]:
        """Return ``element_id -> client_id`` for all live (non-expired) claims."""
        self._prune(session_id)
        return {
            eid: c["client_id"] for eid, c in self._claims.get(session_id, {}).items()
        }
