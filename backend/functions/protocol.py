"""
Broker protocol over a function process's standard streams
(docs/USER_FUNCTION_CONTRACT.md §6.1–§6.2, §8, §11).

``run_session`` speaks JSON-RPC 2.0, one JSON object per line, with a function
process: it sends the ``invoke`` notification, answers ``tool`` requests through
a ``FunctionBroker``, and returns when the function sends its ``result``, closes
its output, breaks the protocol, or the deadline passes. It never buffers more
than one message's worth of output: a line longer than the message limit is
discarded as it is read. Terminating the process is the runner's job; this
module only reports what happened.
"""

from __future__ import annotations

import json
import os
import select
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.functions.broker import (
    ERR_CONCURRENT_CALL,
    ERR_INVALID_ARGUMENTS,
    FunctionBroker,
    ToolCallRecord,
    error_response,
)

MAX_OUTPUT_BYTES = 256 * 1024

# Run-level codes (§11) this layer can produce.
CODE_FUNCTION_ERROR = "function_error"
CODE_NO_RESULT = "no_result"
CODE_PROTOCOL_ERROR = "protocol_error"
CODE_MEMORY_EXCEEDED = "memory_exceeded"
CODE_OUTPUT_TOO_LARGE = "output_too_large"
CODE_TIMEOUT = "timeout"

_READ_CHUNK = 64 * 1024
_MAX_ERROR_TEXT = 1000


@dataclass
class SessionOutcome:
    """How a session ended. ``code`` is None only for a successful result."""

    code: Optional[str]
    output: Any = None
    error_message: str = ""
    function_error_code: str = ""
    tool_calls: List[ToolCallRecord] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.code is None


class _Oversized:
    """A discarded line; ``looks_like_result`` when it named the result method."""

    def __init__(self, looks_like_result: bool):
        self.looks_like_result = looks_like_result


class _DeadlineExceeded(Exception):
    pass


# Markers of the result notification inside a line too long to parse. Only the
# function's own run is affected if it disguises one message as the other.
_RESULT_MARKERS = (b'"method":"result"', b'"method": "result"')
_MARKER_OVERLAP = max(len(m) for m in _RESULT_MARKERS)


def _wait(fd: int, event: int, timeout: float) -> bool:
    """poll() rather than select(): file descriptors may exceed FD_SETSIZE."""
    poller = select.poll()
    poller.register(fd, event)
    return bool(poller.poll(max(timeout, 0) * 1000))


