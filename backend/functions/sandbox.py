"""
Per-run supervisor and sandbox launcher for the hardened-subprocess runner
(docs/adr/0012-user-function-isolation.md, docs/USER_FUNCTION_CONTRACT.md §3.1).

The host starts this file as its own process (``python -I -S sandbox.py <spec>``)
with the function's standard streams already attached. It never imports
application modules: it runs outside the sandbox, as the run's supervisor.

As supervisor it registers as a child subreaper, forks the function process and
traces filesystem changes to enforce its working-directory quota. Once the
function exits, exceeds its quota or the host asks it to stop (SIGTERM), it kills every descendant it can see
before it reports on the status descriptor and exits.

The forked child applies, in order: resource limits, ``no_new_privs``, a
Landlock ruleset and a seccomp filter, each as far as the spec asks, and then
replaces itself with the runtime. A control the spec asks for that cannot be
applied aborts the start: a run never runs less confined than reported.

``--probe`` applies everything the host can offer in a child and reports what
held, so the runner's reported level is what a run actually gets.
"""

import ctypes
import errno
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
        if spec.get("disk_limits"):
            _ptrace(0, 0)  # PTRACE_TRACEME, before confinement denies ptrace.
            os.kill(os.getpid(), signal.SIGSTOP)
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


def _reap_all(spec=None):
    """Kill and reap every descendant; orphans re-parent to this subreaper."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    me = os.getpid()
    disk_exceeded = False
    monitor_error = ""
    while True:
        for pid in _children_of(me):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            pid, status = os.waitpid(-1, 0x40000000)
            if os.WIFSTOPPED(status):
                # SIGKILL itself has a ptrace exit stop, before fd teardown.
                # Account there too: a write in flight when shutdown started
                # may have finished after the preceding scan.
                if spec is not None and not disk_exceeded and not monitor_error:
                    try:
                        disk_exceeded = _work_dir_exceeds_quota(spec)
                    except OSError as exc:
                        monitor_error = str(exc)
                try:
                    _ptrace(7, pid, 0, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except ChildProcessError:
            return disk_exceeded, monitor_error


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
    scan outside the work directory. Disappearing entries are normal during a
    run; other scan failures must stop it rather than disable enforcement.
    """
    limits = spec.get("disk_limits")
    if limits is None:
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    stack = []

    def push(fd):
        try:
            entries = os.scandir(fd)
        except BaseException:
            os.close(fd)
            raise
        stack.append((fd, entries))

    push(os.open(spec["work_dir"], flags))
    work_prefix = os.path.realpath(spec["work_dir"]).rstrip("/") + "/"
    retained = spec.setdefault("_disk_mapping_fds", {})
    live_inodes = set()
    total_bytes = 0
    total_files = 0
    try:
        while stack:
            directory, entries = stack[-1]
            entry = next(entries, None)
            if entry is None:
                entries.close()
                os.close(directory)
                stack.pop()
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            total_files += 1
            # Count sparse files by apparent size and preallocated files by
            # allocation, including filesystem block rounding and metadata.
            total_bytes += max(info.st_size, info.st_blocks * 512)
            if total_bytes > limits["bytes"] or total_files > limits["files"]:
                return True
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, flags, dir_fd=directory)
                except OSError as exc:
                    # A directory renamed away or exchanged for another entry
                    # since its stat; the next scan counts its replacement.
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                        continue
                    raise
                push(child)
        # Unlinked open files have no directory entry. Measure before cleanup
        # closes the run's descriptors and releases their storage.
        pending = _children_of(os.getpid())
        seen = set()
        while pending:
            pid = pending.pop()
            pending.extend(_children_of(pid))
            try:
                tasks = os.listdir(f"/proc/{pid}/task")
            except FileNotFoundError:
                continue
            for task in tasks:
                fd_dir = f"/proc/{pid}/task/{task}/fd"
                try:
                    descriptors = os.listdir(fd_dir)
                except FileNotFoundError:
                    continue
                references = []
                for descriptor in descriptors:
                    reference = f"{fd_dir}/{descriptor}"
                    try:
                        info = os.stat(reference)
                    except FileNotFoundError:
                        continue
                    references.append((info, reference))
                # /proc/map_files dereferencing requires extra capabilities.
                # maps exposes inode identities without them. Pin mapped work
                # inodes through O_PATH descriptors before the run closes its
                # mmap fd, so fstat remains current even after unlink/close.
                try:
                    with open(f"/proc/{pid}/task/{task}/maps") as handle:
                        mappings = handle.readlines()
                except FileNotFoundError:
                    mappings = []
                for mapping in mappings:
                    fields = mapping.split(None, 5)
                    if len(fields) < 6 or not fields[5].startswith(work_prefix):
                        continue
                    major, minor = (int(part, 16) for part in fields[3].split(":"))
                    identity = (os.makedev(major, minor), int(fields[4]))
                    live_inodes.add(identity)
                    if identity not in retained:
                        source = next(
                            (
                                path
                                for info, path in references
                                if (info.st_dev, info.st_ino) == identity
                            ),
                            None,
                        )
                        if source is None:
                            raise OSError("mapped work file usage unavailable")
                        retained[identity] = os.open(source, os.O_PATH | os.O_CLOEXEC)
                    info = os.fstat(retained[identity])
                    if (info.st_dev, info.st_ino) != identity:
                        raise OSError("mapped work file changed during accounting")
                    if info.st_nlink == 0:
                        references.append((info, None))
                for info, reference in references:
                    identity = (info.st_dev, info.st_ino)
                    if (
                        (reference is not None and info.st_nlink != 0)
                        or not stat.S_ISREG(info.st_mode)
                        or identity in seen
                    ):
                        continue
                    try:
                        target = os.readlink(reference) if reference else work_prefix
                    except FileNotFoundError:
                        continue
                    # procfs names an unlinked file by its former path. Memfds
                    # and files deleted outside this tree are not work usage.
                    if not target.startswith(work_prefix):
                        continue
                    seen.add(identity)
                    total_bytes += max(info.st_size, info.st_blocks * 512)
                    if total_bytes > limits["bytes"]:
                        return True
        for identity in set(retained) - live_inodes:
            os.close(retained.pop(identity))
        return False
    finally:
        for directory, entries in reversed(stack):
            entries.close()
            os.close(directory)


