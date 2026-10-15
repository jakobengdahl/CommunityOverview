"""Invariants of the hardened-subprocess runner (ADR 0012, USER_FUNCTION_CONTRACT §3, §8).

Tests that need a control are skipped where the host cannot enforce it, so the
suite says what this host proves rather than failing on an older kernel.
"""

import os
import platform
import textwrap
import time

import pytest

from backend.core.models import Node, NodeType
from backend.functions import sandbox
from backend.functions.broker import (
    GRANT_GRAPH_READ,
    GRANT_GRAPH_WRITE,
    BrokerContext,
    FunctionBroker,
)
from backend.functions.protocol import SessionOutcome
from backend.functions.runner import (
    CONTROLS,
    LEVEL_DEGRADED,
    LEVEL_FULL,
    HardenedSubprocessRunner,
    RunExit,
    RunSpec,
    _result_for,
    run_function,
    safe_relative_path,
)
from backend.service import GraphService

ENVELOPE = {"schema_version": "1.0", "invocation": {"input": {"q": "Alpha"}}}

_HELPER_PRELUDE = "from communityoverview_function import run, ToolError\n"


@pytest.fixture(scope="module")
def runner():
    return HardenedSubprocessRunner()


@pytest.fixture(scope="module")
def degraded_runner():
    return HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)


def needs(runner, *controls):
    missing = [c for c in controls if c in runner.probe().missing]
    if missing:
        pytest.skip(f"host cannot enforce: {', '.join(missing)}")


def make_broker(storage):
    context = BrokerContext(
        run_id="job-runner",
        function_id="runner-test",
        function_version="0.1.0",
        declared_grants=frozenset({GRANT_GRAPH_READ, GRANT_GRAPH_WRITE}),
        correlation_id="corr-runner",
        authorization_inputs={"actor_id": "tester"},
        grant_enabled=lambda grant: True,
    )
    return FunctionBroker(GraphService(storage), context)


def execute(runner, storage, source, *, files=None, **spec_fields):
    spec_fields.setdefault("timeout_seconds", 20)
    spec = RunSpec(
        run_id="job-runner",
        runtime="python",
        entry_point="main.py",
        files={"main.py": textwrap.dedent(source).encode(), **(files or {})},
        **spec_fields,
    )
    return run_function(runner, spec, make_broker(storage), ENVELOPE)


def handler(*parts):
    """A function whose handler runs ``parts`` and returns the dict ``out``."""
    body = "\n".join(textwrap.dedent(part) for part in parts)
    return (
        _HELPER_PRELUDE
        + "import os, sys, socket, ctypes, signal, time, threading\n"
        + "def handle(envelope, tools):\n"
        + "    out = {}\n"
        + textwrap.indent(body, "    ")
        + "\n    return out\n"
        + "run(handle)\n"
    )


ATTEMPT = """
def attempt(label, action):
    try:
        action()
        out[label] = "allowed"
    except BaseException as exc:
        out[label] = type(exc).__name__
"""


# --- the run, end to end ------------------------------------------------------


