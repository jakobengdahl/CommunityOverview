"""The seccomp program, the work directory's rights and the exit mapping, checked
rule by rule rather than through one host's kernel (ADR 0012 §1)."""

import errno
import signal
import struct

import pytest

from backend.functions import sandbox
from backend.functions.runner import (
    HardenedSubprocessRunner,
    exit_from_report,
)
from backend.functions.tests.test_runner import ATTEMPT, execute, handler, needs

_DATA = struct.Struct("=IIQ6Q")  # struct seccomp_data


def evaluate(program, *, nr, arch, arg0=0):
    """Run a classic-BPF seccomp program over one system call."""
    data = _DATA.pack(nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    pc, accumulator = 0, 0
    for _ in range(len(program) + 1):
        code, jt, jf, k = struct.unpack("HBBI", program[pc])
        if code == sandbox._BPF_LD_W_ABS:
            (accumulator,) = struct.unpack_from("=I", data, k)
            pc += 1
        elif code == sandbox._BPF_JEQ_K:
            pc += 1 + (jt if accumulator == k else jf)
        elif code == sandbox._BPF_JGE_K:
            pc += 1 + (jt if accumulator >= k else jf)
        elif code == sandbox._BPF_JSET_K:
            pc += 1 + (jt if accumulator & k else jf)
        elif code == sandbox._BPF_RET_K:
            return k
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")
    raise AssertionError("the program did not return")


EPERM = sandbox._RET_ERRNO | errno.EPERM
ENOSYS = sandbox._RET_ERRNO | errno.ENOSYS
KILL = sandbox._RET_KILL_PROCESS
ALLOW = sandbox._RET_ALLOW
CLONE_THREAD_FLAGS = 0x100 | 0x800 | sandbox.CLONE_THREAD  # VM | SIGHAND | THREAD
NAMESPACE_FLAGS = {
    "NEWNS": 0x00020000,
    "NEWCGROUP": 0x02000000,
    "NEWUTS": 0x04000000,
    "NEWIPC": 0x08000000,
    "NEWUSER": 0x10000000,
    "NEWPID": 0x20000000,
    "NEWNET": 0x40000000,
    "NEWTIME": 0x00000080,
}
# read() in each table: a call the filter must leave alone.
_READ = {"x86_64": 0, "aarch64": 63}


@pytest.fixture(params=sorted(sandbox._SYSCALLS))
def table(request):
    return request.param, sandbox._SYSCALLS[request.param]


def run(table, nr, arg0=0):
    _, entries = table
    return evaluate(
        sandbox.seccomp_program(entries), nr=nr, arch=entries["audit_arch"], arg0=arg0
    )


def test_the_architecture_constants_are_the_kernels():
    # AUDIT_ARCH_* = EM_* | __AUDIT_ARCH_64BIT | __AUDIT_ARCH_LE (linux/audit.h)
    assert sandbox.AUDIT_ARCH_X86_64 == 62 | 0x80000000 | 0x40000000
    assert sandbox.AUDIT_ARCH_AARCH64 == 183 | 0x80000000 | 0x40000000


def test_a_foreign_architecture_is_killed(table):
    _, entries = table
    program = sandbox.seccomp_program(entries)
    for foreign in (0x40000003, 0xC000003E, 0xC00000B7, 0):
        if foreign != entries["audit_arch"]:
            assert evaluate(program, nr=_READ[table[0]], arch=foreign) == KILL


def test_an_ordinary_call_is_allowed(table):
    assert run(table, _READ[table[0]]) == ALLOW


def test_every_denied_call_returns_eperm(table):
    for name, nr in table[1]["deny"].items():
        assert run(table, nr) == EPERM, name


def test_creating_a_socket_returns_eperm(table):
    assert run(table, table[1]["socket"]) == EPERM


def test_clone3_is_unavailable(table):
    assert run(table, table[1]["clone3"]) == ENOSYS


def test_clone_is_allowed_for_a_thread_only(table):
    clone = table[1]["clone"]
    assert run(table, clone, CLONE_THREAD_FLAGS) == ALLOW
    assert run(table, clone, 0x11) == EPERM  # fork: SIGCHLD
    assert run(table, clone, 0x100 | 0x4000 | 0x11) == EPERM  # vfork-like


@pytest.mark.parametrize("flag", sorted(NAMESPACE_FLAGS))
def test_a_thread_with_any_namespace_flag_returns_eperm(table, flag):
    flags = CLONE_THREAD_FLAGS | NAMESPACE_FLAGS[flag]
    assert run(table, table[1]["clone"], flags) == EPERM


def test_the_namespace_mask_is_exactly_the_namespace_flags():
    mask = 0
    for value in NAMESPACE_FLAGS.values():
        mask |= value
    assert sandbox.CLONE_NAMESPACE_FLAGS == mask


def test_an_x32_call_is_killed_on_x86_64():
    entries = sandbox._SYSCALLS["x86_64"]
    program = sandbox.seccomp_program(entries)
    for nr in (sandbox.X32_SYSCALL_BIT | 39, sandbox.X32_SYSCALL_BIT):
        assert evaluate(program, nr=nr, arch=entries["audit_arch"]) == KILL


def test_this_host_has_a_table_where_one_is_defined():
    import os

    if os.uname().machine in sandbox._SYSCALLS:
        assert sandbox.syscall_table() is sandbox._SYSCALLS[os.uname().machine]


# --- the work directory -------------------------------------------------------


def test_the_work_directory_grants_exactly_ordinary_file_rights():
    assert sandbox.WORK_RIGHTS == (
        sandbox.FS_READ_FILE
        | sandbox.FS_WRITE_FILE
        | sandbox.FS_READ_DIR
        | sandbox.FS_REMOVE_DIR
        | sandbox.FS_REMOVE_FILE
        | sandbox.FS_MAKE_DIR
        | sandbox.FS_MAKE_REG
        | sandbox.FS_REFER
        | sandbox.FS_TRUNCATE
    )
    assert not sandbox.WORK_RIGHTS & sandbox.FS_EXECUTE


def test_work_files_can_be_managed_but_not_executed(storage):
    runner = HardenedSubprocessRunner(use_seccomp=False)
    needs(runner, "filesystem")
    result = execute(
        runner,
        storage,
        handler(
            ATTEMPT,
            """
            import sys
            os.makedirs("a/b")
            open("a/b/f.txt", "w").write("x")
            attempt("rename across directories", lambda: os.rename(
                "a/b/f.txt", "a/f.txt"))
            attempt("read", lambda: open("a/f.txt").read())
            attempt("unlink", lambda: os.unlink("a/f.txt"))
            attempt("rmdir", lambda: os.rmdir("a/b"))
            # Keep this below RLIMIT_FSIZE even with a large Python binary.
            with open("runtime-copy", "w") as executable:
                executable.write("#!" + sys.executable + "\\npass\\n")
            os.chmod("runtime-copy", 0o700)
            attempt("execute a work file", lambda: os.execv(
                "runtime-copy", ["runtime-copy", "-c", "pass"]))
            """,
        ),
    )
    assert result.status == "succeeded", result
    assert result.output == {
        "rename across directories": "allowed",
        "read": "allowed",
        "unlink": "allowed",
        "rmdir": "allowed",
        "execute a work file": "PermissionError",
    }


# --- the exit report ------------------------------------------------------------


def _report(**fields):
    return dict(
        {"exit_code": None, "signal": None, "stopped": False, "cpu_seconds": 0},
        **fields,
    )


@pytest.mark.parametrize("killer", [signal.SIGXCPU, signal.SIGKILL])
def test_a_kill_at_the_cpu_limit_is_reported_as_reaching_it(killer):
    reached = exit_from_report(
        _report(signal=killer, cpu_seconds=12.0), cpu_limit_seconds=12, wall_ms=1
    )
    assert reached.cpu_limit_reached is True


@pytest.mark.parametrize(
    "report",
    [
        _report(signal=signal.SIGKILL, cpu_seconds=11.99),
        _report(signal=signal.SIGSEGV, cpu_seconds=30.0),
        _report(signal=signal.SIGKILL, cpu_seconds=30.0, stopped=True),
        _report(exit_code=0, cpu_seconds=30.0),
        _report(signal=signal.SIGKILL, cpu_seconds=None),
    ],
)
def test_other_ends_are_not_the_cpu_limit(report):
    assert (
        exit_from_report(report, cpu_limit_seconds=12, wall_ms=1).cpu_limit_reached
        is False
    )


def test_the_report_fields_carry_into_the_exit():
    process_exit = exit_from_report(
        _report(exit_code=3, peak_memory_kib=2048), cpu_limit_seconds=5, wall_ms=42
    )
    assert (process_exit.outcome, process_exit.exit_code) == ("exited", 3)
    assert (process_exit.peak_memory_kib, process_exit.wall_ms) == (2048, 42)
    stopped = exit_from_report(_report(stopped=True), cpu_limit_seconds=5, wall_ms=1)
    assert stopped.outcome == "killed"


def test_a_handler_that_returns_none_succeeds_with_an_empty_output(storage):
    source = (
        "from communityoverview_function import run\n"
        "run(lambda envelope, tools: None)\n"
    )
    result = execute(HardenedSubprocessRunner(), storage, source)
    assert (result.status, result.output) == ("succeeded", {})