# Trace filesystem-changing calls through their completion. Unlike a timer,
# syscall stops cannot miss a create/write followed immediately by a removal.
# Calls which can discard storage also hold the lock, including close and exit.
# Other calls (notably futex/read/poll) must run concurrently so threads can
# communicate while a writer is blocked in the kernel.
# Each pair is x86-64 / asm-generic (arm64); -1 means absent.
_DISK_CALL_NUMBERS = {
    "write": (1, 64),
    "open": (2, -1),
    "close": (3, 57),
    "mmap": (9, 222),
    "munmap": (11, 215),
    "ioctl": (16, 29),
    "pwrite64": (18, 68),
    "writev": (20, 66),
    "msync": (26, 227),
    "madvise": (28, 233),
    "dup": (32, 23),
    "dup2": (33, -1),
    "sendfile": (40, 71),
    "execve": (59, 221),
    "exit": (60, 93),
    "fcntl": (72, 25),
    "truncate": (76, 45),
    "ftruncate": (77, 46),
    "rename": (82, -1),
    "mkdir": (83, -1),
    "rmdir": (84, -1),
    "creat": (85, -1),
    "link": (86, -1),
    "unlink": (87, -1),
    "symlink": (88, -1),
    "mknod": (133, -1),
    "setxattr": (188, 5),
    "lsetxattr": (189, 6),
    "fsetxattr": (190, 7),
    "removexattr": (197, 14),
    "lremovexattr": (198, 15),
    "fremovexattr": (199, 16),
    "exit_group": (231, 94),
    "openat": (257, 56),
    "mkdirat": (258, 34),
    "mknodat": (259, 33),
    "unlinkat": (263, 35),
    "renameat": (264, 38),
    "linkat": (265, 37),
    "symlinkat": (266, 36),
    "splice": (275, 76),
    "tee": (276, 77),
    "sync_file_range": (277, 84),
    "fallocate": (285, 47),
    "dup3": (292, 24),
    "preadv": (295, 69),
    "pwritev": (296, 70),
    "renameat2": (316, 276),
    "execveat": (322, 281),
    "copy_file_range": (326, 285),
    "pwritev2": (328, 287),
    "close_range": (436, 436),
    "openat2": (437, 437),
}
_DISK_SYSCALLS = {
    arch: {pair[index] for pair in _DISK_CALL_NUMBERS.values() if pair[index] >= 0}
    for arch, index in (("x86_64", 0), ("aarch64", 1))
}


