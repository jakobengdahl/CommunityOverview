"""Attachment tool grants shared by Agents, chat and MCP (ADR 0010)."""

import logging

from backend.runtime.authorization import DefaultGraphAuthorizationHook

logger = logging.getLogger(__name__)

ATTACHMENT_READ_TOOLS = frozenset(
    {"list_node_attachments", "read_node_attachment", "search_node_attachments"}
)
ATTACHMENT_WRITE_TOOLS = frozenset({"add_node_attachment", "delete_node_attachment"})
ATTACHMENT_TOOLS = ATTACHMENT_READ_TOOLS | ATTACHMENT_WRITE_TOOLS


def validate_attachment_setting(value: str, variable: str) -> str:
    if value not in ("off", "read", "read_write"):
        raise ValueError(f"{variable} must be off, read or read_write")
    return value


def agent_attachment_access(value, *, agent_id: str) -> str:
    if agent_id.startswith("federated::"):
        return "none"
    if value not in ("none", "read", "read_write"):
        logger.warning(
            "Agent %s: invalid attachment_access %r; using none", agent_id, value
        )
        return "none"
    return value


def attachment_tool_allowed(name: str, access: str) -> bool:
    if name not in ATTACHMENT_TOOLS:
        return True
    return access == "read_write" or (
        access == "read" and name in ATTACHMENT_READ_TOOLS
    )


def attachment_tool_refusal(name: str, access: str, hook=None):
    """Check the grant and hook before any attachment tool is executed."""
    if name not in ATTACHMENT_TOOLS:
        return None
    if not attachment_tool_allowed(name, access):
        return {
            "success": False,
            "error_code": "access_denied",
            "error": f"Attachment access does not permit tool '{name}'.",
        }

    from backend.service.access import authorize_graph_access

    return authorize_graph_access(
        hook if hook is not None else DefaultGraphAuthorizationHook(),
        action="mutate" if name in ATTACHMENT_WRITE_TOOLS else "read",
        target=name
        if name in ATTACHMENT_WRITE_TOOLS or name == "list_node_attachments"
        else "read_node_attachment_content",
    )