def test_a_handler_reads_the_envelope_calls_a_tool_and_succeeds(runner, storage):
    storage.add_nodes([Node(name="Alpha", type=NodeType.ACTOR)], [])
    result = execute(
        runner,
        storage,
        handler(
            """
            query = envelope["invocation"]["input"]["q"]
            found = tools.graph.search(query=query, limit=5)
            out["names"] = [n["name"] for n in found["nodes"]]
            """
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {"names": ["Alpha"]}
    assert [c.tool for c in result.tool_calls] == ["graph.search"]
    assert result.wall_ms > 0


def test_print_goes_to_the_log_and_never_into_the_protocol(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            print("hello from print")
            os.write(1, b"raw write to descriptor 1\\n")
            """
        ),
    )
    assert result.status == "succeeded", result
    assert "hello from print" in result.log
    assert "raw write to descriptor 1" in result.log


def test_a_refused_tool_call_reaches_the_handler_as_a_tool_error(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            try:
                tools.graph.search(query="x", graph_id="other")
            except ToolError as exc:
                out["code"] = exc.code
            """
        ),
    )
    assert result.output == {"code": "invalid_arguments"}


def test_the_function_gets_an_empty_environment_and_only_its_standard_streams(
    runner, storage
):
    # No helper library: it opens descriptors of its own.
    source = """
    import json, os, sys
    sys.stdin.buffer.readline()
    open_fds = []
    for fd in range(3, 256):
        try:
            os.fstat(fd)
            open_fds.append(fd)
        except OSError:
            pass
    output = {"env": dict(os.environ), "fds": open_fds}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "result",
                                 "params": {"status": "ok", "output": output}}) + "\\n")
    """
    result = execute(runner, storage, source)
    assert result.status == "succeeded", result
    assert result.output == {"env": {}, "fds": []}


def test_an_unhandled_exception_is_a_function_error_with_the_traceback_logged(
    runner, storage
):
    result = execute(runner, storage, handler("raise ValueError('boom')"))
    assert (result.status, result.code) == ("failed", "function_error")
    assert result.function_error_code == "unhandled_exception"
    assert "ValueError: boom" in result.message
    assert "Traceback" in result.log


def test_exiting_without_a_result_is_no_result(runner, storage):
    result = execute(runner, storage, "import sys\nsys.stdin.readline()\n")
    assert (result.status, result.code) == ("failed", "no_result")


def test_a_busy_loop_is_terminated_at_the_deadline_as_timed_out(runner, storage):
    started = time.monotonic()
    result = execute(runner, storage, "while True:\n    pass\n", timeout_seconds=1)
    assert (result.status, result.code) == ("timed_out", "timeout")
    assert time.monotonic() - started < 10


def test_an_allocation_loop_ends_as_memory_exceeded(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            hoard = []
            while True:
                hoard.append(bytearray(8 * 1024 * 1024))
            """
        ),
        memory_mib=96,
    )
    assert (result.status, result.code) == ("failed", "memory_exceeded"), result


def test_the_log_is_cut_at_its_limit_while_the_run_completes(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            """
            for _ in range(64):
                sys.stderr.write("x" * 16384)
            sys.stderr.flush()
            out["done"] = True
            """
        ),
        max_log_bytes=1000,
    )
    assert result.status == "succeeded", result
    assert result.log_truncated is True
    assert len(result.log) == 1000


def test_the_run_directory_is_removed_after_the_run(runner, storage):
    result = execute(runner, storage, handler("out['cwd'] = os.getcwd()"))
    assert result.status == "succeeded", result
    assert not os.path.exists(result.output["cwd"])


def test_functions_import_their_own_modules_and_not_the_applications(runner, storage):
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            """
            import helpers
            out["value"] = helpers.VALUE
            attempt("backend", lambda: __import__("backend"))
            """,
        ),
        files={"helpers.py": b"VALUE = 42\n"},
    )
    assert result.output == {"value": 42, "backend": "ModuleNotFoundError"}


# --- spec validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "entry_point", ["../main.py", "/etc/passwd", "", "a/../../b.py", "a\\b.py"]
)
def test_an_entry_point_that_leaves_the_function_directory_is_refused(
    runner, entry_point
):
    with pytest.raises(ValueError):
        runner.start(
            RunSpec(
                run_id="r",
                runtime="python",
                entry_point=entry_point,
                files={"main.py": b""},
            )
        )


def test_a_file_path_that_leaves_the_function_directory_is_refused(runner):
    with pytest.raises(ValueError):
        runner.start(
            RunSpec(
                run_id="r",
                runtime="python",
                entry_point="main.py",
                files={"main.py": b"", "../escape.py": b""},
            )
        )


def test_a_runtime_the_runner_lacks_is_refused(runner):
    with pytest.raises(ValueError):
        runner.start(
            RunSpec(
                run_id="r",
                runtime="node",
                entry_point="main.js",
                files={"main.js": b""},
            )
        )


@pytest.mark.parametrize(
    "limit",
    [
        {"timeout_seconds": 121},
        {"timeout_seconds": 0},
        {"memory_mib": 257},
        {"memory_mib": -1},
        {"max_log_bytes": 256 * 1024 + 1},
        {"timeout_seconds": True},
    ],
)
def test_a_spec_cannot_raise_or_zero_a_platform_limit(limit):
    with pytest.raises(ValueError):
        RunSpec(
            run_id="r",
            runtime="python",
            entry_point="main.py",
            files={"main.py": b""},
            **limit,
        )


def test_safe_relative_path_normalizes_a_leading_dot():
    assert safe_relative_path("./lib/x.py") == "lib/x.py"


# --- the controls (§3.1) -------------------------------------------------------


def test_only_the_runtime_and_the_function_are_readable_and_only_work_is_writable(
    runner, storage
):
    needs(runner, "filesystem")
    this_file = os.path.abspath(__file__)
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            f"""
            here = os.path.dirname(os.path.abspath(__file__))
            attempt("read /etc/passwd", lambda: open("/etc/passwd").read())
            attempt("read application source", lambda: open({this_file!r}).read())
            attempt("list /", lambda: os.listdir("/"))
            attempt("write /tmp", lambda: open("/tmp/fn-escape", "w").write("x"))
            attempt("write function dir", lambda: open(here + "/x", "w").write("x"))
            attempt("read own file", lambda: open(__file__).read())
            attempt("write work dir", lambda: open("scratch.txt", "w").write("x"))
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "read /etc/passwd": "PermissionError",
        "read application source": "PermissionError",
        "list /": "PermissionError",
        "write /tmp": "PermissionError",
        "write function dir": "PermissionError",
        "read own file": "allowed",
        "write work dir": "allowed",
    }


def test_the_work_directory_takes_no_device_nodes_fifos_or_links(storage):
    # Landlock alone: the seccomp filter also denies mknod, as a second layer.
    runner = HardenedSubprocessRunner(use_seccomp=False)
    needs(runner, "filesystem")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            """
            import stat
            attempt("block device", lambda: os.mknod(
                "blk", 0o600 | stat.S_IFBLK, os.makedev(7, 0)))
            attempt("character device", lambda: os.mknod(
                "chr", 0o600 | stat.S_IFCHR, os.makedev(1, 3)))
            attempt("fifo", lambda: os.mkfifo("fifo"))
            attempt("symlink", lambda: os.symlink("/etc/passwd", "link"))
            attempt("directory", lambda: os.mkdir("sub"))
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "block device": "PermissionError",
        "character device": "PermissionError",
        "fifo": "PermissionError",
        "symlink": "PermissionError",
        "directory": "allowed",
    }


def test_no_socket_can_be_created_but_a_socket_pair_works(runner, storage):
    needs(runner, "network")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            """
            attempt("tcp", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1))
            attempt("udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            attempt("unix", lambda: socket.socket(socket.AF_UNIX))
            attempt("netlink", lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW))
            attempt("socketpair", lambda: socket.socketpair())
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "tcp": "PermissionError",
        "udp": "PermissionError",
        "unix": "PermissionError",
        "netlink": "PermissionError",
        "socketpair": "allowed",
    }


