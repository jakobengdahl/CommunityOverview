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
from pathlib import Path

from backend.llm.llm_providers import positive_int_setting

# Returned, and hashed, instead of an empty reply so the client can replay it.
EMPTY_REPLY_TEXT = "(no text reply)"

# Unset key: a per-process key, so another process's token fails verification
# and the conversation errs towards tainted. A shared key lets a tainted token
# verify elsewhere when the workers also share the session directory.
_PROCESS_KEY = secrets.token_bytes(32)

# Conversations this process has issued a tainted state for, by (actor,
# conversation id) -> time marked. A client holding an older clean token could
# otherwise replay the history up to it and come back clean. An entry older
# than the TTL is dropped: no clean token is issued while it is held, so every
# clean token issued before it has expired too. Its mark time still advances
# _evicted_through, for turns that started before it (_issue_state).
_TAINTED_MAX = 100_000
_tainted = OrderedDict()
_tainted_lock = threading.Lock()
# The record is per process, so a clean token verifies only in the process that
# issued it, and only if no entry marked after its issue has been evicted. The
# record is complete because only the minting process serves a conversation id
# (bind_conversation_id): another worker cannot taint one this process vouches
# clean for. Restart, another worker or eviction leaves it tainted, never clean.
_PROCESS_ID = secrets.token_hex(16)
_evicted_through = float("-inf")


class FileTaintRegistry:
    """Monotonic records beside file-backed sessions, shared by all workers.

    Only a newly minted id may create a clean record. Missing, damaged or
    unreadable records fail closed, including after storage loss. Records may
    be removed after the token TTL; removal makes any later replay tainted.
    """

    def __init__(self, sessions_directory):
        self.directory = Path(sessions_directory) / "conversation-taint"

    def _path(self, actor, conversation_id):
        digest = hashlib.sha256(_conversation_key(actor, conversation_id)).hexdigest()
        return self.directory / digest

    def create(self, actor, conversation_id):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._path(actor, conversation_id).open("x") as record:
                record.write("clean")
                record.flush()
                os.fsync(record.fileno())
        except OSError:
            pass  # A failed creation cannot vouch for a clean conversation.

    def is_tainted(self, actor, conversation_id):
        try:
            # Require writable storage too: read-only storage cannot remember
            # the next taint and must not authorize a clean continuation.
            with self._path(actor, conversation_id).open("r+") as record:
                value = record.read()
                os.fsync(record.fileno())
                return value != "clean"
        except (OSError, UnicodeError):
            return True

    def mark(self, actor, conversation_id):
        # No operation ever writes clean to an existing id. Concurrent marks
        # agree, and an interrupted write is itself fail-closed. Propagate a
        # write failure rather than issuing state whose mark was not saved.
        with self._path(actor, conversation_id).open("w") as record:
            record.write("tainted")
            record.flush()
            os.fsync(record.fileno())


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


def _id_signature(nonce, registry=None):
    # Shared ids are accepted only with a shared registry. The standalone
    # fallback keeps ids local so its in-memory record remains complete.
    return _b64(
        hmac.new(
            _key() if registry is not None else _PROCESS_KEY,
            nonce.encode()
            if registry is not None
            else f"{_PROCESS_ID}:{nonce}".encode(),
            hashlib.sha256,
        ).digest()
    )


def bind_conversation_id(conversation_id, *, registry=None, actor=None):
    """Accept a server-minted id in this registry, or initialize a fresh one."""
    if isinstance(conversation_id, str) and conversation_id.count(".") == 1:
        nonce, signature = conversation_id.split(".")
        if conversation_id.isascii() and hmac.compare_digest(
            signature, _id_signature(nonce, registry)
        ):
            return conversation_id
    nonce = secrets.token_urlsafe(24)
    conversation_id = f"{nonce}.{_id_signature(nonce, registry)}"
    if registry is not None:
        registry.create(actor, conversation_id)
    return conversation_id


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


def _issue_state(tainted, actor, conversation_id, started_at):
    """The token's taint and issue time, decided against the record atomically.

    A turn whose start predates a concurrent tainting turn's mark is issued a
    tainted token too, so no clean token is issued after a mark, even one since
    expired or dropped: a dropped mark made after the turn started may be its.
    """
    global _evicted_through
    key = _conversation_key(actor, conversation_id)
    with _tainted_lock:
        now = time.time()
        if started_at is None:
            started_at = now
        marked = _tainted.get(key)
        tainted = (
            tainted
            or (marked is not None and (now - marked <= _ttl() or marked >= started_at))
            or started_at <= _evicted_through
        )
        if tainted and conversation_id is not None:
            _tainted[key] = now
            _tainted.move_to_end(key)
            while _tainted:
                oldest = next(iter(_tainted.values()))
                if now - oldest <= _ttl() and len(_tainted) <= _TAINTED_MAX:
                    break
                _tainted.popitem(last=False)
                _evicted_through = max(_evicted_through, oldest)
        return tainted, int(now)


def issue(
    *,
    tainted,
    actor,
    messages,
    reply,
    conversation_id=None,
    started_at=None,
    registry=None,
):
    """Token over the conversation as the server saw it after this turn.

    started_at: when the turn read its taint status, before taint_status.
    """
    if registry is None:
        tainted, issued_at = _issue_state(tainted, actor, conversation_id, started_at)
    else:
        tainted = tainted or registry.is_tainted(actor, conversation_id)
        if tainted:
            registry.mark(actor, conversation_id)
        issued_at = int(time.time())
    body = _b64(
        _canonical(
            {
                "tainted": tainted,
                "actor": actor,
                "conversation_id": conversation_id,
                "process": _PROCESS_ID,
                "shared_registry": registry is not None,
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


def taint_status(messages, token, actor, conversation_id=None, *, registry=None):
    """CLEAN, TAINTED (a valid token or the registry says so) or UNVERIFIED."""
    # Checked before the new-conversation case: truncating a tainted
    # conversation's history, even to its first message, must not clean it.
    if registry is not None:
        if registry.is_tainted(actor, conversation_id):
            return TAINTED
    elif conversation_id is not None and _marked_tainted(actor, conversation_id):
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
    if registry is not None:
        return CLEAN if state.get("shared_registry") is True else UNVERIFIED
    if state.get("process") != _PROCESS_ID:
        return UNVERIFIED
    with _tainted_lock:
        if issued_at <= _evicted_through:
            return UNVERIFIED
    return CLEAN


def starts_clean(messages, token, actor, conversation_id=None, *, registry=None):
    """Whether a request may start untainted; anything unverifiable is tainted."""
    return (
        taint_status(messages, token, actor, conversation_id, registry=registry)
        == CLEAN
    )
