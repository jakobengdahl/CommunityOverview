"""Runner invariants beyond a single run: failed starts, a lost supervisor,
the system-call tables, Landlock rights, limits and cleanup (ADR 0012)."""

import errno
import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from backend.functions import runner as runner_module
from backend.functions import sandbox
from backend.functions.runner import (
    CONTROLS,
    HardenedSubprocessRunner,
    RunnerUnavailable,
    RunSpec,
    _controls_for,
    run_function,
    safe_relative_path,
)
from backend.functions.tests.test_runner import (
    ATTEMPT,
    ENVELOPE,
    execute,
    handler,
    make_broker,
    needs,
)

RESULT_LINE = (
    'sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "result", '
    '"params": {"status": "ok", "output": OUTPUT}}) + "\\n"); sys.stdout.flush()'
)


@pytest.fixture(scope="module")
def runner():
    return HardenedSubprocessRunner()


@pytest.fixture(scope="module")
def degraded_runner():
    return HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)


def spec(source, **fields):
    fields.setdefault("timeout_seconds", 20)
    return RunSpec(
        run_id="job-controls",
        runtime="python",
        entry_point="main.py",
        files={"main.py": textwrap.dedent(source).encode()},
        **fields,
    )


def _dead(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().split(b")")[-1].split()[0] == b"Z"
    except OSError:
        return True


def _open_fds():
    return set(os.listdir("/proc/self/fd"))


def _run_dirs(prefix):
    return set(glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*")))


@pytest.fixture
def private_tmp(monkeypatch, tmp_path):
    """Run directories go to a directory no concurrent run shares."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


# --- a start that cannot be confined is not a run ---------------------------------


def test_a_control_that_cannot_be_applied_aborts_the_start(
    runner, storage, monkeypatch
):
    runner.probe()
    limits = runner._limits(5, 64)
    monkeypatch.setattr(
        runner, "_limits", lambda *_: dict(limits, file_size_mib="not a number")
    )
    result = execute(runner, storage, "raise SystemExit('must never run')\n")
    assert (result.status, result.code) == ("failed", "runner_lost")
    assert "must never run" not in result.log


class _FakeLibc:
    def __init__(self, prctl=0, syscall=3):
        self._prctl = prctl
        self._syscall = syscall

    def prctl(self, *args):
        return self._prctl

    def syscall(self, number, *args):
        rc = self._syscall(number) if callable(self._syscall) else self._syscall
        if number == sandbox.SYS_LANDLOCK_CREATE_RULESET and rc >= 0:
            # A real descriptor: confine() closes the ruleset it gets.
            return os.open(os.devnull, os.O_RDONLY)
        return rc


@pytest.fixture
def fake_confine(monkeypatch):
    """confine() against a fake C library: nothing is applied to the tests."""
    monkeypatch.setattr(sandbox, "apply_rlimits", lambda limits: None)

    def install(**libc):
        monkeypatch.setattr(sandbox, "_libc", _FakeLibc(**libc))

    return install


def _confine_spec(**fields):
    return dict(
        {"limits": {}, "landlock_abi": 6, "seccomp": True, "read_paths": ["/"]},
        work_dir=tempfile.gettempdir(),
        **fields,
    )


@pytest.mark.parametrize(
    "failing",
    [
        sandbox.SYS_LANDLOCK_CREATE_RULESET,
        sandbox.SYS_LANDLOCK_ADD_RULE,
        sandbox.SYS_LANDLOCK_RESTRICT_SELF,
    ],
)
def test_each_failing_landlock_step_is_a_setup_error(fake_confine, failing):
    fake_confine(syscall=lambda number: -1 if number == failing else 3)
    with pytest.raises(sandbox.SetupError):
        sandbox.confine(_confine_spec(seccomp=False))


def test_a_failing_no_new_privs_or_seccomp_is_a_setup_error(fake_confine):
    fake_confine(prctl=-1)
    with pytest.raises(sandbox.SetupError):
        sandbox.confine(_confine_spec(landlock_abi=0))


def test_seccomp_without_a_system_call_table_is_a_setup_error(
    fake_confine, monkeypatch
):
    fake_confine()
    monkeypatch.setattr(sandbox, "syscall_table", lambda machine=None: None)
    with pytest.raises(sandbox.SetupError):
        sandbox.confine(_confine_spec(landlock_abi=0))


# --- the probe reports what holds -----------------------------------------------


@pytest.mark.parametrize(
    "abi, seccomp, held",
    [
        (0, False, ()),
        (2, True, ("network", "processes")),
        (3, False, ("filesystem",)),
        (5, True, ("filesystem", "network", "processes")),
        (6, False, ("filesystem",)),
        (6, True, CONTROLS),
        (7, True, CONTROLS),
    ],
)
def test_each_control_is_claimed_only_with_what_enforces_it(abi, seccomp, held):
    assert _controls_for(abi, seccomp) == held


def test_a_host_that_confines_nothing_reports_every_control_missing(monkeypatch):
    runner = HardenedSubprocessRunner()
    monkeypatch.setattr(
        runner, "_probe_offer", lambda abi, seccomp: (abi, seccomp) == (0, False)
    )
    probe = runner.probe()
    assert (probe.level, probe.missing) == ("degraded", CONTROLS)


def test_a_host_that_cannot_start_a_process_at_all_is_unavailable(monkeypatch):
    runner = HardenedSubprocessRunner()
    monkeypatch.setattr(runner, "_probe_offer", lambda abi, seccomp: False)
    with pytest.raises(RunnerUnavailable):
        runner.probe()


def test_a_probe_report_other_than_ok_is_a_failed_offer(monkeypatch):
    runner = HardenedSubprocessRunner()
    monkeypatch.setattr(
        runner, "_spawn_and_collect", lambda mode, spec: [{"probe_ok": False}]
    )
    assert runner._probe_offer(0, False) is False


def test_without_landlock_the_seccomp_controls_still_hold():
    if sandbox.syscall_table() is None:
        pytest.skip("no system-call table for this architecture")
    probe = HardenedSubprocessRunner(use_landlock=False).probe()
    assert probe.missing == ("filesystem", "isolation")


def test_the_probe_leaves_no_directory_behind(private_tmp):
    HardenedSubprocessRunner().probe()
    assert _run_dirs("fnprobe-") == set()


def test_third_party_packages_beside_the_standard_library_are_not_readable(
    monkeypatch, tmp_path
):
    stdlib = tmp_path / "python3.x"
    for name in ("lib-dynload", "site-packages", "dist-packages"):
        (stdlib / name).mkdir(parents=True)
    (stdlib / "os.py").write_text("")
    monkeypatch.setattr(runner_module, "_standard_library_dirs", lambda: [str(stdlib)])
    runner = HardenedSubprocessRunner()
    paths = runner._runtime_read_paths()
    assert str(stdlib / "os.py") in paths
    assert str(stdlib / "lib-dynload") in paths
    assert not any("site-packages" in p or "dist-packages" in p for p in paths)
    assert str(stdlib) not in paths
    assert runner._list_paths == {str(stdlib)}


# --- the system-call tables -----------------------------------------------------

_HEADERS = {
    "x86_64": "/usr/include/x86_64-linux-gnu/asm/unistd_64.h",
    "aarch64": "/usr/include/asm-generic/unistd.h",
}


@pytest.mark.parametrize("machine", sorted(_HEADERS))
def test_the_system_call_numbers_match_the_kernel_headers(machine):
    header = _HEADERS[machine]
    if not os.path.exists(header):
        pytest.skip(f"{header} is not installed")
    with open(header) as handle:
        numbers = dict(
            re.findall(r"#define __NR_(\w+)\s+(\d+)\s*$", handle.read(), re.M)
        )
    table = sandbox._SYSCALLS[machine]
    named = dict(table["deny"], socket=table["socket"], clone=table["clone"])
    named["clone3"] = table["clone3"]
    for name, number in named.items():
        assert int(numbers[name]) == number, name


def test_the_deny_list_names_every_call_adr_0012_closes():
    assert set(sandbox._SYSCALLS["x86_64"]["deny"]) == {
        "fork", "vfork", "setsid", "setpgid", "ptrace", "process_vm_readv",
        "process_vm_writev", "process_madvise", "pidfd_getfd", "kcmp", "unshare",
        "setns", "mount", "umount2", "pivot_root", "chroot", "open_tree",
        "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr",
        "bpf", "userfaultfd", "perf_event_open", "io_uring_setup", "io_uring_enter",
        "io_uring_register", "init_module", "finit_module", "delete_module",
        "kexec_load", "kexec_file_load", "reboot", "keyctl", "add_key",
        "request_key", "swapon", "swapoff", "acct", "iopl", "ioperm", "mknod",
        "mknodat",
    }  # fmt: skip
    assert sandbox._SYSCALLS["aarch64"]["audit_arch"] == sandbox.AUDIT_ARCH_AARCH64


def test_calls_only_the_filter_can_refuse_are_refused(runner, storage):
    needs(runner, "processes", "isolation")
    machine = os.uname().machine
    table = sandbox._SYSCALLS[machine]
    numbers = dict(table["deny"], clone3=table["clone3"])
    result = execute(
        runner,
        storage,
        handler(
            f"""
            libc = ctypes.CDLL(None, use_errno=True)
            numbers = {numbers!r}
            def call(name, *args):
                ctypes.set_errno(0)
                rc = libc.syscall(numbers[name], *args)
                return rc, ctypes.get_errno()
            out["ptrace traceme"] = call("ptrace", 0, 0, 0, 0)
            out["kcmp"] = call("kcmp", os.getpid(), os.getpid(), 0, 0, 0)
            out["io_uring_setup"] = call("io_uring_setup", 1, None)
            out["clone3"] = call("clone3", None, 0)
            """
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "ptrace traceme": [-1, errno.EPERM],
        "kcmp": [-1, errno.EPERM],
        "io_uring_setup": [-1, errno.EPERM],
        "clone3": [-1, errno.ENOSYS],
    }


# --- Landlock rights ------------------------------------------------------------


def test_files_outside_the_work_directory_cannot_be_changed_or_moved_to(
    runner, storage, tmp_path
):
    needs(runner, "filesystem")
    canary = tmp_path / "canary.txt"
    canary.write_text("host data")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            f"""
            own = os.path.abspath(__file__)
            attempt("append own file", lambda: open(own, "a").write("x"))
            attempt("truncate own file", lambda: os.truncate(own, 0))
            attempt("read host canary", lambda: open({str(canary)!r}).read())
            attempt("overwrite host canary", lambda: open({str(canary)!r}, "w"))
            open("moved.txt", "w").write("x")
            attempt("rename out of work", lambda: os.rename(
                "moved.txt", os.path.join(os.path.dirname(own), "moved.txt")))
            attempt("hard link out of work", lambda: os.link(
                own, "linked.py"))
            attempt("truncate work file", lambda: os.truncate("moved.txt", 0))
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "append own file": "PermissionError",
        "truncate own file": "PermissionError",
        "read host canary": "PermissionError",
        "overwrite host canary": "PermissionError",
        "rename out of work": "PermissionError",
        # Landlock refuses a link across rule boundaries with EXDEV.
        "hard link out of work": "OSError",
        "truncate work file": "allowed",
    }
    assert canary.read_text() == "host data"


def test_the_function_runs_under_the_runs_resource_limits(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            import resource
            for name in ("AS", "CPU", "FSIZE", "NOFILE", "CORE"):
                out[name] = list(resource.getrlimit(getattr(resource, "RLIMIT_" + name)))
            """
        ),
        timeout_seconds=7,
        memory_mib=128,
    )
    mib = 1024 * 1024
    assert result.output == {
        "AS": [128 * mib, 128 * mib],
        "CPU": [9, 10],
        "FSIZE": [16 * mib, 16 * mib],
        "NOFILE": [64, 64],
        "CORE": [0, 0],
    }


def test_cpu_time_under_the_backstop_does_not_end_the_run(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            started = time.process_time()
            while time.process_time() - started < 1.5:
                pass
            out["done"] = True
            """
        ),
        timeout_seconds=10,
    )
    assert result.status == "succeeded", result


