"""
Basic agent governance for the open core.

Autonomy levels bound what an agent may do; mutating actions under the
approval-gated levels become durable proposals with a persistent approve/reject
decision and attribution. Advanced multi-approver / enterprise policy lives in
the commercial layer, on top of this baseline.
"""

from .models import (
    AutonomyLevel,
    DEFAULT_AUTONOMY,
    Proposal,
    ProposalStatus,
    coerce_autonomy,
)
from .store import (
    InMemoryProposalStore,
    ProposalStore,
    SqliteProposalStore,
)
from .gate import (
    AutonomyGate,
    MUTATING_ORIGINAL_TOOLS,
    filter_tool_definitions,
    is_mutating_tool,
)
from .manager import GovernanceManager

__all__ = [
    "AutonomyLevel",
    "DEFAULT_AUTONOMY",
    "Proposal",
    "ProposalStatus",
    "coerce_autonomy",
    "ProposalStore",
    "InMemoryProposalStore",
    "SqliteProposalStore",
    "AutonomyGate",
    "MUTATING_ORIGINAL_TOOLS",
    "filter_tool_definitions",
    "is_mutating_tool",
    "GovernanceManager",
]
