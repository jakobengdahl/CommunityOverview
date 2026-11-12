"""Process-local, single-use chat confirmations (ADR 0010 §5)."""

from collections import OrderedDict
from copy import deepcopy
import secrets
import threading
import time

from backend.agents.governance.gate import (
    ATTACHMENT_TAINT_PERMITTED_TOOLS as ATTACHMENT_TAINT_PERMITTED_TOOLS,
)
from backend.llm.llm_providers import positive_int_setting
from backend.runtime.authorization import build_graph_authorization_context


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


def confirmation_outcome(result):
    """The confirm route's response: an explicit outcome over the raw result.

    Tools report a refused write as success: false with the reason in message
    (add_nodes validation) or in error, so success is not implied by a result.
    """
    raw = result if isinstance(result, dict) else {}
    failed = raw.get("success") is False or bool(raw.get("error"))
    texts = (raw.get("error"), raw.get("message")) if failed else (raw.get("message"),)
    response = {
        "outcome": "failed" if failed else "ok",
        "message": next((t for t in texts if isinstance(t, str) and t), None),
        "result": result,
    }
    if raw.get("error_code"):
        response["error_code"] = raw["error_code"]
    return response


def pending_argument_refusal(name, arguments):
    """Bound stored file bodies before retaining an attachment write for approval."""
    if name != "add_node_attachment":
        return None
    from backend.service.attachment_tools import (
        ReadLimits,
        _content_length,
        _decoded_content,
        error,
    )

    if not isinstance(arguments, dict):
        return error("invalid_arguments")
    text, encoded = arguments.get("content_text"), arguments.get("content_base64")
    size = _content_length(text, encoded)
    if size is None:
        return error("invalid_arguments")
    if size > ReadLimits.from_env().mcp_max_decoded_bytes:
        return error("attachment_too_large_for_tool")
    if _decoded_content(text, encoded) is None:
        return error("invalid_arguments")
    return None


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
            "conversation_id": context.get("conversation_id") if context else None,
            "arguments": deepcopy(arguments),
            "notice": "File content was in this turn.",
        }

    def peek(self, token):
        with self._lock:
            entry = self._entries.get(token)
        return entry if entry and entry[0] > time.monotonic() else None

    def take(self, token):
        with self._lock:
            entry = self._entries.pop(token, None)
        return entry if entry and entry[0] > time.monotonic() else None


pending_actions = PendingActions()
