"""
Data model for basic agent governance: autonomy levels and durable proposals.

An agent's ``AutonomyLevel`` bounds what its runs may do. Mutating actions under
the approval-gated levels become durable ``Proposal`` records with a persistent
approve/reject decision and attribution, instead of executing immediately.

These types are storage-free (pure data plus small helpers) so the in-memory and
SQLite proposal stores agree on shape and serialization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutonomyLevel(str, Enum):
    """
    How much an agent may do on its own.

    - ``observe``  – read-only; mutating tools are blocked.
    - ``assist``   – read-only; mutating tools are blocked (the agent assists a
      human who acts). Distinct from ``observe`` as an intent marker.
    - ``propose``  – mutating tools become proposals; approving records the
      decision but does not apply the action (a human applies it).
    - ``act_after_approval`` – mutating tools become proposals; approving applies
      the captured action automatically.
    """

    OBSERVE = "observe"
    ASSIST = "assist"
    PROPOSE = "propose"
    ACT_AFTER_APPROVAL = "act_after_approval"

    @property
    def is_read_only(self) -> bool:
        return self in (AutonomyLevel.OBSERVE, AutonomyLevel.ASSIST)

    @property
    def applies_on_approval(self) -> bool:
        return self is AutonomyLevel.ACT_AFTER_APPROVAL


DEFAULT_AUTONOMY = AutonomyLevel.ACT_AFTER_APPROVAL


def coerce_autonomy(value: Optional[str]) -> AutonomyLevel:
    """Parse a stored autonomy string, falling back to the default when unknown."""
    if not value:
        return DEFAULT_AUTONOMY
    try:
        return AutonomyLevel(value)
    except ValueError:
        return DEFAULT_AUTONOMY


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"  # approved, no auto-apply (propose level)
    REJECTED = "rejected"
    APPLIED = "applied"  # approved and the action was applied
    APPLY_FAILED = "apply_failed"  # approved but applying the action errored

    @property
    def is_decided(self) -> bool:
        return self is not ProposalStatus.PENDING


@dataclass
class Proposal:
    """A mutating agent action awaiting a human approve/reject decision."""

    agent_id: str
    tool: str  # namespaced tool name the agent tried to call
    input_args: Dict[str, Any]
    autonomy_level: AutonomyLevel
    agent_name: Optional[str] = None
    id: str = field(default_factory=lambda: f"prop-{uuid.uuid4()}")
    status: ProposalStatus = ProposalStatus.PENDING
    run_id: Optional[str] = None
    correlation_id: Optional[str] = None
    decided_by: Optional[str] = None
    apply_result: Optional[Dict[str, Any]] = None
    apply_error: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    decided_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.autonomy_level, AutonomyLevel):
            self.autonomy_level = AutonomyLevel(self.autonomy_level)
        if not isinstance(self.status, ProposalStatus):
            self.status = ProposalStatus(self.status)

    def copy(self) -> "Proposal":
        from dataclasses import replace

        clone = replace(self)
        clone.input_args = dict(self.input_args)
        clone.apply_result = (
            dict(self.apply_result) if self.apply_result is not None else None
        )
        return clone

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "tool": self.tool,
            "input_args": self.input_args,
            "autonomy_level": self.autonomy_level.value,
            "status": self.status.value,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "decided_by": self.decided_by,
            "apply_result": self.apply_result,
            "apply_error": self.apply_error,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "decided_at": _iso(self.decided_at),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Proposal":
        prop = cls(
            agent_id=data["agent_id"],
            tool=data["tool"],
            input_args=dict(data.get("input_args") or {}),
            autonomy_level=AutonomyLevel(data["autonomy_level"]),
            agent_name=data.get("agent_name"),
        )
        prop.id = data.get("id", prop.id)
        prop.status = ProposalStatus(data.get("status", ProposalStatus.PENDING.value))
        prop.run_id = data.get("run_id")
        prop.correlation_id = data.get("correlation_id")
        prop.decided_by = data.get("decided_by")
        prop.apply_result = data.get("apply_result")
        prop.apply_error = data.get("apply_error")
        prop.created_at = _parse(data.get("created_at")) or prop.created_at
        prop.updated_at = _parse(data.get("updated_at")) or prop.updated_at
        prop.decided_at = _parse(data.get("decided_at"))
        return prop


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return (
        dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None
    )


def _parse(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
