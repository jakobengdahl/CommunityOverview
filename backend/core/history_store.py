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
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .storage_backends import _lock_file, _unlock_file

if TYPE_CHECKING:
    from .events.models import Event, EventAttribution


# Origins that unambiguously indicate an autonomous/agent-driven mutation.
_AI_ORIGIN_PREFIXES = ("agent:",)
_AI_ORIGIN_EXACT = ("mcp",)
_AI_ACTOR_TYPES = ("agent", "ai")


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
    """Append-only NDJSON history sidecar for graph mutations."""

    def __init__(self, history_path: str | Path):
        self.history_path = Path(history_path)
        self._lock = threading.Lock()

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
        """Read all records in chronological (append) order.

        Malformed lines are skipped so a single bad write can never break the
        whole history query.
        """
        if not self.history_path.exists():
            return []

        records: List[Dict[str, Any]] = []
        with self._lock:
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

    @staticmethod
    def _paginate(
        records: List[Dict[str, Any]], limit: int, offset: int
    ) -> List[Dict[str, Any]]:
        if offset < 0:
            offset = 0
        if limit < 0:
            limit = 0
        return records[offset : offset + limit]
