"""Signed chat conversation state: the taint outlives the turn (ADR 0010 §5)."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from backend.llm.llm_providers import positive_int_setting

# Returned, and hashed, instead of an empty reply so the client can replay it.
EMPTY_REPLY_TEXT = "(no text reply)"

# Unset key: a per-process key, so another process's token fails verification
# and the conversation errs towards tainted.
_PROCESS_KEY = secrets.token_bytes(32)


def _key():
    configured = os.getenv("ATTACHMENT_CONVERSATION_STATE_KEY", "")
    return configured.encode() if configured else _PROCESS_KEY


def _canonical(value):
    # JSON input may carry lone surrogates (\ud800); surrogatepass hashes them
    # instead of raising, and no valid string encodes to the same bytes.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8", "surrogatepass")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(body):
    return _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def conversation_hash(messages):
    return hashlib.sha256(
        _canonical(
            [{"role": m.get("role"), "content": m.get("content")} for m in messages]
        )
    ).hexdigest()


def issue(*, tainted, actor, messages, reply):
    """Token over the conversation as the server saw it after this turn."""
    body = _b64(
        _canonical(
            {
                "tainted": bool(tainted),
                "actor": actor,
                "issued_at": int(time.time()),
                "hash": conversation_hash(
                    [*messages, {"role": "assistant", "content": reply}]
                ),
            }
        )
    )
    return f"{body}.{_sign(body)}"


# Outcomes of taint_status.
CLEAN = "clean"
TAINTED = "tainted"
UNVERIFIED = "unverified"


def taint_status(messages, token, actor):
    """CLEAN, TAINTED (a valid token says so) or UNVERIFIED (treat as tainted)."""
    last = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    if last < 0:
        return CLEAN
    # Non-ASCII would make compare_digest or body.encode() raise, not refuse.
    if not isinstance(token, str) or not token.isascii() or token.count(".") != 1:
        return UNVERIFIED
    body, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(body)):
        return UNVERIFIED
    try:
        state = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return UNVERIFIED
    if not isinstance(state, dict):
        return UNVERIFIED
    issued_at = state.get("issued_at")
    ttl = positive_int_setting("ATTACHMENT_CONVERSATION_STATE_TTL_SECONDS", 86400)
    if not (
        state.get("actor") == actor
        and isinstance(issued_at, int)
        and 0 <= time.time() - issued_at <= ttl
    ):
        return UNVERIFIED
    # A signed taint holds even if the history was edited since: dropping the
    # attachment-derived turns must not launder it.
    if state.get("tainted") is not False:
        return TAINTED
    if state.get("hash") != conversation_hash(messages[: last + 1]):
        return UNVERIFIED
    return CLEAN


def starts_clean(messages, token, actor):
    """Whether a request may start untainted; anything unverifiable is tainted."""
    return taint_status(messages, token, actor) == CLEAN
