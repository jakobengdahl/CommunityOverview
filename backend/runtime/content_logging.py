"""Logging boundaries for an Agent run (ADR 0010 §6)."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import traceback

_state = ContextVar("content_logging_state", default=None)


@contextmanager
def content_logging(*, tainted=False, known_tools=(), taint_probe=None):
    state = {
        "tainted": tainted,
        "known_tools": frozenset(known_tools),
        "taint_probe": taint_probe,
    }
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)


def is_tainted():
    state = _state.get()
    if state and not state["tainted"] and state["taint_probe"] is not None:
        # Approval can outlive its proposal snapshot. Recheck at each logging
        # boundary, and retain taint once observed even if the row is removed.
        try:
            state["tainted"] = bool(state["taint_probe"]())
        except Exception:
            # An unavailable store must never enable content logging.
            state["tainted"] = True
    return bool(state and state["tainted"])


def mark_tainted():
    state = _state.get()
    if state is not None:
        state["tainted"] = True


def safe_tool_name(name, known_tools=()):
    state = _state.get()
    known = state["known_tools"] if state else known_tools
    from backend.runtime.attachment_access import ATTACHMENT_TOOLS

    return (
        name
        if name in known or name.removeprefix("GRAPH__") in ATTACHMENT_TOOLS
        else "unknown_tool"
    )


def argument_sizes(arguments):
    values = arguments.values() if isinstance(arguments, dict) else ()
    return "argument_count=%s argument_sizes=%s" % (
        len(arguments) if isinstance(arguments, dict) else 0,
        [len(json.dumps(value, default=str)) for value in values],
    )


def log_failure(logger, level, code, exc, *, redact=False, context=""):
    frame = traceback.extract_tb(exc.__traceback__)[-1]
    logger.log(
        level,
        "%s error_code=%s error_class=%s raising_frame=%s:%s%s",
        context,
        code,
        type(exc).__name__,
        frame.filename,
        frame.lineno,
        "" if redact or is_tainted() else f" error={exc}",
    )


class _TaintedRequestFilter(logging.Filter):
    def filter(self, record):
        if is_tainted() and record.msg == 'HTTP Request: %s %s "%s %d %s"':
            # URL paths, userinfo, fragments and even response reason phrases
            # can contain argument values. These records carry no safe tool
            # name or argument-size diagnostics, so suppress them entirely.
            return False
        return True


for _logger_name in ("httpx", "httpx2"):
    logging.getLogger(_logger_name).addFilter(_TaintedRequestFilter())