# --- termination ---------------------------------------------------------------


def _start_raw(runner, source, **fields):
    handle = runner.start(spec(source, **fields))
    os.write(handle.stdin_fd, b'{"jsonrpc":"2.0","method":"invoke","params":{}}\n')
    return handle


def _read_line(fd, timeout=10):
    deadline = time.monotonic() + timeout
    data = b""
    while not data.endswith(b"\n") and time.monotonic() < deadline:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data += chunk
    return data


def test_a_supervisor_that_stops_answering_is_lost_and_its_run_ends(
    degraded_runner,
):
    handle = _start_raw(
        degraded_runner,
        """
        import os, sys, time
        sys.stdin.readline()
        sys.stdout.write(str(os.getpid()) + "\\n"); sys.stdout.flush()
        while True:
            time.sleep(0.1)
        """,
    )
    function_pid = int(_read_line(handle.stdout_fd))
    os.kill(handle._process.pid, signal.SIGSTOP)
    handle.kill()
    started = time.monotonic()
    process_exit = handle.wait(timeout=0.5)
    assert process_exit.outcome == "runner_lost"
    assert "did not stop in time" in process_exit.detail
    assert time.monotonic() - started < 5
    time.sleep(0.5)
    assert _dead(function_pid)


def test_a_run_the_host_stops_is_reported_as_killed(runner):
    handle = _start_raw(runner, "import sys\nsys.stdin.readline()\nwhile True: pass\n")
    time.sleep(0.2)
    handle.kill()
    assert handle.wait().outcome == "killed"
    assert handle.wait() is handle.wait()


