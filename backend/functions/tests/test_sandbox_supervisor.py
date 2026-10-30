"""Landlock ABI rules and the real supervisor's status protocol."""

import ctypes
import io
import json
import os
import signal
import subprocess
import sys

import pytest

from backend.functions import sandbox


def test_disk_scan_counts_nested_entries_without_following_symlinks(tmp_path):
    work = tmp_path / "work"
    nested = work / "nested"
    nested.mkdir(parents=True)
    (nested / "file").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 128 * 1024)
    (work / "link").symlink_to(outside, target_is_directory=True)
    spec = {"work_dir": str(work), "disk_limits": {"bytes": 64 * 1024, "files": 3}}
    assert sandbox._work_dir_exceeds_quota(spec) is False
    spec["disk_limits"]["files"] = 2
    assert sandbox._work_dir_exceeds_quota(spec) is True


@pytest.mark.parametrize("sparse", [False, True])
def test_disk_scan_counts_both_allocated_and_apparent_bytes(tmp_path, sparse):
    target = tmp_path / "file"
    with target.open("wb") as stream:
        if sparse:
            stream.truncate(1024 * 1024)
        else:
            stream.write(b"x")
    info = target.stat()
    usage = max(info.st_size, info.st_blocks * 512)
    spec = {"work_dir": str(tmp_path), "disk_limits": {"bytes": usage, "files": 1}}
    assert sandbox._work_dir_exceeds_quota(spec) is False
    spec["disk_limits"]["bytes"] -= 1
    assert sandbox._work_dir_exceeds_quota(spec) is True


