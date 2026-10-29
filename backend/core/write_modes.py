"""Session write modes (docs/SESSION_OVERLAY_CONTRACT.md §5).

A session is `direct` - today's behaviour, every write lands in the graph - or
`staged`, its writes held in a layer of its own (§6). The mode is set when the
session is created and can only ever change from `direct` to `staged`.

The authority is the staged-session record the layer store keeps next to the
graph (§5.3), not the session document: the session store may not be shared
between instances, the graph is. So a session id with an active record is
staged on every instance, whatever its document says, and one whose record is
retired is refused as not found, whatever reappears under its id.

Only `retired` is cached here: once a record reaches it, it never leaves. An
`active` record retires when its session is deleted, possibly by another
instance, and `none` becomes `active` when any instance switches the session,
so both are re-read from the store on every resolution.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, Set, Tuple

from . import layer_store
from .session_store import is_valid_session_id

logger = logging.getLogger(__name__)

WRITE_MODE_DIRECT = "direct"
WRITE_MODE_STAGED = "staged"
WRITE_MODES = (WRITE_MODE_DIRECT, WRITE_MODE_STAGED)

SOURCE_EXPLICIT = "explicit"
SOURCE_INHERITED = "inherited"

# §6.4 and §20 item 2: ample for a workshop, cheap to compose.
DEFAULT_LAYER_MAX_ENTRIES = 1000

STAGED_UNSUPPORTED = "staged_unsupported"
WRITE_MODE_IRREVERSIBLE = "write_mode_irreversible"
INVALID_WRITE_MODE = "invalid_write_mode"

REFUSAL_STATUS = {
    STAGED_UNSUPPORTED: 422,
    WRITE_MODE_IRREVERSIBLE: 409,
    INVALID_WRITE_MODE: 422,
}


class WriteModeRefused(Exception):
    """A write-mode request this deployment, or this session, cannot take."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    @property
    def status(self) -> int:
        return REFUSAL_STATUS.get(self.code, 400)

    def to_result(self, **extra: Any) -> dict:
        return {"success": False, "error": self.code, "message": self.message, **extra}


def staged_unsupported() -> WriteModeRefused:
    return WriteModeRefused(
        STAGED_UNSUPPORTED,
        "staged sessions are not enabled on this deployment's graph backend",
    )


def check_write_mode(value: Any) -> Optional[str]:
    """A requested write mode, None for none requested, or a refusal."""
    if value is None:
        return None
    if value not in WRITE_MODES:
        raise WriteModeRefused(
            INVALID_WRITE_MODE,
            f"write_mode must be one of {', '.join(WRITE_MODES)}",
        )
    return value


class WriteModes:
    """Resolves and records the write mode of every session on this graph.

    ``layer_store`` is the graph backend when it declares `staged_sessions`,
    else None: then no session can be staged, and every resolution is
    `direct`.
    """

    def __init__(
        self,
        layer_store_backend: Optional[layer_store.LayerStoreBackend] = None,
        *,
        default_mode: str = WRITE_MODE_DIRECT,
        max_entries: int = DEFAULT_LAYER_MAX_ENTRIES,
        unreadable_records: Optional[Callable[[], bool]] = None,
    ) -> None:
        if default_mode not in WRITE_MODES:
            raise ValueError(f"default write mode must be one of {WRITE_MODES}")
        if default_mode == WRITE_MODE_STAGED and layer_store_backend is None:
            raise ValueError(
                "a staged default write mode needs a backend with staged_sessions"
            )
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._store = layer_store_backend
        self._default = default_mode
        self._max_entries = max_entries
        self._retired: Set[str] = set()
        self._lock = threading.Lock()
        self._unreadable_records = unreadable_records
        self._saw_unreadable = False

    @property
    def supports_staged(self) -> bool:
        return self._store is not None

    def store_holds_unreadable_records(self) -> bool:
        """Whether, staging being off here, the shared graph store holds
        staged-session records another instance wrote.

        Asked when a request names a session, not only at boot: an instance
        started before any record existed would otherwise go on writing those
        sessions as direct. A negative answer is never cached: another
        instance may create a record before the next request. A failed check
        refuses session requests until the store can be checked again. Once
        true it stays true, as records are never removed.
        """
        if self._store is not None or self._unreadable_records is None:
            return False
        if self._saw_unreadable:
            return True
        try:
            if self._unreadable_records():
                self._saw_unreadable = True
        except Exception:
            logger.warning("could not ask the graph store for staged sessions")
            return True
        return self._saw_unreadable

    @property
    def default_mode(self) -> str:
        return self._default

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def layer_store(self) -> Optional[layer_store.LayerStoreBackend]:
        return self._store

    def staged_status(self, session_id: str) -> str:
        """`active`, `retired` or `none`, read from the store unless retired."""
        if self._store is None or not is_valid_session_id(session_id):
            return layer_store.STATUS_NONE
        with self._lock:
            if session_id in self._retired:
                return layer_store.STATUS_RETIRED
        status = self._store.staged_status(session_id)
        if status == layer_store.STATUS_RETIRED:
            self._note_retired(session_id)
        return status

    def is_retired(self, session_id: str) -> bool:
        return self.staged_status(session_id) == layer_store.STATUS_RETIRED

    def mode_of(self, session_id: str) -> str:
        """`staged` for an active record, else `direct`. A retired id is the
        caller's to refuse; see ``staged_status``."""
        status = self.staged_status(session_id)
        return (
            WRITE_MODE_STAGED
            if status == layer_store.STATUS_ACTIVE
            else WRITE_MODE_DIRECT
        )

    def choose(self, requested: Any) -> Tuple[str, str]:
        """The mode and source a session created with ``requested`` gets."""
        mode = check_write_mode(requested)
        if mode is None:
            mode, source = self._default, SOURCE_INHERITED
        else:
            source = SOURCE_EXPLICIT
        if mode == WRITE_MODE_STAGED and self._store is None:
            raise staged_unsupported()
        return mode, source

    def record_staged(self, session_id: str) -> None:
        """Make ``session_id`` staged on every instance (§5.3)."""
        if self._store is None:
            raise staged_unsupported()
        try:
            self._store.record_staged(session_id)
        except layer_store.StagedSessionRetired:
            self._note_retired(session_id)
            raise

    def retire(self, session_id: str, *, require_empty: bool) -> None:
        """Retire the record and drop the layer as one unit; with
        ``require_empty``, refuse a layer holding entries (LayerNotEmpty)."""
        if self._store is None:
            raise staged_unsupported()
        self._store.retire(session_id, require_empty=require_empty)
        self._note_retired(session_id)

    def _note_retired(self, session_id: str) -> None:
        with self._lock:
            self._retired.add(session_id)
