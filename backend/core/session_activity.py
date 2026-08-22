"""
Per-session activity log for annotation/canvas ops, enabling actor-scoped undo.

``SessionStore.apply_state_op`` is the single choke point every write path
(the async batched ``apply_ops`` and the synchronous MCP write methods) goes
through, so it is also where an activity record is built for the subset of
ops in ``UNDOABLE_OPS``: a snapshot of what changed (``before``/``after``),
who did it (``actor``, the op's ``client_id``), when, and a ready-to-apply
``inverse_op`` that would revert just that change.

The log is bounded (``DEFAULT_MAX_ACTIVITY_RECORDS`` records,
``DEFAULT_ACTIVITY_MAX_AGE_DAYS`` days) and persisted as part of the session
document via the existing ``SessionPersistenceBackend`` seam — no new storage
backend is introduced.

Undo eligibility ("did the affected state change since this action?") is
decided by comparing the *current* state for the record's ``affected`` target
against the ``after`` snapshot captured when the record was written — see
``current_snapshot_for``. A mismatch means something else touched the same
annotation/node/positions since, so replaying the inverse op would silently
clobber it; the caller should report a conflict instead.
"""

from __future__ import annotations

import copy
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

DEFAULT_MAX_ACTIVITY_RECORDS = 500
DEFAULT_ACTIVITY_MAX_AGE_DAYS = 7.0

# Ops the activity log tracks and can build an inverse for. Every other state
# op (session_renamed, group_membership_changed, nodes_added/removed,
# edges_*) is out of scope for this slice — the sessions design already keeps
# edges out of session state entirely (R14), and node add/remove has group
# membership side effects that a single inverse op cannot cleanly undo.
UNDOABLE_OPS = frozenset(
    {
        "annotation_created",
        "annotation_updated",
        "annotation_deleted",
        "node_moved",
        "layout_applied",
        "nodes_hidden",
        "nodes_shown",
    }
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_activity_id() -> str:
    return secrets.token_hex(8)


def build_activity_record(
    *,
    op_type: str,
    actor: str,
    session_id: str,
    seq: int,
    correlation_id: Optional[str],
    affected: Dict[str, Any],
    before: Any,
    after: Any,
    inverse_op: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build one activity record for an applied, undoable state op.

    ``before``/``after`` are deep-copied so later mutation of live session
    state (the same dicts the op just touched) can never retroactively change
    a persisted record.
    """
    return {
        "id": _new_activity_id(),
        "op": op_type,
        "actor": actor,
        "session_id": session_id,
        "seq": seq,
        "correlation_id": correlation_id,
        "occurred_at": _now_iso(),
        "affected": affected,
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "inverse_op": copy.deepcopy(inverse_op) if inverse_op is not None else None,
        "undone": False,
        "undone_at": None,
    }


def prune_activity_log(
    records: List[Dict[str, Any]],
    *,
    max_records: int = DEFAULT_MAX_ACTIVITY_RECORDS,
    max_age_days: float = DEFAULT_ACTIVITY_MAX_AGE_DAYS,
) -> List[Dict[str, Any]]:
    """Return ``records`` (chronological order) trimmed to the retention policy.

    Age filtering runs first, then the max-records cap keeps the newest N —
    mirroring ``GraphHistoryStore._apply_retention``. A record whose timestamp
    cannot be parsed is kept rather than dropped on a parse failure.
    """
    kept = records
    if max_age_days is not None and max_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        kept = [r for r in kept if _within_age(r, cutoff)]
    if max_records is not None and max_records > 0 and len(kept) > max_records:
        kept = kept[-max_records:]
    return kept


def _within_age(record: Dict[str, Any], cutoff: datetime) -> bool:
    raw = record.get("occurred_at")
    if not isinstance(raw, str):
        return True
    try:
        ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return ts >= cutoff


def find_latest_undoable(
    activity_log: List[Dict[str, Any]], actor: str
) -> Optional[Dict[str, Any]]:
    """The most recent not-yet-undone record for ``actor`` with an inverse op.

    Scans newest-first so a later action by the same actor is always offered
    before an older one, regardless of what other actors did in between.
    """
    for record in reversed(activity_log):
        if (
            record.get("actor") == actor
            and not record.get("undone")
            and record.get("inverse_op")
        ):
            return record
    return None


def current_snapshot_for(session_state: Dict[str, Any], record: Dict[str, Any]) -> Any:
    """The current value of whatever ``record['affected']`` points at.

    Shaped to match ``record['after']`` exactly for the same ``kind``, so a
    plain equality check against ``after`` is the whole conflict test.
    """
    affected = record.get("affected") or {}
    kind = affected.get("kind")
    if kind == "annotation":
        ann_id = affected.get("id")
        return next(
            (a for a in session_state.get("annotations", []) if a.get("id") == ann_id),
            None,
        )
    if kind == "node_position":
        return session_state.get("positions", {}).get(affected.get("id"))
    if kind == "node_visibility":
        hidden = set(session_state.get("hidden_node_ids", []))
        return sorted(i for i in affected.get("ids", []) if i in hidden)
    if kind == "layout":
        positions = session_state.get("positions", {})
        return {nid: positions.get(nid) for nid in affected.get("node_ids", [])}
    return None


def undo_conflict_reason(
    session_state: Dict[str, Any], record: Dict[str, Any]
) -> Optional[str]:
    """``None`` if ``record`` is still safe to undo, else a human-readable reason."""
    current = current_snapshot_for(session_state, record)
    expected = record.get("after")
    if current != expected:
        return "affected state changed since this action"
    return None
