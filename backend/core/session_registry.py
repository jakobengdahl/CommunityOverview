"""
Visualization Session Registry.

Manages browser sessions that external AI clients can push visualization
commands to via MCP.  Each session is identified by a short ID generated in
the browser (e.g. "8244-1742") and backed by a bounded asyncio.Queue.  While
a browser holds the session's SSE connection open it receives a pushed
command immediately.  While nothing holds it, the command waits in the queue
(oldest dropped first once full), and a browser that opens the session before
the entry is evicted drains it and may apply it then.

This registry is the MCP *push* channel only.  Session *state* (what the
browser is showing) is owned by the shared-session store
(``core.session_store``); MCP query tools read it from there.  A registry
entry holds a session's push queue; it does not mean a browser is connected.
Whether anything is draining that queue is ``has_consumer`` (see "Entries vs.
consumers" below).

Thread-safety notes
-------------------
asyncio.Queue is not thread-safe across threads.  FastMCP calls sync
tools directly from the event loop thread (not via asyncio.to_thread),
so asyncio.get_running_loop() always succeeds inside push_command_sync
and the call_soon fast path is taken in normal operation.

The call_soon_threadsafe fallback path exists as a safety net for
callers that genuinely run in a separate thread (e.g. background workers
or future framework changes).  In that case the loop reference injected
at startup via set_event_loop() is used instead.

Entries vs. consumers
---------------------
A session entry says only that some id has been registered: ``get_or_create``
creates one, and callers that merely want the session to survive the periodic
prune (``mint_trigger_token``, the session auto-add tools) create one with no
browser anywhere.  Nothing removes an entry when the SSE connection closes, and
every push refreshes ``last_seen`` so TTL eviction never reclaims a session that
is being pushed to.  So an entry outlives its browser indefinitely and cannot
support a claim that a push will be read.  ``has_consumer`` answers that
question instead, ref-counted by ``stream`` for as long as something is actually
draining the queue.

Single-consumer design (V1 known limitation)
--------------------------------------------
Each session holds one queue.  If two SSE connections open for the same
session (e.g. a very fast page reload where the old connection hasn't
been torn down yet), pushed commands are split between them.  In
practice this window is sub-second and both connections belong to the
same browser tab, so messages landing on the closing connection are
silently dropped.  A fan-out design (per-consumer queues + broadcast)
would eliminate the split but is deferred to a future iteration.
"""

import asyncio
import secrets
import time
import re
from typing import Dict, Any, Optional, AsyncIterator

# Grouped-digit id DDDD-DDDD-DDDD-DDDD (four groups); the two-group legacy form
# DDDD-DDDD is still accepted so older visualization-session URLs keep working.
SESSION_ID_RE = re.compile(r"^\d{4}-\d{4}(?:-\d{4}-\d{4})?$")
_SESSION_TTL = 3600  # seconds — sessions not updated for this long are evicted

# Upper bound on buffered, undrained commands per session. Once a browser has
# moved to the shared op stream it closes its legacy SSE EventSource, so nothing
# drains that session's queue; every subsequent MCP-tool or pulse push would grow
# an unbounded queue for the session's lifetime (and each push refreshes
# last_seen, so TTL eviction never reclaims it). Bounding the queue with
# drop-oldest keeps memory finite while still replaying the most recent commands
# to a legacy consumer that connects later — whether within the sub-second
# legacy→op handover (design §8.1 R5) or up to _SESSION_TTL after the last push,
# when a browser opens a session that was pushed to while nothing was draining it.
_MAX_QUEUE_SIZE = 1000