# These calls can block on IPC without changing any disk usage. Their output
# descriptor is argument 0, except splice's argument 2 and tee's argument 1.
_IPC_OUTPUT_CALLS = {
    arch: {
        _DISK_CALL_NUMBERS[name][index]: argument
        for name, argument in (
            ("write", 0),
            ("writev", 0),
            ("sendfile", 0),
            ("splice", 2),
            ("tee", 1),
        )
    }
    for arch, index in (("x86_64", 0), ("aarch64", 1))
}


def _is_ipc_output(pid, number, info):
    argument = _IPC_OUTPUT_CALLS[os.uname().machine].get(number)
    if argument is None:
        return False
    descriptor = struct.unpack_from("Q", info, 32 + 8 * argument)[0]
    try:
        mode = os.stat(f"/proc/{pid}/fd/{descriptor}").st_mode
    except FileNotFoundError:
        return False  # An invalid descriptor cannot block or change storage.
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


def _is_nonmutating_open(number, info):
    """Allow blocking FIFO/device opens to rendezvous with other tracees.

    Without creation or truncation, opening a path cannot grow disk usage.
    The returned descriptor is still accounted at subsequent syscall stops.
    """
    index = 0 if os.uname().machine == "x86_64" else 1
    for name, argument in (("open", 1), ("openat", 2)):
        if number == _DISK_CALL_NUMBERS[name][index]:
            flags = struct.unpack_from("Q", info, 32 + 8 * argument)[0]
            return not flags & (os.O_CREAT | os.O_TRUNC) and (
                flags & os.O_TMPFILE != os.O_TMPFILE
            )
    return False


