"""
Per-run supervisor and sandbox launcher for the hardened-subprocess runner
(docs/adr/0012-user-function-isolation.md, docs/USER_FUNCTION_CONTRACT.md §3.1).

The host starts this file as its own process (``python -I -S sandbox.py <spec>``)
with the function's standard streams already attached. It never imports
application modules: it runs outside the sandbox, as the run's supervisor.

As supervisor it registers as a child subreaper, forks the function process and
polls its working-directory quota. Once the function exits, exceeds its quota
or the host asks it to stop (SIGTERM), it kills every descendant it can see
before it reports on the status descriptor and exits.

The forked child applies, in order: resource limits, ``no_new_privs``, a
Landlock ruleset and a seccomp filter, each as far as the spec asks, and then
replaces itself with the runtime. A control the spec asks for that cannot be
applied aborts the start: a run never runs less confined than reported.

``--probe`` applies everything the host can offer in a child and reports what
held, so the runner's reported level is what a run actually gets.
"""

import ctypes
import json
import os
import resource
import signal
import stat
import struct
import sys
import time

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1
SECCOMP_MODE_FILTER = 2

MIB = 1024 * 1024
DISK_POLL_SECONDS = 0.1
# Rescans allowed when directories move during a scan before it counts as failed.
DISK_SCAN_ATTEMPTS = 5

# Exit code of the function child when setting up the sandbox failed; the
# reason travels over the setup descriptor, so the code is never trusted alone.
SETUP_FAILED = 125

_libc = None


def libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def _errno_text():
    return os.strerror(ctypes.get_errno())


class SetupError(Exception):
    pass


# --- Landlock -------------------------------------------------------------

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1
SCOPE_ABSTRACT_UNIX = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1

# Filesystem rights each ABI handles: ABI 1 handles bits 0-12, ABI 2 adds
# REFER, ABI 3 TRUNCATE, ABI 4 only network rights, ABI 5 IOCTL_DEV. A kernel
# rejects a ruleset that names a right it does not know.
_FS_BY_ABI = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 4: (1 << 15) - 1}
_FS_ALL = (1 << 16) - 1
_FILE_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE | FS_IOCTL_DEV
READ_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# The work directory holds ordinary files and directories only: no device
# nodes, sockets, FIFOs or symbolic links, and nothing to execute.
WORK_RIGHTS = (
    FS_READ_FILE
    | FS_WRITE_FILE
    | FS_READ_DIR
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_REFER
    | FS_TRUNCATE
)