def test_no_process_can_be_started_but_threads_work(runner, storage):
    needs(runner, "processes")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            """
            import subprocess
            attempt("fork", lambda: os.fork())
            attempt("subprocess", lambda: subprocess.run(["/bin/true"]))
            attempt("posix_spawn", lambda: os.posix_spawn(
                "/usr/bin/true", ["true"], {}))
            attempt("setsid", lambda: os.setsid())
            attempt("setpgid", lambda: os.setpgid(0, 0))
            attempt("unshare", lambda: os.unshare(os.CLONE_NEWUSER))
            attempt("mknod", lambda: os.mknod("node", 0o600 | 0o010000))
            libc = ctypes.CDLL(None, use_errno=True)
            # A thread with a namespace of its own; the filter refuses it
            # before the kernel would start it on a bogus stack.
            def namespaced_thread():
                flags = 0x100 | 0x800 | 0x10000 | 0x04000000
                if libc.syscall(libc_clone, flags, ctypes.c_void_p(1), 0, 0, 0) < 0:
                    raise PermissionError(ctypes.get_errno(), "clone")
            libc_clone = {"x86_64": 56, "aarch64": 220}[os.uname().machine]
            attempt("namespaced thread", namespaced_thread)
            results = []
            worker = threading.Thread(target=lambda: results.append(21 * 2))
            worker.start()
            worker.join()
            out["thread"] = results
            """,
        ),
    )
    assert result.status == "succeeded", result
    output = result.output
    assert output.pop("thread") == [42]
    assert output == {
        "fork": "PermissionError",
        "subprocess": "PermissionError",
        "posix_spawn": "PermissionError",
        "setsid": "PermissionError",
        "setpgid": "PermissionError",
        "unshare": "PermissionError",
        "mknod": "PermissionError",
        "namespaced thread": "PermissionError",
    }


