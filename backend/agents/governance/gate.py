"""
The autonomy gate: enforces tool allowlists and autonomy levels at the agent's
tool-execution boundary.

The gate wraps the raw tool executor an agent uses during its LLM loop:

- a tool outside the agent's ``tool_allowlist`` (when one is set) is denied;
- a **mutating** tool is blocked for read-only levels (observe/assist);
- a mutating tool under ``propose`` / ``act_after_approval`` is turned into a
  durable ``Proposal`` awaiting human approval and is **not** executed;
- read-only tools execute normally.

Applying an approved proposal happens later, off this path, via
``GovernanceManager`` using the raw executor.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .models import AutonomyLevel, Proposal
from .store import ProposalStore

logger = logging.getLogger(__name__)

# Tools (by their original, un-namespaced name) that change persistent state.
# Everything else — search/get/list/fetch/read — is treated as read-only.
MUTATING_ORIGINAL_TOOLS = frozenset(
    {
        "add_nodes",
        "update_node",
        "delete_nodes",
        "delete_edges",
        "write_file",
    }
)

ToolExecutor = Callable[[str, Dict[str, Any]], Any]


def is_mutating_tool(mcp_loader: Any, namespaced_name: str) -> bool:
    """True when the named tool changes persistent state."""
    getter = getattr(mcp_loader, "get_tool", None)
    if getter is None:
        return False
    tool = getter(namespaced_name)
    if tool is None:
        return False
    return getattr(tool, "original_name", None) in MUTATING_ORIGINAL_TOOLS


class AutonomyGate:
    """Wraps a tool executor to enforce one agent's governance for one run."""

    def __init__(
        self,
        *,
        autonomy_level: AutonomyLevel,
        tool_allowlist: Optional[List[str]],
        mcp_loader: Any,
        proposal_store: ProposalStore,
        agent_id: str,
        agent_name: Optional[str] = None,
        run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        self._level = autonomy_level
        self._allowlist = set(tool_allowlist) if tool_allowlist else None
        self._mcp_loader = mcp_loader
        self._store = proposal_store
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._run_id = run_id
        self._correlation_id = correlation_id

    def wrap(self, raw_executor: ToolExecutor) -> ToolExecutor:
        def governed(namespaced_name: str, input_args: Dict[str, Any]) -> Any:
            if self._allowlist is not None and namespaced_name not in self._allowlist:
                return {
                    "error": (
                        f"Tool '{namespaced_name}' is not in this agent's "
                        f"allowlist and was blocked."
                    )
                }

            if is_mutating_tool(self._mcp_loader, namespaced_name):
                if self._level.is_read_only:
                    return {
                        "error": (
                            f"Agent autonomy level '{self._level.value}' is "
                            f"read-only; mutating tool '{namespaced_name}' was "
                            f"blocked."
                        )
                    }
                return self._record_proposal(namespaced_name, input_args)

            return raw_executor(namespaced_name, input_args)

        return governed

    def _record_proposal(
        self, namespaced_name: str, input_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            proposal = self._store.create(
                Proposal(
                    agent_id=self._agent_id,
                    agent_name=self._agent_name,
                    tool=namespaced_name,
                    input_args=dict(input_args),
                    autonomy_level=self._level,
                    run_id=self._run_id,
                    correlation_id=self._correlation_id,
                )
            )
            return {
                "status": "proposed",
                "proposal_id": proposal.id,
                "message": (
                    "Action recorded as a proposal awaiting human approval; "
                    "it was not executed."
                ),
            }
        except Exception as exc:
            # A governance-store failure must fail closed: do not execute.
            logger.error("Governance: failed to record proposal: %s", exc)
            return {
                "error": (
                    f"Mutating tool '{namespaced_name}' could not be recorded "
                    f"for approval and was not executed."
                )
            }
