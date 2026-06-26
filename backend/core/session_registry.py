"""
Visualization Session Registry.

Manages short-lived browser sessions that external AI clients can push
visualization commands to via MCP.  Each session is identified by a
short ID generated in the browser (e.g. "8244-1742") and backed by an
asyncio.Queue.  The browser holds an SSE connection open; when an MCP
tool pushes a command to the queue the browser receives it immediately.

Thread-safety notes
-------------------
asyncio.Queue is not thread-safe across threads.  MCP tools run in a
thread pool (FastMCP wraps sync functions with asyncio.to_thread), so
they must not call queue.put() directly.  Instead they call
push_command_sync(), which uses loop.call_soon_threadsafe().  The loop
reference is injected at startup via set_event_loop() once the asyncio
event loop is known.

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

SESSION_ID_RE = re.compile(r'^\d{4}-\d{4}$')
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
                "state": {},
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
    # State management
    # ------------------------------------------------------------------

    def update_state(self, session_id: str, state: Dict[str, Any]) -> bool:
        """Persist the browser's current canvas state.  Returns False if session unknown."""
        if session_id not in self._sessions:
            return False
        self._sessions[session_id]["state"] = state
        self._touch(session_id)
        return True

    def get_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the last known canvas state, or None if the session does not exist."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        self._touch(session_id)
        return entry["state"]

    # ------------------------------------------------------------------
    # Command delivery
    # ------------------------------------------------------------------

    def push_command_sync(self, session_id: str, command: Dict[str, Any]) -> bool:
        """Push *command* to the session queue from a synchronous context.

        Works in two modes:
        - Called from within the asyncio event loop thread (e.g. an ``async def``
          FastAPI endpoint): uses ``call_soon`` on the running loop directly.
        - Called from a thread-pool worker (FastMCP sync tool via
          ``asyncio.to_thread``): uses ``call_soon_threadsafe`` on the stored loop
          reference injected at startup via ``set_event_loop()``.

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
        by a browser state re-upload (which re-creates the session) doesn't leave
        this generator blocked on an orphaned queue.
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
                yield command
            except asyncio.TimeoutError:
                yield {"type": "ping"}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_stale(self) -> int:
        """Remove sessions not updated within SESSION_TTL seconds.  Returns eviction count."""
        cutoff = time.monotonic() - _SESSION_TTL
        stale = [sid for sid, entry in self._sessions.items() if entry["last_seen"] < cutoff]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)

    @property
    def session_count(self) -> int:
        return len(self._sessions)
