"""Signed chat conversation state: the taint outlives the turn (ADR 0010 §5)."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict

from backend.llm.llm_providers import positive_int_setting

# Returned, and hashed, instead of an empty reply so the client can replay it.
EMPTY_REPLY_TEXT = "(no text reply)"

# Unset key: a per-process key, so another process's token fails verification
# and the conversation errs towards tainted. A shared key lets a tainted token
# verify elsewhere; a clean one verifies only where it was issued (below).
_PROCESS_KEY = secrets.token_bytes(32)

# Conversations this process has issued a tainted state for, by (actor,
# conversation id) -> time marked. A client holding an older clean token could
# otherwise replay the history up to it and come back clean. An entry older
# than the TTL is dropped: no clean token is issued while it is held, so every
# clean token issued before it has expired too.
_TAINTED_MAX = 100_000
_tainted = OrderedDict()
_tainted_lock = threading.Lock()
# The record is per process, so a clean token verifies only in the process that
# issued it, and only if no entry marked after its issue has been evicted.
# Restart, another worker or eviction leaves a conversation tainted, never clean.
_PROCESS_ID = secrets.token_hex(16)
_evicted_through = float("-inf")


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


def _ttl():
    return positive_int_setting("ATTACHMENT_CONVERSATION_STATE_TTL_SECONDS", 86400)


def _conversation_key(actor, conversation_id):
    return _canonical([actor, conversation_id])


def _marked_tainted(actor, conversation_id):
    with _tainted_lock:
        marked = _tainted.get(_conversation_key(actor, conversation_id))
    return marked is not None and time.time() - marked <= _ttl()


def _issue_state(tainted, actor, conversation_id):
    """The token's taint and issue time, decided against the record atomically.

    A turn whose start predates a concurrent tainting turn's mark is issued a
    tainted token too, so no clean token is issued after a mark.
    """
    global _evicted_through
    key = _conversation_key(actor, conversation_id)
    with _tainted_lock:
        now = time.time()
        marked = _tainted.get(key)
        tainted = tainted or (marked is not None and now - marked <= _ttl())
        if tainted and conversation_id is not None:
            _tainted[key] = now
            _tainted.move_to_end(key)
            while _tainted:
                oldest = next(iter(_tainted.values()))
                if now - oldest > _ttl():
                    _tainted.popitem(last=False)
                elif len(_tainted) > _TAINTED_MAX:
                    _tainted.popitem(last=False)
                    _evicted_through = max(_evicted_through, oldest)
                else:
                    break
        return tainted, int(now)


def issue(*, tainted, actor, messages, reply, conversation_id=None):
    """Token over the conversation as the server saw it after this turn."""
    tainted, issued_at = _issue_state(tainted, actor, conversation_id)
    body = _b64(
        _canonical(
            {
                "tainted": tainted,
                "actor": actor,
                "conversation_id": conversation_id,
                "process": _PROCESS_ID,
                "issued_at": issued_at,
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


def taint_status(messages, token, actor, conversation_id=None):
    """CLEAN, TAINTED (a valid token or this process says so) or UNVERIFIED."""
    # Checked before the new-conversation case: truncating a tainted
    # conversation's history, even to its first message, must not clean it.
    if conversation_id is not None and _marked_tainted(actor, conversation_id):
        return TAINTED
    last = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    if last < 0:
        return CLEAN
    # Without an id there is no record to hold a taint against rollback.
    if conversation_id is None:
        return UNVERIFIED
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
    if not (
        state.get("actor") == actor
        and state.get("conversation_id") == conversation_id
        and isinstance(issued_at, int)
        and 0 <= time.time() - issued_at <= _ttl()
    ):
        return UNVERIFIED
    # A signed taint holds even if the history was edited since: dropping the
    # attachment-derived turns must not launder it.
    if state.get("tainted") is not False:
        return TAINTED
    if state.get("hash") != conversation_hash(messages[: last + 1]):
        return UNVERIFIED
    # The record that would hold a later taint is this process's, and complete
    # only since its last eviction.
    if state.get("process") != _PROCESS_ID:
        return UNVERIFIED
    with _tainted_lock:
        if issued_at <= _evicted_through:
            return UNVERIFIED
    return CLEAN


def starts_clean(messages, token, actor, conversation_id=None):
    """Whether a request may start untainted; anything unverifiable is tainted."""
    return taint_status(messages, token, actor, conversation_id) == CLEAN