def test_unlinked_descriptor_accounting_deduplicates_inodes(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import os, sys
memfd = os.memfd_create('quota-regression')
os.ftruncate(memfd, 1024 * 1024)
outside = open(sys.argv[2], 'w+b', buffering=0)
os.unlink(sys.argv[2])
outside.truncate(1024 * 1024)
f = open(sys.argv[1], 'w+b', buffering=0)
os.unlink(sys.argv[1])
f.truncate(8192)
duplicate = os.dup(f.fileno())
print('ready', flush=True)
sys.stdin.read()
""",
            str(work / "deleted"),
            str(tmp_path / "work-outside"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        monkeypatch.setattr(
            sandbox,
            "_children_of",
            lambda pid: [child.pid] if pid == os.getpid() else [],
        )
        spec = {"work_dir": str(work), "disk_limits": {"bytes": 8192, "files": 1}}
        assert sandbox._work_dir_exceeds_quota(spec) is False
        (work / "entry").touch()
        assert sandbox._work_dir_exceeds_quota(spec) is False
        spec["disk_limits"]["bytes"] -= 1
        assert sandbox._work_dir_exceeds_quota(spec) is True
    finally:
        child.communicate(timeout=5)


@pytest.mark.parametrize(
    "abi, filesystem, network, scope, work_rights",
    [
        (1, 0x1FFF, 0, 0, 0x01BE),
        (2, 0x3FFF, 0, 0, 0x21BE),
        (3, 0x7FFF, 0, 0, 0x61BE),
        (4, 0x7FFF, 3, 0, 0x61BE),
        (5, 0xFFFF, 3, 0, 0x61BE),
        (6, 0xFFFF, 3, 3, 0x61BE),
        (7, 0xFFFF, 3, 3, 0x61BE),
    ],
)
@pytest.mark.parametrize("work_is_file", [False, True])
def test_landlock_passes_only_abi_and_path_appropriate_rights_to_libc(
    monkeypatch, tmp_path, abi, filesystem, network, scope, work_rights, work_is_file
):
    read_dir = tmp_path / "runtime"
    read_dir.mkdir()
    read_file = read_dir / "python"
    read_file.touch()
    list_dir = tmp_path / "stdlib"
    list_dir.mkdir()
    work = tmp_path / "work"
    work.touch() if work_is_file else work.mkdir()
    calls, descriptors = [], []

    class RecordingLibc:
        def syscall(self, number, *args):
            if number == sandbox.SYS_LANDLOCK_CREATE_RULESET:
                attr = ctypes.cast(
                    args[0], ctypes.POINTER(sandbox._RulesetAttr)
                ).contents
                calls.append(
                    (
                        "create",
                        attr.handled_access_fs,
                        attr.handled_access_net,
                        attr.scoped,
                    )
                )
                assert args[1:] == (ctypes.sizeof(sandbox._RulesetAttr), 0)
                self.ruleset = os.open(os.devnull, os.O_RDONLY)
                descriptors.append(self.ruleset)
                return self.ruleset
            if number == sandbox.SYS_LANDLOCK_ADD_RULE:
                assert args[:2] == (self.ruleset, sandbox.LANDLOCK_RULE_PATH_BENEATH)
                assert args[3] == 0
                rule = ctypes.cast(
                    args[2], ctypes.POINTER(sandbox._PathBeneath)
                ).contents
                descriptors.append(rule.parent_fd)
                calls.append(
                    (
                        "rule",
                        os.readlink(f"/proc/self/fd/{rule.parent_fd}"),
                        rule.allowed_access,
                    )
                )
                return 0
            assert number == sandbox.SYS_LANDLOCK_RESTRICT_SELF
            assert args == (self.ruleset, 0)
            calls.append(("restrict",))
            return 0

    monkeypatch.setattr(sandbox, "_libc", RecordingLibc())
    sandbox.apply_landlock(
        abi,
        [str(read_dir), str(read_file), str(tmp_path / "missing")],
        str(work),
        [str(list_dir)],
    )
    assert calls == [
        ("create", filesystem, network, scope),
        ("rule", str(read_dir), 0xD),
        ("rule", str(read_file), 0x5),
        ("rule", str(list_dir), 0x8),
        ("rule", str(work), work_rights & 0xC007 if work_is_file else work_rights),
        ("restrict",),
    ]
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


# Only the child and usage counters are stubbed. Forking, setup pipes, signal
# delivery, wait statuses, reaping and JSON serialization run in a subprocess.
_SUPERVISOR_SCRIPT = """
import importlib.util, os, signal, sys
from types import SimpleNamespace
loader = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
sandbox = importlib.util.module_from_spec(loader)
loader.loader.exec_module(sandbox)
mode, case, status_fd = sys.argv[2], sys.argv[3], int(sys.argv[4])

def usage(who):
    assert who == sandbox.resource.RUSAGE_CHILDREN
    return SimpleNamespace(ru_maxrss=12345, ru_utime=1.2345, ru_stime=2.3456)
sandbox.resource.getrusage = usage

def child(spec, setup_w):
    sandbox.libc().prctl(sandbox.PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    with open(sys.argv[5], "w") as marker:
        marker.write(str(os.getpid()))
    if case == "setup":
        os.write(setup_w, b"SetupError: stub setup failed")
        os._exit(125)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    os.close(setup_w)
    if case in ("signal", "signal_then_stop"):
        os.kill(os.getpid(), signal.SIGUSR1)
    if case == "stop":
        os.kill(spec["supervisor_pid"], signal.SIGTERM)
    if case in ("stop", "monitor_error"):
        while True:
            signal.pause()
    os._exit(17)
sandbox._child = child

if case == "monitor_error":
    def unreadable(spec):
        raise PermissionError("usage unavailable")
    sandbox._work_dir_exceeds_quota = unreadable

# Force the host's EOF-triggered stop into the gap between reaping the function
# and starting cleanup. The function's exit status must still be reported.
if case in ("exit_then_stop", "signal_then_stop"):
    reap_all = sandbox._reap_all
    def stop_before_cleanup():
        os.kill(os.getpid(), signal.SIGTERM)
        reap_all()
    sandbox._reap_all = stop_before_cleanup

def confine(spec):
    if case == "setup":
        raise sandbox.SetupError("stub setup failed")
    if case == "nonzero":
        os._exit(7)
    if case == "signal":
        os.kill(os.getpid(), signal.SIGKILL)
sandbox.confine = confine

if mode == "supervise":
    sandbox.supervise({"host_pid": os.getppid()}, status_fd)
else:
    sandbox.probe({}, status_fd)
os.close(status_fd)
"""


@pytest.fixture
def supervisor_report(tmp_path):
    def collect(mode, case):
        child_marker = tmp_path / "child.pid"
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stream:
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        _SUPERVISOR_SCRIPT,
                        sandbox.__file__,
                        mode,
                        case,
                        str(write_fd),
                        str(child_marker),
                    ],
                    pass_fds=(write_fd,),
                    capture_output=True,
                    timeout=10,
                )
                assert completed.returncode == 0, completed.stderr
            finally:
                os.close(write_fd)
            report = stream.read()
        child_pid = int(child_marker.read_text()) if child_marker.exists() else None
        return report, child_pid

    return collect


@pytest.mark.parametrize("case", ["exit", "signal", "stop", "setup"])
def test_supervisor_reports_exact_start_setup_and_exit_messages(
    supervisor_report, case
):
    report, child_pid = supervisor_report("supervise", case)
    if case == "setup":
        expected = [{"setup_error": "SetupError: stub setup failed"}]
    else:
        assert child_pid > 1
        expected = [
            {"started": child_pid},
            {
                "exit_code": 17 if case == "exit" else None,
                "signal": int(signal.SIGUSR1) if case == "signal" else None,
                "stopped": case == "stop",
                "peak_memory_kib": 12345,
                "cpu_seconds": 3.58,
            },
        ]
    assert report == b"".join((json.dumps(m) + "\n").encode() for m in expected)


@pytest.mark.parametrize("case", ["exit", "signal"])
def test_stop_after_child_exit_does_not_lose_the_final_report(supervisor_report, case):
    report, child_pid = supervisor_report("supervise", case + "_then_stop")
    messages = [json.loads(line) for line in report.splitlines()]
    assert messages == [
        {"started": child_pid},
        {
            "exit_code": 17 if case == "exit" else None,
            "signal": int(signal.SIGUSR1) if case == "signal" else None,
            "stopped": False,
            "peak_memory_kib": 12345,
            "cpu_seconds": 3.58,
        },
    ]


def test_supervisor_terminates_and_reaps_the_child_when_usage_cannot_be_checked(
    supervisor_report,
):
    report, child_pid = supervisor_report("supervise", "monitor_error")
    final = json.loads(report.splitlines()[-1])
    assert final["stopped"] is True
    assert final["monitor_error"] == "usage unavailable"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.parametrize("case", ["exit", "setup", "nonzero", "signal"])
def test_probe_succeeds_only_for_a_normal_zero_exit(supervisor_report, case):
    report, _ = supervisor_report("probe", case)
    assert report == (
        b'{"probe_ok": true}\n' if case == "exit" else b'{"probe_ok": false}\n'
    )


def test_children_of_parses_the_last_parenthesis_and_ignores_disappearing_pids(
    monkeypatch,
):
    stats = {
        "101": b"101 (a name ) with ( parentheses)) S 42 0 0",
        "102": b"102 (other) S 7 42 0",
        "103": b"103 (short)",
        "104": b"104 (child) Z 42 0 0",
    }
    monkeypatch.setattr(sandbox.os, "listdir", lambda path: [*stats, "105", "self"])

    def read_stat(path, mode):
        assert mode == "rb"
        assert path.startswith("/proc/") and path.endswith("/stat")
        pid = path.split("/")[2]
        if pid == "105":
            raise FileNotFoundError(path)
        return io.BytesIO(stats[pid])

    monkeypatch.setattr("builtins.open", read_stat)
    assert sandbox._children_of(42) == [101, 104]


@pytest.mark.parametrize("transfer", ["write", "writev", "splice"])
def test_blocking_pipe_output_does_not_prevent_reader_closing_duplicate(
    tmp_path, transfer
):
    program = """
import os, sys, threading, time
read_fd, write_fd = os.pipe()
duplicate = os.dup(read_fd)
size = 1024 * 1024

def reader():
    time.sleep(0.2)  # Let the writer fill the pipe and block first.
    os.close(duplicate)
    received = 0
    while received < size:
        received += len(os.read(read_fd, 65536))
    os.close(read_fd)

thread = threading.Thread(target=reader)
thread.start()
if sys.argv[1] == 'splice':
    with open('source', 'w+b', buffering=0) as source:
        source.write(b'x' * size)
        source.seek(0)
        remaining = size
        while remaining:
            remaining -= os.splice(source.fileno(), write_fd, remaining)
else:
    payload = b'x' * size
    assert (os.write(write_fd, payload) if sys.argv[1] == 'write'
            else os.writev(write_fd, [payload])) == size
thread.join()
os.close(write_fd)
print('complete', flush=True)
"""
    driver = """
import os, sys
from backend.functions import sandbox
status_fd = os.dup(2)  # Standard streams are redirected by the supervisor.
sandbox.confine = lambda spec: None
sandbox.supervise({
    'host_pid': os.getppid(), 'work_dir': sys.argv[1],
    'argv': [sys.executable, '-c', sys.argv[2], sys.argv[3]],
    'disk_limits': {'bytes': 4 * 1024 * 1024, 'files': 10},
}, status_fd)
os.close(status_fd)
"""
    completed = subprocess.run(
        [sys.executable, "-c", driver, str(tmp_path), program, transfer],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "complete\n"
    final = json.loads(completed.stderr.splitlines()[-1])
    assert final["exit_code"] == 0
    assert final["stopped"] is False
    assert "disk_exceeded" not in final
    assert "monitor_error" not in final
