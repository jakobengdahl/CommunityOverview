"""Invariants of the broker protocol session (USER_FUNCTION_CONTRACT §6.1–§6.2, §8)."""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from backend.functions.broker import (
    GRANT_GRAPH_READ,
    GRANT_GRAPH_WRITE,
    BrokerContext,
    FunctionBroker,
)
from backend.functions.protocol import LineReader, _Oversized, run_session
from backend.service import GraphService

ENVELOPE = {"schema_version": "1.0", "invocation": {"input": {"q": "Alpha"}}}


def make_broker(storage, **overrides):
    context = BrokerContext(
        run_id="job-proto",
        function_id="proto",
        function_version="0.1.0",
        declared_grants=frozenset({GRANT_GRAPH_READ, GRANT_GRAPH_WRITE}),
        correlation_id="corr-proto",
        authorization_inputs={"actor_id": "tester"},
        grant_enabled=lambda grant: True,
        **overrides,
    )
    return FunctionBroker(GraphService(storage), context)


def run_script(broker, script, *, timeout=10.0, envelope=ENVELOPE, **session_kwargs):
    """Run ``script`` as a real function process and drive its session."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
    )
    try:
        return run_session(
            broker,
            envelope,
            reader_fd=process.stdout.fileno(),
            writer_fd=process.stdin.fileno(),
            deadline=time.monotonic() + timeout,
            **session_kwargs,
        )
    finally:
        process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


PRELUDE = """
import json, sys
def send(msg):
    sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()
def recv():
    return json.loads(sys.stdin.readline())
invoke = recv()
"""


def test_a_function_reads_the_envelope_calls_a_tool_and_returns_its_output(storage):
    broker = make_broker(storage)
    script = (
        PRELUDE
        + """
assert invoke["method"] == "invoke" and "id" not in invoke
query = invoke["params"]["invocation"]["input"]["q"]
send({"jsonrpc": "2.0", "id": 1, "method": "tool",
      "params": {"name": "graph.add",
                 "arguments": {"nodes": [{"name": query, "type": "Actor"}]}}})
added = recv()
send({"jsonrpc": "2.0", "id": 2, "method": "tool",
      "params": {"name": "graph.search", "arguments": {"query": query}}})
found = recv()
names = [n["name"] for n in found["result"]["nodes"]]
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"found": names, "added": added["id"]}}})
"""
    )
    outcome = run_script(broker, script)
    assert outcome.succeeded, outcome
    assert outcome.output == {"found": ["Alpha"], "added": 1}
    assert [r.outcome for r in outcome.tool_calls] == ["ok", "ok"]


def test_exiting_without_a_result_is_no_result(storage):
    outcome = run_script(make_broker(storage), PRELUDE)
    assert outcome.code == "no_result"


def test_output_that_is_not_json_is_a_protocol_error(storage):
    outcome = run_script(make_broker(storage), PRELUDE + "print('hello')\n")
    assert outcome.code == "protocol_error"


def test_a_request_with_an_unknown_method_is_a_protocol_error(storage):
    script = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "id": 1, "method": "shell", "params": {}})
"""
    )
    assert run_script(make_broker(storage), script).code == "protocol_error"


def test_the_deadline_ends_a_silent_session(storage):
    started = time.monotonic()
    outcome = run_script(
        make_broker(storage), PRELUDE + "import time; time.sleep(30)\n", timeout=0.5
    )
    assert outcome.code == "timeout"
    assert time.monotonic() - started < 5


def test_function_errors_keep_their_code_and_memory_errors_map_to_memory_exceeded(
    storage,
):
    error = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "error", "error": {"code": "bad_input", "message": "no"}}})
"""
    )
    outcome = run_script(make_broker(storage), error)
    assert (outcome.code, outcome.function_error_code) == (
        "function_error",
        "bad_input",
    )

    memory = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "error", "error": {"code": "memory_exceeded", "message": ""}}})
"""
    )
    assert run_script(make_broker(storage), memory).code == "memory_exceeded"


def test_an_output_over_the_limit_is_output_too_large(storage):
    script = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"blob": "x" * 5000}}})
"""
    )
    outcome = run_script(make_broker(storage), script, max_output_bytes=1000)
    assert outcome.code == "output_too_large"


def test_an_oversized_message_is_refused_and_the_session_continues(storage):
    broker = make_broker(storage, max_message_bytes=4096)
    script = (
        PRELUDE
        + """
sys.stdout.write("x" * 50000 + "\\n"); sys.stdout.flush()
refusal = recv()
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"refusal": refusal}}})
"""
    )
    outcome = run_script(broker, script)
    assert outcome.succeeded, outcome
    refusal = outcome.output["refusal"]
    assert refusal["id"] is None
    assert refusal["error"]["data"]["code"] == "invalid_arguments"


def test_a_request_sent_before_the_previous_answer_gets_concurrent_call(storage):
    broker = make_broker(storage)
    script = (
        PRELUDE
        + """
req = lambda i: {"jsonrpc": "2.0", "id": i, "method": "tool",
                 "params": {"name": "graph.search", "arguments": {"query": "z"}}}