def test_a_function_that_lingers_after_its_result_is_stopped_and_succeeds(
    runner, storage
):
    started = time.monotonic()
    result = execute(
        runner,
        storage,
        "import json, sys, time\nsys.stdin.readline()\n"
        + RESULT_LINE.replace("OUTPUT", '{"done": True}')
        + "\ntime.sleep(30)\n",
        timeout_seconds=60,
    )
    assert result.status == "succeeded", result
    assert result.output == {"done": True}
    assert time.monotonic() - started < 10


def test_descendants_that_ignore_sigterm_or_hide_behind_a_parent_die(
    degraded_runner, storage
):
    result = execute(
        degraded_runner,
        storage,
        handler(
            """
            read_end, write_end = os.pipe()
            if os.fork() == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                if os.fork() == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    os.write(write_end, (str(os.getpid()) + " ").encode())
                    while True:
                        time.sleep(1)
                os.write(write_end, (str(os.getpid()) + " ").encode())
                while True:
                    time.sleep(1)
            pids = b""
            while pids.count(b" ") < 2:
                pids += os.read(read_end, 64)
            out["pids"] = [int(p) for p in pids.split()]
            """
        ),
    )
    assert result.status == "succeeded", result
    time.sleep(0.2)
    assert all(_dead(pid) for pid in result.output["pids"])


