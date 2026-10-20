"""The session a request acts in (docs/SESSION_OVERLAY_CONTRACT.md §16.1).

Over HTTP a request names its session with ``x-communityoverview-session-id``,
bound into the request context once, by middleware, for every route. Over MCP
a graph tool names it with its ``visualization_session_id`` argument. Either
way the graph service reads it from here, so a write reached by any path - a
route, a tool, the chat assistant, an approved agent proposal - is handled in
that session's write mode or refused, never silently as a direct write (I2).

This module only carries the id and the refusals; what a session id resolves
to is ``backend.core.write_modes``' business.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

SESSION_ID_HEADER = "x-communityoverview-session-id"

# The MCP argument naming the session a graph tool acts in.
SESSION_ARGUMENT = "visualization_session_id"

_bound_session: ContextVar[Optional[str]] = ContextVar(
    "communityoverview_bound_session", default=None
)

SESSION_NOT_FOUND = "session_not_found"
SESSION_NOT_SUPPORTED = "session_not_supported"
SESSION_MISMATCH = "session_mismatch"
STAGED_UNSUPPORTED = "staged_unsupported"

# §16.3.
REFUSAL_STATUS = {
    SESSION_NOT_FOUND: 404,
    SESSION_NOT_SUPPORTED: 400,
    SESSION_MISMATCH: 400,
    STAGED_UNSUPPORTED: 422,
}


class SessionRefused(Exception):
    """A request named a session it cannot act in.

    ``code`` is one of the §16.3 codes above; ``message`` names only what the
    request itself carried.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    @property
    def status(self) -> int:
        return REFUSAL_STATUS.get(self.code, 400)

    def to_result(self, **extra: Any) -> Dict[str, Any]:
        return {"success": False, "error": self.code, "message": self.message, **extra}


def not_found(session_id: str) -> SessionRefused:
    return SessionRefused(SESSION_NOT_FOUND, f"session {session_id!r} not found")


def not_supported(what: str) -> SessionRefused:
    return SessionRefused(
        SESSION_NOT_SUPPORTED, f"{what} does not act in a session; send none"
    )


def mismatch(first: str, second: str) -> SessionRefused:
    return SessionRefused(
        SESSION_MISMATCH,
        f"the request names two sessions, {first!r} and {second!r}",
    )


def sessions_unreadable() -> SessionRefused:
    """This instance cannot read the staged-session records its graph store
    holds, so it cannot tell which sessions stage; it acts in none."""
    return SessionRefused(
        STAGED_UNSUPPORTED,
        "the graph store holds staged sessions, and staged sessions are not "
        "enabled on this instance",
    )


def current_session_id() -> Optional[str]:
    """The session the current request acts in, or None."""
    return _bound_session.get()


@contextmanager
def bind_session(session_id: Optional[str]) -> Iterator[None]:
    """Act in ``session_id`` (None: in no session) for the enclosed calls."""
    token = _bound_session.set(session_id)
    try:
        yield
    finally:
        _bound_session.reset(token)


def reconcile(bound: Optional[str], named: Optional[str]) -> Optional[str]:
    """The one session a request acts in, given what it bound and what it named.

    Naming the session already bound decides nothing; naming a different one
    is ``session_mismatch``.
    """
    if bound and named and bound != named:
        raise mismatch(bound, named)
    return named or bound
