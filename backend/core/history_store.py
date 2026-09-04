"""
File-backed append-only history for graph mutations.

History is persisted next to graph.json as a sidecar NDJSON file
(``graph.history.ndjson``) so that the current graph snapshot in graph.json
stays small while an auditable trail of every mutation is retained durably.

Each line is one self-contained JSON record derived from a mutation
:class:`~backend.core.events.models.Event`. Records are appended in
chronological order; queries return them newest-first.

This helper is intentionally database-free and safe for standalone file mode:
writes are append-only under an exclusive OS file lock plus an in-process lock,
so concurrent writers never interleave a single record.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

from .storage_backends import _lock_file, _unlock_file

if TYPE_CHECKING:
    from .events.models import Event


# Origins that unambiguously indicate an autonomous/agent-driven mutation.
_AI_ORIGIN_PREFIXES = ("agent:",)
_AI_ORIGIN_EXACT = ("mcp",)
_AI_ACTOR_TYPES = ("agent", "ai")

logger = logging.getLogger(__name__)

# When retention is enabled but no explicit throttle is given, check at most
# this often so compaction never runs on every single append.
_DEFAULT_COMPACTION_INTERVAL = 256

# Size of the window used to walk the sidecar backwards. Large enough that a
# page of history is usually one read, small enough that memory stays flat
# however large the file grows.
_REVERSE_CHUNK_BYTES = 64 * 1024


def _iter_lines_reverse(f) -> Iterator[bytes]:
    """Yield a binary file's lines last-first, reading fixed-size chunks.

    The leading fragment of each chunk is held back rather than yielded: bytes
    earlier in the file may continue it. Only complete lines are ever handed
    out, so a multi-byte character split across a chunk boundary is reassembled
    before anyone tries to decode it.
    """
    f.seek(0, os.SEEK_END)
    position = f.tell()
    pending = b""

    while position > 0:
        read_size = min(_REVERSE_CHUNK_BYTES, position)
        position -= read_size
        f.seek(position)
        parts = (f.read(read_size) + pending).split(b"\n")
        pending = parts.pop(0)
        for part in reversed(parts):
            yield part

    if pending:
        yield pending


def derive_is_ai_action(
    event_origin: Optional[str],
    attribution: Optional[Any] = None,
) -> bool:
    """Return whether an event looks like an AI/agent-driven action.

    Signals, in order: an ``agent:<id>`` or ``mcp`` origin, or an attribution
    whose ``actor.actor_type`` is an agent/AI. ``attribution`` may be an
    :class:`EventAttribution` object or its dict form.
    """
    if event_origin:
        if event_origin.startswith(_AI_ORIGIN_PREFIXES):
            return True
        if event_origin in _AI_ORIGIN_EXACT:
            return True

    actor_type = ""
    if attribution is not None:
        actor = getattr(attribution, "actor", None)
        if actor is not None:
            actor_type = getattr(actor, "actor_type", "") or ""
        elif isinstance(attribution, dict):
            actor_type = (attribution.get("actor") or {}).get("actor_type", "") or ""

    return actor_type.lower() in _AI_ACTOR_TYPES


def event_to_history_record(event: "Event") -> Dict[str, Any]:
    """Flatten a mutation event into a durable, UI-friendly history record."""
    origin = event.origin
    attribution = origin.attribution if origin else None

    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        "entity_kind": event.entity.kind.value,
        "entity_id": event.entity.id,
        "entity_type": event.entity.type,
        "before": event.entity.before,
        "after": event.entity.after,
        "patch": event.entity.patch,
        "event_origin": origin.event_origin if origin else None,
        "event_session_id": origin.event_session_id if origin else None,
        "event_correlation_id": origin.event_correlation_id if origin else None,
        "attribution": attribution.to_dict() if attribution else None,
        "is_ai_action": derive_is_ai_action(
            origin.event_origin if origin else None,
            attribution,
        ),
    }


class GraphHistoryStore:
    """Append-only NDJSON history sidecar for graph mutations.

    History is unbounded by default. Optional retention caps
    (``max_events`` and/or ``max_age_days``) trim the sidecar during a
    lazily-triggered compaction pass so it does not grow without bound in
    long-running standalone deployments. Compaction rewrites the file via a
    temp file + atomic rename (mirroring the graph.json save path), so a
    failure can never leave a truncated or corrupt sidecar.
    """

    def __init__(
        self,
        history_path: str | Path,
        *,
        max_events: Optional[int] = None,
        max_age_days: Optional[float] = None,
        compaction_interval: Optional[int] = None,
    ):
        self.history_path = Path(history_path)
        self._lock = threading.Lock()

        # Retention is opt-in: a cap counts only when it is a positive value.
        self.max_events = (
            max_events if (max_events is not None and max_events > 0) else None
        )
        self.max_age_days = (
            max_age_days if (max_age_days is not None and max_age_days > 0) else None
        )
        self._retention_enabled = (
            self.max_events is not None or self.max_age_days is not None
        )

        if compaction_interval is not None:
            self._compaction_interval = max(1, compaction_interval)
        elif self.max_events is not None:
            self._compaction_interval = max(
                1, min(self.max_events, _DEFAULT_COMPACTION_INTERVAL)
            )
        else:
            self._compaction_interval = _DEFAULT_COMPACTION_INTERVAL
        self._appends_since_compaction = 0

    def append_event(self, event: "Event") -> None:
        """Persist one mutation event as a history record."""
        self.append_record(event_to_history_record(event))

    def append_record(self, record: Dict[str, Any]) -> None:
        """Append a single pre-built record as one NDJSON line."""
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    f.write(line + "\n")
                    f.flush()
                finally:
                    _unlock_file(f)

            if self._retention_enabled:
                self._appends_since_compaction += 1
                if self._appends_since_compaction >= self._compaction_interval:
                    # The record is already durably written above; a compaction
                    # failure is best-effort maintenance and must not surface as
                    # a failed mutation. It self-heals on the next interval.
                    try:
                        self._compact_locked()
                    except Exception:
                        logger.warning(
                            "Graph history compaction failed; retrying next interval",
                            exc_info=True,
                        )

    def compact(self) -> None:
        """Force a retention pass now. No-op when retention is disabled."""
        if not self._retention_enabled:
            return
        with self._lock:
            self._compact_locked()

    def get_recent(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Return recent history records, newest first."""
        with self._lock:
            with contextlib.closing(self._iter_records_reverse()) as stream:
                return self._take_page(stream, limit, offset)

    def get_entity_history(
        self,
        entity_id: str,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return history for a single node or edge id, newest first.

        ``kind`` optionally restricts matches to ``"node"`` or ``"edge"`` so a
        node and an edge that happen to share an id do not collide.
        """
        with self._lock:
            with contextlib.closing(self._iter_records_reverse()) as stream:
                matches = (
                    record
                    for record in stream
                    if record.get("entity_id") == entity_id
                    and (kind is None or record.get("entity_kind") == kind)
                )
                return self._take_page(matches, limit, offset)

    def _iter_records_reverse(self) -> Iterator[Dict[str, Any]]:
        """Yield records newest-first, reading the file backwards.

        Memory stays bounded by the chunk size and by whatever the caller
        keeps, however large the sidecar is. Malformed lines are skipped, as
        on every other read path.
        """
        if not self.history_path.exists():
            return

        with open(self.history_path, "rb") as f:
            _lock_file(f, exclusive=False)
            try:
                for raw in _iter_lines_reverse(f):
                    if not raw.strip():
                        continue
                    try:
                        yield json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            finally:
                _unlock_file(f)

    def _compact_locked(self) -> None:
        """Trim the sidecar to the retention policy. Caller holds ``self._lock``.

        Two streaming passes rather than one in-memory list: the first counts
        what survives the age filter, the second copies the tail of that
        through to a temp file. Loading every record to trim them would put the
        whole sidecar in memory at exactly the moment it has grown large enough
        to need trimming. Every record on disk is considered, including any
        that arrived since the last pass, so nothing is lost across trims. The
        rewrite is atomic (temp file + rename), so a failure leaves the
        existing sidecar untouched rather than truncated.
        """
        self._appends_since_compaction = 0
        if not self.history_path.exists():
            return

        cutoff = None
        if self.max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)

        total = 0
        surviving = 0
        for record in self._iter_records_forward():
            total += 1
            if cutoff is None or self._within_age(record, cutoff):
                surviving += 1

        skip = 0
        if self.max_events is not None and surviving > self.max_events:
            skip = surviving - self.max_events

        if skip == 0 and surviving == total:
            return

        self._rewrite_streaming(cutoff, skip)

    def _iter_records_forward(self) -> Iterator[Dict[str, Any]]:
        """Yield records oldest-first without holding them all. Caller holds the lock."""
        for _, record in self._iter_lines_forward():
            yield record

    def _iter_lines_forward(self) -> Iterator[tuple]:
        """Yield ``(raw_line, record)`` oldest-first, skipping malformed lines.

        Read as bytes and decoded per line: a line torn mid-character — what a
        crash during an append leaves behind — would otherwise raise
        UnicodeDecodeError out of compaction, where append_record swallows it
        and retries every interval, silently disabling retention for good.
        """
        if not self.history_path.exists():
            return

        with open(self.history_path, "rb") as f:
            _lock_file(f, exclusive=False)
            try:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        line = stripped.decode("utf-8")
                        yield line, json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            finally:
                _unlock_file(f)

    def _rewrite_streaming(self, cutoff: Optional[datetime], skip: int) -> None:
        """Copy the records retention keeps into a fresh sidecar, atomically.

        The line read from disk is written back as-is rather than being
        re-serialised from the parsed record, so a trim cannot quietly alter a
        record it is only supposed to keep or drop. (The text is decoded and
        re-encoded as UTF-8 on the way through, which round-trips losslessly
        for every line that survived the decode; only the line terminator is
        normalised.)
        """
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".ndjson",
            prefix="graph_history_",
            # Same directory as the sidecar, not TMPDIR: os.rename below is
            # only atomic within a filesystem, and the graph directory is a
            # mounted volume while TMPDIR is the container's own disk. Across
            # them rename raises EXDEV, append_record swallows it, and
            # retention stops running for good.
            dir=self.history_path.parent,
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as out:
                _lock_file(out, exclusive=True)
                try:
                    skipped = 0
                    for raw, record in self._iter_lines_forward():
                        if cutoff is not None and not self._within_age(record, cutoff):
                            continue
                        if skipped < skip:
                            skipped += 1
                            continue
                        out.write(raw + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                finally:
                    _unlock_file(out)

            if sys.platform == "win32" and self.history_path.exists():
                os.replace(temp_path, self.history_path)
            else:
                os.rename(temp_path, self.history_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    @staticmethod
    def _within_age(record: Dict[str, Any], cutoff: datetime) -> bool:
        raw = record.get("occurred_at")
        if not isinstance(raw, str):
            return True
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= cutoff

    @staticmethod
    def _take_page(
        records: Iterator[Dict[str, Any]], limit: int, offset: int
    ) -> List[Dict[str, Any]]:
        """Consume just enough of a newest-first stream to answer one page.

        Stopping at offset+limit is what keeps a query's cost proportional to
        the page rather than to the history.
        """
        if offset < 0:
            offset = 0
        if limit < 0:
            limit = 0
        if limit == 0:
            return []

        page: List[Dict[str, Any]] = []
        for index, record in enumerate(records):
            if index < offset:
                continue
            page.append(record)
            if len(page) >= limit:
                break
        return page
