"""Process-local, single-use chat confirmations (ADR 0010 §5)."""

from collections import OrderedDict
from copy import deepcopy
import secrets
import threading
import time

from backend.llm.llm_providers import positive_int_setting
from backend.runtime.authorization import build_graph_authorization_context

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


def request_actor():
    actor = build_graph_authorization_context(action="read", target="chat").actor
    return {"type": actor["actor_type"], "id": actor["actor_id"]}


def refusal(code):
    return {
        "error_code": code,
        "error": {
            "confirmation_required": "File content was in this turn. Confirm this action to run it.",
            "confirmation_unknown": "Send the request again.",
            "access_denied": "Confirmation access denied.",
        }[code],
    }


class PendingActions:
    def __init__(self):
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def add(self, name, arguments, context):
        token = secrets.token_urlsafe(32)
        ttl = positive_int_setting("ATTACHMENT_CONFIRMATION_TTL_SECONDS", 600)
        with self._lock:
            now = time.monotonic()
            self._entries = OrderedDict(
                (key, entry) for key, entry in self._entries.items() if entry[0] > now
            )
            self._entries[token] = (now + ttl, name, deepcopy(arguments), context)
            maximum = positive_int_setting("ATTACHMENT_PENDING_ACTIONS_MAX", 1000)
            while len(self._entries) > maximum:
                self._entries.popitem(last=False)
        return {
            "token": token,
            "tool": name,
            "arguments": deepcopy(arguments),
            "notice": "File content was in this turn.",
        }

    def take(self, token):
        with self._lock:
            entry = self._entries.pop(token, None)
        return entry if entry and entry[0] > time.monotonic() else None


pending_actions = PendingActions()