def test_the_run_ends_when_its_host_dies(tmp_path):
    script = tmp_path / "host.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, signal, sys
            sys.path.insert(0, {os.getcwd()!r})
            from backend.functions.runner import HardenedSubprocessRunner, RunSpec
            source = (b"import os, sys, time\\nsys.stdin.readline()\\n"
                      b"sys.stdout.write(str(os.getpid()) + '\\\\n'); sys.stdout.flush()\\n"
                      b"time.sleep(60)\\n")
            runner = HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)
            handle = runner.start(RunSpec(run_id="r", runtime="python",
                                          entry_point="main.py", files={{"main.py": source}}))
            os.write(handle.stdin_fd, b'{{"jsonrpc":"2.0","method":"invoke"}}\\n')
            line = b""
            while not line.endswith(b"\\n"):
                line += os.read(handle.stdout_fd, 64)
            print(line.decode().strip(), flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
            """
        )
    )
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, timeout=60
    )
    function_pid = int(completed.stdout.decode().split()[0])
    deadline = time.monotonic() + 10
    while not _dead(function_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _dead(function_pid)


# --- cleanup and inputs -----------------------------------------------------------


def test_a_run_leaves_no_descriptor_or_directory_behind(runner, storage, private_tmp):
    execute(runner, storage, handler("out['warm'] = True"))
    fds = _open_fds()
    execute(runner, storage, handler("out['again'] = True"))
    time.sleep(0.2)
    assert _open_fds() == fds
    assert _run_dirs("fnrun-") == set()


def test_a_failed_spawn_leaves_no_descriptor_or_directory_behind(
    runner, monkeypatch, private_tmp
):
    runner.probe()
    fds = _open_fds()

    def refuse(*args, **kwargs):
        raise OSError("spawn refused")

    monkeypatch.setattr(runner_module.subprocess, "Popen", refuse)
    with pytest.raises(OSError):
        runner.start(spec("pass\n"))
    assert _open_fds() == fds
    assert _run_dirs("fnrun-") == set()


@pytest.mark.parametrize(
    "path", ["/x", "a\\b", "a\x00b", ".", "./", "", "..", "a/../../b", 5, None]
)
def test_paths_that_are_not_plain_relative_paths_are_refused(path):
    with pytest.raises(ValueError):
        safe_relative_path(path)


def test_an_entry_point_that_is_not_one_of_the_files_is_refused(runner):
    with pytest.raises(ValueError):
        runner.start(
            RunSpec(
                run_id="r",
                runtime="python",
                entry_point="other.py",
                files={"main.py": b""},
            )
        )


def test_entry_points_and_files_are_compared_after_normalizing(runner, storage):
    result = run_function(
        runner,
        RunSpec(
            run_id="r",
            runtime="python",
            entry_point="./pkg/main.py",
            files={
                "pkg//main.py": (
                    b"from communityoverview_function import run\n"
                    b"from pkg import util\nrun(lambda envelope, tools: {'v': util.V})\n"
                ),
                "./pkg/util.py": b"V = 7\n",
            },
            timeout_seconds=20,
        ),
        make_broker(storage),
        ENVELOPE,
    )
    assert result.output == {"v": 7}, result


# --- the helper library -----------------------------------------------------------


@pytest.mark.parametrize(
    "returned, code",
    [("[1, 2]", "invalid_output"), ("{'s': {1}}", "unhandled_exception")],
)
def test_a_handler_result_that_is_not_a_json_object_is_a_function_error(
    runner, storage, returned, code
):
    source = (
        "from communityoverview_function import run\n"
        f"run(lambda envelope, tools: {returned})\n"
    )
    result = execute(runner, storage, source)
    assert (result.status, result.code) == ("failed", "function_error")
    assert result.function_error_code == code


def test_the_helper_refuses_a_first_message_that_is_not_the_invocation():
    env = {"PYTHONPATH": runner_module.HELPER_LIBRARY_DIR}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from communityoverview_function import run\n"
            "run(lambda envelope, tools: {})\n",
        ],
        input=json.dumps({"jsonrpc": "2.0", "method": "result"}).encode() + b"\n",
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert completed.returncode != 0
    assert completed.stdout == b""