sys.stdout.write(json.dumps(req(1)) + "\\n" + json.dumps(req(2)) + "\\n")
sys.stdout.flush()
first, second = recv(), recv()
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"first": first, "second": second}}})
"""
    )
    outcome = run_script(broker, script)
    assert outcome.succeeded, outcome
    assert "result" in outcome.output["first"]
    assert outcome.output["second"]["error"]["data"]["code"] == "concurrent_call"


def test_the_line_reader_never_buffers_more_than_one_message(storage):
    read_fd, write_fd = os.pipe()
    limit = 1024

    def writer():
        with os.fdopen(write_fd, "wb") as out:
            for _ in range(200):
                out.write(b"y" * 16384)  # 3.2 MB without a newline
            out.write(b'\n{"ok": true}\n')

    thread = threading.Thread(target=writer)
    thread.start()
    reader = LineReader(read_fd, limit)
    peak = 0
    original_fill = reader._fill

    def tracking_fill(deadline):
        nonlocal peak
        original_fill(deadline)
        peak = max(peak, len(reader._buffer))

    reader._fill = tracking_fill
    deadline = time.monotonic() + 10
    assert isinstance(reader.read_line(deadline), _Oversized)
    assert json.loads(reader.read_line(deadline)) == {"ok": True}
    assert reader.read_line(deadline) is None
    thread.join()
    os.close(read_fd)
    # One read chunk on top of the line limit at most, never the 3.2 MB sent.
    assert peak <= limit + 64 * 1024


@pytest.mark.parametrize("bad_id", [None, True, 1.5, {"x": 1}])
def test_requests_need_a_string_or_integer_id(storage, bad_id):
    script = (
        PRELUDE
        + f"""
send({{"jsonrpc": "2.0", "id": {bad_id!r}, "method": "tool",
      "params": {{"name": "graph.search", "arguments": {{"query": "a"}}}}}})
"""
    )
    assert run_script(make_broker(storage), script).code == "protocol_error"


def test_a_function_that_stops_reading_cannot_hold_the_host_past_the_deadline(storage):
    # The envelope is larger than a pipe buffer and the function never reads it,
    # so the host's write would block forever without the deadline.
    envelope = {"invocation": {"input": {"blob": "x" * (1024 * 1024)}}}
    started = time.monotonic()
    outcome = run_script(
        make_broker(storage),
        "import time; time.sleep(30)\n",
        timeout=0.5,
        envelope=envelope,
    )
    assert outcome.code == "timeout"
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "expression",
    [
        '"[" * 100_000 + "]" * 100_000',
        """'{"jsonrpc": "2.0", "method": "result", '"""
        """ '"params": {"status": "ok", "output": {"v": NaN}}}'""",
    ],
)
def test_pathological_json_ends_the_run_as_a_protocol_error(storage, expression):
    script = PRELUDE + f"sys.stdout.write({expression} + '\\n'); sys.stdout.flush()\n"
    assert run_script(make_broker(storage), script).code == "protocol_error"


def test_a_request_id_longer_than_the_limit_is_a_protocol_error(storage):
    script = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "id": "i" * 200, "method": "tool",
      "params": {"name": "graph.search", "arguments": {"query": "a"}}})
"""
    )
    assert run_script(make_broker(storage), script).code == "protocol_error"


def test_a_result_over_the_message_limit_is_output_too_large(storage):
    broker = make_broker(storage, max_message_bytes=4096)
    script = (
        PRELUDE
        + """
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"blob": "x" * 50000}}})
"""
    )
    assert run_script(broker, script).code == "output_too_large"


def test_a_blank_line_after_a_request_is_not_a_concurrent_request(storage):
    script = (
        PRELUDE
        + """
req = lambda i: {"jsonrpc": "2.0", "id": i, "method": "tool",
                 "params": {"name": "graph.search", "arguments": {"query": "z"}}}
answers = []
for i in (1, 2):
    sys.stdout.write(json.dumps(req(i)) + "\\n\\n"); sys.stdout.flush()
    answers.append(recv())
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"answers": answers}}})
"""
    )
    outcome = run_script(make_broker(storage), script)
    assert outcome.succeeded, outcome
    assert all("result" in answer for answer in outcome.output["answers"])


def test_file_descriptors_above_fd_setsize_are_supported():
    read_fd, write_fd = os.pipe()
    high_read, high_write = 1500, 1501
    try:
        os.dup2(read_fd, high_read)
        os.dup2(write_fd, high_write)
    except OSError:
        pytest.skip("cannot open descriptors above FD_SETSIZE here")
    finally:
        os.close(read_fd)
        os.close(write_fd)
    try:
        os.write(high_write, b'{"ok": 1}\n')
        reader = LineReader(high_read, 1024)
        assert json.loads(reader.read_line(time.monotonic() + 5)) == {"ok": 1}
        assert reader.has_pending() is False
    finally:
        os.close(high_read)
        os.close(high_write)


def _result_script(params_literal):
    return (
        PRELUDE
        + f"""
send({{"jsonrpc": "2.0", "method": "result", "params": {params_literal}}})
"""
    )


