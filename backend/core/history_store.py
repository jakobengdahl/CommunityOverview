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

import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

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
        records = self._read_all()
        records.reverse()
        return self._paginate(records, limit, offset)

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
        matches = [
            r
            for r in self._read_all()
            if r.get("entity_id") == entity_id
            and (kind is None or r.get("entity_kind") == kind)
        ]
        matches.reverse()
        return self._paginate(matches, limit, offset)

    def _read_all(self) -> List[Dict[str, Any]]:
        """Read all records in chronological (append) order."""
        with self._lock:
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> List[Dict[str, Any]]:
        """Read all records in chronological order without taking ``self._lock``.

        The caller must already hold ``self._lock``. Malformed lines are
        skipped so a single bad write can never break the whole history query.
        """
        if not self.history_path.exists():
            return []

        records: List[Dict[str, Any]] = []
        with open(self.history_path, "r", encoding="utf-8") as f:
            _lock_file(f, exclusive=False)
            try:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            finally:
                _unlock_file(f)
        return records

    def _compact_locked(self) -> None:
        """Trim the sidecar to the retention policy. Caller holds ``self._lock``.

        Compaction reads every record currently on disk (including any that
        arrived since the last pass), so records are never lost across trims in
        this process. The rewrite is atomic (temp file + rename), so a failure
        leaves the existing sidecar untouched rather than truncated.
        """
        self._appends_since_compaction = 0
        records = self._read_all_unlocked()
        kept = self._apply_retention(records)
        if len(kept) == len(records):
            return
        self._rewrite_atomic(kept)

    def _apply_retention(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return the records to keep, in chronological order.

        Age filtering runs first, then the max-events cap keeps the newest N.
        Records whose timestamp cannot be parsed are kept (never dropped on a
        parse failure).
        """
        kept = records
        if self.max_age_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
            kept = [r for r in kept if self._within_age(r, cutoff)]
        if self.max_events is not None and len(kept) > self.max_events:
            kept = kept[-self.max_events :]
        return kept

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

    def _rewrite_atomic(self, records: List[Dict[str, Any]]) -> None:
        """Rewrite the sidecar with exactly ``records`` via temp file + rename."""
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".ndjson",
            prefix="graph_history_",
            dir=self.history_path.parent,
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                _lock_file(f, exclusive=True)
                try:
                    for record in records:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    _unlock_file(f)

            if sys.platform == "win32" and self.history_path.exists():
                os.replace(temp_path, self.history_path)
            else:
                os.rename(temp_path, self.history_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    @staticmethod
    def _paginate(
        records: List[Dict[str, Any]], limit: int, offset: int
    ) -> List[Dict[str, Any]]:
        if offset < 0:
            offset = 0
        if limit < 0:
            limit = 0
        return records[offset : offset + limit]