# Landlock ABI each control needs (ADR 0012 §2).
FILESYSTEM_MIN_ABI = 3
ISOLATION_MIN_ABI = 6


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi():
    """The kernel's Landlock ABI version, or 0 when Landlock is unavailable."""
    try:
        abi = libc().syscall(
            SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
    except OSError:
        return 0
    return max(int(abi), 0)


def apply_landlock(abi, read_paths, write_path, list_paths=()):
    handled_fs = _FS_BY_ABI.get(abi, _FS_ALL)
    attr = _RulesetAttr(handled_fs, 0, 0)
    if abi >= 4:
        attr.handled_access_net = NET_BIND_TCP | NET_CONNECT_TCP
    if abi >= 6:
        attr.scoped = SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL
    # Kernels that predate a field accept it when it is zero.
    ruleset = libc().syscall(
        SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if ruleset < 0:
        raise SetupError("landlock_create_ruleset: " + _errno_text())
    try:
        rules = [(path, READ_RIGHTS) for path in read_paths]
        # Listing a directory reads names only, never file contents.
        rules += [(path, FS_READ_DIR) for path in list_paths]
        rules.append((write_path, WORK_RIGHTS))
        for path, rights in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                allowed = rights & handled_fs
                if not os.path.isdir(path):
                    allowed &= _FILE_RIGHTS
                rule = _PathBeneath(allowed, fd)
                if (
                    libc().syscall(
                        SYS_LANDLOCK_ADD_RULE,
                        ruleset,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    < 0
                ):
                    raise SetupError(f"landlock_add_rule {path}: " + _errno_text())
            finally:
                os.close(fd)
        if libc().syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0) < 0:
            raise SetupError("landlock_restrict_self: " + _errno_text())
    finally:
        os.close(ruleset)


# --- seccomp ---------------------------------------------------------------

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
X32_SYSCALL_BIT = 0x40000000
CLONE_THREAD = 0x00010000
# CLONE_NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET, NEWTIME
# (linux/sched.h): a thread must not get namespaces of its own either.
CLONE_NAMESPACE_FLAGS = (
    0x00020000
    | 0x02000000
    | 0x04000000
    | 0x08000000
    | 0x10000000
    | 0x20000000
    | 0x40000000
    | 0x00000080
)
EPERM = 1
ENOSYS = 38

# System-call numbers from the kernel's asm/unistd_64.h (x86-64) and
# asm-generic/unistd.h (arm64). "fork" and "vfork" exist only on x86-64.
_SYSCALLS = {
    "x86_64": {
        "audit_arch": AUDIT_ARCH_X86_64,
        "socket": 41,
        "clone": 56,
        "clone3": 435,
        "deny": {
            "fork": 57,
            "vfork": 58,
            "setsid": 112,
            "setpgid": 109,
            "ptrace": 101,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "process_madvise": 440,
            "pidfd_getfd": 438,
            "kcmp": 312,
            "unshare": 272,
            "setns": 308,
            "mount": 165,
            "umount2": 166,
            "pivot_root": 155,
            "chroot": 161,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "mount_setattr": 442,
            "bpf": 321,
            "userfaultfd": 323,
            "perf_event_open": 298,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "init_module": 175,
            "finit_module": 313,
            "delete_module": 176,
            "kexec_load": 246,
            "kexec_file_load": 320,
            "reboot": 169,
            "keyctl": 250,
            "add_key": 248,
            "request_key": 249,
            "swapon": 167,
            "swapoff": 168,
            "acct": 163,
            "iopl": 172,
            "ioperm": 173,
            "mknod": 133,
            "mknodat": 259,
        },
    },
    "aarch64": {
        "audit_arch": AUDIT_ARCH_AARCH64,
        "socket": 198,
        "clone": 220,
        "clone3": 435,
        "deny": {
            "setsid": 157,
            "setpgid": 154,
            "ptrace": 117,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "process_madvise": 440,
            "pidfd_getfd": 438,
            "kcmp": 272,
            "unshare": 97,
            "setns": 268,
            "mount": 40,
            "umount2": 39,
            "pivot_root": 41,
            "chroot": 51,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "mount_setattr": 442,
            "bpf": 280,
            "userfaultfd": 282,
            "perf_event_open": 241,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "init_module": 105,
            "finit_module": 273,
            "delete_module": 106,
            "kexec_load": 104,
            "kexec_file_load": 294,
            "reboot": 142,
            "keyctl": 219,
            "add_key": 217,
            "request_key": 218,
            "swapon": 224,
            "swapoff": 225,
            "acct": 89,
            "mknodat": 33,
        },
    },
}

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_JSET_K = 0x45
_BPF_RET_K = 0x06
_RET_ALLOW = 0x7FFF0000
_RET_ERRNO = 0x00050000
_RET_KILL_PROCESS = 0x80000000

# struct seccomp_data: nr (u32), arch (u32), instruction_pointer (u64), args[6].
_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0_LOW = 16  # little-endian: the low word of args[0]


def syscall_table(machine=None):
    return _SYSCALLS.get(machine or os.uname().machine)


def _stmt(code, k):
    return struct.pack("HBBI", code, 0, 0, k)


def _jump(code, k, jt, jf):
    return struct.pack("HBBI", code, jt, jf, k)


def seccomp_program(table):
    """The BPF filter of ADR 0012 §1 for one architecture's table."""
    prog = [
        _stmt(_BPF_LD_W_ABS, _OFFSET_ARCH),
        _jump(_BPF_JEQ_K, table["audit_arch"], 1, 0),
        _stmt(_BPF_RET_K, _RET_KILL_PROCESS),
        _stmt(_BPF_LD_W_ABS, _OFFSET_NR),
    ]
    if table["audit_arch"] == AUDIT_ARCH_X86_64:
        # x32 system calls share the architecture value; reject them outright.
        prog += [
            _jump(_BPF_JGE_K, X32_SYSCALL_BIT, 0, 1),
            _stmt(_BPF_RET_K, _RET_KILL_PROCESS),
        ]
    prog += [
        # Creating a socket of any family is denied; socketpair stays.
        _jump(_BPF_JEQ_K, table["socket"], 0, 1),
        _stmt(_BPF_RET_K, _RET_ERRNO | EPERM),
        # clone only for threads, and never with a namespace flag.
        _jump(_BPF_JEQ_K, table["clone"], 0, 5),
        _stmt(_BPF_LD_W_ABS, _OFFSET_ARG0_LOW),
        _jump(_BPF_JSET_K, CLONE_NAMESPACE_FLAGS, 2, 0),
        _jump(_BPF_JSET_K, CLONE_THREAD, 0, 1),
        _stmt(_BPF_RET_K, _RET_ALLOW),
        _stmt(_BPF_RET_K, _RET_ERRNO | EPERM),
        # clone3 passes its flags in memory the filter cannot read; ENOSYS
        # makes the C library fall back to clone.
        _jump(_BPF_JEQ_K, table["clone3"], 0, 1),
        _stmt(_BPF_RET_K, _RET_ERRNO | ENOSYS),
    ]
    for nr in sorted(table["deny"].values()):
        prog += [
            _jump(_BPF_JEQ_K, nr, 0, 1),
            _stmt(_BPF_RET_K, _RET_ERRNO | EPERM),
        ]
    prog.append(_stmt(_BPF_RET_K, _RET_ALLOW))
    return prog


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def apply_seccomp(table):
    prog = seccomp_program(table)
    buffer = ctypes.create_string_buffer(b"".join(prog))
    fprog = _SockFprog(len(prog), ctypes.cast(buffer, ctypes.c_void_p))
    if (
        libc().prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0)
        != 0
    ):
        raise SetupError("seccomp: " + _errno_text())


# --- the function child -----------------------------------------------------


def apply_rlimits(limits):
    memory = int(limits["memory_mib"]) * MIB
    cpu = int(limits["cpu_seconds"])
    file_size = int(limits["file_size_mib"]) * MIB
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def confine(spec):
    """Apply every control the spec asks for, or raise SetupError."""
    apply_rlimits(spec["limits"])
    if libc().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SetupError("no_new_privs: " + _errno_text())
    abi = int(spec.get("landlock_abi") or 0)
    if abi:
        apply_landlock(
            abi, spec["read_paths"], spec["work_dir"], spec.get("list_paths", ())
        )
    if spec.get("seccomp"):
        table = syscall_table()
        if table is None:
            raise SetupError("no system-call table for " + os.uname().machine)
        apply_seccomp(table)


def _child(spec, setup_w):
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        # Without the isolation control the function can kill the supervisor;
        # it then dies with it instead of running on unsupervised.
        libc().prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != spec["supervisor_pid"]:
            raise SetupError("supervisor exited during start")
        confine(spec)
        os.chdir(spec["work_dir"])
        argv = spec["argv"]
        os.execve(argv[0], argv, {})
    except BaseException as exc:  # noqa: BLE001 - anything here is a failed start
        try:
            os.write(setup_w, (type(exc).__name__ + ": " + str(exc)).encode()[:1000])
        finally:
            os._exit(SETUP_FAILED)


# --- the supervisor -----------------------------------------------------------


class _Stop(Exception):
    """Raised by the SIGTERM handler: system calls are retried after a handler
    returns (PEP 475), so a flag alone would leave the supervisor blocked."""


def _on_sigterm(signum, frame):
    # A second stop request must not raise again outside the handling of the
    # first, which would lose the final report.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise _Stop()


def _children_of(pid):
    children = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        # The command name may contain spaces and parentheses; the fields after
        # the last ")" are fixed: state, then ppid.
        fields = stat[stat.rfind(b")") + 2 :].split()
        if len(fields) > 1 and int(fields[1]) == pid:
            children.append(int(entry))
    return children


def _reap_all():
    """Kill and reap every descendant; orphans re-parent to this subreaper."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    me = os.getpid()
    while True:
        for pid in _children_of(me):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return


def _report(status_fd, message):
    os.write(status_fd, (json.dumps(message) + "\n").encode())


def _redirect_standard_streams():
    # The function holds the only copies of the run's pipes, so the host sees
    # end-of-file as soon as the function and its descendants are gone.
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)


def _read_all(fd):
    data = b""
    while True:
        chunk = os.read(fd, 1000)
        if not chunk:
            return data
        data += chunk


def _work_dir_exceeds_quota(spec):
    """Count entries and bytes without following links; stop at the quota.

    Directory descriptors keep renames and symlink swaps from redirecting the
    scan outside the work directory; at most a few are open at once, so depth
    cannot exhaust the supervisor's descriptors. Each inode counts once, so an
    entry moved or unlinked during the scan is not counted twice. Disappearing
    entries are normal during a run, but a directory that was listed and never
    scanned may hide its subtree, so the scan restarts; if every attempt misses
    one, or the scan fails otherwise, it raises so the run stops rather than
    run unenforced.
    """
    limits = spec.get("disk_limits")
    if limits is None:
        return False
    for _ in range(DISK_SCAN_ATTEMPTS):
        exceeded = _scan_work_dir(spec["work_dir"], limits)
        if exceeded is not None:
            return exceeded
    raise OSError("work directory kept changing during every usage scan")


def _scan_work_dir(work_dir, limits):
    """Return whether usage exceeds the limits, or None if a listed directory
    could not be scanned."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    seen = set()
    # Directories found in a listing, and those whose own entries were counted.
    listed = set()
    scanned = set()
    totals = [0, 0]  # bytes, entries

    def exceeds(info):
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            return False
        seen.add(identity)
        # Count sparse files by apparent size and preallocated files by
        # allocation, including filesystem block rounding and metadata.
        totals[0] += max(info.st_size, info.st_blocks * 512)
        totals[1] += 1
        return totals[0] > limits["bytes"] or totals[1] > limits["files"]

    def identity_of(fd):
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino)

    def list_subdirectories(fd, identity):
        """Count a directory's entries; return its unscanned subdirectories,
        or None once the quota is exceeded."""
        scanned.add(identity)
        subdirectories = []
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                child = (info.st_dev, info.st_ino)
                if exceeds(info):
                    return None
                # A directory seen under a name it has since left is listed
                # again here, so its new place is still scanned.
                if stat.S_ISDIR(info.st_mode) and child not in scanned:
                    listed.add(child)
                    subdirectories.append((entry.name, child))
        return subdirectories

    def open_verified(name, identity, parent):
        try:
            fd = os.open(name, flags, dir_fd=parent)
        except (FileNotFoundError, NotADirectoryError):
            return None
        if identity_of(fd) != identity:
            os.close(fd)
            return None
        return fd

    root = os.open(work_dir, flags)
    current = root
    try:
        pending = list_subdirectories(root, identity_of(root))
        if pending is None:
            return True
        # Frames hold names and identities, not descriptors.
        stack = [(None, None, pending)]
        while stack:
            pending = stack[-1][2]
            if pending:
                name, identity = pending.pop()
                if identity in scanned:
                    continue
                child = open_verified(name, identity, current)
                if child is None:
                    continue  # Moved or replaced since it was listed.
                if current != root:
                    os.close(current)
                current = child
                pending = list_subdirectories(current, identity)
                if pending is None:
                    return True
                stack.append((name, identity, pending))
                continue
            stack.pop()
            if not stack:
                break
            if len(stack) == 1:
                parent = root
            else:
                try:
                    parent = open_verified("..", stack[-1][1], current)
                except OSError:
                    parent = None
            if parent is None:
                # The directory moved; walk back down from the root as far as
                # the path still leads to the same directories.
                parent = root
                for depth in range(1, len(stack)):
                    name, identity, _ = stack[depth]
                    child = open_verified(name, identity, parent)
                    if child is None:
                        del stack[depth:]
                        break
                    if parent != root:
                        os.close(parent)
                    parent = child
            if current != root:
                os.close(current)
            current = parent
        if listed - scanned:
            return None
        # Unlinked open files have no directory entry. Measure before cleanup
        # closes the run's descriptors and releases their storage.
        pending = _children_of(os.getpid())
        while pending:
            pid = pending.pop()
            pending.extend(_children_of(pid))
            try:
                descriptors = os.listdir(f"/proc/{pid}/fd")
            except FileNotFoundError:
                continue
            for descriptor in descriptors:
                try:
                    info = os.stat(f"/proc/{pid}/fd/{descriptor}")
                except FileNotFoundError:
                    continue  # Closed or exited during the scan.
                if info.st_nlink != 0 or not stat.S_ISREG(info.st_mode):
                    continue
                if exceeds(info):
                    return True
        return False
    finally:
        if current != root:
            os.close(current)
        os.close(root)


