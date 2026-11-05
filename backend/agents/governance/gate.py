"""
The autonomy gate: enforces tool allowlists and autonomy levels at the agent's
tool-execution boundary.

The gate wraps the raw tool executor an agent uses during its LLM loop:

- a tool outside the agent's ``tool_allowlist`` (when one is set) is denied;
- attachment tools require their own grant independently of the allowlist;
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

from backend.runtime.attachment_access import (
    ATTACHMENT_TOOLS,
    ATTACHMENT_WRITE_TOOLS,
    attachment_tool_allowed,
    attachment_tool_refusal,
)
from backend.runtime.session_context import current_session_id
from backend.core.events.causation import current_agent_mutation_context

from .models import AutonomyLevel, Proposal
from .store import ProposalStore

from backend.runtime.content_logging import log_failure, mark_tainted

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
        *ATTACHMENT_WRITE_TOOLS,
    }
)

# Fixed review boundary from ADR 0010 §5; additions require explicit review.
ATTACHMENT_TAINT_PERMITTED_TOOLS = frozenset(
    {
        "search_graph",
        "get_related_nodes",
        "find_similar_nodes",
        "find_similar_nodes_batch",
        "list_node_types",
        "get_subtypes",
        "get_schema",
        "get_presentation",
        "list_saved_views",
        "get_saved_view",
        "get_node_details",
        "get_graph_stats",
        "get_capabilities",
        "get_runtime_info",
        "get_tenant_context",
        "get_config_context",
        "get_request_actor",
        "get_request_scope",
        "get_request_selection",
        "list_node_attachments",
        "read_node_attachment",
        "search_node_attachments",
        "propose_new_node",
        "present_form",
        "mark_nodes",
        "clear_visualization",
    }
)

ToolExecutor = Callable[[str, Dict[str, Any]], Any]


def original_tool_name(mcp_loader: Any, name: str) -> str:
    getter = getattr(mcp_loader, "get_tool", None)
    tool = getter(name) if getter else None
    return (
        getattr(tool, "original_name", None)
        or name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    )


def is_mutating_tool(mcp_loader: Any, namespaced_name: str) -> bool:
    """True when the named tool changes persistent state."""
    return original_tool_name(mcp_loader, namespaced_name) in MUTATING_ORIGINAL_TOOLS


def filter_tool_definitions(
    tool_definitions: List[Dict[str, Any]],
    *,
    autonomy_level: AutonomyLevel,
    tool_allowlist: Optional[List[str]],
    mcp_loader: Any,
    attachment_access: str = "none",
    proposal_store_available: bool = True,
) -> List[Dict[str, Any]]:
    """Drop the tool definitions the gate would unconditionally reject.

    This is a pure efficiency pre-filter for the definitions handed to the LLM,
    so a read-only or allowlist-restricted agent does not waste turns attempting
    tools it can never use. It mirrors — and must stay in lock-step with — the
    static rejections in :meth:`AutonomyGate.wrap`, reusing the same allowlist
    semantics, the same :data:`AutonomyLevel.is_read_only` notion, and the same
    :func:`is_mutating_tool` classification:

    - a tool outside ``tool_allowlist`` (when one is set) is dropped;
    - attachment tools require ``attachment_access`` even with an empty allowlist;
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
        original = original_tool_name(mcp_loader, name)
        if not attachment_tool_allowed(original, attachment_access):
            continue
        if not proposal_store_available and original in ATTACHMENT_WRITE_TOOLS:
            continue
        if (
            proposal_store_available
            and read_only
            and is_mutating_tool(mcp_loader, name)
        ):
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
        proposal_store: Optional[ProposalStore],
        agent_id: str,
        agent_name: Optional[str] = None,
        run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        attachment_access: str = "none",
        attachment_access_resolver: Optional[Callable[[], str]] = None,
        authorization_hook: Any = None,
    ) -> None:
        self._tainted = False
        self._taint_update_failed = False
        self._proposal_ids: list[str] = []
        self._attachment_access = attachment_access
        self._attachment_access_resolver = attachment_access_resolver
        self._authorization_hook = authorization_hook
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
            if self._taint_update_failed:
                return {
                    "error_code": "access_denied",
                    "error": "Run blocked because proposal taint could not be persisted.",
                }
            if self._allowlist is not None and namespaced_name not in self._allowlist:
                from backend.service.attachment_tools import (
                    audit_attachment_refusal,
                    tool_caller,
                )

                with tool_caller("agent", self._agent_id):
                    audit_attachment_refusal(
                        original_tool_name(self._mcp_loader, namespaced_name),
                        "access_denied",
                    )
                return {
                    "error": (
                        f"Tool '{namespaced_name}' is not in this agent's "
                        f"allowlist and was blocked."
                    )
                }

            original = original_tool_name(self._mcp_loader, namespaced_name)
            mutating = is_mutating_tool(self._mcp_loader, namespaced_name)
            if self._tainted:
                tool = self._mcp_loader.get_tool(namespaced_name)
                if (
                    getattr(tool, "integration_id", None) != "GRAPH"
                    or (
                        original not in ATTACHMENT_TAINT_PERMITTED_TOOLS
                        and not mutating
                    )
                    or (mutating and self._store is None)
                ):
                    return {
                        "error_code": "access_denied",
                        "error": "Tool blocked in a tainted run.",
                    }
            if original in ATTACHMENT_TOOLS:
                access = self._attachment_access
                if self._attachment_access_resolver is not None:
                    access = self._attachment_access_resolver()
                refused = attachment_tool_refusal(
                    original, access, self._authorization_hook
                )
                if refused is not None:
                    from backend.service.attachment_tools import (
                        audit_attachment_refusal,
                        tool_caller,
                    )

                    with tool_caller("agent", self._agent_id):
                        audit_attachment_refusal(original, refused["error_code"])
                    return refused
                if self._store is None and original in ATTACHMENT_WRITE_TOOLS:
                    return {
                        "error_code": "access_denied",
                        "error": "Attachment writes require a proposal store.",
                    }

            if self._store is not None and is_mutating_tool(
                self._mcp_loader, namespaced_name
            ):
                if self._level.is_read_only:
                    return {
                        "error": (
                            f"Agent autonomy level '{self._level.value}' is "
                            f"read-only; mutating tool '{namespaced_name}' was "
                            f"blocked."
                        )
                    }
                return self._record_proposal(namespaced_name, input_args)

            # Persist a conservative taint barrier BEFORE a tool can return
            # untrusted content. A failed write must leave the tool unexecuted,
            # rather than leave proposals approvable as clean after a read.
            protected = self._proposal_ids if not self._tainted else []
            if protected:
                try:
                    self._store.mark_tainted(protected)
                except Exception as exc:
                    return self._block_taint_failure(exc)

            result = None
            try:
                result = raw_executor(namespaced_name, input_args)
                if isinstance(result, dict) and result.get("untrusted_content") is True:
                    self._tainted = True
                    mark_tainted()
            finally:
                # Only this gate's still-clean run may undo its provisional
                # barrier. Actual taint remains sticky, including stale saves.
                if protected and not self._tainted:
                    try:
                        self._store.clear_provisional_taint(protected)
                    except Exception as exc:
                        return self._block_taint_failure(exc)
            return result

        return governed

    def _block_taint_failure(self, exc: Exception) -> Dict[str, Any]:
        self._taint_update_failed = True
        log_failure(logger, logging.ERROR, "proposal_taint_failed", exc, redact=True)
        return {
            "error_code": "access_denied",
            "error": "Run blocked because proposal taint could not be persisted.",
        }

    def _record_proposal(
        self, namespaced_name: str, input_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            context = current_agent_mutation_context()
            proposal = self._store.create(
                Proposal(
                    tainted=self._tainted,
                    agent_id=self._agent_id,
                    agent_name=self._agent_name,
                    tool=namespaced_name,
                    input_args=dict(input_args),
                    autonomy_level=self._level,
                    run_id=self._run_id,
                    correlation_id=(
                        context.event_correlation_id
                        if context
                        else self._correlation_id
                    ),
                    causation_depth=context.event_causation_depth if context else 1,
                    session_id=current_session_id(),
                )
            )
            self._proposal_ids.append(proposal.id)
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
            log_failure(
                logger, logging.ERROR, "proposal_record_failed", exc, redact=True
            )
            return {
                "error": (
                    f"Mutating tool '{namespaced_name}' could not be recorded "
                    f"for approval and was not executed."
                )
            }
