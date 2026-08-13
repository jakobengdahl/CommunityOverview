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


def filter_tool_definitions(
    tool_definitions: List[Dict[str, Any]],
    *,
    autonomy_level: AutonomyLevel,
    tool_allowlist: Optional[List[str]],
    mcp_loader: Any,
) -> List[Dict[str, Any]]:
    """Drop the tool definitions the gate would unconditionally reject.

    This is a pure efficiency pre-filter for the definitions handed to the LLM,
    so a read-only or allowlist-restricted agent does not waste turns attempting
    tools it can never use. It mirrors — and must stay in lock-step with — the
    static rejections in :meth:`AutonomyGate.wrap`, reusing the same allowlist
    semantics, the same :data:`AutonomyLevel.is_read_only` notion, and the same
    :func:`is_mutating_tool` classification:

    - a tool outside ``tool_allowlist`` (when one is set) is dropped;
    - a mutating tool is dropped for read-only levels (observe/assist).

    Mutating tools under ``propose`` / ``act_after_approval`` are **kept**: the
    gate turns those into proposals rather than rejecting them, so the agent can
    still legitimately call them. Enforcement remains authoritative; nothing here
    grants access the gate would deny.
    """
    allow = set(tool_allowlist) if tool_allowlist else None
    read_only = autonomy_level.is_read_only

    filtered: List[Dict[str, Any]] = []
    for definition in tool_definitions:
        name = definition.get("name")
        if allow is not None and name not in allow:
            continue
        if read_only and is_mutating_tool(mcp_loader, name):
            continue
        filtered.append(definition)
    return filtered


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