@pytest.mark.parametrize(
    "message",
    [
        '{"method": "result", "params": {"status": "ok", "output": {}}}',
        '{"jsonrpc": "1.0", "method": "result", '
        '"params": {"status": "ok", "output": {}}}',
        '{"jsonrpc": "2.0", "id": 1, "method": "result", '
        '"params": {"status": "ok", "output": {}}}',
    ],
)
def test_messages_outside_the_sequence_are_protocol_errors(storage, message):
    script = PRELUDE + f"sys.stdout.write({message!r} + '\\n'); sys.stdout.flush()\n"
    assert run_script(make_broker(storage), script).code == "protocol_error"


@pytest.mark.parametrize(
    "params",
    [
        "[1]",
        '{"status": "ok", "output": [1]}',
        '{"status": "ok", "output": "text"}',
        '{"status": "error", "error": "text"}',
        '{"status": "weird", "output": {}}',
        '{"output": {}}',
    ],
)
def test_malformed_results_are_protocol_errors(storage, params):
    outcome = run_script(make_broker(storage), _result_script(params))
    assert outcome.code == "protocol_error"


def test_a_final_result_without_a_trailing_newline_is_accepted(storage):
    script = (
        PRELUDE
        + """
sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "result",
                             "params": {"status": "ok", "output": {"v": 1}}}))
sys.stdout.flush()
"""
    )
    outcome = run_script(make_broker(storage), script)
    assert outcome.succeeded and outcome.output == {"v": 1}


def test_the_output_limit_is_measured_in_bytes_and_is_inclusive(storage):
    exact = len(json.dumps({"t": "a" * 100}, separators=(",", ":")).encode())
    ok = run_script(
        make_broker(storage),
        _result_script('{"status": "ok", "output": {"t": "a" * 100}}'),
        max_output_bytes=exact,
    )
    assert ok.succeeded, ok
    over = run_script(
        make_broker(storage),
        _result_script('{"status": "ok", "output": {"t": "\\u00e9" * 60}}'),
        max_output_bytes=100,
    )
    assert over.code == "output_too_large"


def test_function_error_code_and_message_are_bounded(storage):
    outcome = run_script(
        make_broker(storage),
        _result_script(
            '{"status": "error", "error": {"code": "c" * 5000, "message": "m" * 5000}}'
        ),
    )
    assert outcome.code == "function_error"
    assert len(outcome.function_error_code) <= 100
    assert len(outcome.error_message) <= 1000


def test_a_line_of_exactly_the_limit_is_accepted_and_oversize_at_eof_is_reported():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"a" * 64 + b"\n" + b"b" * 200)
    os.close(write_fd)
    reader = LineReader(read_fd, 64)
    deadline = time.monotonic() + 5
    assert reader.read_line(deadline) == b"a" * 64
    assert isinstance(reader.read_line(deadline), _Oversized)
    assert reader.read_line(deadline) is None
    os.close(read_fd)


def test_a_function_that_closes_its_input_ends_the_run_as_no_result(storage):
    # Before the invoke is written.
    to_fn_read, to_fn_write = os.pipe()
    from_fn_read, from_fn_write = os.pipe()
    os.close(to_fn_read)
    outcome = run_session(
        make_broker(storage),
        ENVELOPE,
        reader_fd=from_fn_read,
        writer_fd=to_fn_write,
        deadline=time.monotonic() + 5,
    )
    assert outcome.code == "no_result"
    for fd in (to_fn_write, from_fn_read, from_fn_write):
        os.close(fd)

    # Before a response is written.
    to_fn_read, to_fn_write = os.pipe()
    from_fn_read, from_fn_write = os.pipe()

    def function():
        os.read(to_fn_read, 1024 * 1024)
        os.close(to_fn_read)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tool",
            "params": {"name": "graph.search", "arguments": {"query": "x"}},
        }
        os.write(from_fn_write, (json.dumps(request) + "\n").encode())

    thread = threading.Thread(target=function)
    thread.start()
    outcome = run_session(
        make_broker(storage),
        ENVELOPE,
        reader_fd=from_fn_read,
        writer_fd=to_fn_write,
        deadline=time.monotonic() + 5,
    )
    thread.join()
    assert outcome.code == "no_result"
    for fd in (to_fn_write, from_fn_read, from_fn_write):
        os.close(fd)


def test_a_concurrent_refusal_does_not_stick_to_later_sequential_requests(storage):
    script = (
        PRELUDE
        + """
req = lambda i: {"jsonrpc": "2.0", "id": i, "method": "tool",
                 "params": {"name": "graph.search", "arguments": {"query": "z"}}}
sys.stdout.write(json.dumps(req(1)) + "\\n" + json.dumps(req(2)) + "\\n")
sys.stdout.flush()
recv(); recv()
send(req(3))
third = recv()
send({"jsonrpc": "2.0", "method": "result",
      "params": {"status": "ok", "output": {"third": third}}})
"""
    )
    outcome = run_script(make_broker(storage), script)
    assert outcome.succeeded, outcome
    assert "result" in outcome.output["third"]
