"""
Visualization Session Registry.

Manages short-lived browser sessions that external AI clients can push
visualization commands to via MCP.  Each session is identified by a
short ID generated in the browser (e.g. "8244-1742") and backed by an
asyncio.Queue.  The browser holds an SSE connection open; when an MCP
tool pushes a command to the queue the browser receives it immediately.

This registry is the MCP *push* channel only.  Session *state* (what the
browser is showing) is owned by the shared-session store
(``core.session_store``); MCP query tools read it from there.  A registry
entry simply signals that a browser is connected to receive pushes.

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
import time
import re
from typing import Dict, Any, Optional, AsyncIterator

# Grouped-digit id DDDD-DDDD-DDDD-DDDD (four groups); the two-group legacy form
# DDDD-DDDD is still accepted so older visualization-session URLs keep working.
SESSION_ID_RE = re.compile(r"^\d{4}-\d{4}(?:-\d{4}-\d{4})?$")
_SESSION_TTL = 3600  # seconds — sessions not updated for this long are evicted


class SessionRegistry:
    """In-memory registry of active browser visualization sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, dict] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
                "queue": asyncio.Queue(),
                "created_at": time.monotonic(),
                "last_seen": time.monotonic(),
            }
        return self._sessions[session_id]["queue"]

    def _touch(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["last_seen"] = time.monotonic()

    def session_exists(self, session_id: str) -> bool:
        return session_id in self._sessions

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
            loop.call_soon(queue.put_nowait, command)
            self._touch(session_id)
            return True
        except RuntimeError:
            # Not in async context — use the stored loop reference for
            # thread-safe delivery from a thread-pool worker.
            if self._loop is None or not self._loop.is_running():
                return False
            self._loop.call_soon_threadsafe(queue.put_nowait, command)
            self._touch(session_id)
            return True

    async def push_command(self, session_id: str, command: Dict[str, Any]) -> bool:
        """Push *command* from an async context.  Returns False if session unknown."""
        if session_id not in self._sessions:
            return False
        await self._sessions[session_id]["queue"].put(command)
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
        while True:
            # Re-anchor to the current session entry in case it was evicted and
            # re-created since the previous iteration.
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