def test_nothing_outside_the_run_can_be_signalled_traced_or_read(runner, storage):
    needs(runner, "isolation")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            f"""
            libc = ctypes.CDLL(None, use_errno=True)
            def ptrace_attach():
                if libc.ptrace(16, os.getppid(), 0, 0) != 0:
                    raise PermissionError(ctypes.get_errno(), "ptrace")
            attempt("signal supervisor", lambda: os.kill(os.getppid(), 0))
            attempt("signal host", lambda: os.kill({os.getpid()}, 0))
            attempt("signal init", lambda: os.kill(1, 0))
            attempt("trace supervisor", ptrace_attach)
            attempt("read host environ", lambda: open("/proc/{os.getpid()}/environ").read())
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "signal supervisor": "PermissionError",
        "signal host": "PermissionError",
        "signal init": "PermissionError",
        "trace supervisor": "PermissionError",
        "read host environ": "PermissionError",
    }


@pytest.mark.skipif(platform.machine() != "x86_64", reason="x32 is x86-64 only")
def test_an_x32_system_call_kills_the_function(runner, storage):
    needs(runner, "processes")
    result = execute(
        runner,
        storage,
        """
        import ctypes, sys
        sys.stdin.readline()
        ctypes.CDLL(None).syscall(0x40000000 | 39)  # x32 getpid
        """,
    )
    assert (result.status, result.code) == ("failed", "no_result")


# --- levels and the supervisor at degraded --------------------------------------


def test_the_reported_level_is_full_only_with_every_control(runner):
    probe = runner.probe()
    assert set(probe.missing) <= set(CONTROLS)
    assert probe.level == (LEVEL_FULL if not probe.missing else LEVEL_DEGRADED)
    assert probe.runtimes == ("python",)


def test_a_runner_without_landlock_or_seccomp_reports_every_control_missing(
    degraded_runner,
):
    probe = degraded_runner.probe()
    assert probe.level == LEVEL_DEGRADED
    assert probe.missing == CONTROLS


def test_an_offer_the_host_cannot_apply_falls_back_to_one_it_can(monkeypatch):
    if not sandbox.landlock_abi() or sandbox.syscall_table() is None:
        pytest.skip("needs both Landlock and a system-call table")
    runner = HardenedSubprocessRunner()
    monkeypatch.setattr(
        runner, "_probe_offer", lambda abi, seccomp: not (abi and seccomp)
    )
    probe = runner.probe()
    assert probe.level == LEVEL_DEGRADED
    assert "isolation" in probe.missing
    assert runner._seccomp is True and runner._landlock_abi == 0


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().split(b")")[-1].split()[0] != b"Z"
    except OSError:
        return False


def test_descendants_that_leave_the_session_die_with_the_run(degraded_runner, storage):
    result = execute(
        degraded_runner,
        storage,
        handler(
            """
            read_end, write_end = os.pipe()
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    os.write(write_end, str(os.getpid()).encode())
                    while True:
                        time.sleep(1)
                os._exit(0)
            out["daemon"] = int(os.read(read_end, 32))
            """
        ),
    )
    assert result.status == "succeeded", result
    assert not _alive(result.output["daemon"])


def test_a_function_that_kills_its_supervisor_fails_with_runner_lost(
    degraded_runner, storage
):
    result = execute(
        degraded_runner,
        storage,
        handler(
            """
            os.kill(os.getppid(), signal.SIGKILL)
            time.sleep(0.5)
            out["pid"] = os.getpid()
            """
        ),
    )
    assert (result.status, result.code) == ("failed", "runner_lost")


def test_the_function_dies_with_a_supervisor_it_killed(degraded_runner, storage):
    started = time.monotonic()
    result = execute(
        degraded_runner,
        storage,
        handler(
            """
            os.kill(os.getppid(), signal.SIGKILL)
            while True:
                time.sleep(0.1)
            """
        ),
        timeout_seconds=30,
    )
    assert (result.status, result.code) == ("failed", "runner_lost")
    # Death signal, not the 30 s deadline, ended the run.
    assert time.monotonic() - started < 15


# --- the system-call tables ----------------------------------------------------


def test_every_architecture_denies_the_same_calls_except_those_it_lacks():
    x86 = set(sandbox._SYSCALLS["x86_64"]["deny"])
    arm = set(sandbox._SYSCALLS["aarch64"]["deny"])
    assert x86 - arm == {"fork", "vfork", "iopl", "ioperm", "mknod"}
    assert arm <= x86


def test_the_filter_rejects_a_foreign_architecture_before_anything_else():
    for table in sandbox._SYSCALLS.values():
        program = sandbox.seccomp_program(table)
        assert program[0] == sandbox._stmt(sandbox._BPF_LD_W_ABS, sandbox._OFFSET_ARCH)
        assert program[1] == sandbox._jump(
            sandbox._BPF_JEQ_K, table["audit_arch"], 1, 0
        )
        assert program[2] == sandbox._stmt(
            sandbox._BPF_RET_K, sandbox._RET_KILL_PROCESS
        )
        assert program[-1] == sandbox._stmt(sandbox._BPF_RET_K, sandbox._RET_ALLOW)


def test_the_ruleset_names_only_rights_the_kernels_abi_knows():
    # Bit 15 (IOCTL_DEV) arrived with ABI 5; ABI 4 added only network rights.
    assert sandbox._FS_BY_ABI[3] == sandbox._FS_BY_ABI[4] == (1 << 15) - 1
    assert sandbox._FS_BY_ABI.get(5, sandbox._FS_ALL) & sandbox.FS_IOCTL_DEV
    for abi in (1, 2, 3, 4):
        assert not sandbox._FS_BY_ABI[abi] & sandbox.FS_IOCTL_DEV


def _exit(**fields):
    return RunExit("exited", **fields)


def test_a_cpu_backstop_kill_without_a_result_is_a_timeout(storage):
    broker = make_broker(storage)
    silent = SessionOutcome(code="no_result")
    cpu_kill = _exit(signal=9, cpu_limit_reached=True)
    assert _result_for(silent, cpu_kill, broker).code == "timeout"
    assert _result_for(silent, _exit(signal=9), broker).code == "no_result"


def test_losing_the_runner_outranks_whatever_the_function_reported(storage):
    broker = make_broker(storage)
    succeeded = SessionOutcome(code=None, output={"ok": True})
    for outcome in ("runner_lost", "failed_to_start"):
        result = _result_for(succeeded, RunExit(outcome), broker)
        assert (result.status, result.code, result.output) == (
            "failed",
            "runner_lost",
            None,
        )