class LineReader:
    """Bounded newline-delimited reader over a raw file descriptor."""

    def __init__(self, fd: int, max_line_bytes: int):
        self._fd = fd
        self._max = max_line_bytes
        self._buffer = b""
        self._discarding = False
        self._discard_tail = b""
        self._discard_is_result = False
        self._eof = False

    def read_line(self, deadline: float) -> Any:
        """Return the next line (bytes, without newline), an _Oversized or None at EOF."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
                if self._discarding:
                    self._scan_discarded(line)
                    return self._end_discard()
                if len(line) > self._max:
                    return _Oversized(_names_result(line))
                return line
            if len(self._buffer) > self._max:
                # Keep reading, but stop holding the bytes: memory stays bounded
                # whatever the function writes.
                if not self._discarding:
                    self._discarding = True
                    self._discard_tail = b""
                    self._discard_is_result = False
                self._scan_discarded(self._buffer)
                self._buffer = b""
            if self._eof:
                if self._discarding:
                    return self._end_discard()
                if self._buffer:
                    line, self._buffer = self._buffer, b""
                    return line
                return None
            self._fill(deadline)

    def has_pending(self) -> bool:
        """True when a non-blank message has already started arriving."""
        if (
            not self._buffer.strip()
            and not self._eof
            and _wait(self._fd, select.POLLIN, 0)
        ):
            self._read_chunk()
        return bool(self._buffer.strip())

    def _scan_discarded(self, data: bytes) -> None:
        window = self._discard_tail + data
        if _names_result(window):
            self._discard_is_result = True
        self._discard_tail = window[-_MARKER_OVERLAP:]

    def _end_discard(self) -> "_Oversized":
        result = _Oversized(self._discard_is_result)
        self._discarding = False
        self._discard_tail = b""
        self._discard_is_result = False
        return result

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _wait(self._fd, select.POLLIN, remaining):
            raise _DeadlineExceeded()
        self._read_chunk()

    def _read_chunk(self) -> None:
        chunk = os.read(self._fd, _READ_CHUNK)
        if not chunk:
            self._eof = True
        else:
            self._buffer += chunk


def _names_result(data: bytes) -> bool:
    return any(marker in data for marker in _RESULT_MARKERS)


_WRITTEN = "written"
_CLOSED = "closed"
_TIMED_OUT = "timed_out"


def _write_message(fd: int, message: Dict[str, Any], deadline: float) -> str:
    """Write one message without blocking past the deadline.

    ``fd`` must be non-blocking: a function that stops reading its input must
    not stall the host once the pipe buffer is full.
    """
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _TIMED_OUT
            if not _wait(fd, select.POLLOUT, remaining):
                return _TIMED_OUT
            continue
        except OSError:
            return _CLOSED
        view = view[written:]
    return _WRITTEN


def run_session(
    broker: FunctionBroker,
    envelope: Dict[str, Any],
    *,
    reader_fd: int,
    writer_fd: int,
    deadline: float,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> SessionOutcome:
    """Drive one run's protocol session; ``deadline`` is a time.monotonic() value."""
    reader = LineReader(reader_fd, broker.context.max_message_bytes)
    os.set_blocking(writer_fd, False)

    def outcome(code: Optional[str], **kwargs: Any) -> SessionOutcome:
        return SessionOutcome(code=code, tool_calls=list(broker.tool_calls), **kwargs)

    def write_failed(message: Dict[str, Any]) -> Optional[SessionOutcome]:
        status = _write_message(writer_fd, message, deadline)
        if status == _TIMED_OUT:
            return outcome(
                CODE_TIMEOUT, error_message="The wall-clock limit was reached."
            )
        if status == _CLOSED:
            return outcome(
                CODE_NO_RESULT, error_message="The process closed its input."
            )
        return None

    failed = write_failed({"jsonrpc": "2.0", "method": "invoke", "params": envelope})
    if failed:
        return failed

    concurrent_pending = False
    while True:
        try:
            line = reader.read_line(deadline)
        except _DeadlineExceeded:
            return outcome(
                CODE_TIMEOUT, error_message="The wall-clock limit was reached."
            )

        if line is None:
            return outcome(
                CODE_NO_RESULT, error_message="The process exited without a result."
            )
        if isinstance(line, _Oversized):
            if line.looks_like_result:
                # Any result over the message limit is over the output limit.
                return outcome(
                    CODE_OUTPUT_TOO_LARGE,
                    error_message="The result exceeds the message size limit.",
                )
            # The request id cannot be recovered from a discarded line.
            failed = write_failed(
                error_response(
                    None, ERR_INVALID_ARGUMENTS, "The message exceeds the size limit."
                )
            )
            if failed:
                return failed
            continue
        if not line.strip():
            continue

        try:
            message = json.loads(line, parse_constant=_reject_constant)
        except Exception:  # ValueError, UnicodeDecodeError, RecursionError
            return outcome(
                CODE_PROTOCOL_ERROR, error_message="Output was not valid JSON."
            )
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return outcome(
                CODE_PROTOCOL_ERROR, error_message="Not a JSON-RPC 2.0 message."
            )

        method = message.get("method")
        if method == "result" and "id" not in message:
            return _finish(message.get("params"), max_output_bytes, outcome)

        request_id = message.get("id")
        if method != "tool" or not _valid_id(request_id):
            return outcome(
                CODE_PROTOCOL_ERROR,
                error_message="Expected a tool request or the result notification.",
            )

        if time.monotonic() >= deadline:
            return outcome(
                CODE_TIMEOUT, error_message="The wall-clock limit was reached."
            )
        if concurrent_pending:
            response = broker.refuse(
                message,
                ERR_CONCURRENT_CALL,
                "A request was sent before the previous one was answered.",
            )
        else:
            response = broker.handle_request(message)
        # Input already waiting before this answer is written was sent while
        # this request was unanswered (§6.2). Detection is best-effort: the
        # broker answers strictly in order either way.
        concurrent_pending = reader.has_pending()
        failed = write_failed(response)
        if failed:
            return failed


_MAX_ID_CHARS = 128


def _valid_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) < 2**63
    return isinstance(value, str) and len(value) <= _MAX_ID_CHARS


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finish(params: Any, max_output_bytes: int, outcome: Any) -> SessionOutcome:
    if not isinstance(params, dict):
        return outcome(
            CODE_PROTOCOL_ERROR, error_message="result.params must be an object."
        )
    status = params.get("status")
    if status == "ok":
        output = params.get("output")
        if not isinstance(output, dict):
            return outcome(
                CODE_PROTOCOL_ERROR, error_message="result.output must be an object."
            )
        size = len(json.dumps(output, separators=(",", ":")).encode("utf-8"))
        if size > max_output_bytes:
            return outcome(
                CODE_OUTPUT_TOO_LARGE,
                error_message=f"result.output is {size} bytes; the limit is {max_output_bytes}.",
            )
        return outcome(None, output=output)
    if status == "error":
        error = params.get("error")
        if not isinstance(error, dict):
            return outcome(
                CODE_PROTOCOL_ERROR, error_message="result.error must be an object."
            )
        code = str(error.get("code") or "")[:100]
        message = str(error.get("message") or "")[:_MAX_ERROR_TEXT]
        if code == CODE_MEMORY_EXCEEDED:
            return outcome(CODE_MEMORY_EXCEEDED, error_message=message)
        return outcome(
            CODE_FUNCTION_ERROR, error_message=message, function_error_code=code
        )
    return outcome(
        CODE_PROTOCOL_ERROR, error_message="result.status must be ok or error."
    )
