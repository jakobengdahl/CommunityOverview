"""Runner path fixtures, lifecycle decisions and boundary bookkeeping."""

import json
import os
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from backend.functions import runner as runner_module
from backend.functions.protocol import SessionOutcome
from backend.functions.runner import HardenedSubprocessRunner, RunHandle, RunSpec
from backend.functions.tests.test_runner import execute, handler


@pytest.mark.parametrize("multiarch", [None, "fixture-linux-gnu"])
def test_runtime_paths_include_only_existing_runtime_files(
    monkeypatch, tmp_path, multiarch
):
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    for entry in ("os.py", "lib-dynload", "site-packages", "dist-packages"):
        (stdlib / entry).touch()
    runner = HardenedSubprocessRunner()
    runner._python = "/runtime/python"
    existing = {
        runner._python,
        "/etc/ld.so.cache",
        "/lib64",
        "/usr/lib64",
        "/lib/fixture-linux-gnu",
        "/usr/lib/fixture-linux-gnu",
        "/mapped/libpython.so",
        str(stdlib / "os.py"),
        str(stdlib / "lib-dynload"),
    }
    monkeypatch.setattr(runner_module, "_standard_library_dirs", lambda: [str(stdlib)])
    monkeypatch.setattr(runner_module.os.path, "exists", lambda path: path in existing)
    monkeypatch.setattr(
        runner_module.sysconfig, "get_config_var", lambda name: multiarch
    )
    mapped = Mock(return_value=["/mapped/libpython.so", "/missing/libgone.so"])
    monkeypatch.setattr(runner_module, "_mapped_libraries", mapped)
    expected = existing - {"/lib/fixture-linux-gnu", "/usr/lib/fixture-linux-gnu"}
    if multiarch:
        expected = existing
    assert runner._runtime_read_paths() == tuple(sorted(expected))
    assert runner._list_paths == {str(stdlib)}
    mapped.assert_called_once_with(runner._python)


def test_mapped_libraries_parse_paths_deduplicate_and_ignore_other_mappings(
    monkeypatch,
):
    maps = b"""00400000-00401000 r-xp 00000000 08:01 1 /runtime/python
7f000000-7f001000 r-xp 00000000 08:01 2 /lib/libz.so.1
7f001000-7f002000 r--p 00001000 08:01 2 /lib/libz.so.1
7f002000-7f003000 r-xp 00000000 08:01 3 /path with spaces/liba.so
7f003000-7f004000 rw-p 00000000 00:00 0 [heap]
7f004000-7f005000 rw-p 00000000 00:00 0
7f005000-7f006000 r--p 00000000 08:01 4 /data/plain-file
7f006000-7f007000 r-xp 00000000 00:00 0 relative.so
"""
    run = Mock(return_value=SimpleNamespace(stdout=maps))
    monkeypatch.setattr(runner_module.subprocess, "run", run)
    assert runner_module._mapped_libraries("/runtime/python") == [
        "/lib/libz.so.1",
        "/path with spaces/liba.so",
    ]
    run.assert_called_once_with(
        ["/runtime/python", "-I", "-S", "-c", "print(open('/proc/self/maps').read())"],
        capture_output=True,
        env={},
        timeout=30,
        check=True,
    )


@pytest.mark.parametrize(
    "error",
    [
        OSError("missing"),
        subprocess.TimeoutExpired("python", 30),
        subprocess.CalledProcessError(1, "python"),
    ],
)
def test_mapped_libraries_are_empty_when_the_interpreter_cannot_be_inspected(
    monkeypatch, error
):
    monkeypatch.setattr(runner_module.subprocess, "run", Mock(side_effect=error))
    assert runner_module._mapped_libraries("/runtime/python") == []