class SessionRegistry:
    """In-memory registry of visualization-session push queues.

    An entry is a queue that may hold commands for a browser that has not opened
    the session yet; it does not mean a browser is connected (see
    ``has_consumer``).
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, dict] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # How many consumers are currently draining each session's queue.
        # Deliberately not stored in the session entry: TTL eviction can drop
        # and re-create that entry under a consumer that never left, which would
        # reset the count while the consumer is still there.
        self._consumers: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Event-loop injection
    # ------------------------------------------------------------------

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store a reference to the running event loop for thread-safe pushes."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_session_id(session_id: str) -> bool:
        return bool(SESSION_ID_RE.match(session_id))

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def get_or_create(self, session_id: str) -> asyncio.Queue:
        """Return the existing queue for *session_id*, creating the session if new."""
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "queue": asyncio.Queue(maxsize=_MAX_QUEUE_SIZE),
                "created_at": time.monotonic(),
                "last_seen": time.monotonic(),
            }
        return self._sessions[session_id]["queue"]

    @staticmethod
    def _enqueue_drop_oldest(queue: asyncio.Queue, command: Dict[str, Any]) -> None:
        """Put *command* on *queue*, discarding the oldest item if it is full.

        Runs in the event-loop thread (directly, or via call_soon/
        call_soon_threadsafe), so the drop-then-put pair is atomic with respect
        to the single SSE consumer draining the queue. Bounds memory for a
        session whose browser has stopped draining the legacy stream while
        keeping the newest commands for a consumer that reconnects within the
        handover window.
        """
        try:
            queue.put_nowait(command)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(command)
            except asyncio.QueueFull:
                pass

    def _touch(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["last_seen"] = time.monotonic()

    def session_exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def consumer_count(self, session_id: str) -> int:
        """How many consumers are currently draining this session's queue.

        A registry *entry* answers a different question and must not be read as
        this one. An entry is created by ``get_or_create`` — including from
        ``mint_trigger_token`` and the session auto-add tools, with no browser
        involved — nothing removes it when the SSE connection closes, and every
        push refreshes ``last_seen``, so ``cleanup_stale`` never reclaims a
        session that is being pushed to. An entry therefore outlives the browser
        that created it indefinitely, which is why "a command was enqueued" is
        not evidence that anything will read it.
        """
        return self._consumers.get(session_id, 0)

    def has_consumer(self, session_id: str) -> bool:
        """True while something is draining this session's queue."""
        return self.consumer_count(session_id) > 0

    # ------------------------------------------------------------------
    # Pulse-trigger tokens
    # ------------------------------------------------------------------
    #
    # A session owner (the browser holding the SSE stream, which knows its own
    # id) mints a capability-scoped secret so an *external* system can fire a
    # visual node pulse into the live visualization without being handed the
    # session id or the broader MCP push capability. The token lives only in
    # memory alongside the session, so it dies with the session (TTL eviction),
    # and re-minting rotates it — a previously shared trigger URL then stops
    # working (revocation). Durable, identity-bound API keys are deliberately a
    # hosted-layer concern and out of scope for the open core.

    def mint_trigger_token(self, session_id: str) -> str:
        """Create or rotate the pulse-trigger token for *session_id* and return it."""
        self.get_or_create(session_id)
        token = secrets.token_urlsafe(24)
        self._sessions[session_id]["trigger_token"] = token
        self._touch(session_id)
        return token

    def verify_trigger_token(self, session_id: str, token: Optional[str]) -> bool:
        """Constant-time check that *token* matches the session's trigger token.

        Returns False for an unknown session, a session with no token minted, or
        a missing/empty token, so a caller learns nothing about session
        existence before presenting a valid token.
        """
        if not token:
            return False
        entry = self._sessions.get(session_id)
        if not entry:
            return False
        stored = entry.get("trigger_token")
        if not stored:
            return False
        return secrets.compare_digest(stored, token)

    # ------------------------------------------------------------------
    # Command delivery
    # ------------------------------------------------------------------

    def push_command_sync(self, session_id: str, command: Dict[str, Any]) -> bool:
        """Push *command* to the session queue from a synchronous context.

        FastMCP calls sync tools from the event loop thread, so
        asyncio.get_running_loop() normally succeeds (fast path).  The
        call_soon_threadsafe fallback handles callers that genuinely run in a
        separate OS thread and need cross-thread delivery.

        Returns True if the command was enqueued, False if the session is unknown
        or no event loop is reachable.
        """
        if session_id not in self._sessions:
            return False
        queue = self._sessions[session_id]["queue"]
        try:
            # Fast path: already running inside the event loop thread.
            loop = asyncio.get_running_loop()
            loop.call_soon(self._enqueue_drop_oldest, queue, command)
            self._touch(session_id)
            return True
        except RuntimeError:
            # Not in async context — use the stored loop reference for
            # thread-safe delivery from a thread-pool worker.
            if self._loop is None or not self._loop.is_running():
                return False
            self._loop.call_soon_threadsafe(self._enqueue_drop_oldest, queue, command)
            self._touch(session_id)
            return True

    async def push_command(self, session_id: str, command: Dict[str, Any]) -> bool:
        """Push *command* from an async context.  Returns False if session unknown."""
        if session_id not in self._sessions:
            return False
        # Non-blocking drop-oldest rather than ``await queue.put`` so a full queue
        # (browser off the legacy stream, nothing draining) never blocks the
        # producer; memory stays bounded by _MAX_QUEUE_SIZE.
        self._enqueue_drop_oldest(self._sessions[session_id]["queue"], command)
        self._touch(session_id)
        return True

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    async def stream(self, session_id: str) -> AsyncIterator[Dict[str, Any]]:
        """Async generator that yields commands from the session queue indefinitely.

        The generator runs until the caller stops iterating (e.g. the HTTP
        connection closes and the StreamingResponse is cancelled).

        Re-looks up the queue on each iteration so that a TTL eviction followed
        by a reconnect (which re-creates the session) doesn't leave this generator
        blocked on an orphaned queue.
        """
        self.get_or_create(session_id)
        # Registering the consumer is what makes a push to this session
        # verifiable as delivered; the ``finally`` releases it when the caller
        # stops iterating, which for the SSE route is the connection closing.
        self._consumers[session_id] = self._consumers.get(session_id, 0) + 1
        try:
            while True:
                # Re-anchor to the current session entry in case it was evicted
                # and re-created since the previous iteration.
                if session_id not in self._sessions:
                    self.get_or_create(session_id)
                queue = self._sessions[session_id]["queue"]
                try:
                    command = await asyncio.wait_for(queue.get(), timeout=25.0)
                    self._touch(session_id)
                    yield command
                except asyncio.TimeoutError:
                    # Keep the session alive while the SSE connection is open,
                    # even when the canvas hasn't changed (no state uploads).
                    self._touch(session_id)
                    yield {"type": "ping"}
        finally:
            remaining = self._consumers.get(session_id, 0) - 1
            if remaining > 0:
                self._consumers[session_id] = remaining
            else:
                self._consumers.pop(session_id, None)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_stale(self) -> int:
        """Remove sessions not updated within SESSION_TTL seconds.  Returns eviction count."""
        cutoff = time.monotonic() - _SESSION_TTL
        stale = [
            sid for sid, entry in self._sessions.items() if entry["last_seen"] < cutoff
        ]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)

    @property
    def session_count(self) -> int:
        return len(self._sessions)