def supervise(spec, status_fd):
    libc().prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    # Dying with the host is better than outliving it; SIGTERM still cleans up.
    libc().prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    signal.signal(signal.SIGTERM, _on_sigterm)
    if os.getppid() != spec["host_pid"]:
        return

    spec = dict(spec, supervisor_pid=os.getpid())
    setup_r, setup_w = os.pipe2(os.O_CLOEXEC)
    # A stop request is held back until the start's outcome is reported: a
    # function that fails to start exits at once, the host then asks to stop,
    # and the setup error must still reach it.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    child = os.fork()
    if child == 0:
        os.close(setup_r)
        os.close(status_fd)
        _child(spec, setup_w)
    os.close(setup_w)
    _redirect_standard_streams()

    stopped = False
    exit_code = None
    exit_signal = None
    disk_exceeded = False
    monitor_error = ""
    try:
        setup_error = _read_all(setup_r)
        if setup_error:
            _reap_all()
            _report(status_fd, {"setup_error": setup_error.decode(errors="replace")})
            return
        _report(status_fd, {"started": child})
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        next_disk_poll = 0
        try:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == child:
                    if os.WIFSIGNALED(status):
                        exit_signal = os.WTERMSIG(status)
                    else:
                        exit_code = os.WEXITSTATUS(status)
                    break
                if time.monotonic() >= next_disk_poll:
                    disk_exceeded = _work_dir_exceeds_quota(spec)
                    if disk_exceeded:
                        stopped = True
                        break
                    next_disk_poll = time.monotonic() + DISK_POLL_SECONDS
                if pid == 0:
                    time.sleep(DISK_POLL_SECONDS)
        except ChildProcessError:
            pass
        # EOF can make the host ask to stop after the child has exited. Ignore
        # that request while still protected by the _Stop handler, so it cannot
        # interrupt the handoff to cleanup and lose the final report.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except _Stop:
        stopped = True
    except OSError as exc:
        monitor_error = str(exc)
        stopped = True
    _reap_all()
    # Catch fast runs and EOF-triggered stop requests between polling ticks.
    if not disk_exceeded and not monitor_error:
        try:
            disk_exceeded = _work_dir_exceeds_quota(spec)
        except OSError as exc:
            monitor_error = str(exc)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    _report(
        status_fd,
        {
            "exit_code": exit_code,
            "signal": exit_signal,
            "stopped": stopped,
            "peak_memory_kib": int(usage.ru_maxrss),
            "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
            **({"disk_exceeded": True} if disk_exceeded else {}),
            **({"monitor_error": monitor_error} if monitor_error else {}),
        },
    )


def probe(spec, status_fd):
    """Apply the offered controls in a throwaway child and report what held."""
    child = os.fork()
    if child == 0:
        try:
            confine(spec)
            os._exit(0)
        except BaseException:  # noqa: BLE001
            os._exit(SETUP_FAILED)
    _, status = os.waitpid(child, 0)
    ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    _report(status_fd, {"probe_ok": ok})


def main():
    spec = json.loads(sys.argv[2])
    status_fd = int(spec["status_fd"])
    if sys.argv[1] == "--probe":
        probe(spec, status_fd)
    else:
        supervise(spec, status_fd)
    os.close(status_fd)


if __name__ == "__main__":
    main()