def _ptrace(request, pid, address=0, data=0):
    result = libc().ptrace(request, pid, ctypes.c_void_p(address), data)
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _sleeps_interruptibly(pid):
    """Whether a tracee waits in the kernel for an event, not on storage I/O.

    Lock waits, FIFO opens and terminal drains sleep interruptibly ("S") and
    may wait for a peer queued behind them; storage I/O sleeps in "D".
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return False  # Its exit is reported by waitpid.
    return stat[stat.rfind(b")") + 2 :].startswith(b"S")


# Each scan walks the whole work directory, so scanning at every storage call
# would cost time quadratic in the entries a run creates. Past a grace period,
# scans are skipped while they have taken more than this share of the run.
_SCAN_GRACE_SECONDS = 10.0
_SCAN_SHARE = 0.1


def _trace_quota(spec, child, status_fd):
    """Return the child's wait status, stopping with all descriptors intact.

    Descendants inherit tracing even when they escape their parent's process
    group. PTRACE_EVENT_EXIT stops before the kernel releases descriptors.
    PTRACE_GET_SYSCALL_INFO avoids architecture-specific register layouts.
    """
    calls = _DISK_SYSCALLS.get(os.uname().machine)
    if calls is None:
        raise OSError("disk enforcement unsupported on this architecture")
    started = time.monotonic()
    scanning = 0.0

    def exceeds(force=False):
        nonlocal scanning
        now = time.monotonic()
        budget = _SCAN_GRACE_SECONDS + _SCAN_SHARE * (now - started)
        if not force and scanning > budget:
            return False
        try:
            return _work_dir_exceeds_quota(spec)
        finally:
            scanning += time.monotonic() - now

    owner = None
    queued = []
    # Serialized calls resumed and not yet at their exit stop. A former owner
    # stays here after yielding the lock, so its completion is still checked.
    in_flight = set()
    newborn = set()
    options = 1 | 2 | 4 | 8 | 16 | 64 | (1 << 20)
    # TRACESYSGOOD, TRACEFORK/VFORK/CLONE/EXEC/EXIT, EXITKILL.
    _, status = os.waitpid(child, os.WUNTRACED)
    if not os.WIFSTOPPED(status):
        return status, False
    _ptrace(0x4200, child, 0, options)  # SETOPTIONS
    _ptrace(24, child)  # SYSCALL
    # Blocked SIGCHLD stays pending, so sigtimedwait cannot miss a stop.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    try:
        while True:
            while owner is None and queued:
                owner = queued.pop(0)
                if exceeds():
                    return None, True
                try:
                    _ptrace(24, owner)
                    in_flight.add(owner)
                except ProcessLookupError:
                    owner = None  # Killed while queued, e.g. by exit_group.
            if owner is not None and queued:
                pid, status = os.waitpid(-1, 0x40000000 | os.WNOHANG)
                if not pid:
                    # An owner waiting for a peer queued behind it would hold
                    # the run until timeout; let the queue proceed instead.
                    timed_out = signal.sigtimedwait({signal.SIGCHLD}, 0.05) is None
                    if timed_out and _sleeps_interruptibly(owner):
                        owner = None
                    continue
            else:
                pid, status = os.waitpid(-1, 0x40000000)  # __WALL, threads too
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                if pid == child:
                    return status, False
                if owner == pid:
                    owner = None
                in_flight.discard(pid)
                if pid in queued:
                    queued.remove(pid)
                continue
            try:
                event = status >> 16
                sig = os.WSTOPSIG(status)
                if event in (1, 2, 3):
                    descendant = ctypes.c_ulong()
                    _ptrace(0x4201, pid, 0, ctypes.byref(descendant))
                    newborn.add(descendant.value)
                if sig == (signal.SIGTRAP | 0x80):
                    info = ctypes.create_string_buffer(88)
                    _ptrace(0x420E, pid, 88, ctypes.byref(info))
                    operation = info.raw[0]
                    number = struct.unpack_from("Q", info.raw, 24)[0]
                    if (
                        operation == 1
                        and number in calls
                        and not _is_ipc_output(pid, number, info.raw)
                        and not _is_nonmutating_open(number, info.raw)
                    ):
                        if owner is not None and owner != pid:
                            queued.append(pid)
                            continue
                        owner = pid
                        in_flight.add(pid)
                        if exceeds():
                            return None, True
                    elif operation == 2 and pid in in_flight:
                        in_flight.discard(pid)
                        if owner == pid:
                            owner = None
                        if exceeds():
                            return None, True
                    sig = 0
                elif event:
                    if event == 4 and pid == child:
                        _report(status_fd, {"started": child})
                        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
                    # The function's own exit is its last chance to count
                    # unlinked files it holds open, so it is never skipped.
                    if exceeds(force=event == 6 and pid == child):
                        return None, True
                    # Exit is held until accounting has run, before fd teardown.
                    if event == 6:
                        in_flight.discard(pid)
                        if owner == pid:
                            owner = None
                    sig = 0
                elif pid in newborn and sig == signal.SIGSTOP:
                    newborn.remove(pid)
                    sig = 0
                _ptrace(24, pid, 0, sig)
            except ProcessLookupError:
                pass  # Killed while stopped; waitpid reports its exit.
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})


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
        traced_status = None
        if spec.get("disk_limits"):
            traced_status, disk_exceeded = _trace_quota(spec, child, status_fd)
            stopped = disk_exceeded
        setup_error = _read_all(setup_r) if not stopped else b""
        if setup_error:
            _reap_all()
            _report(status_fd, {"setup_error": setup_error.decode(errors="replace")})
            return
        if not spec.get("disk_limits"):
            _work_dir_exceeds_quota(spec)
            _report(status_fd, {"started": child})
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
            try:
                while True:
                    pid, status = os.waitpid(-1, 0)
                    if pid == child:
                        traced_status = status
                        break
            except ChildProcessError:
                pass
        if traced_status is not None:
            if os.WIFSIGNALED(traced_status):
                exit_signal = os.WTERMSIG(traced_status)
            else:
                exit_code = os.WEXITSTATUS(traced_status)
        # EOF can make the host ask to stop after the child has exited. Ignore
        # that request while still protected by the _Stop handler, so it cannot
        # interrupt the handoff to cleanup and lose the final report.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except _Stop:
        stopped = True
    except OSError as exc:
        monitor_error = str(exc)
        stopped = True
    # Account before cleanup releases retained unlinked files.
    if not disk_exceeded and not monitor_error:
        try:
            disk_exceeded = _work_dir_exceeds_quota(spec)
        except OSError as exc:
            monitor_error = str(exc)
    if spec.get("disk_limits"):
        cleanup_exceeded, cleanup_error = _reap_all(spec)
        disk_exceeded = disk_exceeded or cleanup_exceeded
        monitor_error = monitor_error or cleanup_error
    else:
        _reap_all()
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