@pytest.fixture
def fake_handle(tmp_path):
    """Real owned descriptors and directories, with no process to signal."""
    handles, peers = [], []

    def make(messages=(), *, timeout=False, keep_status_open=False, raw_status=None):
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        status_r, status_w = os.pipe()
        peers.extend([stdin_r, stdout_w])
        payload = (
            raw_status
            if raw_status is not None
            else b"".join((json.dumps(m) + "\n").encode() for m in messages)
        )
        os.write(status_w, payload)
        if keep_status_open:
            peers.append(status_w)
        else:
            os.close(status_w)
        root = tmp_path / str(len(handles))
        root.mkdir()
        (root / "function.py").touch()
        process = Mock(pid=987654)
        process.wait.side_effect = (
            [subprocess.TimeoutExpired("supervisor", 0.5), 0] if timeout else None
        )
        reader = Mock()
        reader.finish.return_value = ("retained log", True)
        handle = RunHandle(
            process,
            stdin_fd=stdin_w,
            stdout_fd=stdout_r,
            status_fd=status_r,
            log_reader=reader,
            run_root=str(root),
            started=10,
            cpu_limit_seconds=5,
        )
        handles.append((handle, [stdin_w, stdout_r, status_r]))
        return handle

    yield make
    for _, fds in handles:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    for fd in peers:
        os.close(fd)


@pytest.mark.parametrize(
    "messages, timeout, outcome, detail",
    [
        (
            [{"started": 123}],
            False,
            "runner_lost",
            "The supervisor ended without a report.",
        ),
        (
            [{"started": 123}, {"exit_code": 0}],
            True,
            "runner_lost",
            "The supervisor did not stop in time.",
        ),
        (
            [{"setup_error": "denied"}, {"started": 123}, {"exit_code": 0}],
            False,
            "failed_to_start",
            "denied",
        ),
        (
            [
                {"started": 123},
                {
                    "exit_code": 7,
                    "signal": None,
                    "stopped": False,
                    "peak_memory_kib": 99,
                    "cpu_seconds": 0.25,
                },
            ],
            False,
            "exited",
            "",
        ),
        ([{"exit_code": None, "signal": 9, "stopped": True}], False, "killed", ""),
    ],
)
def test_wait_uses_reports_and_timeout_then_cleans_up_once(
    fake_handle, monkeypatch, messages, timeout, outcome, detail
):
    handle = fake_handle(messages, timeout=timeout)
    stdin_fd = handle.stdin_fd
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: 10.125)
    leftovers = Mock()
    monkeypatch.setattr(handle, "_kill_leftovers", leftovers)
    result = handle.wait(timeout=0.5)
    assert (result.outcome, result.detail, result.wall_ms) == (outcome, detail, 125)
    assert (result.log, result.log_truncated) == ("retained log", True)
    if outcome == "exited":
        assert (result.exit_code, result.signal, result.peak_memory_kib) == (
            7,
            None,
            99,
        )
    if outcome == "killed":
        assert (result.exit_code, result.signal) == (None, 9)
    if outcome == "runner_lost":
        leftovers.assert_called_once_with({"started": 123})
    else:
        leftovers.assert_not_called()
    assert handle._process.wait.call_args_list == (
        [call(timeout=0.5), call()] if timeout else [call(timeout=0.5)]
    )
    assert handle._process.kill.call_count == int(timeout)
    handle._log_reader.finish.assert_called_once_with(2.0)
    assert handle.stdin_fd == -1
    assert not os.path.exists(handle._run_root)
    for fd in (stdin_fd, handle.stdout_fd, handle._status_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
    calls = handle._process.mock_calls[:]
    assert handle.wait() is result
    handle.close_input()
    assert handle._process.mock_calls == calls
    handle._log_reader.finish.assert_called_once()


@pytest.mark.parametrize("keep_open", [False, True])
def test_read_status_ignores_invalid_lines_and_stops_at_eof_or_would_block(
    fake_handle, monkeypatch, keep_open
):
    handle = fake_handle(
        keep_status_open=keep_open,
        raw_status=b'not json\n[]\nnull\n42\n{"started": 123}\n{"exit_code": 0}\n',
    )
    read = os.read
    monkeypatch.setattr(
        runner_module.os, "read", lambda fd, size: read(fd, min(size, 7))
    )
    assert handle._read_status() == [{"started": 123}, {"exit_code": 0}]
    with pytest.raises(OSError):
        os.fstat(handle._status_fd)


@pytest.mark.parametrize(
    "started",
    [None, {}, {"started": "123"}, {"started": 0}, {"started": 1}, {"started": 123}],
)
@pytest.mark.parametrize("failure", [None, ProcessLookupError, PermissionError])
def test_kill_leftovers_targets_the_group_and_only_a_safe_child_pid(
    fake_handle, monkeypatch, started, failure
):
    handle = fake_handle()
    killpg, kill = Mock(side_effect=failure), Mock(side_effect=failure)
    monkeypatch.setattr(runner_module.os, "killpg", killpg)
    monkeypatch.setattr(runner_module.os, "kill", kill)
    handle._kill_leftovers(started)
    killpg.assert_called_once_with(handle._process.pid, signal.SIGKILL)
    assert kill.call_args_list == (
        [call(123, signal.SIGKILL)] if started == {"started": 123} else []
    )


@pytest.mark.parametrize(
    "chunks, truncated",
    [
        ([b"abcd"], False),
        ([b"abcde"], True),
        ([b"ab", b"cd"], False),
        ([b"ab", b"cd", b"e"], True),
    ],
)
def test_log_reader_keeps_exactly_the_limit_and_marks_only_excess_bytes(
    monkeypatch, chunks, truncated
):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    reader = runner_module._LogReader(read_fd, 4)
    # A readable EOF pipe drives polling; controlled reads force chunk boundaries.
    monkeypatch.setattr(runner_module.os, "read", Mock(side_effect=[*chunks, b""]))
    reader.start()
    assert reader.finish(2) == ("abcd", truncated)
    assert not reader.is_alive()
    assert reader._kept == 4
    with pytest.raises(OSError):
        os.fstat(read_fd)


@pytest.mark.parametrize(
    "code",
    [
        None,
        "function_error",
        "memory_exceeded",
        "output_too_large",
        "timeout",
        "no_result",
        "protocol_error",
    ],
)
@pytest.mark.parametrize("exits", [False, True])
def test_run_function_allows_grace_only_after_a_result(monkeypatch, code, exits):
    handle = Mock(stdin_fd=10, stdout_fd=11)
    handle.exited_within.return_value = exits
    runner = Mock()
    runner.start.return_value = handle
    outcome = SessionOutcome(code=code)
    session = Mock(return_value=outcome)
    monkeypatch.setattr(runner_module, "run_session", session)
    result_for = Mock()
    monkeypatch.setattr(runner_module, "_result_for", result_for)
    spec = RunSpec("r", "python", "main.py", {"main.py": b""})
    broker, envelope = object(), {"schema_version": "1.0"}
    assert (
        runner_module.run_function(runner, spec, broker, envelope)
        is result_for.return_value
    )
    runner.start.assert_called_once_with(spec)
    sent_result = code not in ("timeout", "no_result", "protocol_error")
    expected = [call.close_input()]
    if sent_result:
        expected.append(call.exited_within(runner_module.EXIT_GRACE_SECONDS))
    if not (sent_result and exits):
        expected.append(call.kill())
    assert handle.mock_calls == [*expected, call.wait()]
    result_for.assert_called_once_with(outcome, handle.wait.return_value, broker)


def test_run_function_still_kills_and_waits_when_the_session_raises(monkeypatch):
    handle = Mock()
    runner = Mock()
    runner.start.return_value = handle
    monkeypatch.setattr(
        runner_module, "run_session", Mock(side_effect=RuntimeError("session failed"))
    )
    with pytest.raises(RuntimeError, match="session failed"):
        runner_module.run_function(
            runner, RunSpec("r", "python", "main.py", {}), object(), {}
        )
    assert handle.mock_calls == [call.close_input(), call.kill(), call.wait()]


def test_child_has_no_new_privileges_and_isolated_interpreter_flags(storage):
    # /proc/self/status is deliberately outside the filesystem allowlist.
    runner = HardenedSubprocessRunner(use_landlock=False)
    result = execute(
        runner,
        storage,
        handler("""
        with open("/proc/self/status") as status:
            out["no_new_privs"] = next(line.split()[1] for line in status if line.startswith("NoNewPrivs:"))
        out["flags"] = [sys.flags.isolated, sys.flags.no_site, sys.flags.dont_write_bytecode]
    """),
    )
    assert result.status == "succeeded", result
    assert result.output == {"no_new_privs": "1", "flags": [1, 1, 1]}
